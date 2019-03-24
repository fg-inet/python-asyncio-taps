import asyncio
import json
import sys
import ssl
from .endpoint import LocalEndpoint, RemoteEndpoint
from .transportProperties import *
from .utility import *
color = "green"


class Connection(asyncio.Protocol):
    """The TAPS connection class.

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
                        Parameters for the connection
    """
    def __init__(self, preconnection):
                # Initializations
                self.local_endpoint = preconnection.local_endpoint
                self.remote_endpoint = preconnection.remote_endpoint
                self.transport_properties = preconnection.transport_properties
                self.security_parameters = preconnection.security_parameters
                self.security_context = None
                self.loop = asyncio.get_event_loop()
                self.message_count = 0

                # Required for receiving data
                self.msg_buffer = None
                self.recv_buffer = None
                self.at_eof = False
                self.waiter = None

                # Callbacks
                self.ready = preconnection.ready
                self.initiate_error = preconnection.initiate_error
                self.sent = None
                self.send_error = None
                self.expired = None
                self.connection_error = None
                self.connection_received = preconnection.connection_received
                self.listen_error = preconnection.listen_error
                self.stopped = preconnection.stopped
                self.received = None
                self.received_partial = None
                self.receive_error = None
                self.closed = None
                self.reader = None
                self.writer = None
                # Assertions
                if self.local_endpoint is None and self.remote_endpoint is None:
                    raise Exception("At least one endpoint need "
                                    "to be specified")

    # Asyncio Callbacks
    def connection_made(self, transport):
        if transport.get_extra_info('peername') is None:
            print_time("New datagram connection", color)
            return
        self.transport = transport
        new_remote_endpoint = RemoteEndpoint()
        print_time("Received new connection.", color)
        new_remote_endpoint.with_address(transport.get_extra_info("peername")[0])
        new_remote_endpoint.with_port(transport.get_extra_info("peername")[1])
        self.remote_endpoint = new_remote_endpoint
        print_time("Created new connection object.", color)
        if self.connection_received:
            self.loop.create_task(self.connection_received(self))
            print_time("Called connection_received cb", color)
        return

    def data_received(self, data):
        if self.recv_buffer is None:
            self.recv_buffer = data
        else:
            self.recv_buffer = self.recv_buffer + data
            printtime("Received " + self.recv_buffer.decode(), color)
        
        if self.waiter is not None:
            self.waiter.set_result(None)
        # print(self.recv_buffer)

    def datagram_received(self, data, addr):
        print("Received data " + data.decode())
    """ Tries to create a (TCP) connection to a remote endpoint
        If a local endpoint was specified on connection class creation,
        it will be used.
    """
    async def connect(self):
        if self.remote_endpoint.hostname is not None:
            # Resolve remote endpoint
            remote_info = await self.loop.getaddrinfo(
                                                 self.remote_endpoint.hostname,
                                                 self.remote_endpoint.port)
            self.remote_endpoint.address = remote_info[0][4][0]
            print_time("Resolved hostname " + self.remote_endpoint.hostname
                       + " to " + self.remote_endpoint.address, color)

        # Attempt connection
        try:
            if self.security_parameters is None:
                print_time("Connecting plain TCP.", color)
                if(self.local_endpoint is None):
                    print_time("Connecting with unspecified localEP.", color)
                    self.reader, self.writer = await asyncio.open_connection(
                                        self.remote_endpoint.address,
                                        self.remote_endpoint.port)
                else:
                    print_time("Connecting with specified localEP.", color)
                    self.reader, self.writer = await asyncio.open_connection(
                                    self.remote_endpoint.address,
                                    self.remote_endpoint.port,
                                    local_addr=(self.local_endpoint.interface,
                                                self.local_endpoint.port))
            else:
                await self._connect_TLS_TCP()
        except:
            if self.initiate_error:
                print_time("Initiate Error occured.", color)
                self.loop.create_task(self.initiate_error())
                print_time("Queued InitiateError cb.", color)
            return
        if self.ready:
            print_time("Connected successfully.", color)
            self.loop.create_task(self.ready(self))
            print_time("Queued Ready cb.", color)
        return

    """ Tries to create a TLS (over TCP) connection to a remote endpoint
        If a local endpoint was specified on connection class creation,
        it will be used.
    """
    async def _connect_TLS_TCP(self):
        print_time("Connecting TLS over TCP.", color)
        self.security_context = ssl.create_default_context()
        if self.security_parameters.identity:
            self.security_context.load_cert_chain(
                                self.security_parameters.identity)
        for cert in self.security_parameters.trustedCA:
            self.security_context.load_verify_locations(cert)

        if(self.local_endpoint is None):
            print_time("Connecting with unspecified localEP.", color)
            self.reader, self.writer = await asyncio.open_connection(
                                self.remote_endpoint.address,
                                self.remote_endpoint.port,
                                ssl=self.security_context,
                                server_hostname=self.remote_endpoint.hostname)
        else:
            print_time("Connecting with specified localEP.", color)
            self.reader, self.writer = await asyncio.open_connection(
                            self.remote_endpoint.address,
                            self.remote_endpoint.port,
                            local_addr=(self.local_endpoint.interface,
                                        self.local_endpoint.port),
                            ssl=self.security_context)

    """ Tries to send the (string) stored in data
    """
    async def send_data(self, data, message_count):
        print_time("Writing data.", color)
        try:
            self.writer.write(data.encode())
            await self.writer.drain()
        except:
            if self.send_error:
                print_time("SendError occured.", color)
                self.loop.create_task(self.send_error(message_count))
                print_time("Queued SendError cb.", color)
            return
        print_time("Data written successfully.", color)

        # Queue sent callback if there is one
        if self.sent:
            self.loop.create_task(self.sent(message_count))
            print_time("Queued Sent cb..", color)
        return

    """ Wrapper function that assigns MsgRef
        and then calls async helper function
        to send a message
    """
    async def send_message(self, data):
        print_time("Sending data.", color)
        self.message_count += 1
        self.loop.create_task(self.send_data(data, self.message_count))
        print_time("Returning MsgRef.", color)
        return self.message_count

    async def await_data(self):
        if self.waiter is not None:
            print_time("Already waiting for data", color)
            return
        self.waiter = self.loop.create_future()
        try:
            await self.waiter
        finally:
            self.waiter = None
        
    async def read_buffer(self, max_length=-1):
        if self.recv_buffer is None:
            await self.await_data()
        if max_length == -1:
            data = self.recv_buffer
            self.recv_buffer = None
            return data
        # if len(self.recv_buffer) > max_length:

    """ Queues reception of a message
    """
    async def receive_message(self, min_incomplete_length,
                              max_length):
        #try:
        data = await self.read_buffer(-1)
        if data is None:
            return
        data = data.decode()
        if self.msg_buffer is None:
            self.msg_buffer = data
        else:
            self.msg_buffer = self.msg_buffer + data
        """except:
            print_time("Connection Error", color)
            if self.connection_error is not None:
                self.loop.create_task(self.connection_error(self))
            return"""
        if self.at_eof:
            print_time("Received full message", color)
            if self.received:
                self.loop.create_task(self.received(self.msg_buffer,
                                                    "Context", self))
                print_time("Called received cb.", color)
            self.msg_buffer = None
            return

        elif len(self.msg_buffer) > min_incomplete_length:
            print_time("Received partial message.", color)
            if self.received_partial:
                self.loop.create_task(self.received_partial(self.msg_buffer,
                                      "Context", False, self))
                print_time("Called partial_receive cb.", color)
                self.msg_buffer = None

    """ Wrapper function to make receive return immediately
    """
    async def receive(self, min_incomplete_length=float("inf"), max_length=-1):
        self.loop.create_task(self.receive_message(min_incomplete_length,
                              max_length))
    """ Tries to close the connection
        TODO: Check why port isnt always freed
    """
    async def close_connection(self):
        print_time("Closing connection.", color)
        self.writer.close()
        await self.writer.wait_closed()
        print_time("Connection closed.", color)
        if self.closed:
            self.loop.create_task(self.closed())

    """ Wrapper function for close_connection,
        required to make close return immediately
    """
    def close(self):
        self.loop.create_task(self.close_connection())
    """ Function to set reader/writer for passive open
    """
    def set_reader_writer(self, reader, writer):
        self.reader = reader
        self.writer = writer

    # Events for active open
    def on_ready(self, a):
        self.ready = a

    def on_initiate_error(self, a):
        self.initiate_error = a

    # Events for sending messages
    def on_sent(self, a):
        self.sent = a

    def on_send_error(self, a):
        self.send_error = a

    def on_expired(self, a):
        self.expired = a

    # Events for receiving messages
    def on_received(self, a):
        self.received = a

    def on_received_partial(self, a):
        self.received_partial = a

    def on_receive_error(self, a):
        self.receive_error = a

    def on_connection_error(self, a):
        self.connection_error = a

    # Events for closing a connection
    def on_closed(self, a):
        self.closed = a
