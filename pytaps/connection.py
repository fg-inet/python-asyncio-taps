import asyncio
import ipaddress
import socket
from copy import deepcopy
try:
    import netifaces
except ImportError:
    netifaces = None

from . import transports as transport_impl
from .connection_group import ConnectionGroup
from .endpoint import LocalEndpoint, RemoteEndpoint
from .message import (
    MESSAGE_PROPERTY_DEFAULTS,
    MessageContext,
    ReceivedMessage,
    canonicalize_message_property_name,
    is_message_property,
)
from .transportProperties import (
    CONNECTION_PROPERTY_DEFAULTS,
    PREFERENCE_SELECTION_PROPERTIES,
    PreferenceLevel,
    canonicalize_property_name,
    get_backend_property_support,
    get_protocol_capabilities,
)
from .transports import MulticastSendTransport, QuicAssociationManager, TcpTransport, UdpTransport
from .utility import (
    Candidate,
    CandidateBranch,
    ConnectionState,
    SleepClassForRacing,
    _CURRENT_CANDIDATE_VIEW,
    build_candidate_branches,
    build_protocol_candidates,
    create_candidates,
    order_candidates_for_racing,
    order_remote_addresses,
    schedule_callback,
    setup_logger,
)
logger = setup_logger(__name__)
# Wait for 100 ms between connection attempts when racing
RACING_DELAY = 0.1


async def _open_asyncio_candidate(open_awaitable, transport):
    try:
        result = await open_awaitable
        await transport.wait_open()
        return result
    except BaseException:
        await transport._close_raw()
        raise


def _require_netifaces():
    if netifaces is None:
        raise ImportError(
            "Interface-constrained endpoint selection requires the 'netifaces' package."
        )


class PartialSendError(RuntimeError):
    def __init__(self, bytes_sent, total_bytes):
        self.bytes_sent = bytes_sent
        self.total_bytes = total_bytes
        super().__init__(
            f"Partial send completed ({bytes_sent}/{total_bytes} bytes sent)"
        )


