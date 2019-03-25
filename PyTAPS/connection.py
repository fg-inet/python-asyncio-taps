import asyncio
import json
import sys
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
        securityParams (tbd): Security Parameters for the preconnection
    """
    def __init__(self, preconnection):
                # Initializations
                self.local_endpoint = preconnection.local_endpoint
                self.remote_endpoint = preconnection.remote_endpoint
                self.transport_properties = preconnection.transport_properties
                self.security_parameters = preconnection.security_parameters
                self.loop = asyncio.get_event_loop()
                self.message_count = 0
                self.active = preconnection.active
                self.protocol = preconnection.protocol
                self.message_based = True

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

                # protocol specific stuff
                if self.protocol == 'tcp':
                    self.message_based = False
                elif self.protocol == 'udp':
                    self.message_based = True

                preconnection.connection = self
                if preconnection.waiter is not None:
                    preconnection.waiter.set_result(None)
                if self.protocol == "udp" and not self.active:
                    self.handler = preconnection.handler
                # Assertions
                if (self.local_endpoint is None and
                        self.remote_endpoint is None):
                    raise Exception("At least one endpoint need "
                                    "to be specified")

    # Asyncio Callbacks
    def connection_made(self, transport):
        if self.active is False:
            self.transport = transport
            new_remote_endpoint = RemoteEndpoint()
            print_time("Received new connection.", color)
            new_remote_endpoint.with_address(
                                transport.get_extra_info("peername")[0])
            new_remote_endpoint.with_port(
                                transport.get_extra_info("peername")[1])
            self.remote_endpoint = new_remote_endpoint
            if self.connection_received:
                self.loop.create_task(self.connection_received(self))
            return
        elif self.active:
            self.transport = transport
            print_time("Connected successfully.", color)
            if self.ready:
                self.loop.create_task(self.ready(self))
            return

    def data_received(self, data):
        print("Received " + data.decode())
        if self.recv_buffer is None:
            self.recv_buffer = data
        else:
            self.recv_buffer = self.recv_buffer + data
            print_time("Received " + self.recv_buffer.decode(), color)

        if self.waiter is not None:
            self.waiter.set_result(None)
        #print(self.recv_buffer)

    def eof_received(self):
        print_time("EOF received", color)
        self.at_eof = True

    def datagram_received(self, data, addr):
        if self.recv_buffer is None:
            self.recv_buffer = list()
        self.recv_buffer.append(data)
        print_time("Received " + data.decode() + " from OS", color)
        # print(self.recv_buffer)
        if self.waiter is not None:
            self.waiter.set_result(None)
    
    def error_received(self, err):
        if type(err) is ConnectionRefusedError:
            if self.connection_error:
                print_time("Connection Error occured.", color)
                self.loop.create_task(self.connection_error())
            return
    def connection_lost(self, exc):
        print_time("Conenction lost", "magenta")

    """ Tries to create a (TCP) connection to a remote endpoint
        If a local endpoint was specified on connection class creation,
        it will be used.
    """
    async def connect(self):
        # Resolve remote endpoint
        remote_info = await self.loop.getaddrinfo(self.remote_endpoint.address,
                                                  self.remote_endpoint.port)
        self.remote_endpoint.address = remote_info[0][4][0]
        # Attempt connection
        try:
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

    """ Tries to send the (string) stored in data
    """
    async def send_data(self, data, message_count):
        if self.protocol == 'tcp':
            print_time("Writing TCP data.", color)
            try:
                self.transport.write(data.encode())
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

        elif self.protocol == 'udp':
            print_time("Writing UDP data.", color)
            try:
                if self.active:
                    self.transport.sendto(data.encode())
                else:
                    self.handler.send_to(self, data.encode())
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
        return self.message_count

    async def await_data(self):
        while(True):
            if self.waiter is not None:
                await self.waiter
            else:
                break
        self.waiter = self.loop.create_future()
        try:
            await self.waiter
        finally:
            self.waiter = None

    async def read_buffer(self, max_length=-1):
        if not self.recv_buffer:
            await self.await_data()
        if self.message_based:
            if len(self.recv_buffer[0]) <= max_length or max_length == -1:
                data = self.recv_buffer.pop(0)
                return data
            elif len(self.recv_buffer[0]) > max_length:
                data = self.recv_buffer[0][:max_length]
                self.recv_buffer[0] = self.recv_buffer[0][max_length:]
                return data
        else:
            if max_length == -1 or len(self.recv_buffer) <= max_length:
                data = self.recv_buffer
                self.recv_buffer = None
                return data
            elif len(self.recv_buffer) > max_length:
                data = self.recv_buffer[:max_length]
                self.recv_buffer = self.recv_buffer[max_length:]
                return data

    """ Queues reception of a message
    """
    async def receive_message(self, min_incomplete_length,
                              max_length):
        print_time("Reading message", "red")
        #try:
        data = await self.read_buffer(max_length)
        if self.msg_buffer is None:
            self.msg_buffer = data.decode()
        else:
            self.msg_buffer = self.msg_buffer + data.decode()
        """except:
            print_time("Connection Error", color)
            if self.connection_error is not None:
                self.loop.create_task(self.connection_error(self))
            return"""
        if self.message_based or self.at_eof:
            print_time("Received full message", color)
            if self.received:
                self.loop.create_task(self.received(self.msg_buffer,
                                                    "Context", self))
                print_time("Called received cb.", color)
            self.msg_buffer = None
            return

        elif len(self.msg_buffer) >= min_incomplete_length:
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
        self.transport.close()
        print_time("Connection closed.", color)
        if self.closed:
            self.loop.create_task(self.closed())

    """ Wrapper function for close_connection,
        required to make close return immediately
    """
    def close(self):
        self.loop.create_task(self.close_connection())

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


class DatagramHandler(asyncio.Protocol):

    def __init__(self, preconnection):
        self.preconnection = preconnection
        self.remotes = dict()
        self.preconnection.handler = self

    def send_to(self, connection, data):
        remote_address = connection.remote_endpoint.address
        remote_port = connection.remote_endpoint.port
        self.transport.sendto(data, (remote_address, remote_port))

    def connection_made(self, transport):
        self.transport = transport
        return

    def datagram_received(self, data, addr):
        print("Receied new dgram")
        if addr in self.remotes:
            self.remotes[addr].datagram_received(data, addr)
            return
        new_connection = Connection(self.preconnection)
        new_remote_endpoint = RemoteEndpoint()
        print_time("Received new connection.", color)
        new_remote_endpoint.with_address(addr[0])
        new_remote_endpoint.with_port(addr[1])
        new_connection.remote_endpoint = new_remote_endpoint
        print_time("Created new connection object.", color)
        if new_connection.connection_received:
            new_connection.loop.create_task(
                new_connection.connection_received(new_connection))
            print_time("Called connection_received cb", color)
        new_connection.datagram_received(data, addr)
        self.remotes[addr] = new_connection
        return