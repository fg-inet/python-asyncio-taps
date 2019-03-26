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
        preconnection (:obj:'preconnection'):
                Preconnection object that was involved in
                creating this connection object
    """
    def __init__(self, preconnection):
                # Initializations
                self.local_endpoint = preconnection.local_endpoint
                self.remote_endpoint = preconnection.remote_endpoint
                self.transport_properties = preconnection.transport_properties
                self.loop = preconnection.loop
                self.active = preconnection.active
                self.protocol = preconnection.protocol

                # Keeping track of how many messages have been sent for msgref
                self.message_count = 0
                # Determines if the protocol is message based or not (needed?)
                self.message_based = True
                # Message buffer, probably no longer required
                self.msg_buffer = None
                # Reception buffer, holding data returned from the OS
                self.recv_buffer = None
                # Boolean to indicate that EOF has been reached
                self.at_eof = False
                # Waiter required to stop receive requests until data arrives
                self.waiter = None

                # Callbacks
                self.ready = preconnection.ready
                self.initiate_error = preconnection.initiate_error
                self.connection_received = preconnection.connection_received
                self.listen_error = preconnection.listen_error
                self.stopped = preconnection.stopped
                self.sent = None
                self.send_error = None
                self.expired = None
                self.connection_error = None
                self.received = None
                self.received_partial = None
                self.receive_error = None
                self.closed = None
                self.reader = None
                self.writer = None

                # Decide if protocol is message based
                if self.protocol == 'tcp':
                    self.message_based = False
                elif self.protocol == 'udp':
                    self.message_based = True

                # Assign this connection to the preconnection and trigger
                # the conenction waiter
                if self.active:
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

    """ ASYNCIO function that gets called when a new
        connection has been made, similar to TAPS ready callback.
    """
    def connection_made(self, transport):
        # Check if its an incoming or outgoing connection
        if self.active is False:
            self.transport = transport
            new_remote_endpoint = RemoteEndpoint()
            print_time("Received new connection.", color)
            # Get information about the newly connected endpoint
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
    """ ASYNCIO function that gets called when new data is made available
        by the OS. Stores new data in buffer and triggers the receive waiter
    """
    def data_received(self, data):
        print_time("Received " + data.decode(), color)

        # See if we already have so data buffered
        if self.recv_buffer is None:
            self.recv_buffer = data
        else:
            self.recv_buffer = self.recv_buffer + data

        # If there is already a receive queued by the connection,
        # trigger its waiter to let it know new data has arrived
        if self.waiter is not None:
            self.waiter.set_result(None)
    """ ASYNCIO function that gets called when EOF is received
    """
    def eof_received(self):
        print_time("EOF received", color)
        self.at_eof = True
    """ ASYNCIO function that gets called when a new datagram
        is received. It stores the datagram in the recv_buffer
    """
    def datagram_received(self, data, addr):
        if self.recv_buffer is None:
            self.recv_buffer = list()
        self.recv_buffer.append(data)
        print_time("Received " + data.decode(), color)
        if self.waiter is not None:
            self.waiter.set_result(None)
    """ ASYNCIO function that gets called when the connection has
        an error.
        TODO: proper error handling
    """
    def error_received(self, err):
        if type(err) is ConnectionRefusedError:
            print_time("Connection Error occured.", color)
            if self.connection_error:
                self.loop.create_task(self.connection_error())
            return

    """ ASNYCIO function that gets called when the connection
        is lost
    """
    def connection_lost(self, exc):
        print_time("Conenction lost", color)

    """ Function responsible for sending data. It decides which
        protocol is used and then uses the appropriate functions
    """
    async def send_data(self, data, message_count):
        # Check what protocol we are using
        if self.protocol == 'tcp':
            print_time("Writing TCP data.", color)
            try:
                # Attempt to write data
                self.transport.write(data.encode())
            except:
                print_time("SendError occured.", color)
                if self.send_error:
                    self.loop.create_task(self.send_error(message_count))
                return
            print_time("Data written successfully.", color)
            if self.sent:
                self.loop.create_task(self.sent(message_count))
            return

        elif self.protocol == 'udp':
            print_time("Writing UDP data.", color)
            try:
                # See if the udp flow was the result of passive or active open
                if self.active:
                    # Write the data
                    self.transport.sendto(data.encode())
                else:
                    # Delegate sending to the datagram handler
                    self.handler.send_to(self, data.encode())
            except:
                print_time("SendError occured.", color)
                if self.send_error:
                    self.loop.create_task(self.send_error(message_count))
                return
            print_time("Data written successfully.", color)
            if self.sent:
                self.loop.create_task(self.sent(message_count))
            return

    """ Wrapper function that assigns MsgRef
        and then calls async helper function
        to send a message
    """
    async def send_message(self, data):
        self.message_count += 1
        self.loop.create_task(self.send_data(data, self.message_count))
        return self.message_count

    """ Function that blocks until new data has arrived
    """
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

    """ Function that reads and manages the buffer and returns data
    """
    async def read_buffer(self, max_length=-1):
        # If the buffer is empty, wait for new data
        if not self.recv_buffer:
            await self.await_data()
        # See if we have to deal datagrams or stream data
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

    """ Queues reception of a message. Will block until message that
        is at least min_incomplete_length long is in the msg_buffer
    """
    async def receive_message(self, min_incomplete_length,
                              max_length):
        print_time("Reading message", color)

        # Try to read data from the recv_buffer
        try:
            data = await self.read_buffer(max_length)
            if self.msg_buffer is None:
                self.msg_buffer = data.decode()
            else:
                self.msg_buffer = self.msg_buffer + data.decode()
        except:
            print_time("Connection Error", color)
            if self.connection_error is not None:
                self.loop.create_task(self.connection_error(self))
            return

        # If we are at EOF or message based, issue a full message received cb
        if self.message_based or self.at_eof:
            print_time("Received full message", color)
            if self.received:
                self.loop.create_task(self.received(self.msg_buffer,
                                                    "Context", self))
            self.msg_buffer = None
            return

        else:
            # Wait until message is equal or longer than min_incomplete length
            while(len(self.msg_buffer) < min_incomplete_length):
                data = await self.read_buffer(max_length)
                self.msg_buffer = self.msg_buffer + data.decode()

            print_time("Received partial message.", color)
            if self.received_partial:
                self.loop.create_task(self.received_partial(self.msg_buffer,
                                      "Context", False, self))
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

""" Class required to handle incoming datagram flows
"""
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