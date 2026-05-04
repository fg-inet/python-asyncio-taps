import socket
try:
    import netifaces
except ImportError:
    netifaces = None

from .connection_group import ConnectionGroup
from .message import MessageContext
from .transportProperties import TransportProperties, canonicalize_property_name
from .transports import TcpTransport, UdpTransport
from .utility import (
    Candidate,
    ConnectionState,
    SleepClassForRacing,
    build_protocol_candidates,
    create_candidates,
    setup_logger,
)

logger = setup_logger(__name__)
# Wait for 100 ms between connection attempts when racing
RACING_DELAY = 0.1


def _require_netifaces():
    if netifaces is None:
        raise ImportError(
            "Interface-constrained endpoint selection requires the 'netifaces' package."
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
        self.local_endpoint = (
            preconnection.local_endpoint.clone()
            if preconnection.local_endpoint else None
        )
        self.remote_endpoint = (
            preconnection.remote_endpoint.clone()
            if preconnection.remote_endpoint else None
        )
        self.transport_properties = TransportProperties(
            selection_properties=preconnection.transport_properties.get_selection_properties(),
            connection_properties=preconnection.transport_properties.get_connection_properties(),
        )
        self.security_parameters = preconnection.security_parameters
        self.security_context = preconnection.security_context
        self.loop = preconnection.loop
        self.active = False
        self.framer = preconnection.framer
        self.sleeper_for_racing = SleepClassForRacing()
        self.pending = []
        self._originating_preconnection = preconnection
        self._ready_waiter = self.loop.create_future()
        self._pending_message = None
        # Security Context for SSL
        self.security_context = None
        # Current state of the connection object
        self.state = ConnectionState.ESTABLISHING
        # List of possible underlying transports
        self.transports = []
        self.protocol = None
        self.multicast_open = False
        self.connection_group = ConnectionGroup(self)
        self.race_task = None

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
        self.stopped = preconnection.stopped
        self.listen_error = preconnection.listen_error
        self.connection_received = preconnection.connection_received
        self.initiate_error = preconnection.initiate_error
        self.ready = preconnection.ready

    def _coerce_message_context(self, message_context=None, *, end_of_message=True):
        if message_context is None:
            return MessageContext(end_of_message=end_of_message)
        message_context.end_of_message = end_of_message
        return message_context

    def set_property(self, prop, value):
        self.transport_properties.set_property(prop, value)
        canonical = canonicalize_property_name(prop)
        if self.connection_group is not None:
            self.connection_group.set_property(canonical, value)
        return None

    def get_properties(self):
        return {
            "selection": self.transport_properties.get_selection_properties(),
            "connection": self.transport_properties.get_connection_properties(),
            "readOnly": {
                "state": self.state.name,
                "protocol": self.protocol,
                "localEndpoint": self.local_endpoint,
                "remoteEndpoint": self.remote_endpoint,
                "groupSize": len(self.connection_group) if self.connection_group else 1,
            },
        }

    async def wait_ready(self):
        await self._ready_waiter
        return self

    def add_remote(self, remote_endpoints):
        for endpoint in remote_endpoints:
            endpoint_copy = endpoint.clone()
            for address in endpoint_copy.address:
                if self.remote_endpoint is None:
                    self.remote_endpoint = endpoint_copy
                    break
                self.remote_endpoint.with_address(address)
            if self.remote_endpoint and endpoint_copy.host_name and not self.remote_endpoint.host_name:
                self.remote_endpoint.host_name = endpoint_copy.host_name
        return self.remote_endpoint

    def remove_remote(self, remote_endpoints):
        if self.remote_endpoint is None:
            return None
        for endpoint in remote_endpoints:
            for address in endpoint.address:
                self.remote_endpoint.without_address(address)
        return self.remote_endpoint

    def add_local(self, local_endpoints):
        for endpoint in local_endpoints:
            endpoint_copy = endpoint.clone()
            if self.local_endpoint is None:
                self.local_endpoint = endpoint_copy
                continue
            for address in endpoint_copy.address:
                self.local_endpoint.with_address(address)
            for interface in endpoint_copy.interface:
                self.local_endpoint.with_interface(interface)
        return self.local_endpoint

    def remove_local(self, local_endpoints):
        if self.local_endpoint is None:
            return None
        for endpoint in local_endpoints:
            for address in endpoint.address:
                self.local_endpoint.without_address(address)
            for interface in endpoint.interface:
                self.local_endpoint.without_interface(interface)
        return self.local_endpoint

    async def clone(self):
        template = self._originating_preconnection.clone()
        template.local_endpoint = self.local_endpoint.clone() if self.local_endpoint else None
        template.remote_endpoint = self.remote_endpoint.clone() if self.remote_endpoint else None
        template.transport_properties = TransportProperties(
            selection_properties=self.transport_properties.get_selection_properties(),
            connection_properties=self.transport_properties.get_connection_properties(),
        )
        cloned_connection = await template.initiate()
        self.connection_group.add_connection(cloned_connection)
        return cloned_connection

    async def send(self, data, message_context=None, end_of_message=True):
        if isinstance(data, str):
            data = data.encode()
        context = self._coerce_message_context(
            message_context,
            end_of_message=end_of_message,
        )
        return self.transports[0].send(data, context, end_of_message)

    async def initiate_with_send(self, data, message_context=None, end_of_message=True):
        context = self._coerce_message_context(
            message_context,
            end_of_message=end_of_message,
        )
        self._pending_message = (data, context, end_of_message)
        return self

    async def close_group(self):
        if self.connection_group:
            await self.connection_group.close()

    def abort(self, reason="Aborted by local endpoint"):
        for transport in list(self.transports):
            if getattr(transport, "transport", None) is not None:
                transport.transport.close()
        self.state = ConnectionState.CLOSED
        if self.connection_error:
            self.loop.create_task(self.connection_error(reason, self))

    async def abort_group(self):
        if self.connection_group:
            await self.connection_group.abort()

    def grouped_connections(self):
        if self.connection_group is None:
            return [self]
        return list(self.connection_group.connections)

    async def race(self):
        # This is an active connection attempt
        self.active = True
        protocol_candidates = build_protocol_candidates(self.transport_properties)

        if len(protocol_candidates) == 0:
            logger.critical("Candidate set is empty, aborting")
            if self.initiate_error:
                self.loop.create_task(self.initiate_error())
            return

        if self.remote_endpoint.host_name:
            # Resolve address
            # FIXME: Unfortunately, asyncio getaddrinfo does not
            # FIXME: allow to resolve on specific interfaces
            # FIXME: Consider migrating to something better, e.g., getdns
            remote_info = await self.loop.getaddrinfo(
                self.remote_endpoint.host_name, self.remote_endpoint.port)
            # Concat v6 and v4 address lists, making sure we try v6 first
            remote_addrs_v6 = list(
                set([
                    info[4][0] for info in remote_info
                    if info[0] == socket.AddressFamily.AF_INET6]
                    )
            )
            remote_addrs_v4 = list(
                set([
                    info[4][0] for info in remote_info
                    if info[0] == socket.AddressFamily.AF_INET]
                    )
            )
            remote_addrs = [
                (socket.AddressFamily.AF_INET6, address) for address in remote_addrs_v6
            ] + [
                (socket.AddressFamily.AF_INET, address) for address in remote_addrs_v4
            ]
            logger.info("Resolved " + str(self.remote_endpoint.host_name) +
                        " to " + str([address for _, address in remote_addrs]))

        else:
            remote_addrs = []
            for address in self.remote_endpoint.address:
                family = (
                    socket.AddressFamily.AF_INET6
                    if ":" in address else socket.AddressFamily.AF_INET
                )
                remote_addrs.append((family, address))
            logger.info("Not resolving - using address " +
                        str(self.remote_endpoint.address) + " --> " +
                        str([address for _, address in remote_addrs]))

        candidate_set = create_candidates(self, remote_addrs)

        if self.local_endpoint:
            # Local interface specified -->
            # try local addresses on that interface
            _require_netifaces()
            local_addresses_by_family = {}
            for local_interface in self.local_endpoint.interface:
                try:
                    # Unfortunately, link-local IPv6 addresses don't work
                    # because they're broken in
                    # asyncio: https://bugs.python.org/issue35545
                    local_v6_addrs = [entry['addr']
                                      for entry in netifaces.ifaddresses
                                      (local_interface)[netifaces.AF_INET6]
                                      if entry['addr'][:4] != "fe80"]
                    local_v4_addrs = [entry['addr']
                                      for entry in netifaces.ifaddresses
                                      (local_interface)[netifaces.AF_INET]]
                    local_addresses_by_family[local_interface] = {
                        socket.AddressFamily.AF_INET6: local_v6_addrs,
                        socket.AddressFamily.AF_INET: local_v4_addrs,
                    }
                    logger.info("Trying addresses of local interface " +
                                str(self.local_endpoint.interface) + " --> " +
                                str(local_v6_addrs) + ", " +
                                str(local_v4_addrs))
                except ValueError as err:
                    logger.critical("Cannot get IP addresses for " +
                                    str(self.local_endpoint.interface) + ": " +
                                    str(err))
                    # TODO throw error
            expanded_candidates = []
            for candidate in candidate_set:
                if candidate.path == "default":
                    expanded_candidates.append(candidate)
                    continue
                family_addrs = local_addresses_by_family.get(candidate.path, {})
                for local_address in family_addrs.get(candidate.address_family, []):
                    expanded_candidates.append(
                        Candidate(
                            protocol=candidate.protocol,
                            remote_address=candidate.remote_address,
                            address_family=candidate.address_family,
                            path=candidate.path,
                            local_address=local_address,
                        )
                    )
            candidate_set = expanded_candidates
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
            if candidate.local_address:
                # bind to a specific local address
                local_address_to_use = (candidate.local_address, None)
                self.local_endpoint.address = [candidate.local_address]
            else:
                local_address_to_use = None

            if candidate.protocol == 'udp':
                self.protocol = 'udp'
                logger.info("Creating UDP connect task with remote addr " +
                            str(candidate.remote_address) + ", port " +
                            str(self.remote_endpoint.port))
                self.remote_endpoint.address = [candidate.remote_address]

                # Create a datagram endpoint
                self.loop.create_task(
                    self.loop.create_datagram_endpoint(
                        lambda: UdpTransport(
                            connection=self,
                            remote_endpoint=self.remote_endpoint),
                        remote_addr=(self.remote_endpoint.address[0],
                                     self.remote_endpoint.port),
                        local_addr=local_address_to_use))

                logger.info("Not racing multiple addrs for UDP" +
                            " -- stop racing")
                break

            elif candidate.protocol == 'tcp':
                self.protocol = 'tcp'
                logger.info("Creating TCP connect task to " + candidate.remote_address +
                            ".")
                self.remote_endpoint.address = [candidate.remote_address]
                # If the protocol is tcp, create a asyncio connection
                self.loop.create_task(
                    self.loop.create_connection(
                        lambda: TcpTransport(
                            connection=self,
                            remote_endpoint=self.remote_endpoint),
                        self.remote_endpoint.address[0],
                        self.remote_endpoint.port,
                        ssl=self.security_context,
                        server_hostname=(
                            self.remote_endpoint.host_name
                            if self.security_context else None),
                        local_addr=local_address_to_use))
                # Wait before starting next connection attempt
                await self.sleeper_for_racing.sleep(RACING_DELAY)

    async def send_message(self, data, message_context=None, end_of_message=True):
        """ Attempts to send data on the connection.
            Attributes:
                data (string, required):
                    Data to be send.
        """
        if isinstance(data, str):
            data = data.encode()
        return await self.send(data, message_context, end_of_message)

    async def receive(self, min_incomplete_length=float("inf"), max_length=-1):
        """ Queues the reception of a message.
        Attributes:
            min_incomplete_length (integer, optional):
                The minimum length an incomplete message
                needs to have.
            max_length (integer, optional):
                The maximum length a message can have.
        """
        self.transports[0].receive(min_incomplete_length, max_length)

    def close(self):
        """ Attempts to close the connection, issues a closed event
        on success.
        """
        if self.multicast_open:
            self.loop.create_task(self.multicast_leave())
        self.loop.create_task(self.transports[0].close())
        self.state = ConnectionState.CLOSING

    def parse(self, min_incomplete_length=0, max_length=0):
        """ Returns the message buffer of the
            connection.
        """
        return self.transports[0].recv_buffer, None, False

    # Events for active open
    def on_ready(self, callback):
        """ Set callback for ready events that
            get thrown once the connection is ready
            to send and receive data.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.ready = callback

    def on_initiate_error(self, callback):
        """ Set callback for initiate error events that
            get thrown if an error occurs
            during initiation.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.initiate_error = callback

    # Events for sending messages
    def on_sent(self, callback):
        """ Set callback for sent events that get thrown if a message has been
        successfully sent.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.sent = callback

    def on_send_error(self, callback):
        """ Set callback for send error events
            that get thrown if an error occurs
            during sending of a message.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.send_error = callback

    def on_expired(self, callback):
        """ Set callback for expired events that
            get thrown if a message expires.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.expired = callback

    # Events for receiving messages
    def on_received(self, callback):
        """ Set callback for received events that get thrown if a new message
        has been received.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.received = callback

    def on_received_partial(self, callback):
        """ Set callback for partial received events that
            get thrown if a new partial
            message has been received.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.received_partial = callback

    def on_receive_error(self, callback):
        """ Set callback for receive error events that
            get thrown if an error occurs
            during reception of a message.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.receive_error = callback

    def on_connection_error(self, callback):
        """ Set callback for connection error events that
            get thrown if an error occurs
            while the connection is open.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.connection_error = callback

    # Events for closing a connection
    def on_closed(self, callback):
        """ Set callback for on closed events that get thrown if the
        connection has been closed successfully.

        Attributes:
            callback (callback, required): Function that implements the
                callback.  Callback signature should accept a connection
                as its parameter.
        """
        self.closed = callback

    def multicast_leave(self):
        pass
