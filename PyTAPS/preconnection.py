import asyncio
import ssl
from .connection import Connection
from .transportProperties import TransportProperties
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
                 transport_properties=None, security_parameters=None,
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
                self.read = None
                self.initiate_error = None
                self.connection_received = None
                self.listen_error = None
                self.stopped = None

    """async def initiate_helper(self, con):
        # Helper function to allow for immediate return of
        # Connection Object
        print_time("Created connect task.", color)
        asyncio.create_task(con.connect())"""

    """ Initiates the preconnection, i.e. creates a connection object
        and attempts to connect it to the specified remote endpoint.
    """
    async def initiate(self):
        print_time("Initiating connection.", color)
        connection = Connection(self.local_endpoint,
                                self.remote_endpoint,
                                self.transport_properties,
                                self.security_parameters)
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
    def handle_new_connection(self, reader, writer):
        new_remote_endpoint = RemoteEndpoint()
        print_time("Received new connection.", color)
        new_remote_endpoint.with_address(writer.get_extra_info("peername")[0])
        new_remote_endpoint.with_port(writer.get_extra_info("peername")[1])
        new_connection = Connection(self.local_endpoint, new_remote_endpoint,
                                    self.transport_properties,
                                    self.security_parameters)
        new_connection.set_reader_writer(reader, writer)
        print_time("Created new connection object (from "
                   + new_remote_endpoint.address + ":"
                   + str(new_remote_endpoint.port) + ")", color)
        if self.connection_received:
            self.loop.create_task(self.connection_received(new_connection))
            print_time("Called connection_received cb", color)
        return

    async def start_listener(self):
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

        try:
            await asyncio.start_server(self.handle_new_connection,
                                       self.local_endpoint.address,
                                       self.local_endpoint.port,
                                       ssl=self.security_context)
        except:
            print_time("Listen Error occured.", color)
            if self.listen_error_:
                self.loop.create_task(self.listen_error())
                print_time("Queued listen_error cb.", color)
        return
        print_time("Listening for new connections...", color)

    async def listen(self):
        self.loop.create_task(self.start_listener())
        return

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