class Connection:
    """The TAPS connection class.

    Attributes:
        preconnection (Preconnection, required):
                Preconnection object from which this Connection
                object was created.
    """

    def __getattribute__(self, name):
        if name in {
            "local_endpoint",
            "local_endpoints",
            "remote_endpoint",
            "remote_endpoints",
            "protocol",
        }:
            candidate_view = _CURRENT_CANDIDATE_VIEW.get()
            if (
                candidate_view is not None
                and candidate_view[0] is self
                and name in candidate_view[1]
            ):
                return candidate_view[1][name]
        return object.__getattribute__(self, name)

    def __init__(self, preconnection):
        # Initializations
        self.local_endpoints = [
            endpoint.clone() for endpoint in preconnection.local_endpoints
        ]
        self.remote_endpoints = [
            endpoint.clone() for endpoint in preconnection.remote_endpoints
        ]
        self.local_endpoint = (
            self.local_endpoints[0] if self.local_endpoints else None
        )
        self.remote_endpoint = (
            self.remote_endpoints[0] if self.remote_endpoints else None
        )
        self.transport_properties = preconnection.transport_properties.clone()
        self.security_parameters = deepcopy(preconnection.security_parameters)
        self.security_context = preconnection.security_context
        self.message_properties = deepcopy(preconnection.message_properties)
        self.connection_context = preconnection.connection_context
        self.loop = preconnection.loop
        self.active = False
        self.framers = tuple(preconnection.framers)
        self.framer_stack = None
        self.sleeper_for_racing = SleepClassForRacing()
        self.pending = []
        self._originating_preconnection = preconnection
        self._ready_waiter = self.loop.create_future()
        self._closed_waiter = self.loop.create_future()
        self._first_message_waiter = self.loop.create_future()
        self._pending_message = None
        self._pre_ready_sends = []
        self._pre_ready_flush_task = None
        self._receive_waiters = []
        self.last_error = None
        # Current state of the connection object
        self.state = ConnectionState.ESTABLISHING
        # List of possible underlying transports
        self.transports = []
        self.protocol = None
        self._protocol_capabilities = None
        self._backend_property_effects = {}
        self._resolution_tasks = []
        self.multicast_open = False
        self.quic_association = None
        self.connection_group = ConnectionGroup(self)
        self.race_task = None
        self._batch_counter = 0
        self._send_sequence = 0
        self._send_call_sequence = 0
        self._message_sequence = 0
        self._next_send_event_id = 1
        self._pending_send_events = {}
        self._send_calls = {}
        self._completed_send_calls = set()
        self._send_drain_waiter = self.loop.create_future()
        self._send_drain_waiter.set_result(None)
        self._partial_send_contexts = {}
        self._receive_sequence = 0
        self._queued_messages = []
        self._sent_final_message = False
        self._received_final_message = False
        self._close_requested = False
        self._close_task = None
        self._rendezvous_mode = getattr(preconnection, "_rendezvous_mode", False)
        self._rendezvous_companions = []
        self._context_ready_recorded = False
        self._context_detached = False
        self._establishment_started_at = self.loop.time()
        self._performance_establishment_recorded = False
        self._current_path = {
            "local": None,
            "remote": None,
        }
        self._previous_path = {
            "local": None,
            "remote": None,
        }
        self._soft_errors = []
        self._event_history = []
        self._recommended_candidates = []
        self._reestablishment_advice = None
        self._auto_reestablishment_enabled = False
        self._auto_reestablishment_triggers = {"connection_error"}
        self._auto_reestablishment_min_penalty = 3
        self.connection_context.attach_connection(self)
        self._auto_reestablishment_timeout = 5
        self._auto_reestablishment_task = None
        self._last_reestablished_connection = None
        self._system_policy_adaptation_task = None

        # Callbacks
        self.writer = None
        self.reader = None
        self.closed = None
        self.receive_error = None
        self.received_partial = None
        self.received = None
        self.connection_error = None
        self.expired = None
        self.send_error = None
        self.sent = None
        self.soft_error = None
        self.path_change = None
        self.clone_error = None
        self.reestablishment_suggested = None
        self.reestablished = None
        self.establishment_error = None
        self.rendezvous_done = None
        self.stopped = getattr(preconnection, "stopped", None)
        self.listen_error = getattr(preconnection, "listen_error", None)
        self.connection_received = getattr(preconnection, "connection_received", None)
        self.initiate_error = getattr(preconnection, "initiate_error", None)
        self.ready = getattr(preconnection, "ready", None)
        self.establishment_error = getattr(preconnection, "establishment_error", None)
        self.rendezvous_done = getattr(preconnection, "rendezvous_done", None)

    def _coerce_message_context(self, message_context=None, *, end_of_message=True):
        if message_context is None:
            message_context = MessageContext(end_of_message=end_of_message)
        message_context.end_of_message = end_of_message
        return self._apply_message_defaults(
            message_context.ensure_created(),
            resolve_connection_defaults=self.protocol is not None,
        )

    def _apply_message_defaults(
        self,
        context,
        *,
        resolve_connection_defaults=True,
    ):
        for prop in self.message_properties.explicit_properties:
            if prop not in context.explicit_properties:
                if getattr(context, prop) == MESSAGE_PROPERTY_DEFAULTS[prop]:
                    setattr(context, prop, getattr(self.message_properties, prop))
        if not resolve_connection_defaults:
            return context
        if context.ordered is None:
            if self.protocol in {"tcp", "tls-tcp"}:
                context.ordered = True
            elif self.protocol == "udp":
                context.ordered = False
            elif self.protocol == "quic":
                context.ordered = bool(
                    (self._selected_protocol_details() or {}).get(
                        "preserveOrder"
                    )
                )
            else:
                context.ordered = (
                    self.transport_properties.get("preserveOrder")
                    is not PreferenceLevel.PROHIBIT
                )
        if context.reliable is None:
            if self.protocol in {"tcp", "tls-tcp"}:
                context.reliable = True
            elif self.protocol == "udp":
                context.reliable = False
            elif self.protocol == "quic":
                context.reliable = bool(
                    (self._selected_protocol_details() or {}).get(
                        "reliability"
                    )
                )
            else:
                context.reliable = (
                    self.transport_properties.get("reliability")
                    is not PreferenceLevel.PROHIBIT
                )
        if context.capacity_profile is None:
            context.capacity_profile = self.transport_properties.get("connCapacityProfile")
        return context

    def new_message_context(self, **properties):
        context = MessageContext()
        for name, value in properties.items():
            context.set_property(name, value)
        return self._apply_message_defaults(
            context.ensure_created(),
            resolve_connection_defaults=self.protocol is not None,
        )

    def get_message_properties(self, message_or_context):
        if isinstance(message_or_context, ReceivedMessage):
            properties = message_or_context.get_properties()
        else:
            properties = message_or_context.get_properties()
        properties["selection"] = self._selection_properties_view()
        return properties

    def _set_state(self, state, error=None):
        self.state = state
        if error is not None:
            self.last_error = error

    def _is_terminal(self):
        return self.state is ConnectionState.CLOSED and self._closed_waiter.done()

    def _record_event(self, name, **details):
        event = {
            "name": name,
            "state": self.state.name.title(),
            "details": details,
        }
        self._event_history.append(event)
        self.connection_context.record_event(
            name,
            source="connection",
            state=event["state"],
            details=details,
        )
        return event

    def _detach_from_connection_context(self):
        if self._context_detached:
            return
        adaptation_task = self._system_policy_adaptation_task
        if adaptation_task is not None and not adaptation_task.done():
            try:
                current_task = asyncio.current_task()
            except RuntimeError:
                current_task = None
            if adaptation_task is not current_task:
                adaptation_task.cancel()
        self.connection_context.detach_connection(
            self,
            was_ready=self._context_ready_recorded,
        )
        self._context_detached = True

    def _performance_network_id(self):
        interface_id = getattr(self.local_endpoint, "interface", None)
        local_address = (
            self.local_endpoint.effective_address()
            if self.local_endpoint is not None
            else None
        )
        return self.connection_context.get_network_id(
            interface_id,
            local_address=local_address,
        )

    def _records_protocol_establishment(self):
        if self.protocol != "quic":
            return True
        return (
            self.quic_association is not None
            and self.quic_association._handshake_owner is self
        )

    def _record_establishment_performance(self):
        if (
            self._performance_establishment_recorded
            or self.protocol is None
            or not self._records_protocol_establishment()
        ):
            return

        local_path = self._current_path.get("local")
        remote_path = self._current_path.get("remote")
        if remote_path is None and self.remote_endpoint is not None:
            remote_address = self.remote_endpoint.effective_address()
            if remote_address is not None:
                remote_path = (
                    remote_address,
                    self.remote_endpoint.port,
                )
        if remote_path is None:
            return

        recorded = self.connection_context.record_performance_observation(
            local_path,
            remote_path,
            self.protocol,
            network_id=self._performance_network_id(),
            establishment_latency=max(
                0,
                self.loop.time() - self._establishment_started_at,
            ),
            success=True,
            source=(
                "quic-association-establishment"
                if self.protocol == "quic"
                else "connection-establishment"
            ),
        )
        self._performance_establishment_recorded = recorded

    def _mark_ready(self):
        self.last_error = None
        self._set_state(ConnectionState.ESTABLISHED)
        if not self._context_ready_recorded:
            self.connection_context.mark_connection_ready()
            self._context_ready_recorded = True
        if not self._rendezvous_mode:
            self._record_event(
                "ready",
                protocol=self.protocol,
                path=self._current_path.copy(),
            )
        if self._records_protocol_establishment():
            self.connection_context.record_candidate_outcome(
                self._current_path.get("local"),
                self._current_path.get("remote"),
                self.protocol,
                True,
            )
        self._record_establishment_performance()
        self.sleeper_for_racing.cancel_all()
        if not self._ready_waiter.done():
            self._ready_waiter.set_result(self)
        if self._pre_ready_sends and (
            self._pre_ready_flush_task is None
            or self._pre_ready_flush_task.done()
        ):
            self._pre_ready_flush_task = self.loop.create_task(
                self._flush_pre_ready_sends()
            )
        if not self._rendezvous_mode:
            schedule_callback(self.loop, self.ready, (self,))

    def _mark_passive_ready(self):
        self._set_state(ConnectionState.ESTABLISHED)
        if not self._context_ready_recorded:
            self.connection_context.mark_connection_ready()
            self._context_ready_recorded = True
        if self._records_protocol_establishment():
            self.connection_context.record_candidate_outcome(
                self._current_path.get("local"),
                self._current_path.get("remote"),
                self.protocol,
                True,
            )
        if not self._ready_waiter.done():
            self._ready_waiter.set_result(self)

    def _mark_first_message(self):
        if not self._first_message_waiter.done():
            self._first_message_waiter.set_result(self)

    async def wait_first_message(self, timeout=None):
        waiter = asyncio.shield(self._first_message_waiter)
        if timeout is None:
            return await waiter
        return await asyncio.wait_for(waiter, timeout)

    def _mark_rendezvous_done(self):
        if self.state is not ConnectionState.ESTABLISHED:
            self._mark_passive_ready()
        self._record_event(
            "rendezvous_done",
            protocol=self.protocol,
            path=self._current_path.copy(),
        )
        return self

    def _report_sent(self, message_context):
        self._queue_send_event("sent", message_context)

    def _mark_closed(self):
        if self._is_terminal():
            return
        self._set_state(ConnectionState.CLOSED)
        self._record_event("closed", last_error=str(self.last_error) if self.last_error else None)
        if not self._first_message_waiter.done():
            self._first_message_waiter.cancel()
        self._detach_from_connection_context()
        if not self._closed_waiter.done():
            self._closed_waiter.set_result(self)

    def _fail_initiate(self, error):
        if self._is_terminal():
            return
        self._fail_pending_sends(
            error,
            suppress_initiate_with_send=True,
        )
        self._fail_receive_waiters(error)
        self._set_state(ConnectionState.CLOSED, error)
        self._record_event("establishment_error", error=str(error))
        self._detach_from_connection_context()
        if not self._ready_waiter.done():
            self._ready_waiter.set_exception(error)
            self._ready_waiter.exception()
        if not self._first_message_waiter.done():
            self._first_message_waiter.cancel()
        if not self._closed_waiter.done():
            self._closed_waiter.set_result(self)
        if not self._rendezvous_mode:
            schedule_callback(
                self.loop,
                self.initiate_error,
                (error, self),
                (self,),
                (),
            )
            schedule_callback(
                self.loop,
                self.establishment_error,
                (error, self),
                (self,),
                (),
            )

    async def _discard_failed_clone(self, error):
        for transport in list(self.transports):
            try:
                await transport._stop_framers()
                await transport._close_raw()
            except Exception:
                pass
        self._fail_pending_sends(
            error,
            suppress_initiate_with_send=True,
        )
        self._set_state(ConnectionState.CLOSED, error)
        self.last_error = error
        self._detach_from_connection_context()
        if not self._ready_waiter.done():
            if isinstance(error, asyncio.CancelledError):
                self._ready_waiter.cancel()
            else:
                self._ready_waiter.set_exception(error)
                self._ready_waiter.exception()
        if not self._first_message_waiter.done():
            self._first_message_waiter.cancel()
        if not self._closed_waiter.done():
            self._closed_waiter.set_result(self)

    def _report_connection_error(self, error, *, suggest_reestablishment=True):
        if self._is_terminal():
            return
        was_establishing = self.state is ConnectionState.ESTABLISHING
        if not isinstance(error, BaseException):
            error = ConnectionAbortedError(str(error))
        self._fail_pending_sends(error)
        self._fail_receive_waiters(error)
        current_local = self._current_path.get("local")
        current_remote = self._current_path.get("remote")
        if current_local is not None or current_remote is not None:
            self.connection_context.degrade_path(
                current_local,
                current_remote,
                reason=error,
                penalty=5,
                lifetime=180,
            )
        self.connection_context.record_candidate_outcome(
            self._current_path.get("local"),
            self._current_path.get("remote"),
            self.protocol,
            False,
            error,
        )
        if suggest_reestablishment:
            self._refresh_reestablishment_guidance("connection_error")
        self._set_state(ConnectionState.CLOSED, error)
        self.last_error = error
        self._record_event("connection_error", error=str(error))
        if was_establishing and not self._ready_waiter.done():
            self._ready_waiter.set_exception(error)
            self._ready_waiter.exception()
        if not self._first_message_waiter.done():
            self._first_message_waiter.cancel()
        self._detach_from_connection_context()
        if not self._closed_waiter.done():
            self._closed_waiter.set_result(self)
        schedule_callback(self.loop, self.connection_error, (error, self))

    def _report_receive_error(self, message_context, reason=None):
        if self._is_terminal():
            return
        self._record_event(
            "receive_error",
            reason=str(reason) if reason is not None else None,
            message_id=getattr(message_context, "message_id", None),
        )
        schedule_callback(
            self.loop,
            self.receive_error,
            (message_context, reason, self),
            (message_context, self),
            (self,),
            (),
        )

    def _fail_receive_waiters(self, reason, message_context=None):
        waiters = list(self._receive_waiters)
        self._receive_waiters.clear()
        for waiter in waiters:
            if waiter.done():
                continue
            self._report_receive_error(message_context, reason)
            waiter.set_exception(reason)

    def _report_clone_error(self, reason, *, detached=False):
        self.last_error = reason
        self._record_event(
            "clone_error",
            reason=str(reason),
            detached=detached,
        )
        schedule_callback(
            self.loop,
            self.clone_error,
            (reason, self),
            (self,),
            (),
        )

    def _report_soft_error(self, reason):
        if self._is_terminal():
            return
        self._soft_errors.append(reason)
        self._record_event("soft_error", reason=str(reason))
        schedule_callback(
            self.loop,
            self.soft_error,
            (reason, self),
            (self,),
            (),
        )

    def _candidate_summary(self, candidate):
        return {
            "protocol": candidate.protocol,
            "remoteAddress": candidate.remote_address,
            "addressFamily": candidate.address_family,
            "path": candidate.path,
            "localAddress": candidate.local_address,
        }

    def _refresh_reestablishment_guidance(self, trigger):
        candidates = self.get_reestablishment_candidates()
        self._recommended_candidates = candidates
        current_local = self._current_path.get("local")
        current_remote = self._current_path.get("remote")
        advisory = self.connection_context.get_path_advisory(
            current_local,
            current_remote,
        )
        if not candidates:
            self._reestablishment_advice = None
            return []

        best = candidates[0]
        current_remote_address = current_remote[0] if current_remote else None
        current_local_address = current_local[0] if current_local else None
        recommendation_needed = bool(advisory)
        if best.protocol != self.protocol:
            recommendation_needed = True
        if best.remote_address != current_remote_address:
            recommendation_needed = True
        if best.local_address is not None and best.local_address != current_local_address:
            recommendation_needed = True

        advice = {
            "trigger": trigger,
            "recommendedCandidate": self._candidate_summary(best),
            "candidateCount": len(candidates),
            "pathDegraded": advisory is not None,
            "pathAdvisory": advisory,
        }
        self._reestablishment_advice = advice
        if recommendation_needed:
            self._record_event(
                "reestablishment_suggested",
                trigger=trigger,
                recommendation=advice,
            )
            schedule_callback(
                self.loop,
                self.reestablishment_suggested,
                (advice, candidates, self),
                (advice, self),
                (self,),
                (),
            )
            self._maybe_schedule_auto_reestablishment(trigger, advice)
        return candidates

    def _maybe_schedule_auto_reestablishment(self, trigger, advice):
        if not self._auto_reestablishment_enabled:
            return False
        if (
            self._system_policy_adaptation_task is not None
            and not self._system_policy_adaptation_task.done()
        ):
            return False
        if trigger not in self._auto_reestablishment_triggers:
            return False
        if self._auto_reestablishment_task is not None and not self._auto_reestablishment_task.done():
            return False
        if advice.get("pathDegraded"):
            advisory = advice.get("pathAdvisory") or {}
            if advisory.get("penalty", 0) < self._auto_reestablishment_min_penalty:
                return False
        self._auto_reestablishment_task = self.loop.create_task(
            self.attempt_reestablishment(timeout=self._auto_reestablishment_timeout)
        )
        return True

    def _report_path_change(
        self,
        previous_path,
        current_path,
        *,
        record_transition=True,
    ):
        if self._is_terminal():
            return
        self._previous_path = previous_path.copy()
        if record_transition:
            self.connection_context.record_path_transition(
                previous_path,
                current_path,
                protocol=self.protocol,
            )
        self._record_event(
            "path_change",
            previous_path=previous_path.copy(),
            current_path=current_path.copy(),
        )
        schedule_callback(
            self.loop,
            self.path_change,
            (previous_path, current_path, self),
            (current_path, self),
            (self,),
            (),
        )

    def _report_closed(self):
        if self._is_terminal():
            return
        self._fail_pending_sends(
            ConnectionError("Connection closed before the Send completed")
        )
        self._fail_receive_waiters(
            ConnectionError("Connection closed before the Receive completed")
        )
        self._mark_closed()
        schedule_callback(self.loop, self.closed, (self,))

    def _report_expired(self, message_context):
        if self._is_terminal():
            return
        self._queue_send_event("expired", message_context)

    def _allocate_send_call_id(self):
        self._send_call_sequence += 1
        return self._send_call_sequence

    def _track_send_call(
        self,
        message_context,
        *,
        initiate_with_send=False,
    ):
        call_id = self._allocate_send_call_id()
        if not self._send_calls and self._send_drain_waiter.done():
            self._send_drain_waiter = self.loop.create_future()
        self._send_calls[call_id] = {
            "context": message_context,
            "initiateWithSend": initiate_with_send,
        }
        return call_id

    def _finish_send_call(self, call_id):
        self._send_calls.pop(call_id, None)
        self._completed_send_calls.add(call_id)
        if not self._send_calls and not self._send_drain_waiter.done():
            self._send_drain_waiter.set_result(None)

    def _fail_pending_sends(self, reason, *, suppress_initiate_with_send=False):
        for call_id in sorted(self._send_calls):
            send_call = self._send_calls.get(call_id)
            if send_call is None:
                continue
            if suppress_initiate_with_send and send_call["initiateWithSend"]:
                self._finish_send_call(call_id)
                continue
            self._queue_send_event(
                "send_error",
                send_call["context"],
                reason,
                send_call_id=call_id,
            )
        self._pre_ready_sends.clear()
        self._queued_messages.clear()

    def _queue_send_event(self, kind, message_context, reason=None, *, send_call_id=None):
        if self._is_terminal():
            return
        call_id = send_call_id
        if call_id is None:
            call_id = getattr(message_context, "_pytaps_send_call_id", None)
        if call_id is None:
            call_id = self._allocate_send_call_id()
        if call_id in self._completed_send_calls:
            return
        self._pending_send_events[call_id] = (kind, message_context, reason)
        self._finish_send_call(call_id)
        self._flush_send_events()

    def _flush_send_events(self):
        while self._next_send_event_id in self._pending_send_events:
            if self._is_terminal():
                self._pending_send_events.clear()
                return
            kind, message_context, reason = self._pending_send_events.pop(
                self._next_send_event_id
            )
            self._next_send_event_id += 1
            if kind == "sent":
                self._record_event(
                    "sent",
                    message_id=getattr(message_context, "message_id", None),
                )
                schedule_callback(
                    self.loop,
                    self.sent,
                    (message_context, self),
                    (message_context,),
                    (self,),
                    (),
                )
            elif kind == "expired":
                self._record_event(
                    "expired",
                    message_id=getattr(message_context, "message_id", None),
                )
                schedule_callback(self.loop, self.expired, (message_context, self))
            elif kind == "send_error":
                self.last_error = reason
                reason_details = {
                    "message_id": getattr(message_context, "message_id", None),
                    "reason": str(reason),
                }
                if isinstance(reason, PartialSendError):
                    reason_details["bytesSent"] = reason.bytes_sent
                    reason_details["totalBytes"] = reason.total_bytes
                self._record_event(
                    "send_error",
                    **reason_details,
                )
                schedule_callback(
                    self.loop,
                    self.send_error,
                    (message_context, reason, self),
                    (message_context, self),
                    (self,),
                    (),
                )

    def _handle_attempt_done(self, task):
        if task in self.pending:
            self.pending.remove(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            if self.state is ConnectionState.ESTABLISHED:
                for pending in list(self.pending):
                    pending.cancel()
            return
        candidate = getattr(task, "_pytaps_candidate", None)
        if candidate is not None:
            local_path = (
                (candidate.local_address, candidate.local_endpoint.port)
                if candidate.local_address is not None
                and candidate.local_endpoint is not None
                else None
            )
            remote_path = (
                candidate.remote_address,
                candidate.remote_endpoint.port,
            )
            self.connection_context.record_candidate_outcome(
                local_path,
                remote_path,
                candidate.protocol,
                False,
                exc,
            )
            self.connection_context.record_performance_observation(
                local_path,
                remote_path,
                candidate.protocol,
                network_id=self._candidate_network_id(candidate),
                success=False,
                source="candidate-establishment",
            )
        if self.state is ConnectionState.ESTABLISHING:
            self.last_error = exc
        logger.warning("Connection attempt failed: %s", exc)

    def _candidate_racing_delay(self, candidate):
        local_path = (
            (candidate.local_address, candidate.local_endpoint.port)
            if candidate.local_address is not None
            and candidate.local_endpoint is not None
            else None
        )
        remote_path = (
            candidate.remote_address,
            candidate.remote_endpoint.port,
        )
        score = self.connection_context.get_path_score(
            local_path,
            remote_path,
            protocol=candidate.protocol,
            network_id=self._candidate_network_id(candidate),
        )
        if score > 0:
            return max(0.02, RACING_DELAY / (1 + min(score, 3)))
        if score < 0:
            return RACING_DELAY * (1 + min(abs(score), 3))
        return RACING_DELAY

    def _candidate_network_id(self, candidate):
        interface_id = (
            candidate.local_endpoint.interface
            if candidate.local_endpoint is not None
            else (
                candidate.path
                if candidate.path != "default"
                else None
            )
        )
        return self.connection_context.get_network_id(
            interface_id,
            local_address=candidate.local_address,
        )

    def _deliver_received(self, data, context):
        if self._is_terminal():
            return None
        self._mark_first_message()
        self._receive_sequence += 1
        context.received_at = context.received_at or self.loop.time()
        context.receive_sequence = self._receive_sequence
        received_message = ReceivedMessage(data, context, self)
        self._record_event(
            "received",
            bytesReceived=len(data),
            message_id=getattr(context, "message_id", None),
            receiveSequence=context.receive_sequence,
        )
        if self._receive_waiters:
            waiter = self._receive_waiters.pop(0)
            if not waiter.done():
                waiter.set_result(received_message)
        if self.received:
            self.loop.create_task(self.received(data, context, self))
        return received_message

    def _deliver_received_partial(self, data, context):
        if self._is_terminal():
            return None
        self._mark_first_message()
        self._receive_sequence += 1
        context.received_at = context.received_at or self.loop.time()
        context.receive_sequence = self._receive_sequence
        received_message = ReceivedMessage(data, context, self)
        self._record_event(
            "received_partial",
            bytesReceived=len(data),
            message_id=getattr(context, "message_id", None),
            receiveSequence=context.receive_sequence,
            endOfMessage=context.end_of_message,
        )
        if self._receive_waiters:
            waiter = self._receive_waiters.pop(0)
            if not waiter.done():
                waiter.set_result(received_message)
        if self.received_partial:
            self.loop.create_task(
                self.received_partial(
                    data,
                    context,
                    context.end_of_message,
                    self,
                )
            )
        return received_message

    def _check_send_allowed(self, message_context=None):
        if self._sent_final_message:
            return RuntimeError("Cannot send after a final message has been sent")
        if self._close_requested or self.state in {
            ConnectionState.CLOSING,
            ConnectionState.CLOSED,
        }:
            return RuntimeError("Cannot send after Close has been requested")
        direction = str(self.transport_properties.get("direction") or "").lower()
        if direction == "unidirectional receive":
            return RuntimeError("The Connection does not support sending")
        return None

    def _send_limits(self):
        if self.transports:
            message_size_limits = getattr(
                self.transports[0],
                "message_size_limits",
                None,
            )
            if callable(message_size_limits):
                return message_size_limits()
        if self.protocol == "udp":
            return {
                "singularTransmissionMsgMaxLen": 65507,
                "sendMsgMaxLen": 65507,
                "recvMsgMaxLen": 65507,
            }
        if self.protocol in {"tcp", "tls-tcp", "quic"}:
            return {
                "singularTransmissionMsgMaxLen": 0,
                "sendMsgMaxLen": (1 << 63) - 1,
                "recvMsgMaxLen": (1 << 63) - 1,
            }
        return {
            "singularTransmissionMsgMaxLen": 0,
            "sendMsgMaxLen": 0,
            "recvMsgMaxLen": 0,
        }

    def _can_send(self):
        direction = str(self.transport_properties.get("direction") or "").lower()
        if direction == "unidirectional receive":
            return False
        if self.transports and getattr(
            self.transports[0],
            "can_send",
            True,
        ) is False:
            return False
        return self.state is ConnectionState.ESTABLISHED and not self._close_requested

    def _can_receive(self):
        direction = str(self.transport_properties.get("direction") or "").lower()
        if direction == "unidirectional send":
            return False
        if self.transports and getattr(
            self.transports[0],
            "can_receive",
            True,
        ) is False:
            return False
        return (
            self.state is ConnectionState.ESTABLISHED
            and not self._close_requested
            and not self._received_final_message
        )

    def _selected_protocol_details(self):
        if self.protocol is None:
            return None
        selected = get_protocol_capabilities(
            self.protocol,
            self.transport_properties,
        )
        if self._protocol_capabilities:
            selected.update(self._protocol_capabilities)
        return selected

    def _runtime_available_protocols(self):
        available = {"tcp", "udp"}
        if self.security_context is not None:
            available.add("tls-tcp")
        if transport_impl.aioquic_connect is not None:
            available.add("quic")
        return available

    def _backend_property_support(self):
        if self.protocol is None:
            return {"connection": {}, "message": {}}
        return get_backend_property_support(
            self.protocol,
            self.transport_properties,
        )

    def _selection_properties_view(self):
        if self.state not in {
            ConnectionState.ESTABLISHED,
            ConnectionState.CLOSING,
            ConnectionState.CLOSED,
        }:
            return self.transport_properties.get_selection_properties()

        selected_protocol = self._selected_protocol_details() or {}
        properties = {}
        for prop, value in self.transport_properties.get_selection_properties().items():
            if prop in PREFERENCE_SELECTION_PROPERTIES:
                properties[prop] = bool(selected_protocol.get(prop))
            else:
                properties[prop] = value
        return properties

    def _validate_message_context(self, data, context):
        selected_protocol = self._selected_protocol_details() or {}
        if (
            self.protocol is not None
            and context.ordered
            and not selected_protocol.get("preserveOrder", False)
        ):
            return RuntimeError(
                "msgOrdered requires an ordered Connection"
            )
        connection_reliable = self._apply_message_defaults(MessageContext()).reliable
        if (
            context.reliable is not None
            and context.reliable != connection_reliable
            and not self._selection_properties_view().get(
                "perMsgReliability",
                False,
            )
        ):
            return RuntimeError(
                "Per-message reliability overrides require perMsgReliability support"
            )
        if self.protocol == "udp" and not context.safely_replayable:
            return RuntimeError(
                "UDP Messages must set safelyReplayable to true"
            )
        limits = self._send_limits()
        data_len = len(data)
        send_limit = limits["sendMsgMaxLen"]
        singular_limit = limits["singularTransmissionMsgMaxLen"]
        if send_limit and data_len > send_limit:
            return RuntimeError("Message exceeds sendMsgMaxLen")
        if (
            (context.no_segmentation or context.no_fragmentation)
            and singular_limit
            and data_len > singular_limit
        ):
            return RuntimeError("Message exceeds singularTransmissionMsgMaxLen")
        return None

    def _report_send_error(self, message_context, reason):
        if self._is_terminal():
            return
        self._queue_send_event("send_error", message_context, reason)

    def _propose_connection_property(self, canonical, value, *, explicit):
        proposed = self.transport_properties.clone()
        if explicit:
            proposed.set_property(canonical, value)
        else:
            proposed.default(canonical)
        return proposed

    def _validate_connection_property(
        self,
        canonical,
        value,
        *,
        protocol=None,
        transport_properties=None,
        ignore_inapplicable_protocol=False,
    ):
        properties = transport_properties or self.transport_properties
        selected_protocol = self.protocol if protocol is None else protocol

        if canonical in {"maxSendRate", "maxRecvRate"} and value != "Unlimited":
            raise NotImplementedError(
                f"{canonical} requires application-rate shaping, which is "
                "not implemented"
            )
        if (
            canonical == "connScheduler"
            and value != CONNECTION_PROPERTY_DEFAULTS["connScheduler"]
        ):
            raise NotImplementedError(
                "Only the default Connection Group scheduler is available"
            )
        if canonical.startswith("tcp."):
            if selected_protocol not in {None, "tcp", "tls-tcp"}:
                if ignore_inapplicable_protocol:
                    return
                raise ValueError(
                    f"TCP-specific Property {canonical} is not available "
                    f"on {selected_protocol}"
                )
            if value != CONNECTION_PROPERTY_DEFAULTS[canonical]:
                raise NotImplementedError(
                    f"{canonical} requires the RFC 5482 TCP User Timeout "
                    "Option, which is not exposed by this backend"
                )
        if (
            canonical == "multipathPolicy"
            and properties.get("multipath") != "Disabled"
            and value != "Handover"
        ):
            raise NotImplementedError(
                "This implementation supports only the Handover "
                "multipathPolicy"
            )
        if (
            canonical == "connTimeout"
            and value != "Disabled"
            and selected_protocol == "quic"
            and properties.get("_pytaps.quicTransportMode") != "Datagram"
        ):
            raise NotImplementedError(
                "connTimeout is not configurable on QUIC streams"
            )

    def _validate_connection_configuration(self, protocol):
        for canonical in (
            self.transport_properties.get_explicit_connection_properties()
        ):
            self._validate_connection_property(
                canonical,
                self.transport_properties.get(canonical),
                protocol=protocol,
                ignore_inapplicable_protocol=True,
            )

    def _set_local_connection_property(
        self,
        canonical,
        value,
        *,
        explicit,
    ):
        proposed = self._propose_connection_property(
            canonical,
            value,
            explicit=explicit,
        )
        normalized = proposed.get(canonical)
        self._validate_connection_property(
            canonical,
            normalized,
            transport_properties=proposed,
        )
        previous_properties = self.transport_properties
        previous_effects = self._backend_property_effects.copy()
        self.transport_properties = proposed
        try:
            self._apply_connection_properties(strict_property=canonical)
        except Exception:
            self.transport_properties = previous_properties
            self._backend_property_effects = previous_effects
            try:
                self._apply_connection_properties(
                    strict_property=canonical,
                )
            except Exception:
                pass
            raise

    def _apply_connection_properties(self, *, strict_property=None):
        if (
            self.protocol in {"tcp", "tls-tcp"}
            and self.transport_properties.get("connTimeout") != "Disabled"
        ):
            if self.connection_group is not None:
                self.connection_group.set_derived_property(
                    "tcp.userTimeoutChangeable",
                    False,
                )
            else:
                self.transport_properties.connection_properties[
                    "tcp.userTimeoutChangeable"
                ] = False
        for transport in list(self.transports):
            apply_properties = getattr(
                transport,
                "apply_connection_properties",
                None,
            )
            if callable(apply_properties):
                apply_properties(strict_property=strict_property)

    def set_property(self, prop, value):
        if is_message_property(prop):
            self.message_properties.set_property(prop, value)
        else:
            canonical = canonicalize_property_name(prop)
            if canonical in self.transport_properties.selection_properties:
                raise ValueError(
                    f"Selection Property {canonical} is read-only on a Connection"
                )
            if canonical in self.transport_properties.protocol_properties:
                raise ValueError(
                    f"Protocol Property {canonical} is fixed at establishment"
                )
            if (
                self.connection_group is not None
                and canonical in self.connection_group.ENTANGLED_PROPERTIES
            ):
                self.connection_group.set_property(
                    canonical,
                    value,
                )
            else:
                self._set_local_connection_property(
                    canonical,
                    value,
                    explicit=True,
                )
        return None

    def get_property(self, prop, default=None):
        if is_message_property(prop):
            return self.message_properties.get(prop, default)

        canonical = canonicalize_property_name(prop)
        read_only = self.get_properties()["readOnly"]
        if canonical in read_only:
            return read_only.get(canonical, default)
        selection = self.get_properties()["selection"]
        if canonical in selection:
            return selection.get(canonical, default)
        return self.transport_properties.get_property(prop, default)

    def default_property(self, prop):
        if is_message_property(prop):
            canonical = canonicalize_message_property_name(prop)
            setattr(self.message_properties, canonical, MESSAGE_PROPERTY_DEFAULTS[canonical])
            self.message_properties.explicit_properties.discard(canonical)
            return self

        canonical = canonicalize_property_name(prop)
        if canonical in self.transport_properties.selection_properties:
            raise ValueError(
                f"Selection Property {canonical} is read-only on a Connection"
            )
        if canonical in self.transport_properties.protocol_properties:
            raise ValueError(
                f"Protocol Property {canonical} is fixed at establishment"
            )
        if (
            self.connection_group is not None
            and canonical in self.connection_group.ENTANGLED_PROPERTIES
        ):
            self.connection_group.set_property(
                canonical,
                CONNECTION_PROPERTY_DEFAULTS[canonical],
                explicit=False,
            )
        else:
            self._set_local_connection_property(
                canonical,
                CONNECTION_PROPERTY_DEFAULTS[canonical],
                explicit=False,
            )
        return self

    def get_properties(self):
        limits = self._send_limits()
        return {
            "selection": self._selection_properties_view(),
            "connection": self.transport_properties.get_connection_properties(),
            "protocolSpecific": (
                self.transport_properties.get_protocol_properties()
            ),
            "message": self.message_properties.get_properties(),
            "security": (
                self.security_parameters.get_configuration()
                if self.security_parameters else {}
            ),
            "readOnly": {
                "connState": self.state.name.title(),
                "canSend": self._can_send(),
                "canReceive": self._can_receive(),
                "singularTransmissionMsgMaxLen": limits["singularTransmissionMsgMaxLen"],
                "sendMsgMaxLen": limits["sendMsgMaxLen"],
                "recvMsgMaxLen": limits["recvMsgMaxLen"],
                "protocol": self.protocol,
                "localEndpoint": self.local_endpoint,
                "remoteEndpoint": self.remote_endpoint,
                "groupSize": len(self.connection_group) if self.connection_group else 1,
                "securityAvailable": (
                    self.protocol in {"tls-tcp", "quic"}
                    or self.security_context is not None
                ),
                "backendCapabilities": self._selected_protocol_details(),
                "propertySupport": self._backend_property_support(),
                "propertyEffects": dict(self._backend_property_effects),
                "quicAssociation": (
                    self.quic_association.resource_snapshot()
                    if self.quic_association is not None
                    else None
                ),
                "messageDefaults": self.message_properties.get_properties(),
                "receiveSequence": self._receive_sequence,
                "softErrorCount": len(self._soft_errors),
                "connectionContext": self.connection_context.get_snapshot(),
                "currentPath": self._current_path.copy(),
                "previousPath": self._previous_path.copy(),
                "softErrors": [str(error) for error in self._soft_errors],
                "recommendedCandidate": (
                    self._reestablishment_advice["recommendedCandidate"]
                    if self._reestablishment_advice else None
                ),
                "reestablishmentCandidateCount": len(self._recommended_candidates),
                "autoReestablishmentEnabled": self._auto_reestablishment_enabled,
                "reestablishmentInProgress": (
                    self._auto_reestablishment_task is not None
                    and not self._auto_reestablishment_task.done()
                ),
                "systemPolicyAdaptationInProgress": (
                    self._system_policy_adaptation_task is not None
                    and not self._system_policy_adaptation_task.done()
                ),
                "pathDegraded": (
                    self._reestablishment_advice["pathDegraded"]
                    if self._reestablishment_advice else False
                ),
                "eventCount": len(self._event_history),
                "lastEvent": self._event_history[-1] if self._event_history else None,
                "lastError": str(self.last_error) if self.last_error else None,
            },
        }

    def get_event_history(self):
        return list(self._event_history)

    async def wait_ready(self, timeout=None):
        async def wait_for_ready_and_race_cleanup():
            await asyncio.shield(self._ready_waiter)
            race_task = self.race_task
            if (
                race_task is not None
                and race_task is not asyncio.current_task()
                and not race_task.done()
            ):
                await asyncio.shield(race_task)

        action = wait_for_ready_and_race_cleanup()
        if timeout is None:
            await action
        else:
            await asyncio.wait_for(action, timeout)
        return self

    async def wait_closed(self, timeout=None):
        if timeout is None:
            await asyncio.shield(self._closed_waiter)
        else:
            await asyncio.wait_for(asyncio.shield(self._closed_waiter), timeout)
        return self

    def add_remote(self, remote_endpoints):
        for endpoint in remote_endpoints:
            if not hasattr(endpoint, "clone"):
                raise TypeError("Remote endpoints must be RemoteEndpoint objects")
            self.remote_endpoints.append(endpoint.clone())
        if self.remote_endpoint is None and self.remote_endpoints:
            self.remote_endpoint = self.remote_endpoints[0]
        return list(self.remote_endpoints)

    def remove_remote(self, remote_endpoints):
        remove_values = [endpoint.__dict__ for endpoint in remote_endpoints]
        self.remote_endpoints = [
            endpoint
            for endpoint in self.remote_endpoints
            if endpoint.__dict__ not in remove_values
        ]
        if (
            self.remote_endpoint is not None
            and self.remote_endpoint.__dict__ in remove_values
        ):
            self.remote_endpoint = (
                self.remote_endpoints[0] if self.remote_endpoints else None
            )
        return list(self.remote_endpoints)

    def add_local(self, local_endpoints):
        for endpoint in local_endpoints:
            if not hasattr(endpoint, "clone"):
                raise TypeError("Local endpoints must be LocalEndpoint objects")
            self.local_endpoints.append(endpoint.clone())
        if self.local_endpoint is None and self.local_endpoints:
            self.local_endpoint = self.local_endpoints[0]
        return list(self.local_endpoints)

    async def migrate_path(self, local_endpoint=None, *, timeout=5):
        """Validate and hand over a live QUIC association to a local path.

        This implementation extension maps RFC 9622 Handover policy to QUIC
        connection migration. Every live Connection in the QUIC Connection
        Group receives PathChange only after the new path is validated.
        """
        if self.protocol != "quic" or self.quic_association is None:
            raise RuntimeError(
                "Path migration is currently available only for QUIC"
            )
        if local_endpoint is None:
            local_endpoint = LocalEndpoint()
        if not isinstance(local_endpoint, LocalEndpoint):
            raise TypeError("local_endpoint must be a LocalEndpoint")
        return await self.quic_association.migrate_local_path(
            self,
            local_endpoint,
            timeout=timeout,
        )

    def remove_local(self, local_endpoints):
        remove_values = [endpoint.__dict__ for endpoint in local_endpoints]
        self.local_endpoints = [
            endpoint
            for endpoint in self.local_endpoints
            if endpoint.__dict__ not in remove_values
        ]
        if (
            self.local_endpoint is not None
            and self.local_endpoint.__dict__ in remove_values
        ):
            self.local_endpoint = (
                self.local_endpoints[0] if self.local_endpoints else None
            )
        return list(self.local_endpoints)

    async def clone(self, framer=None, connection_properties=None):
        """Create another Connection entangled with this Connection.

        On QUIC, a Clone normally creates another stream on the same
        association. ``_pytaps.quicStreamType`` can select a bidirectional or
        unidirectional stream, while
        ``_pytaps.quicTransportMode=Datagram`` exposes the association-wide
        RFC 9221 datagram service. Raw QUIC supports one such datagram
        Connection per association because DATAGRAM frames have no stream or
        application-flow identifier.
        """
        template = self._originating_preconnection.clone()
        template.local_endpoints = [
            self.local_endpoint.clone()
        ] if self.local_endpoint else []
        template.remote_endpoints = [
            self.remote_endpoint.clone()
        ] if self.remote_endpoint else []
        template.transport_properties = self.transport_properties.clone()
        template.message_properties = deepcopy(self.message_properties)
        if framer is not None:
            template.framers = [framer]
        protocol_specific_properties = set()
        if connection_properties:
            for prop, value in connection_properties.items():
                template.transport_properties.set_property(prop, value)
                canonical = canonicalize_property_name(prop)
                if (
                    canonical
                    in template.transport_properties.protocol_properties
                ):
                    protocol_specific_properties.add(canonical)
        if (
            template.transport_properties.get("_pytaps.quicTransportMode")
            == "Datagram"
        ):
            template.transport_properties.prohibit("reliability")
            template.transport_properties.prohibit("preserveOrder")
            template.transport_properties.require("preserveMsgBoundaries")
            template.transport_properties.prohibit("perMsgReliability")
        template.connection_context = self.connection_context
        template._reuse_isolated_context = True
        cloned_connection = None
        try:
            if (
                self.protocol == "quic"
                and self.quic_association is not None
            ):
                cloned_connection = Connection(template)
                self.connection_group.add_connection(cloned_connection)
                if (
                    template.transport_properties.get(
                        "_pytaps.quicTransportMode"
                    )
                    == "Datagram"
                ):
                    await self.quic_association.open_datagram_connection(
                        cloned_connection
                    )
                else:
                    await self.quic_association.open_stream_connection(
                        cloned_connection
                    )
            else:
                cloned_connection = await template.initiate()
                self.connection_group.add_connection(cloned_connection)
            if connection_properties:
                for prop, value in connection_properties.items():
                    canonical = canonicalize_property_name(prop)
                    if canonical not in protocol_specific_properties:
                        cloned_connection.set_property(prop, value)
            return cloned_connection
        except BaseException as exc:
            if (
                cloned_connection is not None
                and not cloned_connection._is_terminal()
            ):
                await cloned_connection._discard_failed_clone(exc)
            if (
                cloned_connection is not None
                and cloned_connection.connection_group is self.connection_group
            ):
                self.connection_group.remove_connection(cloned_connection)
            self._report_clone_error(exc)
            raise

    def _prepare_send_context(self, message_context, end_of_message):
        if not isinstance(end_of_message, bool):
            raise TypeError("endOfMessage must be a Boolean")
        if message_context is not None and not isinstance(
            message_context,
            MessageContext,
        ):
            raise TypeError("messageContext must be a MessageContext")
        if not end_of_message and message_context is None:
            raise ValueError("Partial sends require a MessageContext")

        partial_key = id(message_context) if message_context is not None else None
        partial = (
            self._partial_send_contexts.get(partial_key)
            if partial_key is not None
            else None
        )
        if partial is not None and partial["source"] is message_context:
            context = deepcopy(partial["context"])
        else:
            context = deepcopy(message_context) if message_context is not None else MessageContext()
            context.end_of_message = end_of_message
            context = self._apply_message_defaults(
                context.ensure_created(),
                resolve_connection_defaults=self.protocol is not None,
            )
            self._message_sequence += 1
            context.message_id = self._message_sequence
            if message_context is not None:
                message_context.message_id = context.message_id
            if not end_of_message:
                self._partial_send_contexts[partial_key] = {
                    "source": message_context,
                    "context": deepcopy(context),
                }

        context.end_of_message = end_of_message
        if end_of_message and partial_key is not None:
            self._partial_send_contexts.pop(partial_key, None)
        return context

    def _enqueue_send_action(
        self,
        data,
        message_context=None,
        end_of_message=True,
        *,
        defer=False,
        initiate_with_send=False,
    ):
        if isinstance(data, str):
            data = data.encode()
        context = self._prepare_send_context(
            message_context,
            end_of_message,
        )
        send_call_id = self._track_send_call(
            context,
            initiate_with_send=initiate_with_send,
        )
        setattr(context, "_pytaps_send_call_id", send_call_id)
        send_error = self._check_send_allowed(context)
        if send_error is not None:
            self._queue_send_event(
                "send_error",
                context,
                send_error,
                send_call_id=send_call_id,
            )
            return context.message_id
        if context.final:
            self._sent_final_message = True

        entry = {
            "sequence": send_call_id,
            "data": data,
            "context": context,
            "end_of_message": end_of_message,
            "send_call_id": send_call_id,
            "initiateWithSend": initiate_with_send,
        }
        if defer:
            self._queued_messages.append(entry)
        elif self.state is ConnectionState.ESTABLISHING:
            self._pre_ready_sends.append(entry)
        elif self.state is ConnectionState.ESTABLISHED:
            self._dispatch_send(entry)
        else:
            self._queue_send_event(
                "send_error",
                context,
                RuntimeError("Connection is not established"),
                send_call_id=send_call_id,
            )
        return context.message_id

    def _replayable_initiate_with_send_entry(self):
        if (
            self.transport_properties.get("zeroRttMsg")
            is PreferenceLevel.PROHIBIT
        ):
            return None
        for entry in self._pre_ready_sends:
            context = entry["context"]
            if (
                entry.get("initiateWithSend")
                and context.safely_replayable
                and not context.is_expired()
                and entry["data"]
            ):
                return entry
        return None

    def _claim_pre_ready_send(self, entry):
        for index, queued in enumerate(self._pre_ready_sends):
            if queued is entry:
                return self._pre_ready_sends.pop(index)
        return None

    def _dispatch_send(self, entry):
        context = self._apply_message_defaults(entry["context"])
        send_call_id = entry["send_call_id"]
        if context.is_expired():
            self._queue_send_event("expired", context, send_call_id=send_call_id)
            return
        validation_error = self._validate_message_context(entry["data"], context)
        if validation_error is not None:
            self._queue_send_event(
                "send_error",
                context,
                validation_error,
                send_call_id=send_call_id,
            )
            return
        if not self.transports:
            self._queue_send_event(
                "send_error",
                context,
                RuntimeError("Connection has no established transport"),
                send_call_id=send_call_id,
            )
            return
        try:
            result = self.transports[0].send(
                entry["data"],
                context,
                entry["end_of_message"],
                send_call_id=send_call_id,
            )
        except Exception as exc:
            self._queue_send_event(
                "send_error",
                context,
                exc,
                send_call_id=send_call_id,
            )
            return
        if isinstance(result, PartialSendError):
            self._queue_send_event(
                "send_error",
                context,
                result,
                send_call_id=send_call_id,
            )
            return
        if result is not None and context.final:
            self._sent_final_message = True

    async def _flush_pre_ready_sends(self):
        queued = self._pre_ready_sends
        self._pre_ready_sends = []
        for entry in queued:
            self._dispatch_send(entry)

    async def send(self, data, message_context=None, end_of_message=True):
        return self._enqueue_send_action(
            data,
            message_context,
            end_of_message,
        )

    async def send_batch(self, messages):
        self._batch_counter += 1
        batch_id = self._batch_counter
        queued_ids = []

        for entry in messages:
            if isinstance(entry, tuple):
                data = entry[0]
                context = entry[1] if len(entry) > 1 else None
                end_of_message = entry[2] if len(entry) > 2 else True
            else:
                data = entry
                context = None
                end_of_message = True

            if context is None:
                context = MessageContext()
            if context.batch_id is None:
                context.batch_id = batch_id
            queued_ids.append(
                self.enqueue_message(data, context, end_of_message)
            )
        await self.flush_messages()
        return queued_ids

    def enqueue_message(self, data, message_context=None, end_of_message=True):
        return self._enqueue_send_action(
            data,
            message_context,
            end_of_message,
            defer=True,
        )

    async def flush_messages(self):
        def sort_key(entry):
            context = entry["context"]
            final_rank = 1 if context.final else 0
            ordered_rank = 0 if context.ordered else 1
            return (final_rank, context.priority, ordered_rank, entry["sequence"])

        self._queued_messages.sort(key=sort_key)
        queued_messages = self._queued_messages
        self._queued_messages = []

        message_ids = []
        for entry in queued_messages:
            context = entry["context"]
            message_ids.append(context.message_id)
            if self.state is ConnectionState.ESTABLISHING:
                self._pre_ready_sends.append(entry)
            else:
                self._dispatch_send(entry)
        return message_ids

    async def initiate_with_send(self, data, message_context=None, end_of_message=True):
        if not end_of_message:
            raise ValueError("InitiateWithSend does not support partial sends")
        return self._enqueue_send_action(
            data,
            message_context,
            end_of_message,
            initiate_with_send=True,
        )

    async def close_group(self):
        if self.connection_group:
            await self.connection_group.close()

    def abort(self, reason="Aborted by local endpoint"):
        if self._is_terminal():
            return
        self._close_requested = True
        self._report_connection_error(reason, suggest_reestablishment=False)
        for transport in list(self.transports):
            abort_member = getattr(transport, "abort", None)
            if callable(abort_member):
                result = abort_member(reason)
                if asyncio.iscoroutine(result):
                    task = self.loop.create_task(result)
                    if self._close_task is None or self._close_task.done():
                        self._close_task = task
                continue
            if getattr(transport, "transport", None) is not None:
                abort_transport = getattr(transport.transport, "abort", None)
                if callable(abort_transport):
                    abort_transport()
                else:
                    transport.transport.close()
            else:
                self.loop.create_task(transport.close())

    async def abort_group(self):
        if self.connection_group:
            await self.connection_group.abort()

    def grouped_connections(self):
        if self.connection_group is None:
            return [self]
        return sorted(
            self.connection_group.connections,
            key=lambda connection: connection.transport_properties.get("connPriority"),
        )

    def _expanded_local_endpoints_for_racing(self):
        if not self.local_endpoints:
            system_endpoints = (
                self.connection_context.get_system_local_endpoints()
            )
            return system_endpoints or [None]
        expanded = []
        for endpoint in self.local_endpoints:
            if endpoint.address is not None or endpoint.interface is None:
                expanded.append(endpoint.clone())
                continue
            _require_netifaces()
            try:
                interface_addresses = netifaces.ifaddresses(
                    endpoint.interface
                )
            except ValueError as error:
                logger.warning(
                    "Cannot get IP addresses for %s: %s",
                    endpoint.interface,
                    error,
                )
                continue
            addresses = []
            addresses.extend(
                entry["addr"].split("%", 1)[0]
                for entry in interface_addresses.get(netifaces.AF_INET6, [])
                if not entry["addr"].lower().startswith("fe80")
            )
            addresses.extend(
                entry["addr"]
                for entry in interface_addresses.get(netifaces.AF_INET, [])
            )
            logger.info(
                "Expanded path %s to local addresses %s",
                endpoint.interface,
                addresses,
            )
            for address in dict.fromkeys(addresses):
                candidate_endpoint = endpoint.clone()
                candidate_endpoint.address = address
                expanded.append(candidate_endpoint)
        return expanded

    @staticmethod
    def _branch_address_family(branch):
        local_endpoint = branch.local_endpoint
        if local_endpoint is None or local_endpoint.address is None:
            return socket.AddressFamily.AF_UNSPEC
        if ":" in local_endpoint.address:
            return socket.AddressFamily.AF_INET6
        return socket.AddressFamily.AF_INET

    async def _resolve_candidate_branch(self, branch: CandidateBranch):
        remote_endpoint = branch.remote_endpoint.clone()
        remote_endpoint.port = remote_endpoint.effective_port(branch.protocol)
        if remote_endpoint.port is None:
            raise ValueError(
                f"Remote Endpoint has no port for {branch.protocol}"
            )
        requested_family = self._branch_address_family(branch)
        configured_address = remote_endpoint.effective_address()
        resolution_source = "configured"
        resolution_duration = 0.0

        if configured_address is not None:
            address_family = (
                socket.AddressFamily.AF_INET6
                if ":" in configured_address
                else socket.AddressFamily.AF_INET
            )
            resolved_addresses = [(address_family, configured_address)]
        elif remote_endpoint.host_name is not None:
            cache_key_family = int(requested_family)
            cached = self.connection_context.get_cached_resolution(
                branch.path,
                remote_endpoint.host_name,
                branch.protocol,
                cache_key_family,
            )
            if cached is not None:
                resolved_addresses = cached
                resolution_source = "cache"
            else:
                started = self.loop.time()
                socket_type = (
                    socket.SOCK_DGRAM
                    if branch.protocol in {"udp", "quic"}
                    else socket.SOCK_STREAM
                )
                try:
                    remote_info = await self.loop.getaddrinfo(
                        remote_endpoint.host_name,
                        remote_endpoint.port,
                        family=requested_family,
                        type=socket_type,
                    )
                except BaseException as error:
                    resolution_duration = self.loop.time() - started
                    self.connection_context.record_resolution(
                        branch.path,
                        remote_endpoint.host_name,
                        branch.protocol,
                        cache_key_family,
                        [],
                        duration=resolution_duration,
                        error=error,
                    )
                    raise
                resolution_duration = self.loop.time() - started
                resolved_addresses = []
                seen = set()
                for info in remote_info:
                    if info[0] not in {
                        socket.AddressFamily.AF_INET,
                        socket.AddressFamily.AF_INET6,
                    }:
                        continue
                    key = (info[0], info[4][0])
                    if key in seen:
                        continue
                    seen.add(key)
                    resolved_addresses.append(key)
                self.connection_context.record_resolution(
                    branch.path,
                    remote_endpoint.host_name,
                    branch.protocol,
                    cache_key_family,
                    resolved_addresses,
                    duration=resolution_duration,
                )
                resolution_source = "dns"
        else:
            raise ValueError(
                "A Remote Endpoint needs a hostname, IP address, "
                "or multicast group"
            )

        resolved_addresses = order_remote_addresses(
            resolved_addresses,
            connection_context=self.connection_context,
        )
        expanded_addresses = []
        for address_family, address in resolved_addresses:
            if (
                requested_family != socket.AddressFamily.AF_UNSPEC
                and address_family != requested_family
            ):
                continue
            for alternate_family, alternate_address in (
                self.connection_context.get_alternate_remotes(
                    address,
                    protocol=branch.protocol,
                )
            ):
                expanded_addresses.append(
                    (
                        alternate_family
                        or (
                            socket.AddressFamily.AF_INET6
                            if ":" in alternate_address
                            else socket.AddressFamily.AF_INET
                        ),
                        alternate_address,
                    )
                )
            expanded_addresses.append((address_family, address))

        candidates = []
        seen = set()
        for address_family, address in expanded_addresses:
            key = (address_family, address, remote_endpoint.port)
            if key in seen:
                continue
            seen.add(key)
            candidate_remote = remote_endpoint.clone()
            candidate_remote.address = address
            candidate_local = (
                branch.local_endpoint.clone()
                if branch.local_endpoint is not None
                else None
            )
            candidates.append(
                Candidate(
                    protocol=branch.protocol,
                    remote_address=address,
                    address_family=address_family,
                    path=branch.path,
                    local_address=(
                        candidate_local.address
                        if candidate_local is not None
                        else None
                    ),
                    local_endpoint=candidate_local,
                    remote_endpoint=candidate_remote,
                    branch_id=branch.branch_id,
                    resolution_source=resolution_source,
                    resolution_duration=resolution_duration,
                )
            )
        return candidates

    def _candidate_branches_for_racing(self):
        available_protocols = self._runtime_available_protocols()
        protocol_candidates = build_protocol_candidates(
            self.transport_properties,
            connection_context=self.connection_context,
            available_protocols=available_protocols,
        )
        local_endpoints = self._expanded_local_endpoints_for_racing()
        return build_candidate_branches(
            self,
            local_endpoints=local_endpoints,
            available_protocols=protocol_candidates,
        )

    async def _gather_candidate_leaves(self):
        branches = self._candidate_branches_for_racing()
        if not branches:
            return []

        tasks = [
            self.loop.create_task(self._resolve_candidate_branch(branch))
            for branch in branches
        ]
        self._resolution_tasks = tasks
        try:
            results = await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            self._resolution_tasks = []

        candidates = []
        for result in results:
            if isinstance(result, BaseException):
                if not isinstance(result, asyncio.CancelledError):
                    self.last_error = result
                    logger.warning(
                        "Candidate branch resolution failed: %s",
                        result,
                    )
                continue
            candidates.extend(result)
        return order_candidates_for_racing(self, candidates)

    @staticmethod
    def _candidate_local_address(candidate):
        candidate_local = (
            candidate.local_endpoint.clone()
            if candidate.local_endpoint is not None
            else None
        )
        if candidate.local_address and candidate_local is not None:
            candidate_local.address = candidate.local_address
        return candidate_local

    def _candidate_local_bind(self, candidate_local):
        if candidate_local is None or (
            candidate_local.address is None
            and candidate_local.port is None
        ):
            return None
        return (
            candidate_local.socket_address(),
            0 if self._rendezvous_mode else candidate_local.port or 0,
        )

    async def _discard_candidate_transport(self, transport):
        if transport in self.transports:
            self.transports.remove(transport)
        try:
            await transport._stop_framers()
        finally:
            await transport._close_raw()

    async def _open_candidate(self, candidate):
        logger.info(
            "Trying candidate protocol=%s path=%s remote=%s local=%s "
            "branch=%s source=%s",
            candidate.protocol,
            candidate.path,
            candidate.remote_address,
            candidate.local_address,
            candidate.branch_id,
            candidate.resolution_source,
        )
        candidate_remote = candidate.remote_endpoint.clone()
        candidate_local = self._candidate_local_address(candidate)
        local_address_to_use = self._candidate_local_bind(candidate_local)

        if candidate.protocol == "udp":
            if candidate_remote.is_multicast:
                transport = MulticastSendTransport(
                    connection=self,
                    local_endpoint=candidate_local,
                    remote_endpoint=candidate_remote,
                )
                try:
                    return await transport.active_open(None)
                except BaseException:
                    await self._discard_candidate_transport(transport)
                    raise
            transport = UdpTransport(
                connection=self,
                local_endpoint=candidate_local,
                remote_endpoint=candidate_remote,
            )
            try:
                return await _open_asyncio_candidate(
                    self.loop.create_datagram_endpoint(
                        lambda: transport,
                        remote_addr=(
                            candidate_remote.socket_address(),
                            candidate_remote.port,
                        ),
                        local_addr=local_address_to_use,
                    ),
                    transport,
                )
            except BaseException:
                await self._discard_candidate_transport(transport)
                raise

        if candidate.protocol == "quic":
            association = QuicAssociationManager(loop=self.loop)
            open_quic = (
                association.open_datagram_connection
                if self.transport_properties.get(
                    "_pytaps.quicTransportMode"
                )
                == "Datagram"
                else association.open_stream_connection
            )
            try:
                result = await open_quic(
                    self,
                    local_endpoint=candidate_local,
                    remote_endpoint=candidate_remote,
                )
                if self.quic_association is not association:
                    await association.close_association()
                return result
            except BaseException:
                await association.close_association()
                raise

        if candidate.protocol in {"tcp", "tls-tcp"}:
            server_hostname = None
            if candidate.protocol == "tls-tcp":
                server_hostname = (
                    self.security_parameters.server_name
                    if (
                        self.security_parameters is not None
                        and self.security_parameters.server_name
                    )
                    else (
                        candidate_remote.host_name
                        or candidate_remote.address
                    )
                )
            transport = TcpTransport(
                connection=self,
                local_endpoint=candidate_local,
                remote_endpoint=candidate_remote,
                protocol_name=candidate.protocol,
            )
            try:
                return await _open_asyncio_candidate(
                    self.loop.create_connection(
                        lambda: transport,
                        candidate_remote.socket_address(),
                        candidate_remote.port,
                        ssl=(
                            self.security_context
                            if candidate.protocol == "tls-tcp"
                            else None
                        ),
                        server_hostname=server_hostname,
                        local_addr=local_address_to_use,
                    ),
                    transport,
                )
            except BaseException:
                await self._discard_candidate_transport(transport)
                raise

        raise RuntimeError(
            f"No backend is registered for protocol {candidate.protocol}"
        )

    async def _attempt_candidate_after_delay(self, candidate, delay):
        if delay > 0:
            await asyncio.sleep(delay)
        if self.state is not ConnectionState.ESTABLISHING:
            return None
        return await self._open_candidate(candidate)

    async def race(self):
        self.active = True
        self._establishment_started_at = self.loop.time()
        try:
            branches = self._candidate_branches_for_racing()
        except BaseException as error:
            self._fail_initiate(error)
            return

        if not branches:
            error = self.last_error or RuntimeError("Candidate set is empty")
            logger.critical("Candidate set is empty, aborting")
            self._fail_initiate(error)
            return

        race_started = self.loop.time()
        resolution_tasks = {}
        for branch_index, branch in enumerate(branches):
            task = self.loop.create_task(
                self._resolve_candidate_branch(branch)
            )
            resolution_tasks[task] = (branch_index, branch)
        self._resolution_tasks = list(resolution_tasks)
        pending_attempts = set()
        all_attempts = []
        try:
            while (
                (resolution_tasks or pending_attempts)
                and self.state is ConnectionState.ESTABLISHING
            ):
                done, _pending = await asyncio.wait(
                    set(resolution_tasks) | pending_attempts,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                ordered_done = sorted(
                    done,
                    key=lambda task: (
                        0,
                        resolution_tasks[task][0],
                    )
                    if task in resolution_tasks
                    else (1, 0),
                )
                for task in ordered_done:
                    if task in resolution_tasks:
                        branch_index, branch = resolution_tasks.pop(task)
                        self._resolution_tasks = list(resolution_tasks)
                        try:
                            candidate_set = task.result()
                        except asyncio.CancelledError:
                            continue
                        except BaseException as error:
                            self.last_error = error
                            logger.warning(
                                "Candidate branch %s resolution failed: %s",
                                branch.branch_id,
                                error,
                            )
                            continue

                        candidate_set = order_candidates_for_racing(
                            self,
                            candidate_set,
                        )
                        if not candidate_set:
                            continue
                        logger.info(
                            "Resolved candidate branch %s: %s",
                            branch.branch_id,
                            candidate_set,
                        )
                        first_delay = self._candidate_racing_delay(
                            candidate_set[0]
                        )
                        target_offset = branch_index * first_delay
                        for candidate in candidate_set:
                            delay = max(
                                0,
                                race_started
                                + target_offset
                                - self.loop.time(),
                            )
                            attempt = self.loop.create_task(
                                self._attempt_candidate_after_delay(
                                    candidate,
                                    delay,
                                )
                            )
                            attempt._pytaps_candidate = candidate
                            self.pending.append(attempt)
                            attempt.add_done_callback(
                                self._handle_attempt_done
                            )
                            pending_attempts.add(attempt)
                            all_attempts.append(attempt)
                            target_offset += self._candidate_racing_delay(
                                candidate
                            )
                        continue

                    pending_attempts.discard(task)
                    if not task.cancelled():
                        error = task.exception()
                        if (
                            error is not None
                            and self.state is ConnectionState.ESTABLISHING
                        ):
                            self.last_error = error

            if self.state is ConnectionState.ESTABLISHED:
                for task in resolution_tasks:
                    task.cancel()
                for attempt in pending_attempts:
                    attempt.cancel()
        except asyncio.CancelledError:
            for task in resolution_tasks:
                task.cancel()
            for attempt in pending_attempts:
                attempt.cancel()
            raise
        finally:
            if resolution_tasks:
                await asyncio.gather(
                    *resolution_tasks,
                    return_exceptions=True,
                )
            self._resolution_tasks = []
            if all_attempts:
                await asyncio.gather(
                    *all_attempts,
                    return_exceptions=True,
                )
            for attempt in all_attempts:
                if attempt in self.pending:
                    self.pending.remove(attempt)

        if self.state is not ConnectionState.ESTABLISHED:
            self._fail_initiate(
                self.last_error
                or RuntimeError("Connection establishment failed")
            )

    async def send_message(self, data, message_context=None, end_of_message=True):
        """ Attempts to send data on the connection.
            Attributes:
                data (string, required):
                    Data to be send.
        """
        if isinstance(data, str):
            data = data.encode()
        return await self.send(data, message_context, end_of_message)

    @staticmethod
    def _normalize_receive_length(value, name, *, allow_zero):
        if value in {None, "Infinite", -1} or value == float("inf"):
            return float("inf")
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or (value == 0 and not allow_zero)
        ):
            qualifier = "non-negative" if allow_zero else "positive"
            raise ValueError(f"{name} must be a {qualifier} Integer or Infinite")
        return value

    async def receive(
        self,
        min_incomplete_length=float("inf"),
        max_length=float("inf"),
        timeout=None,
    ):
        """ Queues the reception of a message.
        Attributes:
            min_incomplete_length (integer, optional):
                The minimum length an incomplete message
                needs to have.
            max_length (integer, optional):
                The maximum length a message can have.
        """
        min_incomplete_length = self._normalize_receive_length(
            min_incomplete_length,
            "minIncompleteLength",
            allow_zero=True,
        )
        max_length = self._normalize_receive_length(
            max_length,
            "maxLength",
            allow_zero=False,
        )
        if (
            min_incomplete_length != float("inf")
            and max_length != float("inf")
            and min_incomplete_length > max_length
        ):
            raise ValueError("minIncompleteLength cannot exceed maxLength")

        async def _receive_action():
            if self.state is ConnectionState.ESTABLISHING:
                await asyncio.shield(self._ready_waiter)
            if self._received_final_message:
                error = RuntimeError(
                    "No more messages can be received after a final message"
                )
                self._report_receive_error(None, error)
                raise error
            if not self._can_receive():
                error = RuntimeError("The Connection cannot receive data")
                self._report_receive_error(None, error)
                raise error
            if not self.transports:
                error = RuntimeError("Connection has no established transport")
                self._report_receive_error(None, error)
                raise error

            waiter = self.loop.create_future()
            self._receive_waiters.append(waiter)
            read_task = self.transports[0].receive(
                min_incomplete_length,
                max_length,
            )
            try:
                if read_task is None:
                    return await waiter
                done, _pending = await asyncio.wait(
                    {waiter, read_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if waiter in done:
                    if not read_task.done():
                        read_task.cancel()
                    else:
                        try:
                            read_task.exception()
                        except asyncio.CancelledError:
                            pass
                    return await waiter

                error = read_task.exception()
                if error is not None:
                    self._report_receive_error(
                        getattr(
                            self.transports[0],
                            "current_message_context",
                            None,
                        ),
                        error,
                    )
                    raise error
                if waiter.done():
                    return await waiter
                error = RuntimeError(
                    "Receive completed without a Receive event"
                )
                self._report_receive_error(None, error)
                raise error
            finally:
                if read_task is not None and not read_task.done():
                    read_task.cancel()
                if waiter in self._receive_waiters:
                    self._receive_waiters.remove(waiter)
                if not waiter.done():
                    waiter.cancel()

        action = _receive_action()
        if timeout is None:
            return await action
        return await asyncio.wait_for(action, timeout)

    async def receive_message(
        self,
        min_incomplete_length=float("inf"),
        max_length=float("inf"),
        timeout=None,
    ):
        return await self.receive(min_incomplete_length, max_length, timeout=timeout)

    def close(self):
        """ Attempts to close the connection, issues a closed event
        on success.
        """
        if self._is_terminal():
            return self._close_task
        if self._close_task is not None and not self._close_task.done():
            return self._close_task
        self._close_requested = True
        if self.state is ConnectionState.ESTABLISHING:
            self.abort("Close requested before establishment completed")
            return self._close_task
        self._set_state(ConnectionState.CLOSING)
        self._close_task = self.loop.create_task(self._graceful_close())
        return self._close_task

    async def _graceful_close(self):
        try:
            if self._queued_messages:
                await self.flush_messages()
            await asyncio.shield(self._send_drain_waiter)
            companion_tasks = []
            companions = self._rendezvous_companions
            self._rendezvous_companions = []
            for companion in companions:
                if companion._is_terminal():
                    continue
                close_task = companion.close()
                if close_task is not None:
                    companion_tasks.append(close_task)
            if companion_tasks:
                await asyncio.gather(
                    *companion_tasks,
                    return_exceptions=True,
                )
            if self.transports:
                await self.transports[0].close()
            else:
                self._report_closed()
        except Exception as exc:
            self._report_connection_error(exc)

    def parse(self, min_incomplete_length=0, max_length=0):
        """ Returns the message buffer of the
            connection.
        """
        transport = self.transports[0]
        return (
            transport.recv_buffer,
            getattr(transport, "current_message_context", None),
            getattr(transport, "at_eof", False),
        )

    def note_path_change(
        self,
        *,
        local_address=None,
        local_port=None,
        remote_address=None,
        remote_port=None,
        _record_transition=True,
        _update_group=True,
    ):
        previous_path = self._current_path.copy()
        if local_address is not None:
            if self.local_endpoint is None:
                origin = getattr(self._originating_preconnection, "local_endpoint", None)
                if origin is not None:
                    self.local_endpoint = origin.clone()
            if self.local_endpoint is None:
                self.local_endpoint = LocalEndpoint()
            self.local_endpoint.address = local_address
        if local_port is not None and self.local_endpoint is not None:
            self.local_endpoint.port = local_port
        if remote_address is not None:
            if self.remote_endpoint is None:
                origin = getattr(self._originating_preconnection, "remote_endpoint", None)
                if origin is not None:
                    self.remote_endpoint = origin.clone()
            if self.remote_endpoint is None:
                self.remote_endpoint = RemoteEndpoint()
            self.remote_endpoint.address = remote_address
        if remote_port is not None and self.remote_endpoint is not None:
            self.remote_endpoint.port = remote_port
        current_path = {
            "local": (self.local_endpoint.address, self.local_endpoint.port)
            if self.local_endpoint and self.local_endpoint.address else None,
            "remote": (self.remote_endpoint.address, self.remote_endpoint.port)
            if self.remote_endpoint and self.remote_endpoint.address else None,
        }
        if current_path == previous_path:
            return current_path
        self._current_path = current_path.copy()
        if self.local_endpoint is not None and not any(
            endpoint.__dict__ == self.local_endpoint.__dict__
            for endpoint in self.local_endpoints
        ):
            self.local_endpoints.append(self.local_endpoint.clone())
        if self.remote_endpoint is not None and not any(
            endpoint.__dict__ == self.remote_endpoint.__dict__
            for endpoint in self.remote_endpoints
        ):
            self.remote_endpoints.append(self.remote_endpoint.clone())
        if _update_group and self.connection_group is not None:
            self.connection_group.note_path_change(
                previous_path,
                current_path,
                initial=(
                    previous_path["local"] is None
                    and previous_path["remote"] is None
                ),
            )
        self.connection_context.record_path_use(
            current_path["local"],
            current_path["remote"],
            protocol=self.protocol,
        )
        self._report_path_change(
            previous_path,
            current_path,
            record_transition=_record_transition,
        )
        self._refresh_reestablishment_guidance("path_change")
        return current_path

    def note_soft_error(self, reason, *, penalty=2, lifetime=60):
        current_local = self._current_path.get("local")
        current_remote = self._current_path.get("remote")
        if current_local is not None or current_remote is not None:
            self.connection_context.degrade_path(
                current_local,
                current_remote,
                reason=reason,
                penalty=penalty,
                lifetime=lifetime,
            )
        self._report_soft_error(reason)
        self._refresh_reestablishment_guidance("soft_error")
        return reason

    def _handle_system_policy_update(self, changes):
        if self._is_terminal() or self.local_endpoint is None:
            return
        interface_id = (
            self.local_endpoint.interface
            or self.connection_context.get_interface_for_address(
                self.local_endpoint.effective_address()
            )
        )
        interface_change = changes.get("interfaces", {}).get(interface_id)
        if interface_change is None:
            return
        previous_policy = interface_change.get("previous") or {}
        current_policy = interface_change["current"]
        previous_network = previous_policy.get("networkId")
        current_network = current_policy.get("networkId")
        self._record_event(
            "system_policy_changed",
            interface=interface_id,
            available=current_policy.get("available", True),
            previous_network=previous_network,
            current_network=current_network,
        )
        if self.state is not ConnectionState.ESTABLISHED:
            return

        current_address = self.local_endpoint.effective_address()
        policy_addresses = current_policy.get("addresses")
        address_withdrawn = (
            policy_addresses is not None
            and current_address is not None
            and not any(
                self._system_addresses_equal(
                    (
                        entry.get("address")
                        if isinstance(entry, dict)
                        else entry
                    ),
                    current_address,
                )
                for entry in policy_addresses
            )
        )
        network_changed = (
            previous_network is not None
            and current_network is not None
            and previous_network != current_network
        )
        if current_policy.get("available") is False:
            reason = (
                f"System Policy marked interface {interface_id!r} unavailable"
            )
        elif address_withdrawn:
            reason = (
                f"System Policy withdrew local address {current_address!r} "
                f"from interface {interface_id!r}"
            )
        elif network_changed:
            reason = (
                "System Policy changed network identity for interface "
                f"{interface_id!r}"
            )
        else:
            return

        self._maybe_schedule_system_policy_adaptation(
            allow_current_path=network_changed,
        )
        self.note_soft_error(
            RuntimeError(reason),
            penalty=4,
            lifetime=60,
        )

    def _system_policy_migration_candidate(
        self,
        *,
        allow_current_path=False,
    ):
        current_address = self.local_endpoint.effective_address()
        remote_address = (
            self.remote_endpoint.effective_address()
            if self.remote_endpoint is not None
            else None
        )
        family_reference = remote_address or current_address
        try:
            required_family = (
                ipaddress.ip_address(
                    str(family_reference).split("%", 1)[0]
                ).version
                if family_reference is not None
                else None
            )
        except ValueError:
            required_family = None
        try:
            remote_is_loopback = (
                ipaddress.ip_address(
                    str(remote_address).split("%", 1)[0]
                ).is_loopback
                if remote_address is not None
                else False
            )
        except ValueError:
            remote_is_loopback = False

        for endpoint in self.connection_context.get_system_local_endpoints():
            constraint = self._system_policy_endpoint_constraint(endpoint)
            if constraint is False:
                continue
            candidate_address = endpoint.effective_address()
            if (
                not allow_current_path
                and endpoint.interface == self.local_endpoint.interface
                and self._system_addresses_equal(
                    candidate_address,
                    current_address,
                )
            ):
                continue
            try:
                candidate_ip = ipaddress.ip_address(candidate_address)
            except ValueError:
                continue
            if (
                required_family is not None
                and candidate_ip.version != required_family
            ):
                continue
            if candidate_ip.is_loopback and not remote_is_loopback:
                continue
            candidate = endpoint.clone()
            candidate.port = (
                constraint.effective_port(self.protocol)
                if constraint is not None
                else 0
            ) or 0
            if (
                allow_current_path
                and candidate.port != 0
                and endpoint.interface == self.local_endpoint.interface
                and self._system_addresses_equal(
                    candidate_address,
                    current_address,
                )
            ):
                continue
            return candidate
        return None

    def _system_policy_endpoint_constraint(self, candidate):
        configured = self._originating_preconnection.local_endpoints
        if not configured:
            return None
        candidate_address = candidate.effective_address()
        for constraint in configured:
            if (
                constraint.interface is not None
                and constraint.interface != candidate.interface
            ):
                continue
            constraint_address = constraint.effective_address()
            if (
                constraint_address is not None
                and not self._system_addresses_equal(
                    constraint_address,
                    candidate_address,
                )
            ):
                continue
            return constraint
        return False

    @staticmethod
    def _system_addresses_equal(first, second):
        if first is None or second is None:
            return first == second
        try:
            return ipaddress.ip_address(
                str(first).split("%", 1)[0]
            ) == ipaddress.ip_address(
                str(second).split("%", 1)[0]
            )
        except ValueError:
            return str(first).casefold() == str(second).casefold()

    def _maybe_schedule_system_policy_adaptation(
        self,
        *,
        allow_current_path=False,
    ):
        if (
            self.protocol != "quic"
            or self.quic_association is None
            or self.transport_properties.get("multipath") != "Active"
            or self.transport_properties.get("multipathPolicy")
            != "Handover"
        ):
            return False
        association_owner = (
            self.quic_association.anchor_connection
            or self.quic_association._handshake_owner
        )
        if association_owner is not self:
            return False
        if (
            self._system_policy_adaptation_task is not None
            and not self._system_policy_adaptation_task.done()
        ):
            return False
        candidate = self._system_policy_migration_candidate(
            allow_current_path=allow_current_path,
        )
        if candidate is None:
            return False
        self._system_policy_adaptation_task = self.loop.create_task(
            self._adapt_quic_path_for_system_policy(candidate)
        )
        return True

    async def _adapt_quic_path_for_system_policy(self, local_endpoint):
        self._record_event(
            "path_adaptation_started",
            interface=local_endpoint.interface,
            address=local_endpoint.effective_address(),
        )
        try:
            path = await self.migrate_path(local_endpoint, timeout=5)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self._is_terminal():
                self._record_event(
                    "path_adaptation_failed",
                    interface=local_endpoint.interface,
                    address=local_endpoint.effective_address(),
                    error=str(error),
                )
                self._refresh_reestablishment_guidance(
                    "path_adaptation_failed"
                )
                advice = self._reestablishment_advice
                if advice is not None:
                    self.loop.call_soon(
                        self._resume_auto_reestablishment_after_adaptation,
                        advice,
                    )
            return None
        if not self._is_terminal():
            self._record_event(
                "path_adaptation_succeeded",
                interface=local_endpoint.interface,
                address=local_endpoint.effective_address(),
                path=path,
            )
        return path

    def _resume_auto_reestablishment_after_adaptation(self, advice):
        adaptation_task = self._system_policy_adaptation_task
        if adaptation_task is not None and not adaptation_task.done():
            self.loop.call_soon(
                self._resume_auto_reestablishment_after_adaptation,
                advice,
            )
            return
        self._maybe_schedule_auto_reestablishment(
            "soft_error",
            advice,
        )

    def clear_path_degradation(self, *, local_path=None, remote_path=None):
        self.connection_context.clear_path_degradation(
            local_path if local_path is not None else self._current_path.get("local"),
            remote_path if remote_path is not None else self._current_path.get("remote"),
        )
        return self

    async def attempt_reestablishment(self, timeout=None):
        candidates = self._recommended_candidates or self.get_reestablishment_candidates()
        if not candidates:
            return None
        best = candidates[0]
        template = self._originating_preconnection.clone()
        template.connection_context = self.connection_context.clone()
        for protocol_name in template.connection_context.protocol_policy.keys():
            template.connection_context.set_protocol_policy(
                protocol_name,
                **{
                    **template.connection_context.protocol_policy[protocol_name],
                    "available": protocol_name == best.protocol,
                },
            )
        if best.protocol not in template.connection_context.protocol_policy:
            template.connection_context.set_protocol_policy(
                best.protocol,
                available=True,
                preference_adjustment=10,
            )
        if best.local_endpoint is not None:
            template.local_endpoints = [
                best.local_endpoint.clone()
            ]
        elif self.local_endpoint is not None:
            template.local_endpoints = [self.local_endpoint.clone()]
        if (
            template.local_endpoint is not None
            and best.local_address is not None
        ):
            template.local_endpoint.address = best.local_address
        if self.remote_endpoint is not None:
            template.remote_endpoints = [self.remote_endpoint.clone()]
            template.remote_endpoint.address = best.remote_address
        new_connection = await template.initiate(timeout=timeout)
        self._last_reestablished_connection = new_connection
        self._record_event(
            "reestablished",
            protocol=new_connection.protocol,
            remote_address=best.remote_address,
            local_address=best.local_address,
        )
        schedule_callback(
            self.loop,
            self.reestablished,
            (new_connection, self),
            (new_connection,),
            (self,),
            (),
        )
        return new_connection

    def get_reestablishment_candidates(self):
        if self.remote_endpoint is None:
            return []
        address = self.remote_endpoint.effective_address()
        if address is None:
            return []
        family = (
            socket.AddressFamily.AF_INET6
            if ":" in address
            else socket.AddressFamily.AF_INET
        )
        remote_addrs = [(family, address, self.remote_endpoint)]
        local_endpoints = self.local_endpoints
        if not self._originating_preconnection.local_endpoints:
            local_endpoints = (
                self.connection_context.get_system_local_endpoints()
                or local_endpoints
            )
        return order_candidates_for_racing(
            self,
            create_candidates(
                self,
                remote_addrs,
                available_protocols=self._runtime_available_protocols(),
                local_endpoints=local_endpoints,
            ),
        )

    def enable_auto_reestablishment(
        self,
        *,
        triggers=None,
        min_penalty=3,
        timeout=5,
    ):
        self._auto_reestablishment_enabled = True
        if triggers is not None:
            self._auto_reestablishment_triggers = set(triggers)
        self._auto_reestablishment_min_penalty = min_penalty
        self._auto_reestablishment_timeout = timeout
        return self

    def disable_auto_reestablishment(self):
        self._auto_reestablishment_enabled = False
        return self

    def get_group_properties(self):
        if self.connection_group is None:
            return {
                "size": 1,
                "connections": [self],
                "sharedConnectionProperties": {},
                "connectionContext": self.connection_context.get_snapshot(),
            }
        return self.connection_group.get_properties()

    def get_connection_context(self):
        return self.connection_context

    def subscribe_monitoring(self, callback):
        self.connection_context.subscribe(callback, self.loop)
        return callback

    def unsubscribe_monitoring(self, callback):
        self.connection_context.unsubscribe(callback)
        return self

    def set_interface_policy(self, interface_id, **policy):
        self.connection_context.set_interface_policy(interface_id, **policy)
        return self

    def set_protocol_policy(self, protocol, **policy):
        self.connection_context.set_protocol_policy(protocol, **policy)
        return self

    def set_pvd_policy(self, pvd_id, **policy):
        self.connection_context.set_pvd_policy(pvd_id, **policy)
        return self

    def set_address_family_policy(self, family, preference_adjustment=0):
        self.connection_context.set_address_family_policy(
            family,
            preference_adjustment=preference_adjustment,
        )
        return self

    def note_alternate_remote(
        self,
        base_remote,
        alternate_remote,
        *,
        address_family=None,
        protocol=None,
        lifetime=None,
    ):
        self.connection_context.note_alternate_remote(
            base_remote,
            alternate_remote,
            address_family=address_family,
            protocol=protocol,
            lifetime=lifetime,
        )
        return self

    def get_monitoring_snapshot(self):
        return {
            "connectionContext": self.connection_context.get_snapshot(),
            "events": self.get_event_history(),
            "properties": self.get_properties(),
            "reestablishmentAdvice": self._reestablishment_advice,
            "reestablishmentCandidates": [
                self._candidate_summary(candidate)
                for candidate in self._recommended_candidates
            ],
        }

    # Events for active open
    def on_ready(self, callback):
        """ Set callback for ready events that
            get thrown once the connection is ready
            to send and receive data.

        Args:
            callback (callback, required): Function that implements the
                callback.
        """
        self.ready = callback

    def on_initiate_error(self, callback):
        """ Set callback for initiate error events that
            get thrown if an error occurs
            during initiation.

        Args:
            callback (callback, required): Function that implements the
                callback.
        """
        self.initiate_error = callback

    # Events for sending messages
    def on_sent(self, callback):
        """ Set callback for sent events that get thrown if a message has been
        successfully sent.

        Args:
            callback (callback, required): Function that implements the
                callback.
        """
        self.sent = callback

    def on_send_error(self, callback):
        """ Set callback for send error events
            that get thrown if an error occurs
            during sending of a message.

        Args:
            callback (callback, required): Function that implements the
                callback.
        """
        self.send_error = callback

    def on_expired(self, callback):
        """ Set callback for expired events that
            get thrown if a message expires.

        Args:
            callback (callback, required): Function that implements the
                callback.
        """
        self.expired = callback

    # Events for receiving messages
    def on_received(self, callback):
        """ Set callback for received events that get thrown if a new message
        has been received.

        Args:
            callback (callback, required): Function that implements the
                callback.
        """
        self.received = callback

    def on_received_partial(self, callback):
        """ Set callback for partial received events that
            get thrown if a new partial
            message has been received.

        Args:
            callback (callback, required): Function that implements the
                callback.
        """
        self.received_partial = callback

    def on_receive_error(self, callback):
        """ Set callback for receive error events that
            get thrown if an error occurs
            during reception of a message.

        Args:
            callback (callback, required): Function that implements the
                callback.
        """
        self.receive_error = callback

    def on_connection_error(self, callback):
        """ Set callback for connection error events that
            get thrown if an error occurs
            while the connection is open.

        Args:
            callback (callback, required): Function that implements the
                callback.
        """
        self.connection_error = callback

    def on_soft_error(self, callback):
        self.soft_error = callback

    def on_path_change(self, callback):
        self.path_change = callback

    def on_clone_error(self, callback):
        self.clone_error = callback

    def on_reestablishment_suggested(self, callback):
        self.reestablishment_suggested = callback

    def on_reestablished(self, callback):
        self.reestablished = callback

    def on_establishment_error(self, callback):
        self.establishment_error = callback

    def on_rendezvous_done(self, callback):
        self.rendezvous_done = callback

    # Events for closing a connection
    def on_closed(self, callback):
        """ Set callback for on closed events that get thrown if the
        connection has been closed successfully.

        Args:
            callback (callback, required): Function that implements the
                callback.  Callback signature should accept a connection
                as its parameter.
        """
        self.closed = callback

    def multicast_leave(self):
        if self.transports:
            self.loop.create_task(self.transports[0].close())
