import asyncio
import socket
from copy import deepcopy
try:
    import netifaces
except ImportError:
    netifaces = None

from . import transports as transport_impl
from .connection_group import ConnectionGroup
from .message import (
    MESSAGE_PROPERTY_DEFAULTS,
    MessageContext,
    ReceivedMessage,
    canonicalize_message_property_name,
    is_message_property,
)
from .transportProperties import (
    PREFERENCE_SELECTION_PROPERTIES,
    PreferenceLevel,
    canonicalize_property_name,
)
from .transports import MulticastSendTransport, QuicAssociationManager, TcpTransport, UdpTransport
from .utility import (
    Candidate,
    ConnectionState,
    SleepClassForRacing,
    build_protocol_candidates,
    create_candidates,
    order_candidates_for_racing,
    schedule_callback,
    setup_logger,
)
from .transportProperties import get_protocols

logger = setup_logger(__name__)
# Wait for 100 ms between connection attempts when racing
RACING_DELAY = 0.1


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
        self.framer = preconnection.framer
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
        self._context_ready_recorded = False
        self._context_detached = False
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
        self.connection_context.attach_connection()
        self._auto_reestablishment_timeout = 5
        self._auto_reestablishment_task = None
        self._last_reestablished_connection = None

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
        self.connection_context.detach_connection(
            was_ready=self._context_ready_recorded,
        )
        self._context_detached = True

    def _mark_ready(self):
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
        self.connection_context.record_candidate_outcome(
            self._current_path.get("local"),
            self._current_path.get("remote"),
            self.protocol,
            True,
        )
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

    def _report_path_change(self, previous_path, current_path):
        if self._is_terminal():
            return
        self._previous_path = previous_path.copy()
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
        )
        if score > 0:
            return max(0.02, RACING_DELAY / (1 + min(score, 3)))
        if score < 0:
            return RACING_DELAY * (1 + min(abs(score), 3))
        return RACING_DELAY

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
        return self.state is ConnectionState.ESTABLISHED and not self._close_requested

    def _can_receive(self):
        direction = str(self.transport_properties.get("direction") or "").lower()
        if direction == "unidirectional send":
            return False
        return (
            self.state is ConnectionState.ESTABLISHED
            and not self._close_requested
            and not self._received_final_message
        )

    def _selected_protocol_details(self):
        if self.protocol is None:
            return None
        for protocol in get_protocols():
            if protocol["name"] == self.protocol:
                return protocol
        return None

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
        connection_reliable = self._apply_message_defaults(MessageContext()).reliable
        if (
            context.reliable is not None
            and context.reliable != connection_reliable
            and self.transport_properties.get("perMsgReliability")
            is not PreferenceLevel.REQUIRE
        ):
            return RuntimeError(
                "Per-message reliability overrides require perMsgReliability support"
            )
        if self.protocol == "udp" and not context.safely_replayable:
            return RuntimeError(
                "UDP messages must be marked safely replayable to satisfy RFC 9622 semantics"
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

    def set_property(self, prop, value):
        if is_message_property(prop):
            self.message_properties.set_property(prop, value)
        else:
            canonical = canonicalize_property_name(prop)
            if canonical in self.transport_properties.selection_properties:
                raise ValueError(
                    f"Selection Property {canonical} is read-only on a Connection"
                )
            self.transport_properties.set_property(canonical, value)
            if self.connection_group is not None:
                self.connection_group.set_property(canonical, value)
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
        self.transport_properties.default_property(canonical)
        if self.connection_group is not None:
            self.connection_group.set_property(
                canonical,
                self.transport_properties.get(canonical),
            )
        return self

    def get_properties(self):
        limits = self._send_limits()
        return {
            "selection": self._selection_properties_view(),
            "connection": self.transport_properties.get_connection_properties(),
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
                "securityAvailable": self.security_context is not None,
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
        if timeout is None:
            await asyncio.shield(self._ready_waiter)
        else:
            await asyncio.wait_for(asyncio.shield(self._ready_waiter), timeout)
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
            template.framer = framer
        if connection_properties:
            for prop, value in connection_properties.items():
                template.transport_properties.set_property(prop, value)
        template.connection_context = self.connection_context
        template._reuse_isolated_context = True
        try:
            if (
                self.protocol == "quic"
                and self.quic_association is not None
            ):
                cloned_connection = Connection(template)
                self.connection_group.add_connection(cloned_connection)
                await self.quic_association.open_stream_connection(cloned_connection)
            else:
                cloned_connection = await template.initiate()
                self.connection_group.add_connection(cloned_connection)
            if connection_properties:
                for prop, value in connection_properties.items():
                    cloned_connection.set_property(prop, value)
            return cloned_connection
        except Exception as exc:
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

    async def race(self):
        # This is an active connection attempt
        self.active = True
        protocol_candidates = build_protocol_candidates(
            self.transport_properties,
            connection_context=self.connection_context,
        )

        if len(protocol_candidates) == 0:
            logger.critical("Candidate set is empty, aborting")
            self._fail_initiate(RuntimeError("Candidate set is empty"))
            return

        remote_addrs = []
        for remote_endpoint in self.remote_endpoints:
            address = remote_endpoint.effective_address()
            if address is not None:
                family = (
                    socket.AddressFamily.AF_INET6
                    if ":" in address
                    else socket.AddressFamily.AF_INET
                )
                remote_addrs.append((family, address, remote_endpoint.clone()))
                logger.info("Not resolving - using address %s", address)
                continue

            if remote_endpoint.host_name:
                # asyncio does not expose interface-scoped DNS resolution.
                remote_info = await self.loop.getaddrinfo(
                    remote_endpoint.host_name,
                    remote_endpoint.port or 0,
                )
                seen = set()
                for info in remote_info:
                    if info[0] not in {
                        socket.AddressFamily.AF_INET,
                        socket.AddressFamily.AF_INET6,
                    }:
                        continue
                    key = (info[0], info[4][0], info[4][1])
                    if key in seen:
                        continue
                    seen.add(key)
                    resolved_endpoint = remote_endpoint.clone()
                    resolved_endpoint.port = remote_endpoint.port
                    remote_addrs.append(
                        (info[0], info[4][0], resolved_endpoint)
                    )
                logger.info(
                    "Resolved %s to %s",
                    remote_endpoint.host_name,
                    [address for _family, address, _endpoint in remote_addrs],
                )
                continue

            raise ValueError(
                "A Remote Endpoint needs a hostname, IP address, "
                "or multicast group"
            )

        candidate_set = create_candidates(self, remote_addrs)

        if any(endpoint.interface for endpoint in self.local_endpoints):
            _require_netifaces()
            local_addresses_by_family = {}
            for endpoint in self.local_endpoints:
                local_interface = endpoint.interface
                if local_interface is None:
                    continue
                try:
                    interface_addresses = netifaces.ifaddresses(local_interface)
                    local_v6_addrs = [
                        entry["addr"]
                        for entry in interface_addresses.get(netifaces.AF_INET6, [])
                        if entry["addr"][:4] != "fe80"
                    ]
                    local_v4_addrs = [
                        entry["addr"]
                        for entry in interface_addresses.get(netifaces.AF_INET, [])
                    ]
                    local_addresses_by_family[local_interface] = {
                        socket.AddressFamily.AF_INET6: local_v6_addrs,
                        socket.AddressFamily.AF_INET: local_v4_addrs,
                    }
                    logger.info(
                        "Trying addresses of local interface %s --> %s, %s",
                        local_interface,
                        local_v6_addrs,
                        local_v4_addrs,
                    )
                except ValueError as err:
                    logger.critical(
                        "Cannot get IP addresses for %s: %s",
                        local_interface,
                        err,
                    )
            expanded_candidates = []
            for candidate in candidate_set:
                if candidate.path == "default" or candidate.local_address is not None:
                    expanded_candidates.append(candidate)
                    continue
                family_addrs = local_addresses_by_family.get(candidate.path, {})
                for local_address in family_addrs.get(candidate.address_family, []):
                    local_endpoint = candidate.local_endpoint.clone()
                    local_endpoint.address = local_address
                    expanded_candidates.append(
                        Candidate(
                            protocol=candidate.protocol,
                            remote_address=candidate.remote_address,
                            address_family=candidate.address_family,
                            path=candidate.path,
                            local_address=local_address,
                            local_endpoint=local_endpoint,
                            remote_endpoint=candidate.remote_endpoint,
                        )
                    )
            candidate_set = expanded_candidates
        candidate_set = order_candidates_for_racing(self, candidate_set)
        logger.info("Final Candidates: " + str(candidate_set))

        # Attempt to establish a connection with each candidate
        for candidate in candidate_set:

            if self.state == ConnectionState.ESTABLISHED:
                logger.info("Connection established -- stop racing")
                break

            logger.info("Trying candidate protocol: " + str(candidate.protocol) +
                        " on path " + str(candidate.path) +
                        " and remote address: " + str(candidate.remote_address) +
                        (" and local address: " + str(candidate.local_address)
                         if candidate.local_address else ""))
            candidate_remote = candidate.remote_endpoint.clone()
            candidate_local = (
                candidate.local_endpoint.clone()
                if candidate.local_endpoint is not None
                else None
            )
            if candidate.local_address and candidate_local is not None:
                candidate_local.address = candidate.local_address
            local_address_to_use = None
            if candidate_local is not None and (
                candidate_local.address is not None
                or candidate_local.port is not None
            ):
                local_address_to_use = (
                    candidate_local.socket_address(),
                    (
                        0
                        if self._rendezvous_mode
                        else candidate_local.port or 0
                    ),
                )

            if candidate.protocol == 'udp':
                logger.info("Creating UDP connect task with remote addr " +
                            str(candidate.remote_address) + ", port " +
                            str(candidate_remote.port))

                if candidate_remote.is_multicast:
                    task = self.loop.create_task(
                        MulticastSendTransport(
                            connection=self,
                            local_endpoint=candidate_local,
                            remote_endpoint=candidate_remote,
                        ).active_open(None)
                    )
                    task._pytaps_candidate = candidate
                    self.pending.append(task)
                    task.add_done_callback(self._handle_attempt_done)
                    logger.info("Using multicast publication transport.")
                    break

                # Create a datagram endpoint
                udp_transport = UdpTransport(
                    connection=self,
                    local_endpoint=candidate_local,
                    remote_endpoint=candidate_remote,
                )
                task = self.loop.create_task(
                    self.loop.create_datagram_endpoint(
                        lambda: udp_transport,
                        remote_addr=(candidate_remote.socket_address(),
                                     candidate_remote.port),
                        local_addr=local_address_to_use))
                task._pytaps_candidate = candidate
                self.pending.append(task)
                task.add_done_callback(self._handle_attempt_done)

                logger.info("Not racing multiple addrs for UDP" +
                            " -- stop racing")
                break

            elif candidate.protocol == "quic":
                if transport_impl.aioquic_connect is None:
                    logger.info(
                        "Skipping quic candidate for %s because aioquic is not installed.",
                        candidate.remote_address,
                    )
                    continue
                logger.info(
                    "Creating QUIC stream candidate to %s:%s.",
                    candidate.remote_address,
                    candidate_remote.port,
                )
                self.quic_association = QuicAssociationManager(loop=self.loop)
                task = self.loop.create_task(
                    self.quic_association.open_stream_connection(
                        self,
                        local_endpoint=candidate_local,
                        remote_endpoint=candidate_remote,
                    )
                )
                task._pytaps_candidate = candidate
                self.pending.append(task)
                task.add_done_callback(self._handle_attempt_done)
                await self.sleeper_for_racing.sleep(
                    self._candidate_racing_delay(candidate)
                )

            elif candidate.protocol in {'tcp', 'tls-tcp'}:
                if candidate.protocol == "tls-tcp" and self.security_context is None:
                    logger.info(
                        "Skipping tls-tcp candidate for %s because no security context is configured.",
                        candidate.remote_address,
                    )
                    continue
                logger.info(
                    "Creating %s connect task to %s.",
                    candidate.protocol,
                    candidate.remote_address,
                )
                server_hostname = None
                if candidate.protocol == "tls-tcp" and self.security_context:
                    if (
                        self.security_parameters
                        and self.security_parameters.server_name
                    ):
                        server_hostname = self.security_parameters.server_name
                    else:
                        server_hostname = (
                            candidate_remote.host_name
                            or candidate_remote.address
                        )
                # If the protocol is tcp, create a asyncio connection
                tcp_transport = TcpTransport(
                    connection=self,
                    local_endpoint=candidate_local,
                    remote_endpoint=candidate_remote,
                    protocol_name=candidate.protocol,
                )
                task = self.loop.create_task(
                    self.loop.create_connection(
                        lambda: tcp_transport,
                        candidate_remote.socket_address(),
                        candidate_remote.port,
                        ssl=self.security_context if candidate.protocol == "tls-tcp" else None,
                        server_hostname=server_hostname,
                        local_addr=local_address_to_use))
                task._pytaps_candidate = candidate
                self.pending.append(task)
                task.add_done_callback(self._handle_attempt_done)
                # Wait before starting next connection attempt
                await self.sleeper_for_racing.sleep(
                    self._candidate_racing_delay(candidate)
                )

        if self.pending and self.state != ConnectionState.ESTABLISHED:
            await asyncio.gather(*list(self.pending), return_exceptions=True)

        if self.state != ConnectionState.ESTABLISHED:
            error = self.last_error or RuntimeError("Connection establishment failed")
            self._fail_initiate(error)

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
    ):
        previous_path = self._current_path.copy()
        if local_address is not None:
            if self.local_endpoint is None:
                origin = getattr(self._originating_preconnection, "local_endpoint", None)
                if origin is not None:
                    self.local_endpoint = origin.clone()
            if self.local_endpoint is None:
                return self._current_path
            self.local_endpoint.address = local_address
        if local_port is not None and self.local_endpoint is not None:
            self.local_endpoint.port = local_port
        if remote_address is not None:
            if self.remote_endpoint is None:
                origin = getattr(self._originating_preconnection, "remote_endpoint", None)
                if origin is not None:
                    self.remote_endpoint = origin.clone()
            if self.remote_endpoint is None:
                return self._current_path
            self.remote_endpoint.address = remote_address
        if remote_port is not None and self.remote_endpoint is not None:
            self.remote_endpoint.port = remote_port
        current_path = {
            "local": (self.local_endpoint.address, self.local_endpoint.port)
            if self.local_endpoint and self.local_endpoint.address else None,
            "remote": (self.remote_endpoint.address, self.remote_endpoint.port)
            if self.remote_endpoint and self.remote_endpoint.address else None,
        }
        self._current_path = current_path.copy()
        self.connection_context.record_path_use(
            current_path["local"],
            current_path["remote"],
            protocol=self.protocol,
        )
        self._report_path_change(previous_path, current_path)
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
        if self.local_endpoint is not None:
            template.local_endpoints = [self.local_endpoint.clone()]
            if best.local_address is not None:
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
        return order_candidates_for_racing(
            self,
            create_candidates(self, remote_addrs),
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
