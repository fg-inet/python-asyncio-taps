import asyncio
import json
import sys
import ssl
from .endpoint import LocalEndpoint, RemoteEndpoint
from .transportProperties import *
from .utility import *
from .transports import *
color = "green"


class Connection(asyncio.Protocol):
    """The TAPS connection class.

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
                self.loop = preconnection.loop
                self.active = preconnection.active
                self.protocol = preconnection.protocol
                self.framer = preconnection.framer

                # Waiter required to stop receive requests until data arrives
                # Current state of the connection object
                self.state = ConnectionState.ESTABLISHING
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
                self.open_receives = 0
                self.transports = []

                # Assign this connection to the preconnection and trigger
                # the conenction waiter
                if self.active:
                    preconnection.connection = self
                    if preconnection.waiter is not None:
                        preconnection.waiter.set_result(None)
                if self.protocol == "udp" and not self.active:
                    self.handler = preconnection.handler

    """ Function that blocks until new data has arrived
    """
    async def await_data(self):
        waiter = self.loop.create_future()
        self.waiters.append(waiter)
        try:
            await waiter
        finally:
            del self.waiters[0]

    def send_tcp(self, data, message_count):
        """ Send tcp data
        """
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

    async def send_message(self, data):
        """ Attempts to send data on the connection.
            Attributes:
                data (string, required):
                    Data to be send.
        """
        self.transports[0].message_count += 1
        self.loop.create_task(self.transports[0].process_send_data(data, self.transports[0].message_count))
        return self.transports[0].message_count

    async def read_buffer(self, max_length=-1):
        # If the buffer is empty, wait for new data
        if self.recv_buffer is None or len(self.recv_buffer) == 0:
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

    """ Receive handling if a framer is in use
    """
    async def receive_framed(self, min_incomplete_length,
                             max_length):
        print_time("Ready to read message with framer", color)
        # If the buffer is empty, wait for new data
        if not self.recv_buffer:
            await self.await_data()
        print_time("Deframing", color)
        self.open_receives += 1
        context, message, eom = await self.framer.handle_received(self)
        if self.received:
            self.loop.create_task(self.received(message,
                                                "Context", self))
        self.open_receives -= 1
        return

    async def receive_message(self, min_incomplete_length,
                              max_length):
        """ Queues reception of a message if there is no framer. Will block until
            message that is at least min_incomplete_length long is in the
            msg_buffer
        """
        print_time("Reading message", color)

        # Try to read data from the recv_buffer
        try:
            data = await self.read_buffer()
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

    async def receive(self, min_incomplete_length=float("inf"), max_length=-1):
        """ Queues the reception of a message.

        Attributes:
            min_incomplete_length (integer, optional):
                The minimum length an incomplete message
                needs to have.

            max_length (integer, optional):
                The maximum length a message can have.
        """
        if self.framer:
            self.loop.create_task(self.receive_framed(min_incomplete_length,
                                  max_length))
        else:
            self.loop.create_task(self.receive_message(min_incomplete_length,
                                  max_length))

    async def close_connection(self):
        """ Function wrapped by close
        """
        print_time("Closing connection.", color)
        self.transport.close()
        self.state = ConnectionState.CLOSED
        if self.closed:
            self.loop.create_task(self.closed())

    def close(self):
        """ Attempts to close the connection, issues a closed event
        on success.
        """
        self.loop.create_task(self.close_connection())
        self.state = ConnectionState.CLOSING

    # Events for active open
    def on_ready(self, callback):
        """ Set callback for ready events that get thrown once the connection is ready
        to send and receive data.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.ready = callback

    def on_initiate_error(self, callback):
        """ Set callback for initiate error events that get thrown if an error occurs
        during initiation.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.initiate_error = callback

    # Events for sending messages
    def on_sent(self, callback):
        """ Set callback for sent events that get thrown if a message has been
        succesfully sent.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.sent = callback

    def on_send_error(self, callback):
        """ Set callback for send error events that get thrown if an error occurs
        during sending of a message.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.send_error = callback

    def on_expired(self, callback):
        """ Set callback for expired events that get thrown if a message expires.

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
        """ Set callback for partial received events that get thrown if a new partial
        message has been received.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.received_partial = callback

    def on_receive_error(self, callback):
        """ Set callback for receive error events that get thrown if an error occurs
        during reception of a message.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.receive_error = callback

    def on_connection_error(self, callback):
        """ Set callback for connection error events that get thrown if an error occurs
        while the connection is open.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.connection_error = callback

    # Events for closing a connection
    def on_closed(self, callback):
        """ Set callback for on closed events that get thrown if the
        connection has been closed succesfully.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.closed = callback


class DatagramHandler(asyncio.Protocol):
    """ Class required to handle incoming datagram flows
    """
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
        print_time("New UDP flow", color)
        return

    def datagram_received(self, data, addr):
        print_time("Received new datagram", color)
        if addr in self.remotes:
            self.remotes[addr].datagram_received(data, addr)
            return
        new_connection = Connection(self.preconnection)
        new_connection.state = ConnectionState.ESTABLISHED
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
