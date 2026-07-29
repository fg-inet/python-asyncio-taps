import asyncio
import ipaddress
import socket

try:
    import netifaces
except ImportError:
    netifaces = None

from . import transports as transport_impl
from .connection import Connection
from .connection_context import ConnectionContext
from .endpoint import RemoteEndpoint
from .multicast import (
    do_join,
    do_leave,
    join_subscription,
    leave_subscription,
)
from .transports import QuicAssociationManager, TcpTransport, UdpTransport
from .utility import (
    ConnectionState,
    build_protocol_candidates,
    schedule_callback,
    setup_logger,
)

logger = setup_logger(__name__, "cyan")

POLICY_RECONCILE_RETRY_BASE_DELAY = 0.25
POLICY_RECONCILE_RETRY_MAX_DELAY = 5.0


def _require_netifaces():
    if netifaces is None:
        raise ImportError(
            "Interface-constrained listeners require the 'netifaces' package."
        )


class Listener:
    """The TAPS listener class.

    Attributes:
        preconnection (Preconnection, required):
                Preconnection object from which this Connection
                object was created.
    """

    def __init__(self, preconnection, *, action=None):
        # Initializations
        preconnection._framer_configuration_locked = True
        self.preconnection = preconnection._copy_configuration(
            action=action or "listen",
            security_role="listener",
        )
        self.preconnection._framer_configuration_locked = True
        self.local_endpoints = [
            endpoint.clone() for endpoint in self.preconnection.local_endpoints
        ]
        self.remote_endpoints = [
            endpoint.clone() for endpoint in self.preconnection.remote_endpoints
        ]
        self.local_endpoint = (
            self.local_endpoints[0] if self.local_endpoints else None
        )
        self.remote_endpoint = (
            self.remote_endpoints[0] if self.remote_endpoints else None
        )
        self.transport_properties = self.preconnection.transport_properties.clone()
        self.security_parameters = self.preconnection.security_parameters
        self.security_context = self.preconnection.security_context
        self.connection_context = self.preconnection.connection_context
        self.loop = self.preconnection.loop
        self.framers = tuple(self.preconnection.framers)
        self.active_ports = {}
        self.protocol = None
        self.quic_association = None
        self.listen_task = None
        self.state = ConnectionState.ESTABLISHING
        self._listen_waiter = self.loop.create_future()
        self._connection_waiters = []
        self._accepted_connections = []
        self._new_connection_limit = float("inf")
        self._resolved_remote_constraints = [
            endpoint.clone() for endpoint in self.remote_endpoints
        ]
        self._servers = []
        self._datagram_transports = []
        self._binding_records = []
        self._join_ctx = None
        self._draining_quic_bindings = {}
        self._binding_ports = {}
        self._protocol_candidates = []
        self._system_policy_paths = []
        self._policy_reconcile_requested = False
        self._policy_reconcile_task = None
        self._policy_reconcile_retry_handle = None
        self._policy_reconcile_retry_attempt = 0
        self._policy_reconcile_last_error = None
        self._multicast_refresh_interfaces = set()
        self._resources_closed = False
        self._stopped_waiter = self.loop.create_future()
        self.last_error = None
        self._event_history = []
        self._context_listening_recorded = False
        self._context_detached = False
        self.connection_context.attach_listener(self)

        # Callbacks
        self.stopped = self.preconnection.stopped
        self.listen_error = self.preconnection.listen_error
        self.establishment_error = self.preconnection.establishment_error
        self.connection_received = self.preconnection.connection_received
        self.initiate_error = self.preconnection.initiate_error
        self.ready = self.preconnection.ready

    async def wait_listening(self, timeout=None):
        waiter = asyncio.shield(self._listen_waiter)
        if timeout is None:
            await waiter
        else:
            await asyncio.wait_for(waiter, timeout)
        return self

    def set_new_connection_limit(self, limit):
        if (
            limit is None
            or limit == "Infinite"
            or limit == float("inf")
        ):
            self._new_connection_limit = float("inf")
            return self
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 0
        ):
            raise ValueError(
                "New Connection Limit must be a non-negative Integer or Infinite"
            )
        self._new_connection_limit = limit
        return self

    def accept(self, timeout=None):
        if self._accepted_connections:
            connection = self._accepted_connections.pop(0)

            async def _return_queued_connection():
                return connection

            return self.loop.create_task(_return_queued_connection())

        waiter = self.loop.create_future()
        self._connection_waiters.append(waiter)

        async def _wait_for_connection():
            try:
                if timeout is None:
                    return await waiter
                return await asyncio.wait_for(waiter, timeout)
            finally:
                if waiter in self._connection_waiters:
                    self._connection_waiters.remove(waiter)

        return self.loop.create_task(_wait_for_connection())

    def _record_event(self, name, **details):
        event = {
            "name": name,
            "state": self.state.name.title(),
            "details": details,
        }
        self._event_history.append(event)
        self.connection_context.record_event(
            name,
            source="listener",
            state=event["state"],
            details=details,
        )
        return event

    def _detach_from_connection_context(self):
        if self._context_detached:
            return
        self.connection_context.detach_listener(
            self,
            was_listening=self._context_listening_recorded,
        )
        self._context_detached = True

    def _mark_listening(self):
        self.state = ConnectionState.ESTABLISHED
        if not self._context_listening_recorded:
            self.connection_context.mark_listener_listening()
            self._context_listening_recorded = True
        self._record_event(
            "listening",
            protocol=self.protocol,
            local_endpoint=self.local_endpoint,
        )
        if not self._listen_waiter.done():
            self._listen_waiter.set_result(self)

    def _mark_stopped(self):
        if self.state is ConnectionState.CLOSED and self._stopped_waiter.done():
            return
        self.state = ConnectionState.CLOSED
        self._record_event("stopped", last_error=str(self.last_error) if self.last_error else None)
        self._detach_from_connection_context()
        if not self._stopped_waiter.done():
            self._stopped_waiter.set_result(self)
        for waiter in self._connection_waiters:
            if not waiter.done():
                waiter.set_exception(ConnectionAbortedError("Listener stopped"))
        self._connection_waiters.clear()
        self._accepted_connections.clear()

    def _fail_listen(self, error):
        if self.state is ConnectionState.CLOSED:
            return
        self.last_error = error
        self.state = ConnectionState.CLOSED
        self._record_event("establishment_error", error=str(error))
        self._detach_from_connection_context()
        if not self._listen_waiter.done():
            self._listen_waiter.set_exception(error)
        if not self._stopped_waiter.done():
            self._stopped_waiter.set_result(self)
        for waiter in self._connection_waiters:
            if not waiter.done():
                waiter.set_exception(error)
        self._connection_waiters.clear()
        if not self.preconnection._rendezvous_mode:
            schedule_callback(
                self.loop,
                self.establishment_error,
                (error, self),
                (self,),
                (),
            )
            schedule_callback(
                self.loop,
                self.listen_error,
                (error, self),
                (self,),
                (),
            )

    @staticmethod
    def _addresses_equal(first, second):
        if first is None or second is None:
            return first == second
        try:
            return ipaddress.ip_address(first) == ipaddress.ip_address(second)
        except ValueError:
            return str(first).casefold() == str(second).casefold()

    def _connection_matches_remote_constraints(self, connection):
        if not self._resolved_remote_constraints:
            return True
        remote = connection.remote_endpoint
        if remote is None:
            return False
        for constraint in self._resolved_remote_constraints:
            if (
                constraint.protocol is not None
                and constraint.protocol != connection.protocol
            ):
                continue
            address = constraint.effective_address()
            if (
                address is not None
                and not self._addresses_equal(address, remote.effective_address())
            ):
                continue
            port = constraint.effective_port(connection.protocol)
            if (
                port is not None
                and port != remote.port
                and not self.preconnection._rendezvous_mode
            ):
                continue
            return True
        return False

    def _reject_connection(self, connection, reason):
        connection._report_connection_error(ConnectionAbortedError(reason))
        if connection._ready_waiter.done():
            connection._ready_waiter.exception()

    def _new_connection(self, *, connection_context=None):
        template = self.preconnection.clone()
        if connection_context is not None:
            template.connection_context = connection_context
        elif self.transport_properties.get("isolateSession"):
            template.connection_context = ConnectionContext()
            template.connection_context.register_preconnection()
        return Connection(template)

    def _deliver_connection(self, connection):
        if self.state is not ConnectionState.ESTABLISHED:
            self._reject_connection(connection, "Listener is not accepting connections")
            return False
        if not self._connection_matches_remote_constraints(connection):
            self._reject_connection(
                connection,
                "Remote Endpoint does not satisfy Listener constraints",
            )
            return False
        if self._new_connection_limit == 0:
            self._reject_connection(
                connection,
                "Listener New Connection Limit reached",
            )
            return False

        connection._mark_passive_ready()
        if self._new_connection_limit != float("inf"):
            self._new_connection_limit -= 1
        if not self.preconnection._rendezvous_mode:
            self._record_event(
                "connection_received",
                remote_endpoint=connection.remote_endpoint,
            )
        if self._connection_waiters:
            waiter = self._connection_waiters.pop(0)
            if not waiter.done():
                waiter.set_result(connection)
        else:
            self._accepted_connections.append(connection)
        if not self.preconnection._rendezvous_mode:
            schedule_callback(self.loop, self.connection_received, (connection,))
        return True

    def get_properties(self):
        return {
            "localEndpoints": [endpoint.clone() for endpoint in self.local_endpoints],
            "remoteEndpoints": [endpoint.clone() for endpoint in self.remote_endpoints],
            "selection": self.transport_properties.get_selection_properties(),
            "connection": self.transport_properties.get_connection_properties(),
            "connectionContext": self.connection_context.get_snapshot(),
            "security": (
                self.security_parameters.get_configuration()
                if self.security_parameters else {}
            ),
            "readOnly": {
                "state": self.state.name,
                "connState": self.state.name.title(),
                "protocol": self.protocol,
                "localEndpoint": self.local_endpoint,
                "remoteEndpoint": self.remote_endpoint,
                "securityAvailable": self.security_context is not None,
                "pendingConnections": len(self._accepted_connections),
                "pendingAccepts": len(self._connection_waiters),
                "newConnectionLimit": (
                    "Infinite"
                    if self._new_connection_limit == float("inf")
                    else self._new_connection_limit
                ),
                "systemPolicyPaths": [
                    endpoint.clone()
                    for endpoint in self._system_policy_paths
                ],
                "boundLocalEndpoints": [
                    record["endpoint"].clone()
                    for record in self._binding_records
                ],
                "drainingLocalEndpoints": [
                    endpoint.clone()
                    for endpoint in self._draining_quic_bindings.values()
                ],
                "multicastSubscriptions": [
                    {
                        "group": record["endpoint"].multicast_group,
                        "source": record["endpoint"].multicast_source,
                        "port": record["endpoint"].port,
                        "interface": record.get("multicastInterface"),
                    }
                    for record in self._binding_records
                    if record["kind"] == "multicast"
                ],
                "wildcardBinding": any(
                    endpoint.effective_address() is None
                    and endpoint.host_name is None
                    and endpoint.interface is None
                    and not endpoint.is_multicast
                    for endpoint in self.local_endpoints
                ),
                "pathReconciliationInProgress": (
                    self._policy_reconcile_task is not None
                    and not self._policy_reconcile_task.done()
                ),
                "pathReconciliationRetryScheduled": (
                    self._policy_reconcile_retry_handle is not None
                    and not self._policy_reconcile_retry_handle.cancelled()
                ),
                "pathReconciliationRetryAttempt": (
                    self._policy_reconcile_retry_attempt
                ),
                "pathReconciliationError": (
                    str(self._policy_reconcile_last_error)
                    if self._policy_reconcile_last_error
                    else None
                ),
                "connectionContext": self.connection_context.get_snapshot(),
                "eventCount": len(self._event_history),
                "lastEvent": self._event_history[-1] if self._event_history else None,
                "lastError": str(self.last_error) if self.last_error else None,
            },
        }

    def get_property(self, prop, default=None):
        canonical = prop
        read_only = self.get_properties()["readOnly"]
        if canonical in read_only:
            return read_only.get(canonical, default)
        return self.transport_properties.get_property(prop, default)

    def get_event_history(self):
        return list(self._event_history)

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
        }

    async def wait_stopped(self, timeout=None):
        waiter = asyncio.shield(self._stopped_waiter)
        if timeout is None:
            await waiter
        else:
            await asyncio.wait_for(waiter, timeout)
        return self

    async def stop(self):
        if (
            self.state is ConnectionState.CLOSED
            and self._resources_closed
        ):
            return self

        current_task = asyncio.current_task()
        if (
            self.listen_task is not None
            and self.listen_task is not current_task
            and not self.listen_task.done()
        ):
            self.listen_task.cancel()
            await asyncio.gather(
                self.listen_task,
                return_exceptions=True,
            )
        if (
            self._policy_reconcile_task is not None
            and self._policy_reconcile_task is not current_task
            and not self._policy_reconcile_task.done()
        ):
            self._policy_reconcile_task.cancel()
            await asyncio.gather(
                self._policy_reconcile_task,
                return_exceptions=True,
            )
        self._policy_reconcile_task = None
        self._policy_reconcile_requested = False
        self._cancel_policy_reconcile_retry(reset=True)
        self._multicast_refresh_interfaces.clear()

        await self._close_owned_resources()
        was_closed = self.state is ConnectionState.CLOSED
        if not was_closed:
            self._mark_stopped()
            schedule_callback(self.loop, self.stopped, ())
        return self

    @staticmethod
    def _endpoint_key(endpoint):
        return (
            endpoint.interface,
            endpoint.effective_address(),
            endpoint.port,
            endpoint.protocol,
        )

    @staticmethod
    def _endpoint_summary(endpoint):
        return {
            "interface": endpoint.interface,
            "address": endpoint.effective_address(),
            "port": endpoint.port,
            "protocol": endpoint.protocol,
        }

    def _matching_system_paths(self):
        system_endpoints = self.connection_context.get_system_local_endpoints()
        paths = []
        protocols = self._protocol_candidates or (
            [self.protocol] if self.protocol else [None]
        )
        for template_index, configured in enumerate(self.local_endpoints):
            if configured.is_multicast:
                continue
            configured_address = configured.effective_address()
            for system_endpoint in system_endpoints:
                if (
                    configured.interface is not None
                    and configured.interface
                    != system_endpoint.interface
                ):
                    continue
                if (
                    configured_address is not None
                    and not self._addresses_equal(
                        configured_address,
                        system_endpoint.effective_address(),
                    )
                ):
                    continue
                for protocol in protocols:
                    if (
                        configured.protocol is not None
                        and configured.protocol != protocol
                    ):
                        continue
                    path = system_endpoint.clone()
                    path.protocol = protocol
                    path.port = self._binding_ports.get(
                        (template_index, protocol),
                        configured.effective_port(protocol),
                    )
                    if not any(
                        self._endpoint_key(existing)
                        == self._endpoint_key(path)
                        for existing in paths
                    ):
                        paths.append(path)
        return paths

    def _refresh_system_policy_paths(self, *, record=True):
        current_paths = self._matching_system_paths()
        previous = {
            self._endpoint_key(endpoint): endpoint
            for endpoint in self._system_policy_paths
        }
        current = {
            self._endpoint_key(endpoint): endpoint
            for endpoint in current_paths
        }
        added = [
            current[key]
            for key in current.keys() - previous.keys()
        ]
        removed = [
            previous[key]
            for key in previous.keys() - current.keys()
        ]
        self._system_policy_paths = current_paths
        if record and (added or removed):
            self._record_event(
                "listener_paths_updated",
                added=[
                    self._endpoint_summary(endpoint)
                    for endpoint in added
                ],
                removed=[
                    self._endpoint_summary(endpoint)
                    for endpoint in removed
                ],
            )
        return added, removed

    def _handle_system_policy_update(self, changes):
        if self.state is ConnectionState.CLOSED:
            return
        for interface, change in changes.get("interfaces", {}).items():
            previous = change.get("previous")
            current = change.get("current") or {}
            if previous is None:
                continue
            previous_network = previous.get("networkId")
            current_network = current.get("networkId")
            if (
                previous_network != current_network
                and (
                    previous_network is not None
                    or current_network is not None
                )
                and any(
                    self._multicast_template_is_dynamic(endpoint)
                    and endpoint.interface == interface
                    for endpoint in self.local_endpoints
                )
            ):
                self._multicast_refresh_interfaces.add(interface)
        added, removed = self._refresh_system_policy_paths()
        affected_interfaces = {
            endpoint.interface
            for endpoint in (*added, *removed)
            if endpoint.interface is not None
        }
        affected_interfaces.update(
            changes.get("interfaces", {}).keys()
        )
        if affected_interfaces:
            self._record_event(
                "system_policy_changed",
                interfaces=sorted(affected_interfaces),
            )
        if self.state is not ConnectionState.ESTABLISHED:
            return
        if not changes.get("interfaces"):
            return
        self._cancel_policy_reconcile_retry(reset=True)
        self._policy_reconcile_requested = True
        if (
            self._policy_reconcile_task is None
            or self._policy_reconcile_task.done()
        ):
            self._policy_reconcile_task = self.loop.create_task(
                self._reconcile_system_policy_bindings()
            )

    async def _reconcile_system_policy_bindings(self):
        try:
            while self._policy_reconcile_requested:
                self._policy_reconcile_requested = False
                await self._reconcile_interface_bindings()
                await self._reconcile_multicast_bindings()
            recovered = self._policy_reconcile_retry_attempt > 0
            previous_error = self._policy_reconcile_last_error
            self._cancel_policy_reconcile_retry(reset=True)
            self._policy_reconcile_last_error = None
            if self.last_error is previous_error:
                self.last_error = None
            if recovered:
                self._record_event(
                    "listener_path_reconciliation_recovered"
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.last_error = error
            self._policy_reconcile_last_error = error
            self._policy_reconcile_retry_attempt += 1
            retry_exponent = min(
                self._policy_reconcile_retry_attempt - 1,
                16,
            )
            retry_delay = min(
                POLICY_RECONCILE_RETRY_BASE_DELAY
                * (2 ** retry_exponent),
                POLICY_RECONCILE_RETRY_MAX_DELAY,
            )
            self._record_event(
                "listener_path_reconciliation_failed",
                error=str(error),
                retry_delay=retry_delay,
            )
            self._schedule_policy_reconcile_retry(retry_delay)

    def _cancel_policy_reconcile_retry(self, *, reset=False):
        handle = self._policy_reconcile_retry_handle
        if handle is not None:
            handle.cancel()
        self._policy_reconcile_retry_handle = None
        if reset:
            self._policy_reconcile_retry_attempt = 0

    def _schedule_policy_reconcile_retry(self, delay):
        self._cancel_policy_reconcile_retry()
        if self.state is not ConnectionState.ESTABLISHED:
            return
        self._policy_reconcile_retry_handle = self.loop.call_later(
            delay,
            self._run_policy_reconcile_retry,
        )

    def _run_policy_reconcile_retry(self):
        self._policy_reconcile_retry_handle = None
        if self.state is not ConnectionState.ESTABLISHED:
            return
        if (
            self._policy_reconcile_task is not None
            and not self._policy_reconcile_task.done()
        ):
            self._policy_reconcile_retry_handle = self.loop.call_soon(
                self._run_policy_reconcile_retry
            )
            return
        self._policy_reconcile_requested = True
        self._policy_reconcile_task = self.loop.create_task(
            self._reconcile_system_policy_bindings()
        )

    @staticmethod
    def _usable_policy_addresses(policy):
        if policy.get("available") is False:
            return []
        addresses = []
        for entry in policy.get("addresses", ()):
            address = (
                entry.get("address")
                if isinstance(entry, dict)
                else entry
            )
            if not address:
                continue
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                continue
            if (
                parsed.is_unspecified
                or parsed.is_multicast
                or parsed.is_link_local
            ):
                continue
            normalized = str(parsed)
            if normalized not in addresses:
                addresses.append(normalized)
        return addresses

    @staticmethod
    def _normalize_multicast_interface_selector(selector, family):
        if selector is None:
            return None
        selector = str(selector).strip()
        if family == 6 and selector.isdigit():
            if int(selector) == 0:
                raise ValueError(
                    "IPv6 multicast interface index must not be zero"
                )
            return selector
        address, separator, scope = selector.partition("%")
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as error:
            raise ValueError(
                f"Invalid multicast interface address: {selector}"
            ) from error
        if parsed.version != family:
            raise ValueError(
                "Multicast group and interface address families must match"
            )
        normalized = str(parsed)
        if separator:
            normalized = f"{normalized}%{scope}"
        return normalized

    @staticmethod
    def _multicast_interface_is_name(endpoint):
        interface = endpoint.interface
        if interface is None:
            return False
        group = ipaddress.ip_address(endpoint.multicast_group)
        if group.version == 6 and interface.isdigit():
            return False
        try:
            ipaddress.ip_address(interface.split("%", 1)[0])
        except ValueError:
            return True
        return False

    @staticmethod
    def _multicast_policy_addresses(policy, family):
        addresses = []
        for entry in policy.get("addresses", ()):
            address = (
                entry.get("address")
                if isinstance(entry, dict)
                else entry
            )
            if not address:
                continue
            address = str(address)
            try:
                parsed = ipaddress.ip_address(
                    address.split("%", 1)[0]
                )
            except ValueError:
                continue
            if (
                parsed.version != family
                or parsed.is_unspecified
                or parsed.is_multicast
            ):
                continue
            normalized = str(parsed)
            if "%" in address:
                normalized = (
                    f"{normalized}%{address.rsplit('%', 1)[1]}"
                )
            if normalized not in addresses:
                addresses.append(normalized)
        return addresses

    def _native_multicast_interface_selectors(
        self,
        interface,
        family,
    ):
        if family == 6:
            try:
                index = socket.if_nametoindex(interface)
            except OSError:
                index = 0
            if index:
                return [str(index)]
        _require_netifaces()
        addresses = netifaces.ifaddresses(interface)
        netifaces_family = (
            netifaces.AF_INET6 if family == 6 else netifaces.AF_INET
        )
        return self._multicast_policy_addresses(
            {"addresses": addresses.get(netifaces_family, ())},
            family,
        )

    def _multicast_interface_selectors(self, endpoint):
        group = ipaddress.ip_address(endpoint.multicast_group)
        explicit = getattr(
            self.preconnection,
            "multicast_interface_address",
            None,
        )
        if explicit is None:
            explicit = endpoint.address
        if explicit is not None:
            return [
                self._normalize_multicast_interface_selector(
                    explicit,
                    group.version,
                )
            ]
        if endpoint.interface is None:
            return [None]
        if not self._multicast_interface_is_name(endpoint):
            return [
                self._normalize_multicast_interface_selector(
                    endpoint.interface,
                    group.version,
                )
            ]

        policy = self.connection_context.interface_policy.get(
            endpoint.interface
        )
        if policy is not None and policy.get("available") is False:
            return []
        if group.version == 6:
            index = (policy or {}).get("index")
            if isinstance(index, int) and not isinstance(index, bool) and index > 0:
                return [str(index)]
            try:
                index = socket.if_nametoindex(endpoint.interface)
            except OSError:
                index = 0
            if index:
                return [str(index)]
        if policy is not None and "addresses" in policy:
            addresses = self._multicast_policy_addresses(
                policy,
                group.version,
            )
        else:
            addresses = self._native_multicast_interface_selectors(
                endpoint.interface,
                group.version,
            )
        return [
            self._normalize_multicast_interface_selector(
                address,
                group.version,
            )
            for address in addresses[:1]
        ]

    def _multicast_template_is_dynamic(self, endpoint):
        return (
            endpoint.is_multicast
            and endpoint.address is None
            and endpoint.interface is not None
            and getattr(
                self.preconnection,
                "multicast_interface_address",
                None,
            )
            is None
            and self._multicast_interface_is_name(endpoint)
        )

    async def _reconcile_interface_bindings(self):
        for template_index, template in enumerate(self.local_endpoints):
            if (
                template.interface is None
                or template.effective_address() is not None
                or template.host_name is not None
                or template.is_multicast
            ):
                continue
            policy = self.connection_context.interface_policy.get(
                template.interface
            )
            if policy is None:
                continue
            desired_addresses = set(
                self._usable_policy_addresses(policy)
            )
            for protocol in self._protocol_candidates:
                if (
                    template.protocol is not None
                    and template.protocol != protocol
                ):
                    continue
                records = [
                    record
                    for record in self._binding_records
                    if (
                        record["templateIndex"] == template_index
                        and record["protocol"] == protocol
                    )
                ]
                existing_addresses = {
                    record["endpoint"].effective_address()
                    for record in records
                }
                for address in sorted(
                    desired_addresses - existing_addresses
                ):
                    endpoint = template.clone()
                    endpoint.address = address
                    started = await self._start_candidate(
                        protocol,
                        endpoint,
                        template_index=template_index,
                    )
                    if not started:
                        raise RuntimeError(
                            "Could not establish replacement "
                            f"{protocol} Listener binding on {address}"
                        )
                    self._record_event(
                        "listener_binding_added",
                        **self._endpoint_summary(endpoint),
                    )
                for record in records:
                    if (
                        record["endpoint"].effective_address()
                        not in desired_addresses
                    ):
                        summary = self._endpoint_summary(
                            record["endpoint"]
                        )
                        await self._close_binding(record)
                        self._record_event(
                            "listener_binding_removed",
                            **summary,
                        )
        self._refresh_system_policy_paths(record=False)

    async def _reconcile_multicast_bindings(self):
        if "udp" not in self._protocol_candidates:
            return
        processed_refreshes = set()
        for template_index, template in enumerate(self.local_endpoints):
            if not self._multicast_template_is_dynamic(template):
                continue
            force_refresh = (
                template.interface
                in self._multicast_refresh_interfaces
            )
            desired_interfaces = set(
                self._multicast_interface_selectors(template)
            )
            records = [
                record
                for record in self._binding_records
                if (
                    record["templateIndex"] == template_index
                    and record["kind"] == "multicast"
                )
            ]
            existing_interfaces = {
                record.get("multicastInterface")
                for record in records
            }
            interfaces_to_add = (
                desired_interfaces
                if force_refresh
                else desired_interfaces - existing_interfaces
            )
            for interface in sorted(
                interfaces_to_add,
                key=lambda value: value or "",
            ):
                endpoint = template.clone()
                endpoint.address = interface
                started = await self._start_candidate(
                    "udp",
                    endpoint,
                    template_index=template_index,
                )
                if not started:
                    raise RuntimeError(
                        "Could not establish replacement multicast "
                        f"subscription on {interface!r}"
                    )
                self._record_event(
                    "listener_binding_added",
                    **self._endpoint_summary(endpoint),
                    subscription_interface=interface,
                )
            for record in records:
                interface = record.get("multicastInterface")
                if (
                    force_refresh
                    or interface not in desired_interfaces
                ):
                    summary = self._endpoint_summary(
                        record["endpoint"]
                    )
                    await self._close_binding(record)
                    self._record_event(
                        "listener_binding_removed",
                        **summary,
                        subscription_interface=interface,
                    )
            if force_refresh:
                processed_refreshes.add(template.interface)
        self._multicast_refresh_interfaces.difference_update(
            processed_refreshes
        )

    async def _close_binding(self, record):
        resource = record["resource"]
        kind = record["kind"]
        if kind == "server":
            resource.close()
            await self._wait_for_server_close(resource)
            if resource in self._servers:
                self._servers.remove(resource)
        elif kind == "datagram":
            resource.close()
            if resource in self._datagram_transports:
                self._datagram_transports.remove(resource)
            await asyncio.sleep(0)
        elif kind == "quic":
            await resource.stop_listener()
            if getattr(resource, "server", None) is not None:
                self._draining_quic_bindings[resource] = (
                    record["endpoint"].clone()
                )
            else:
                self._draining_quic_bindings.pop(resource, None)
        elif kind == "multicast":
            leave_subscription(resource)
        if record in self._binding_records:
            self._binding_records.remove(record)
        if kind == "multicast" and self._join_ctx is resource:
            self._join_ctx = next(
                (
                    candidate["resource"]
                    for candidate in self._binding_records
                    if candidate["kind"] == "multicast"
                ),
                None,
            )
        if kind == "quic" and self.quic_association is resource:
            self.quic_association = next(
                (
                    candidate["resource"]
                    for candidate in self._binding_records
                    if candidate["kind"] == "quic"
                ),
                None,
            )

    def _quic_listener_drain_completed(self, association):
        self._draining_quic_bindings.pop(association, None)

    @staticmethod
    async def _wait_for_server_close(server):
        try:
            await asyncio.wait_for(server.wait_closed(), timeout=0.1)
        except TimeoutError:
            # Accepted Connections outlive their Listener by design.
            logger.debug(
                "Listener socket closed while accepted Connections remain active"
            )

    async def _close_owned_resources(self):
        if self._resources_closed:
            return

        for record in list(self._binding_records):
            await self._close_binding(record)
        if getattr(self, "_join_ctx", None) is not None:
            do_leave(self)
        if self.quic_association is not None:
            await self.quic_association.stop_listener()
            self.quic_association = None
        for transport in list(self._datagram_transports):
            transport.close()
        self._datagram_transports.clear()
        for server in list(self._servers):
            server.close()
        if self._servers:
            await asyncio.gather(
                *(
                    self._wait_for_server_close(server)
                    for server in self._servers
                ),
                return_exceptions=True,
            )
        self._servers.clear()
        self._binding_records.clear()
        await asyncio.sleep(0)
        self._resources_closed = True

    async def _resolve_remote_constraints(self):
        resolved = []
        for endpoint in self.remote_endpoints:
            if endpoint.effective_address() is not None:
                resolved.append(endpoint.clone())
                continue
            if endpoint.host_name is None:
                resolved.append(endpoint.clone())
                continue
            endpoint_info = await self.loop.getaddrinfo(
                endpoint.host_name,
                endpoint.effective_port(endpoint.protocol) or 0,
            )
            for address in dict.fromkeys(info[4][0] for info in endpoint_info):
                constraint = endpoint.clone()
                constraint.address = address
                resolved.append(constraint)
        self._resolved_remote_constraints = resolved

    def _store_binding(
        self,
        protocol,
        local_endpoint,
        resource,
        kind,
        template_index,
        **metadata,
    ):
        record = {
            "protocol": protocol,
            "endpoint": local_endpoint.clone(),
            "resource": resource,
            "kind": kind,
            "templateIndex": template_index,
        }
        record.update(metadata)
        self._binding_records.append(record)

    async def _start_candidate(
        self,
        protocol,
        local_endpoint,
        *,
        template_index,
    ):
        local_endpoint.protocol = protocol
        port_key = (template_index, protocol)
        selected_port = self._binding_ports.get(port_key)
        if selected_port is None:
            selected_port = local_endpoint.effective_port(protocol)
        local_endpoint.port = 0 if selected_port is None else selected_port
        ephemeral_port = local_endpoint.port == 0
        resource = None
        kind = None
        binding_metadata = {}

        if protocol == "udp":
            self.protocol = "udp"
            logger.info(
                "UDP local endpoint: address %s port: %s",
                local_endpoint.address,
                local_endpoint.port,
            )
            check_addr = (
                ipaddress.ip_address(local_endpoint.address)
                if (
                    local_endpoint.address is not None
                    and not local_endpoint.is_multicast
                )
                else None
            )
            if local_endpoint.is_multicast or (
                check_addr is not None and check_addr.is_multicast
            ):
                if (
                    self.transport_properties.properties.get("direction")
                    != "Unidirectional Receive"
                ):
                    raise RuntimeError(
                        "Multicast listeners require direction "
                        "'Unidirectional Receive'."
                    )
                resource = join_subscription(
                    self,
                    local_endpoint=local_endpoint,
                    interface=local_endpoint.address,
                )
                kind = "multicast"
                binding_metadata["multicastInterface"] = resource[
                    "interface"
                ]
                if self._join_ctx is None:
                    self._join_ctx = resource
            else:
                resource, _ = await self.loop.create_datagram_endpoint(
                    lambda endpoint=local_endpoint: DatagramHandler(
                        self,
                        endpoint,
                    ),
                    local_addr=(
                        local_endpoint.socket_address(),
                        local_endpoint.port,
                    ),
                )
                self._datagram_transports.append(resource)
                if ephemeral_port:
                    sockname = resource.get_extra_info("sockname")
                    if sockname:
                        local_endpoint.port = sockname[1]
                kind = "datagram"
        elif protocol in {"tcp", "tls-tcp"}:
            if protocol == "tls-tcp" and self.security_context is None:
                logger.info(
                    "Skipping tls-tcp listener candidate on %s:%s because "
                    "no security context is configured.",
                    local_endpoint.address,
                    local_endpoint.port,
                )
                return False
            self.protocol = protocol
            logger.info(
                "TCP local endpoint: address %s port: %s",
                local_endpoint.address,
                local_endpoint.port,
            )
            resource = await self.loop.create_server(
                lambda protocol_name=protocol, endpoint=local_endpoint: (
                    StreamHandler(
                        self,
                        protocol_name,
                        endpoint,
                    )
                ),
                local_endpoint.socket_address(),
                local_endpoint.port,
                ssl=(
                    self.security_context
                    if protocol == "tls-tcp"
                    else None
                ),
            )
            self._servers.append(resource)
            if ephemeral_port and resource.sockets:
                local_endpoint.port = resource.sockets[0].getsockname()[1]
            kind = "server"
        elif protocol == "quic":
            if transport_impl.aioquic_serve is None:
                logger.info(
                    "Skipping quic listener candidate on %s:%s because "
                    "aioquic is not installed.",
                    local_endpoint.address,
                    local_endpoint.port,
                )
                return False
            self.protocol = "quic"
            association = QuicAssociationManager(
                loop=self.loop,
                listener=self,
            )
            await association.start_listener(
                self,
                local_endpoint=local_endpoint,
            )
            if ephemeral_port:
                bound_port = association.bound_port()
                if bound_port is not None:
                    local_endpoint.port = bound_port
            resource = association
            kind = "quic"
            if self.quic_association is None:
                self.quic_association = association
        else:
            return False

        if not self._binding_records:
            self.local_endpoint = local_endpoint.clone()
        self._binding_ports.setdefault(port_key, local_endpoint.port)
        self._store_binding(
            protocol,
            local_endpoint,
            resource,
            kind,
            template_index,
            **binding_metadata,
        )
        self._resources_closed = False
        logger.info(
            "Started %s Listener on %s:%s",
            protocol,
            local_endpoint.address or "default",
            local_endpoint.port,
        )
        return True

    async def start_listener(self):
        """ method wrapped by listen
        """
        logger.info("Starting listener with endpoints: %s.", self.local_endpoints)

        # Create set of candidate protocols
        available_protocols = {"tcp", "udp"}
        if self.security_context is not None:
            available_protocols.add("tls-tcp")
        if transport_impl.aioquic_serve is not None:
            available_protocols.add("quic")
        protocol_candidates = build_protocol_candidates(
            self.transport_properties,
            connection_context=self.connection_context,
            available_protocols=available_protocols,
        )
        self._protocol_candidates = list(protocol_candidates)

        try:
            await self._resolve_remote_constraints()
        except Exception as err:
            self._fail_listen(err)
            return
        # If the candidate set is empty issue an InitiateError cb
        if not protocol_candidates:
            logger.warning("Protocol selection Error occurred.")
            self._fail_listen(RuntimeError("Protocol selection error"))
            return

        listen_endpoints = []
        for template_index, endpoint in enumerate(self.local_endpoints):
            if endpoint.is_multicast:
                try:
                    multicast_interfaces = (
                        self._multicast_interface_selectors(endpoint)
                    )
                except Exception as error:
                    logger.warning(
                        "Could not resolve multicast interface: %s",
                        error,
                    )
                    self.last_error = error
                    continue
                if not multicast_interfaces:
                    self.last_error = RuntimeError(
                        "No usable multicast interface is available for "
                        f"{endpoint.interface!r}"
                    )
                    continue
                for interface in multicast_interfaces:
                    candidate_endpoint = endpoint.clone()
                    candidate_endpoint.address = interface
                    listen_endpoints.append(
                        (template_index, candidate_endpoint)
                    )
                continue
            endpoint_addresses = []
            effective_address = endpoint.effective_address()
            if endpoint.host_name and effective_address is None:
                endpoint_info = await self.loop.getaddrinfo(
                    endpoint.host_name,
                    endpoint.port or endpoint.service,
                )
                endpoint_addresses.extend(
                    dict.fromkeys(info[4][0] for info in endpoint_info)
                )
                logger.info(
                    "Resolved %s to %s",
                    endpoint.host_name,
                    endpoint_addresses,
                )
            if effective_address:
                endpoint_addresses.append(effective_address)
            if endpoint.interface:
                _require_netifaces()
                local_interface = endpoint.interface
                try:
                    interface_addresses = netifaces.ifaddresses(local_interface)
                    endpoint_addresses.extend(
                        entry["addr"]
                        for entry in interface_addresses.get(netifaces.AF_INET6, [])
                        if entry["addr"][:4] != "fe80"
                    )
                    endpoint_addresses.extend(
                        entry["addr"]
                        for entry in interface_addresses.get(netifaces.AF_INET, [])
                    )
                except ValueError as err:
                    logger.info(
                        "Cannot get IP addresses for %s: %s",
                        local_interface,
                        err,
                    )
            if not endpoint_addresses:
                endpoint_addresses.append(None)
            for address in dict.fromkeys(endpoint_addresses):
                candidate_endpoint = endpoint.clone()
                candidate_endpoint.address = address
                listen_endpoints.append(
                    (template_index, candidate_endpoint)
                )

        candidate_set = [
            (protocol, endpoint, template_index)
            for template_index, endpoint in listen_endpoints
            for protocol in protocol_candidates
            if endpoint.protocol is None or endpoint.protocol == protocol
        ]

        # Attempt to set up the appropriate listener for the candidate protocol
        started = False
        for candidate in candidate_set:
            try:
                protocol, local_endpoint, template_index = candidate
                candidate_started = await self._start_candidate(
                    protocol,
                    local_endpoint,
                    template_index=template_index,
                )
                started = started or candidate_started
            except Exception as err:
                logger.warning("Listen Error occurred: %s", err)
                self.last_error = err
        if started:
            self._refresh_system_policy_paths(record=False)
            self._mark_listening()
        elif not self._listen_waiter.done():
            self._fail_listen(
                self.last_error
                or RuntimeError("Listener failed to start any candidates.")
            )
        return

    """ ASYNCIO function that gets called when joining a multicast flow
    """

    async def multicast_join(self):
        logger.info("Joining multicast session.")
        DatagramHandler(self)
        do_join(self)

    """ ASYNCIO function that receives data from multicast flows
    """

    # TODO: Fix this...
    async def do_multicast_receive(self):
        raise NotImplementedError("Multicast receive callback path is not implemented.")

    """ ASYNCIO function that gets called when leaving a multicast flow
    """

    async def multicast_leave(self):
        logger.info("Leaving multicast session.")
        self.multicast_false = True
        do_leave(self)
        self._mark_stopped()


class DatagramHandler(asyncio.Protocol):
    """ Class required to handle incoming datagram flows
    """

    def __init__(self, preconnection, local_endpoint=None):
        self.preconnection = preconnection
        self.local_endpoint = (
            local_endpoint.clone()
            if local_endpoint is not None
            else preconnection.local_endpoint.clone()
        )
        self.remotes = dict()
        self.preconnection.handler = self
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        logger.info("New UDP flow.")
        return

    def datagram_received(self, data, addr):
        logger.info("Received new datagram")
        if addr in self.remotes:
            self.remotes[addr].transports[0].datagram_received(data, addr)
            return
        new_connection = self.preconnection._new_connection()
        new_connection._originating_preconnection = self.preconnection
        new_connection.local_endpoint = self.local_endpoint.clone()
        new_connection.local_endpoints = [new_connection.local_endpoint]
        new_connection.protocol = "udp"
        new_remote_endpoint = RemoteEndpoint()
        logger.info("Received new connection from " +
                    str(addr[0]) + ":" + str(addr[1]) + ".")
        new_remote_endpoint.with_address(addr[0])
        new_remote_endpoint.with_port(addr[1])
        new_connection.remote_endpoint = new_remote_endpoint
        new_connection.remote_endpoints = [new_remote_endpoint]
        logger.info("Created new connection object.")
        new_udp = UdpTransport(new_connection,
                               new_connection.local_endpoint,
                               new_remote_endpoint)
        new_udp.transport = self.transport
        if new_udp.framer_stack is not None:
            self.remotes[addr] = new_connection
            new_udp.datagram_received(data, addr)
            task = new_connection.loop.create_task(
                new_udp.passive_open(self.transport)
            )
            task.add_done_callback(
                lambda completed, remote=addr: self._passive_open_done(
                    completed,
                    remote,
                )
            )
            return
        if not self.preconnection._deliver_connection(new_connection):
            return
        logger.info("Delivered new connection to listener.")
        new_udp.datagram_received(data, addr)
        self.remotes[addr] = new_connection
        return

    def _passive_open_done(self, task, remote):
        if task.cancelled():
            self.remotes.pop(remote, None)
            return
        error = task.exception()
        if error is None:
            logger.info("Delivered new framed connection to listener.")
            return
        connection = self.remotes.pop(remote, None)
        if connection is not None:
            connection._report_connection_error(error)


class StreamHandler(asyncio.Protocol):

    def __init__(self, listener, protocol_name="tcp", local_endpoint=None):
        new_connection = listener._new_connection()
        new_connection._originating_preconnection = listener
        if local_endpoint is not None:
            new_connection.local_endpoint = local_endpoint.clone()
            new_connection.local_endpoints = [new_connection.local_endpoint]
        self.connection = new_connection
        self.protocol_name = protocol_name

    def connection_made(self, transport):
        new_remote_endpoint = RemoteEndpoint()
        logger.info("Received new connection.")
        # Get information about the newly connected endpoint
        new_remote_endpoint.with_address(
            transport.get_extra_info("peername")[0])
        new_remote_endpoint.with_port(
            transport.get_extra_info("peername")[1])
        self.connection.remote_endpoint = new_remote_endpoint
        self.connection.remote_endpoints = [new_remote_endpoint]
        new_tcp = TcpTransport(self.connection,
                               self.connection.local_endpoint,
                               new_remote_endpoint,
                               protocol_name=self.protocol_name)
        new_tcp.transport = transport
        self.connection.protocol = self.protocol_name
        if new_tcp.framer_stack is not None:
            task = self.connection.loop.create_task(
                new_tcp.passive_open(transport)
            )
            task.add_done_callback(self._passive_open_done)
            return
        if not self.connection._originating_preconnection._deliver_connection(
            self.connection
        ):
            close = getattr(transport, "close", None)
            if callable(close):
                close()
        return

    def _passive_open_done(self, task):
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        self.connection._report_connection_error(error)
        transport = self.connection.transports[0]
        self.connection.loop.create_task(transport._close_raw())

    def eof_received(self):
        self.connection.transports[0].eof_received()

    def data_received(self, data):
        self.connection.transports[0].data_received(data)

    def error_received(self, err):
        self.connection.transports[0].error_received(err)

    def connection_lost(self, exc):
        self.connection.transports[0].connection_lost(exc)
