import asyncio
from .connection import Connection
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
        securityParams (tbd): Security Parameters for the preconnection
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
                self.loop = event_loop
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

    """ Initiates the preconnection, i.e. creates a connection object
        and attempts to connect it to the specified remote endpoint.
    """
    async def initiate(self):
        print_time("Initiating connection.", color)
        # Create set of candidate protocols
        candidate_set = self.create_candidates()
        if not candidate_set:
            print_time("Empty candidate set", color)
            if self.initiate_error:
                print_time("Protocol selection Error occured.", color)
                self.loop.create_task(self.initiate_error())
                print_time("Queued InitiateError cb.", color)
            return

        connection = Connection(self)
        connection.on_initiate_error(self.initiate_error)
        connection.on_ready(self.ready)
        # This is required because initiate isnt async and therefor
        # there isnt necessarily a running eventloop
        # if self.loop.is_running():
        asyncio.create_task(connection.connect())
        # else:
        # self.loop.run_until_complete(self.initiate_helper(con))
        print_time("Created connect task.", color)
        print_time("Returning connection object.", color)
        return connection

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
        print_time("Created new connection object.", color)
        if self.connection_received:
            self.loop.create_task(self.connection_received(new_connection))
            print_time("Called connection_received cb", color)
        return

    async def start_listener(self):
        candidates = self.create_candidates()
        try:
            if candidates[0][0] == 'udp':
                print_time("Starting UDP Listener.", color)
                await self.loop.create_datagram_endpoint(
                                lambda: Connection(self),
                                local_addr=(self.local_endpoint.interface,
                                            self.local_endpoint.port))
            elif candidates[0][0] == 'tcp':
                print_time("Starting TCP Listener.", color)
                server = await self.loop.create_server(
                                lambda: Connection(self),
                                self.local_endpoint.interface,
                                self.local_endpoint.port)
            """
            await asyncio.start_server(self.handle_new_connection,
                                       self.local_endpoint.interface,
                                       self.local_endpoint.port)"""
        except:
            print_time("Listen Error occured.", color)
            if self.listen_error:
                self.loop.create_task(self.listen_error())
                print_time("Queued listen_error cb.", color)
        return
        print_time("Listening for new connections...", color)

    async def listen(self):
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
