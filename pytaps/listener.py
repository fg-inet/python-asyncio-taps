import ipaddress

import netifaces

from .connection import Connection
from .multicast import do_join, do_leave
from .transports import *

logger = setup_logger(__name__, "cyan")


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
        self.loop = preconnection.loop
        self.framer = preconnection.framer
        self.security_context = None
        self.active_ports = {}
        self.protocol = None

        # Callbacks
        self.stopped = preconnection.stopped
        self.listen_error = preconnection.listen_error
        self.connection_received = preconnection.connection_received
        self.initiate_error = preconnection.initiate_error
        self.ready = preconnection.ready

    async def start_listener(self):
        """ method wrapped by listen
        """
        logger.info("Starting listener with hostname: " +
                    str(self.local_endpoint.host_name) +
                    ", interface: " + str(self.local_endpoint.interface) +
                    ", addresses: " + str(self.local_endpoint.address) +
                    ".")

        # Create set of candidate protocols
        protocol_candidates = create_candidates(self)

        if self.remote_endpoint:
            if not self.remote_endpoint.address:
                remote_info = await self.loop.getaddrinfo(
                    self.remote_endpoint.host_name, self.remote_endpoint.port)
                self.remote_endpoint.address = [remote_info[0][4][0]]
        # If the candidate set is empty issue an InitiateError cb
        if not protocol_candidates:
            logger.warn("Protocol selection Error occurred.")
            if self.listen_error:
                self.loop.create_task(self.listen_error())
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
        candidate_set = [protocol + (address,)
                         for address in all_addrs
                         for protocol in protocol_candidates]

        # Attempt to set up the appropriate listener for the candidate protocol
        for candidate in candidate_set:
            try:
                if candidate[0] == 'udp':
                    self.protocol = 'udp'
                    self.local_endpoint.address = [candidate[2]]
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
                                get('direction') == 'unidirection-receive':
                            logger.info("direction is unicast receive")
                            # multicast_receiver = True
                            self.loop.create_task(self.multicast_join())
                    else:
                        await self.loop.create_datagram_endpoint(
                            lambda: DatagramHandler(self),
                            local_addr=(
                                self.local_endpoint.address[0],
                                self.local_endpoint.port))
                elif candidate[0] == 'tcp':
                    self.protocol = 'tcp'
                    self.local_endpoint.address = [candidate[2]]
                    logger.info("TCP local endpoint: address " +
                                str(self.local_endpoint.address) +
                                " port: " + str(self.local_endpoint.port))
                    await self.loop.create_server(
                        lambda: StreamHandler(self),
                        self.local_endpoint.address[0],
                        self.local_endpoint.port,
                        ssl=self.security_context)
            except Exception as err:
                logger.warn("Listen Error occurred: " + str(err))
                if self.listen_error:
                    self.loop.create_task(self.listen_error())

            logger.info("Started " + self.protocol + " Listener on " +
                        (str(self.local_endpoint.address) if
                         self.local_endpoint.address else "default") + ":" +
                        str(self.local_endpoint.port))
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
        if multicast.do_receive():
            self.loop.create_task(multicast.do_receive())

    """ ASYNCIO function that gets called when leaving a multicast flow
    """

    async def multicast_leave(self):
        logger.info("Leaving multicast session.")
        self.multicast_false = True
        do_leave(self)


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
        if new_connection.connection_received:
            new_connection.loop.create_task(
                new_connection.connection_received(new_connection))
            logger.info("Called connection_received cb")
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
        if self.connection.connection_received:
            self.connection.loop.create_task(
                self.connection.connection_received(self.connection)
            )
        return

    def eof_received(self):
        self.connection.transports[0].eof_received()

    def data_received(self, data):
        self.connection.transports[0].data_received(data)

    def error_received(self, err):
        self.connection.transports[0].error_received(err)

    def connection_lost(self, exc):
        self.connection.transports[0].connection_lost(exc)
