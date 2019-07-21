import asyncio
import json
import sys
import ssl
from .endpoint import LocalEndpoint, RemoteEndpoint
from .transportProperties import *
from .utility import *
color = "white"


class TransportLayer(asyncio.Protocol):
    """ One possible transport for a TAPS connection
    """
    def __init__(self, connection,local_endpoint=None, remote_endpoint=None):
                self.local_endpoint = local_endpoint
                self.remote_endpoint = remote_endpoint
                self.connection = connection
                self.loop = connection.loop
                self.connection.transports.append(self)
                self.recv_buffer = None
                self.waiters = []
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
    def send(self, data, message_count):
        pass
    
    def receive():
        pass
    
    def close():
        pass

    async def process_send_data(self, data, message_count):
        """ Function responsible for sending data. It decides which
            protocol is used and then uses the appropriate functions
        """
        if self.connection.state is not ConnectionState.ESTABLISHED:
                print_time("SendError occured, connection is not established.",
                           color)
                if self.send_error:
                    self.loop.create_task(self.send_error(message_count))
                return
        # Frame the data
        if self.connection.framer:
            self.connection.framer.handle_new_sent_message(data, None, False)
        else:
            self.send(data, message_count)


class UdpTransport(TransportLayer):
    def send(self, data, message_count):
        """ Sends udp data
        """
        print_time("Writing UDP data.", color)
        try:
            # See if the udp flow was the result of passive or active open
            if self.connection.active:
                # Write the data
                self.transport.sendto(data.encode())
            else:
                # Delegate sending to the datagram handler
                self.handler.send_to(self, data.encode())
        except:
            print_time("SendError occured.", color)
            if self.connection.send_error:
                self.loop.create_task(self.connection.send_error(message_count))
            return
        print_time("Data written successfully.", color)
        if self.connection.sent:
            self.loop.create_task(self.connection.sent(message_count))
        return

    async def active_open(self, transport):
        self.transport = transport
        print_time("Connected successfully.", color)
        self.connection.state = ConnectionState.ESTABLISHED
        if self.connection.framer:
            # Send a start even to the framer and wait for a reply
            await self.connection.framer.handle_start(self)
        if self.connection.ready:
            self.loop.create_task(self.connection.ready(self))
        return

    # Asyncio Callbacks

    """ ASYNCIO function that gets called when a new
        connection has been made, similar to TAPS ready callback.
    """
    def connection_made(self, transport):
        # Check if its an incoming or outgoing connection
        if self.connection.active is False:
            self.transport = transport
            new_remote_endpoint = RemoteEndpoint()
            print_time("Received new connection.", color)
            # Get information about the newly connected endpoint
            new_remote_endpoint.with_address(
                                transport.get_extra_info("peername")[0])
            new_remote_endpoint.with_port(
                                transport.get_extra_info("peername")[1])
            self.remote_endpoint = new_remote_endpoint
            self.connection.state = ConnectionState.ESTABLISHED
            if self.connection.connection_received:
                self.loop.create_task(self.connection_received(self))
            return

        elif self.connection.active:
            self.loop.create_task(self.active_open(transport))

    """ ASYNCIO function that gets called when EOF is received
    """
    def eof_received(self):
        print_time("EOF received", color)
        self.connection.at_eof = True
    """ ASYNCIO function that gets called when a new datagram
        is received. It stores the datagram in the recv_buffer
    """
    def datagram_received(self, data, addr):
        if self.recv_buffer is None:
            self.recv_buffer = list()
        self.recv_buffer.append(data)
        print_time("Received " + data.decode(), color)
        for i in range(self.open_receives):
            self.loop.create_task(self.framer.handle_received_data(self))
        if self.framer:
            for i in self.waiters:
                i.set_result(None)
        for w in self.waiters:
            if not w.done():
                w.set_result(None)
                return
    """ ASYNCIO function that gets called when the connection has
        an error.
        TODO: proper error handling
    """
    def error_received(self, err):
        if type(err) is ConnectionRefusedError:
            print_time("Connection Error occured.", color)
            print(err)
            if self.connection.connection_error:
                self.loop.create_task(self.connection.connection_error())
            return

    """ ASNYCIO function that gets called when the connection
        is lost
    """
    def connection_lost(self, exc):
        print_time("Connection lost", color)
