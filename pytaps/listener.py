import asyncio
import ipaddress

try:
    import netifaces
except ImportError:
    netifaces = None

from .connection import Connection
from .endpoint import RemoteEndpoint
from .multicast import do_join, do_leave
from .transports import TcpTransport, UdpTransport
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

    def __init__(self, preconnection):
        # Initializations
        self.preconnection = preconnection
        self.local_endpoint = preconnection.local_endpoint
        self.remote_endpoint = preconnection.remote_endpoint
        self.transport_properties = preconnection.transport_properties
        self.security_parameters = preconnection.security_parameters
        self.security_context = preconnection.security_context
        self.connection_context = preconnection.connection_context
        self.loop = preconnection.loop
        self.framer = preconnection.framer
        self.active_ports = {}
        self.protocol = None
        self.listen_task = None
        self.state = ConnectionState.ESTABLISHING
        self._listen_waiter = self.loop.create_future()
        self._connection_waiters = []
        self._accepted_connections = []
        self._servers = []
        self._datagram_transports = []
        self._stopped_waiter = self.loop.create_future()
        self.last_error = None
        self._event_history = []

        # Callbacks
        self.stopped = preconnection.stopped
        self.listen_error = preconnection.listen_error
        self.connection_received = preconnection.connection_received
        self.initiate_error = preconnection.initiate_error
        self.ready = preconnection.ready

    async def wait_listening(self, timeout=None):
        if timeout is None:
            await self._listen_waiter
        else:
            await asyncio.wait_for(self._listen_waiter, timeout)
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
        self.connection_context.record_event(name)
        return event

    def _mark_listening(self):
        self.state = ConnectionState.ESTABLISHED
        self._record_event(
            "listening",
            protocol=self.protocol,
            local_endpoint=self.local_endpoint,
        )
        if not self._listen_waiter.done():
            self._listen_waiter.set_result(self)

    def _mark_stopped(self):
        self.state = ConnectionState.CLOSED
        self._record_event("stopped", last_error=str(self.last_error) if self.last_error else None)
        if not self._stopped_waiter.done():
            self._stopped_waiter.set_result(self)
        for waiter in self._connection_waiters:
            if not waiter.done():
                waiter.set_exception(ConnectionAbortedError("Listener stopped"))
        self._connection_waiters.clear()
        self._accepted_connections.clear()

    def _fail_listen(self, error):
        self.last_error = error
        self.state = ConnectionState.CLOSED
        self._record_event("listen_error", error=str(error))
        if not self._listen_waiter.done():
            self._listen_waiter.set_exception(error)
        schedule_callback(
            self.loop,
            self.listen_error,
            (error, self),
            (self,),
            (),
        )

    def _deliver_connection(self, connection):
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
        schedule_callback(self.loop, self.connection_received, (connection,))

    def get_properties(self):
        return {
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

    def get_monitoring_snapshot(self):
        return {
            "connectionContext": self.connection_context.get_snapshot(),
            "events": self.get_event_history(),
            "properties": self.get_properties(),
        }

    async def wait_stopped(self, timeout=None):
        if timeout is None:
            await self._stopped_waiter
        else:
            await asyncio.wait_for(self._stopped_waiter, timeout)
        return self

    async def stop(self):
        for server in list(self._servers):
            server.close()
            await server.wait_closed()
        for transport in list(self._datagram_transports):
            transport.close()
        self._mark_stopped()
        schedule_callback(self.loop, self.stopped, ())

    async def start_listener(self):
        """ method wrapped by listen
        """
        logger.info("Starting listener with hostname: " +
                    str(self.local_endpoint.host_name) +
                    ", interface: " + str(self.local_endpoint.interface) +
                    ", addresses: " + str(self.local_endpoint.address) +
                    ".")

        # Create set of candidate protocols
        protocol_candidates = build_protocol_candidates(self.transport_properties)

        if self.remote_endpoint:
            if not self.remote_endpoint.address:
                remote_info = await self.loop.getaddrinfo(
                    self.remote_endpoint.host_name, self.remote_endpoint.port)
                self.remote_endpoint.address = [remote_info[0][4][0]]
        # If the candidate set is empty issue an InitiateError cb
        if not protocol_candidates:
            logger.warning("Protocol selection Error occurred.")
            self._fail_listen(RuntimeError("Protocol selection error"))
            return

        all_addrs = []
        if self.local_endpoint.host_name:
            endpoint_info = await self.loop.getaddrinfo(
                self.local_endpoint.host_name, self.local_endpoint.port)
            all_addrs += list(set([info[4][0] for info in endpoint_info]))
            logger.info("Resolved " + str(self.local_endpoint.host_name) +
                        " to " + str(all_addrs))
        if len(self.local_endpoint.address) > 0:
            all_addrs += self.local_endpoint.address
            logger.info("Adding addresses to listen: " +
                        str(self.local_endpoint.address) + " --> " +
                        str(all_addrs))
        if self.local_endpoint.interface:
            _require_netifaces()
            for local_interface in self.local_endpoint.interface:
                try:
                    # Unfortunately, listening on link-local
                    # IPv6 addresses does not work
                    # because it's broken in asyncio:
                    # https://bugs.python.org/issue35545
                    all_addrs += [entry['addr']
                                  for entry in netifaces.ifaddresses
                                  (local_interface)[netifaces.AF_INET6]
                                  if entry['addr'][:4] != "fe80"]
                    all_addrs += [entry['addr']
                                  for entry in netifaces.ifaddresses
                                  (local_interface)[netifaces.AF_INET]]
                    logger.info("Adding addresses of local interface " +
                                str(self.local_endpoint.interface) + " --> " +
                                str(all_addrs))
                except ValueError as err:
                    logger.info("Cannot get IP addresses for " +
                                str(self.local_endpoint.interface) + ": " +
                                str(err))

        # Get all combinations of protocols and remote IP addresses
        # to listen on all of them
        candidate_set = [(protocol, address)
                         for address in all_addrs
                         for protocol in protocol_candidates]

        # Attempt to set up the appropriate listener for the candidate protocol
        started = False
        for candidate in candidate_set:
            try:
                if candidate[0] == 'udp':
                    self.protocol = 'udp'
                    self.local_endpoint.address = [candidate[1]]
                    # multicast_receiver = False
                    # See if the address of the local endpoint
                    # is a multicast address
                    logger.info("UDP local endpoint: address " +
                                str(self.local_endpoint.address) +
                                " port: " +
                                str(self.local_endpoint.port))
                    check_addr = ipaddress.ip_address(
                        self.local_endpoint.address[0])
                    if check_addr.is_multicast:
                        logger.info("addr is multicast")
                        # If the address is multicast, make sure that the
                        # application set the direction of communication
                        # to receive only
                        if self.transport_properties.properties. \
                                get('direction') == 'Unidirectional Receive':
                            logger.info("direction is unicast receive")
                            await self.multicast_join()
                            started = True
                        else:
                            raise RuntimeError(
                                "Multicast listeners require direction "
                                "'Unidirectional Receive'."
                            )
                    else:
                        transport, _ = await self.loop.create_datagram_endpoint(
                            lambda: DatagramHandler(self),
                            local_addr=(
                                self.local_endpoint.address[0],
                                self.local_endpoint.port))
                        self._datagram_transports.append(transport)
                        started = True
                elif candidate[0] in {'tcp', 'tls-tcp'}:
                    if candidate[0] == "tls-tcp" and self.security_context is None:
                        logger.info(
                            "Skipping tls-tcp listener candidate on %s:%s because no security context is configured.",
                            self.local_endpoint.address,
                            self.local_endpoint.port,
                        )
                        continue
                    self.protocol = candidate[0]
                    self.local_endpoint.address = [candidate[1]]
                    logger.info("TCP local endpoint: address " +
                                str(self.local_endpoint.address) +
                                " port: " + str(self.local_endpoint.port))
                    server = await self.loop.create_server(
                        lambda: StreamHandler(self),
                        self.local_endpoint.address[0],
                        self.local_endpoint.port,
                        ssl=self.security_context if candidate[0] == "tls-tcp" else None)
                    self._servers.append(server)
                    started = True
            except Exception as err:
                logger.warning("Listen Error occurred: " + str(err))
                self.last_error = err
                schedule_callback(
                    self.loop,
                    self.listen_error,
                    (err, self),
                    (self,),
                    (),
                )

            logger.info("Started " + self.protocol + " Listener on " +
                        (str(self.local_endpoint.address) if
                         self.local_endpoint.address else "default") + ":" +
                        str(self.local_endpoint.port))
        if started:
            self._mark_listening()
        elif not self._listen_waiter.done():
            self._fail_listen(RuntimeError("Listener failed to start any candidates."))
        return

    """ ASYNCIO function that gets called when joining a multicast flow
    """

    async def multicast_join(self):
        logger.info("Joining multicast session.")
        DatagramHandler(self)
        do_join(self)
        self._mark_listening()

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

    def __init__(self, preconnection):
        self.preconnection = preconnection
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
        new_connection = Connection(self.preconnection)
        new_connection.state = ConnectionState.ESTABLISHED
        new_remote_endpoint = RemoteEndpoint()
        logger.info("Received new connection from " +
                    str(addr[0]) + ":" + str(addr[1]) + ".")
        new_remote_endpoint.with_address(addr[0])
        new_remote_endpoint.with_port(addr[1])
        new_connection.remote_endpoint = new_remote_endpoint
        logger.info("Created new connection object.")
        new_udp = UdpTransport(new_connection,
                               new_connection.local_endpoint,
                               new_remote_endpoint)
        new_udp.transport = self.transport
        self.preconnection._deliver_connection(new_connection)
        logger.info("Delivered new connection to listener.")
        new_udp.datagram_received(data, addr)
        self.remotes[addr] = new_connection
        return


class StreamHandler(asyncio.Protocol):

    def __init__(self, preconnection):
        new_connection = Connection(preconnection)
        self.connection = new_connection

    def connection_made(self, transport):
        new_remote_endpoint = RemoteEndpoint()
        logger.info("Received new connection.")
        # Get information about the newly connected endpoint
        new_remote_endpoint.with_address(
            transport.get_extra_info("peername")[0])
        new_remote_endpoint.with_port(
            transport.get_extra_info("peername")[1])
        self.connection.remote_endpoint = new_remote_endpoint
        new_tcp = TcpTransport(self.connection,
                               self.connection.local_endpoint,
                               new_remote_endpoint)
        new_tcp.transport = transport
        self.connection.state = ConnectionState.ESTABLISHED
        self.connection._originating_preconnection._deliver_connection(self.connection)
        return

    def eof_received(self):
        self.connection.transports[0].eof_received()

    def data_received(self, data):
        self.connection.transports[0].data_received(data)

    def error_received(self, err):
        self.connection.transports[0].error_received(err)

    def connection_lost(self, exc):
        self.connection.transports[0].connection_lost(exc)
