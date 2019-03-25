import asyncio
import ssl
from .connection import Connection, DatagramHandler
from .transportProperties import *
from .endpoint import LocalEndpoint, RemoteEndpoint
from .utility import *
color = "red"


class Preconnection:
    """The TAPS preconnection class.

    Attributes:
        localEndpoint (:obj:'localEndpoint', optional): LocalEndpoint of the
                       preconnection, required if the connection
                       will be used to listen
        remoteEndpoint (:obj:'remoteEndpoint', optional): RemoteEndpoint of the
                        preconnection, required if a connection
                        will be initiated
        transportProperties (:obj:'transportProperties', optional): object with
                        the transport properties
                        with specified preferenceLevel
        securityParams (:obj:'securityParameters', optional): Security
                        Parameters for the preconnection
        eventLoop (:obj: 'eventLoop', optional): event loop on which all
                        coroutines will be scheduled, if none if given
                        the one of the current thread is used by default
    """
    def __init__(self, local_endpoint=None, remote_endpoint=None,
                 transport_properties=TransportProperties(),
                 security_parameters=None,
                 event_loop=asyncio.get_event_loop()):
                # Assertions
                if local_endpoint is None and remote_endpoint is None:
                    raise Exception("At least one endpoint need "
                                    "to be specified")
                # Initializations
                self.local_endpoint = local_endpoint
                self.remote_endpoint = remote_endpoint
                self.transport_properties = transport_properties
                self.security_parameters = security_parameters
                self.security_context = None
                self.loop = event_loop
                self.connection = None
                self.protocol = None
                self.waiter = None

                self.read = None
                self.initiate_error = None
                self.connection_received = None
                self.listen_error = None
                self.stopped = None
                self.ready = None
                self.initiate_error = None
                self.connection_received = None
                self.listen_error = None
                self.stopped = None
                self.active = False

    async def await_connection(self):
        if self.waiter is not None:
            print_time("Already waiting for data", color)
            return
        self.waiter = self.loop.create_future()
        try:
            await self.waiter
        finally:
            self.waiter = None

    async def inititate_helper(self):
        return
    """ Initiates the preconnection, i.e. creates a connection object
        and attempts to connect it to the specified remote endpoint.
    """
    async def initiate(self):
        print_time("Initiating connection.", color)
        self.active = True
        # Create set of candidate protocols
        candidate_set = self.create_candidates()
        if not candidate_set:
            print_time("Empty candidate set", color)
            if self.initiate_error:
                print_time("Protocol selection Error occured.", color)
                self.loop.create_task(self.initiate_error())
                print_time("Queued InitiateError cb.", color)
            return
        if self.security_parameters:
            self.security_context = ssl.create_default_context(
                                                ssl.Purpose.CLIENT_AUTH)
            if self.security_parameters.identity:
                print_time("Identity: "
                           + str(self.security_parameters.identity))
                self.security_context.load_cert_chain(
                                        self.security_parameters.identity)
            for cert in self.security_parameters.trustedCA:
                self.security_context.load_verify_locations(cert)
        """
        connection = Connection(self)
        connection.on_initiate_error(self.initiate_error)
        connection.on_ready(self.ready) """
        remote_info = await self.loop.getaddrinfo(self.remote_endpoint.host_name,
                                                  self.remote_endpoint.port)
        print(remote_info)
        self.remote_endpoint.address = remote_info[0][4][0]
        print(self.remote_endpoint.address)
        if candidate_set[0][0] == 'udp':
            self.protocol = 'udp'
            print_time("Creating UDP connect task.", color)
            asyncio.create_task(self.loop.create_datagram_endpoint(
                                lambda: Connection(self),
                                remote_addr=(self.remote_endpoint.address,
                                             self.remote_endpoint.port)))
        elif candidate_set[0][0] == 'tcp':
            self.protocol = 'tcp'
            print_time("Creating TCP connect task.", color)
            asyncio.create_task(self.loop.create_connection(
                                lambda: Connection(self),
                                self.remote_endpoint.address,
                                self.remote_endpoint.port,
                                ssl=ssl.create_default_context()))
        # else:
        # self.loop.run_until_complete(self.initiate_helper(con))
        await self.await_connection()
        print_time("Returning connection object.", color)
        return self.connection

    """Handles a new connection detected by listener
    """
    def handle_new_connection(self, transport):
        new_remote_endpoint = RemoteEndpoint()
        print_time("Received new connection.", color)
        new_remote_endpoint.with_address(tra.get_extra_info("peername")[0])
        new_remote_endpoint.with_port(writer.get_extra_info("peername")[1])
        new_connection = Connection(self.local_endpoint, new_remote_endpoint,
                                    self.transport_properties,
                                    self.security_parameters)
        # new_connection.set_reader_writer(reader, writer)
        print_time("Created new connection object (from "
                   + new_remote_endpoint.address + ":"
                   + str(new_remote_endpoint.port) + ")", color)
        if self.connection_received:
            self.loop.create_task(self.connection_received(new_connection))
            print_time("Called connection_received cb", color)
        return

    async def start_listener(self):
        candidate_set = self.create_candidates()
        if not candidate_set:
            print_time("Empty candidate set", color)
            if self.initiate_error:
                print_time("Protocol selection Error occured.", color)
                self.loop.create_task(self.initiate_error())
                print_time("Queued InitiateError cb.", color)
            return

        print_time("Starting Listener on " +
                   (str(self.local_endpoint.address) if
                    self.local_endpoint.address else "default") + ":"
                   + str(self.local_endpoint.port), color)
        if self.security_parameters:
            self.security_context = ssl.create_default_context(
                                                ssl.Purpose.CLIENT_AUTH)
            if self.security_parameters.identity:
                print_time("Identity: "
                           + str(self.security_parameters.identity))
                self.security_context.load_cert_chain(
                                        self.security_parameters.identity)
            for cert in self.security_parameters.trustedCA:
                self.security_context.load_verify_locations(cert)


        #try:
        if candidate_set[0][0] == 'udp':
            self.protocol = 'udp'
            print_time("Starting UDP Listener.", color)
            await self.loop.create_datagram_endpoint(
                            lambda: DatagramHandler(self),
                            local_addr=(self.local_endpoint.interface,
                                        self.local_endpoint.port))
        elif candidate_set[0][0] == 'tcp':
            self.protocol = 'tcp'
            print_time("Starting TCP Listener.", color)
            server = await self.loop.create_server(
                            lambda: Connection(self),
                            self.local_endpoint.interface,
                            self.local_endpoint.port,
                            ssl=self.security_context)
            """
            await asyncio.start_server(self.handle_new_connection,
                                       self.local_endpoint.interface,
                                       self.local_endpoint.port)
                                       ssl=self.security_context)"""
        """except:
            print_time("Listen Error occured.", color)
            if self.listen_error:
                self.loop.create_task(self.listen_error())
                print_time("Queued listen_error cb.", color)
        return"""
        print_time("Listening for new connections...", color)

    async def listen(self):
        self.active = False
        self.loop.create_task(self.start_listener())
        return

    def create_candidates(self):
        available_protocols = get_protocols()
        candidate_protocols = dict([(row["name"], list((0, 0)))
                                   for row in available_protocols])
        for protocol in available_protocols:
            for transport_property in self.transport_properties.properties:
                if (self.transport_properties.properties[transport_property]
                        is PreferenceLevel.PROHIBIT):
                    if (protocol[transport_property] is True and
                            protocol["name"] in candidate_protocols):
                        del candidate_protocols[protocol["name"]]
                if (self.transport_properties.properties[transport_property]
                        is PreferenceLevel.REQUIRE):
                    if (protocol[transport_property] is False and
                            protocol["name"] in candidate_protocols):
                        del candidate_protocols[protocol["name"]]
                if (self.transport_properties.properties[transport_property]
                        is PreferenceLevel.PREFER):
                    if (protocol[transport_property] is True and
                            protocol["name"] in candidate_protocols):
                        candidate_protocols[protocol["name"]][0] += 1
                if (self.transport_properties.properties[transport_property]
                        is PreferenceLevel.AVOID):
                    if (protocol[transport_property] is True and
                            protocol["name"] in candidate_protocols):
                        candidate_protocols[protocol["name"]][1] -= 1
        sorted_candidates = sorted(candidate_protocols.items(),
                                   key=lambda value: (value[1][0],
                                   value[1][1]), reverse=True)

        return sorted_candidates

    # Events for active open
    def on_ready(self, a):
        self.ready = a

    def on_initiate_error(self, a):
        self.initiate_error = a

    # Events for passive open
    def on_connection_received(self, a):
        self.connection_received = a

    def on_listen_error(self, a):
        self.listen_error = a

    def on_stopped(self, a):
        self.stopped = a
