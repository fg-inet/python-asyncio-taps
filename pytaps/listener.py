import asyncio
import ssl
from .connection import Connection
from .securityParameters import SecurityParameters
from .transportProperties import *
from .endpoint import LocalEndpoint, RemoteEndpoint
from .utility import *
from .transports import *

color = "cyan"


class Listener():
    """The TAPS listener class.

    Attributes:
        preconnection (Preconnection, required):
                Preconnection object from which this Connection
                object was created.
    """
    def __init__(self, preconnection):
                # Initializations
                self.local_endpoint = preconnection.local_endpoint
                self.remote_endpoint = preconnection.remote_endpoint
                self.transport_properties = preconnection.transport_properties
                self.security_parameters = preconnection.security_parameters
                self.loop = preconnection.loop
                self.active = preconnection.active
                self.framer = preconnection.framer
                self.security_context = None
                self.set_callbacks(preconnection)

    async def start_listener(self):
        """ method wrapped by listen
        """
        print_time("Starting listener.", color)

        # Create set of candidate protocols
        candidate_set = self.create_candidates()

        # If the candidate set is empty issue an InitiateError cb
        if not candidate_set:
            print_time("Protocol selection Error occured.", color)
            if self.initiate_error:
                self.loop.create_task(self.initiate_error())
            return

        # If security_parameters were given, initialize ssl context
        if self.security_parameters:
            self.security_context = ssl.create_default_context(
                                                ssl.Purpose.CLIENT_AUTH)
            if self.security_parameters.identity:
                print_time("Identity: " +
                           str(self.security_parameters.identity))
                self.security_context.load_cert_chain(
                                        self.security_parameters.identity)
            for cert in self.security_parameters.trustedCA:
                self.security_context.load_verify_locations(cert)
        # Attempt to set up the appropriate listener for the candidate protocol
        for candidate in candidate_set:
            try:
                if candidate[0] == 'udp':
                    self.protocol = 'udp'
                    await self.loop.create_datagram_endpoint(
                                    lambda: DatagramHandler(self),
                                    local_addr=(self.local_endpoint.interface,
                                                self.local_endpoint.port))
                elif candidate[0] == 'tcp':
                    self.protocol = 'tcp'
                    server = await self.loop.create_server(
                                    lambda: StreamHandler(self),
                                    self.local_endpoint.interface,
                                    self.local_endpoint.port,
                                    ssl=self.security_context)
            except:
                print_time("Listen Error occured.", color)
                if self.listen_error:
                    self.loop.create_task(self.listen_error())

            print_time("Starting " + self.protocol + " Listener on " +
                       (str(self.local_endpoint.address) if
                        self.local_endpoint.address else "default") + ":" +
                       str(self.local_endpoint.port), color)
        return

    def create_candidates(self):
        """ Decides which protocols are candidates and then orders them
        according to the TAPS interface draft
        """
        # Get the protocols know to the implementation from transportProperties
        available_protocols = get_protocols()

        # At the beginning, all protocols are candidates
        candidate_protocols = dict([(row["name"], list((0, 0)))
                                   for row in available_protocols])

        # Iterate over all available protocols and over all properties
        for protocol in available_protocols:
            for transport_property in self.transport_properties.properties:
                # If a protocol has a prohibited property remove it
                if (self.transport_properties.properties[transport_property]
                        is PreferenceLevel.PROHIBIT):
                    if (protocol[transport_property] is True and
                            protocol["name"] in candidate_protocols):
                        del candidate_protocols[protocol["name"]]
                # If a protocol doesnt have a required property remove it
                if (self.transport_properties.properties[transport_property]
                        is PreferenceLevel.REQUIRE):
                    if (protocol[transport_property] is False and
                            protocol["name"] in candidate_protocols):
                        del candidate_protocols[protocol["name"]]
                # Count how many PREFER properties each protocol has
                if (self.transport_properties.properties[transport_property]
                        is PreferenceLevel.PREFER):
                    if (protocol[transport_property] is True and
                            protocol["name"] in candidate_protocols):
                        candidate_protocols[protocol["name"]][0] += 1
                # Count how many AVOID properties each protocol has
                if (self.transport_properties.properties[transport_property]
                        is PreferenceLevel.AVOID):
                    if (protocol[transport_property] is True and
                            protocol["name"] in candidate_protocols):
                        candidate_protocols[protocol["name"]][1] -= 1

        # Sort candidates by number of PREFERs and then by AVOIDs on ties
        sorted_candidates = sorted(candidate_protocols.items(),
                                   key=lambda value: (value[1][0],
                                   value[1][1]), reverse=True)

        return sorted_candidates

    def set_callbacks(self, preconnection):
            self.ready = preconnection.ready
            self.initiate_error = preconnection.initiate_error
            self.connection_received = preconnection.connection_received
            self.listen_error = preconnection.listen_error
            self.stopped = preconnection.stopped


class DatagramHandler(asyncio.Protocol):
    """ Class required to handle incoming datagram flows
    """
    def __init__(self, preconnection):
        self.preconnection = preconnection
        self.remotes = dict()
        self.preconnection.handler = self

    def connection_made(self, transport):
        self.transport = transport
        print_time("New UDP flow", color)
        return

    def datagram_received(self, data, addr):
        print_time("Received new datagram", color)
        if addr in self.remotes:
            self.remotes[addr].transports[0].datagram_received(data, addr)
            return
        new_connection = Connection(self.preconnection)
        new_connection.state = ConnectionState.ESTABLISHED
        new_remote_endpoint = RemoteEndpoint()
        print_time("Received new connection.", color)
        new_remote_endpoint.with_address(addr[0])
        new_remote_endpoint.with_port(addr[1])
        new_connection.remote_endpoint = new_remote_endpoint
        print_time("Created new connection object.", color)
        new_udp = UdpTransport(new_connection, new_connection.local_endpoint, new_remote_endpoint)
        new_udp.transport = self.transport
        print(self.transport)
        if new_connection.connection_received:
            new_connection.loop.create_task(
                new_connection.connection_received(new_connection))
            print_time("Called connection_received cb", color)
        new_udp.datagram_received(data, addr)
        self.remotes[addr] = new_connection
        return


class StreamHandler(asyncio.Protocol):

    def __init__(self, preconnection):
        new_connection = Connection(preconnection)
        self.connection = new_connection

    def connection_made(self, transport):
        new_remote_endpoint = RemoteEndpoint()
        print_time("Received new connection.", color)
        # Get information about the newly connected endpoint
        new_remote_endpoint.with_address(
                            transport.get_extra_info("peername")[0])
        new_remote_endpoint.with_port(
                            transport.get_extra_info("peername")[1])
        self.connection.remote_endpoint = new_remote_endpoint
        new_tcp = TcpTransport(self.connection, self.connection.local_endpoint, new_remote_endpoint)
        new_tcp.transport = transport
        self.connection.state = ConnectionState.ESTABLISHED
        if self.connection.connection_received:
            self.connection.loop.create_task(self.connection.connection_received(self.connection))
        return

    def eof_received(self):
        self.connection.transports[0].eof_received()

    def data_received(self, data):
        self.connection.transports[0].data_received(data)

    def error_received(self, err):
        self.connection.transports[0].error_received(err)

    def connection_lost(self, exc):
        self.connection.transports[0].connection_lost(exc)
