import asyncio
import ipaddress

try:
    import netifaces
except ImportError:
    netifaces = None

from . import transports as transport_impl
from .connection import Connection
from .connection_context import ConnectionContext
from .endpoint import RemoteEndpoint
from .multicast import do_join, do_leave
from .transports import QuicAssociationManager, TcpTransport, UdpTransport
from .utility import (
    ConnectionState,
    build_protocol_candidates,
    schedule_callback,
    setup_logger,
)

logger = setup_logger(__name__, "cyan")


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
        self.preconnection = preconnection._copy_configuration(
            action=action or "listen",
            security_role="listener",
        )
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
        self.framer = self.preconnection.framer
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
        self._stopped_waiter = self.loop.create_future()
        self.last_error = None
        self._event_history = []
        self._context_listening_recorded = False
        self._context_detached = False
        self.connection_context.attach_listener()

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
            async def _return_queued_connection():
                return self._accepted_connections.pop(0)

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
        if self.state is ConnectionState.CLOSED:
            return self
        if self.quic_association is not None:
            await self.quic_association.stop_listener()
        for server in list(self._servers):
            server.close()
        await asyncio.sleep(0)
        self._mark_stopped()
        schedule_callback(self.loop, self.stopped, ())
        return self

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

    async def start_listener(self):
        """ method wrapped by listen
        """
        logger.info("Starting listener with endpoints: %s.", self.local_endpoints)

        # Create set of candidate protocols
        protocol_candidates = build_protocol_candidates(self.transport_properties)

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
        for endpoint in self.local_endpoints:
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
                listen_endpoints.append(candidate_endpoint)

        candidate_set = [
            (protocol, endpoint)
            for endpoint in listen_endpoints
            for protocol in protocol_candidates
            if endpoint.protocol is None or endpoint.protocol == protocol
        ]

        # Attempt to set up the appropriate listener for the candidate protocol
        started = False
        for candidate in candidate_set:
            try:
                protocol, local_endpoint = candidate
                local_endpoint.port = local_endpoint.effective_port(protocol)
                if local_endpoint.port is None:
                    local_endpoint.port = 0
                if protocol == 'udp':
                    self.protocol = 'udp'
                    logger.info(
                        "UDP local endpoint: address %s port: %s",
                        local_endpoint.address,
                        local_endpoint.port,
                    )
                    check_addr = (
                        ipaddress.ip_address(local_endpoint.address)
                        if local_endpoint.address is not None
                        else None
                    )
                    if local_endpoint.is_multicast or (
                        check_addr is not None and check_addr.is_multicast
                    ):
                        logger.info("addr is multicast")
                        if self.transport_properties.properties. \
                                get('direction') == 'Unidirectional Receive':
                            self.local_endpoint = local_endpoint
                            await self.multicast_join()
                            started = True
                        else:
                            raise RuntimeError(
                                "Multicast listeners require direction "
                                "'Unidirectional Receive'."
                            )
                    else:
                        transport, _ = await self.loop.create_datagram_endpoint(
                            lambda endpoint=local_endpoint: DatagramHandler(
                                self,
                                endpoint,
                            ),
                            local_addr=(
                                local_endpoint.socket_address(),
                                local_endpoint.port,
                            ),
                        )
                        self._datagram_transports.append(transport)
                        started = True
                elif protocol in {'tcp', 'tls-tcp'}:
                    if protocol == "tls-tcp" and self.security_context is None:
                        logger.info(
                            "Skipping tls-tcp listener candidate on %s:%s because no security context is configured.",
                            local_endpoint.address,
                            local_endpoint.port,
                        )
                        continue
                    self.protocol = protocol
                    logger.info(
                        "TCP local endpoint: address %s port: %s",
                        local_endpoint.address,
                        local_endpoint.port,
                    )
                    server = await self.loop.create_server(
                        lambda protocol_name=protocol, endpoint=local_endpoint: StreamHandler(
                            self,
                            protocol_name,
                            endpoint,
                        ),
                        local_endpoint.socket_address(),
                        local_endpoint.port,
                        ssl=self.security_context if protocol == "tls-tcp" else None,
                    )
                    self._servers.append(server)
                    started = True
                elif protocol == "quic":
                    if transport_impl.aioquic_serve is None:
                        logger.info(
                            "Skipping quic listener candidate on %s:%s because aioquic is not installed.",
                            local_endpoint.address,
                            local_endpoint.port,
                        )
                        continue
                    self.protocol = "quic"
                    self.local_endpoint = local_endpoint
                    self.quic_association = QuicAssociationManager(
                        loop=self.loop,
                        listener=self,
                    )
                    await self.quic_association.start_listener(self)
                    started = True
            except Exception as err:
                logger.warning("Listen Error occurred: " + str(err))
                self.last_error = err

            logger.info(
                "Started %s Listener on %s:%s",
                protocol,
                local_endpoint.address or "default",
                local_endpoint.port,
            )
        if started:
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
        if not self.preconnection._deliver_connection(new_connection):
            return
        logger.info("Delivered new connection to listener.")
        new_udp.datagram_received(data, addr)
        self.remotes[addr] = new_connection
        return


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
        if not self.connection._originating_preconnection._deliver_connection(
            self.connection
        ):
            close = getattr(transport, "close", None)
            if callable(close):
                close()
        return

    def eof_received(self):
        self.connection.transports[0].eof_received()

    def data_received(self, data):
        self.connection.transports[0].data_received(data)

    def error_received(self, err):
        self.connection.transports[0].error_received(err)

    def connection_lost(self, exc):
        self.connection.transports[0].connection_lost(exc)
