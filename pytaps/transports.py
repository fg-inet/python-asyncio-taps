from .endpoint import RemoteEndpoint
from .framer import *

logger = setup_logger(__name__, "blue")


class MessageContext(object):
    def __init__(self):
        self.addr = None


class TransportLayer(asyncio.Protocol):
    """ One possible underlying transport for a TAPS connection

        Attributes:
        connection (Connection, required):
                Connection object to which this
                Transport will be attached.
        local_endpoint (LocalEndpoint, optional):
                        LocalEndpoint
        remote_endpoint (RemoteEndpoint, optional):
                        RemoteEndpoint
    """

    def __init__(self, connection, local_endpoint=None, remote_endpoint=None):
        self.local_endpoint = local_endpoint
        self.remote_endpoint = remote_endpoint
        self.connection = connection
        self.loop = connection.loop
        self.connection.transports.append(self)
        self.waiters = []
        self.open_receives = 0
        # Keeping track of how many messages have been sent for msgref
        self.message_count = 0
        # Determines if the protocol is message based or not (needed?)
        self.message_based = True
        # Reception buffer, holding data returned from the OS
        self.recv_buffer = None
        # Boolean to indicate that EOF has been reached
        self.at_eof = False

        # If we have a framer, create a buffer for deframed messages
        if connection.framer:
            self.active_framer = None
            self.framer_buffer = []

        self.transport = None

    """ Function that blocks until new data has arrived
    """

    async def await_data(self):
        waiter = self.loop.create_future()
        self.waiters.append(waiter)
        try:
            await waiter
        finally:
            del self.waiters[0]

    """ Function that blocks until a framer has finished deframing
    """

    async def await_framer(self):
        self.active_framer = self.loop.create_future()
        try:
            await self.active_framer
        finally:
            self.active_framer = None

    """ Invokes the framer to deframe newly arrived data
    """

    async def invoke_framer(self):
        # If there is already another deframing in progress,
        #  wait for it to complete
        if self.active_framer:
            await self.active_framer
        self.active_framer = self.loop.create_future()
        # Try to call the deframing function implemented
        # by the individual framer
        try:
            ctx, msg, length, eom = await \
                self.connection.framer.handle_received_data(self.connection)
        except (DeframingFailed, ValueError, TypeError):
            # If the framer throws an DeframingFailed Error, stop trying
            # to deframe until new data arrives
            self.active_framer.set_result(None)
            self.active_framer = None
            return
        # If a message was deframed successful, modify the recv buffer,
        #  add the message to the framer buffer
        self.recv_buffer = self.recv_buffer[length:]
        self.framer_buffer.append(msg)
        self.active_framer.set_result(None)
        self.active_framer = None
        for w in self.waiters:
            if not w.done():
                w.set_result(None)
                return
        # Since there might be another message that is able to be deframed,
        #  invoke the framer again
        self.loop.create_task(self.invoke_framer())

    def send(self, data):
        """ Function responsible for sending data.
        """
        self.message_count += 1
        if self.connection.state is not ConnectionState.ESTABLISHED:
            logger.warn("SendError occurred, connection is not established.")
            if self.connection.send_error:
                self.loop.create_task(
                    self.connection.send_error(self.message_count, self.connection)
                )
            return
        self.loop.create_task(self.write(data))
        return self.message_count

    async def write(self, data):
        pass

    def receive(self, min_incomplete_length, max_length):
        """if self.connection.framer:
            self.loop.create_task(self.read_framed(min_incomplete_length,
                                  max_length))
        else:"""
        self.loop.create_task(self.read(min_incomplete_length,
                                        max_length))

    async def read(self, min_incomplete_length,
                   max_length):
        pass

    async def close(self):
        pass

    """ ASYNCIO function that gets called when EOF is received
    """

    def eof_received(self):
        logger.info("EOF received")
        self.connection.at_eof = True

    """ ASYNCIO function that gets called when the connection has
        an error.
        TODO: proper error handling
    """

    def error_received(self, err):
        if type(err) is ConnectionRefusedError:
            logger.warn("Connection Error occurred.")
            if self.connection.connection_error:
                self.loop.create_task(
                    self.connection.connection_error(err, self.connection)
                )
            return

    """ ASYNCIO function that gets called when the connection
        is lost
    """

    def connection_lost(self, exc):
        if exc is None:
            logger.warn("Connection lost without error.")
            if self.connection.closed and self.connection.state != ConnectionState.CLOSED:
                self.loop.create_task(self.connection.closed(self.connection))
        else:
            logger.warn("Connection lost with error.")
            if self.connection.connection_error:
                self.loop.create_task(
                    self.connection.connection_error(exc, self.connection)
                )

    async def passive_open(self, transport):
        # If there is a framer, call the start event
        if self.connection.framer:
            await self.connection.framer.handle_start(self.connection)
        self.transport = transport
        new_remote_endpoint = RemoteEndpoint()
        logger.info("Received new connection.")
        # Get information about the newly connected endpoint
        new_remote_endpoint.with_address(
            transport.get_extra_info("peername")[0])
        new_remote_endpoint.with_port(
            transport.get_extra_info("peername")[1])
        self.remote_endpoint = new_remote_endpoint
        self.connection.state = ConnectionState.ESTABLISHED
        if self.connection.connection_received:
            self.loop.create_task(self.connection.connection_received(self))
        return


class UdpTransport(TransportLayer):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.context = MessageContext()

    async def active_open(self, transport):
        # If there is a framer, call the start event
        if self.connection.framer:
            await self.connection.framer.handle_start(self.connection)
        self.transport = transport
        for t in self.connection.pending:
            t.cancel()
        logger.info("Connected successfully UDP to " +
                    str(self.connection.remote_endpoint.address) +
                    ":" + str(self.connection.remote_endpoint.port) +
                    ".")
        self.connection.state = ConnectionState.ESTABLISHED
        if self.connection.ready:
            self.loop.create_task(self.connection.ready(self.connection))
        return

    async def write(self, data):
        """ Sends udp data
        """
        logger.info("Writing UDP data to " +
                    str(self.connection.remote_endpoint.address[0]) +
                    ":" + str(self.connection.remote_endpoint.port) +
                    ".")
        if isinstance(data, str):
            data = data.encode()
        try:
            # See if the udp flow was the result of passive or active open
            if self.connection.active:
                # Frame the data
                if self.connection.framer:
                    data = await self.connection.framer. \
                        handle_new_sent_message(data, None, False)
                # Write the data
                self.transport.sendto(data)
            else:
                remote_address = self.remote_endpoint.address[0]
                remote_port = self.remote_endpoint.port
                self.transport.sendto(data, (remote_address, remote_port))
        except InterruptedError:
            logger.warn("SendError occurred.")
            if self.connection.send_error:
                self.loop.create_task(
                    self.connection.send_error(
                        self.message_count, self.connection
                    )
                )
            return
        logger.info("Data written successfully.")
        if self.connection.sent:
            self.loop.create_task(
                self.connection.sent(self.message_count, self.connection)
            )
        return

    async def close(self):
        logger.info("Closing connection.")
        self.transport.close()
        self.connection.state = ConnectionState.CLOSED
        if self.connection.closed:
            self.loop.create_task(self.connection.closed(self.connection))

    async def read(self, min_incomplete_length, max_length):
        if self.connection.framer:
            if len(self.framer_buffer) == 0:
                await self.await_data()
            data = self.framer_buffer.pop(0)
        else:
            if self.recv_buffer is None:
                await self.await_data()
            if len(self.recv_buffer) == 1:
                data = self.recv_buffer[0]
                self.recv_buffer = None
            else:
                data = self.recv_buffer.pop(0)
        if self.connection.received:
            self.loop.create_task(self.connection.received(data,
                                                           self.context, self.connection))

    # Asyncio Callbacks

    """ ASYNCIO function that gets called when a new
        connection has been made, similar to TAPS ready callback.
    """

    def connection_made(self, transport):
        if self.connection.state == ConnectionState.ESTABLISHED:
            transport.close()
            return

        # Check if its an incoming or outgoing connection
        if self.connection.active:
            self.loop.create_task(self.active_open(transport))
        else:
            self.loop.create_task(self.passive_open(transport))
            # Stub code for forcefully killing connection tasks
            # Before establishment to the peer has been completed
            """
            for t in self.connection.transports:
                if t != self:
                    self.connection.transports.remove(t)

            for t in self.connection.pending.keys():
                if self.connection.pending[t] != self:
                    print(self.connection.pending[t])
                    print(t)
                    t.cancel()"""

    """ ASYNCIO function that gets called when a new datagram
        is received. It stores the datagram in the recv_buffer
    """

    def datagram_received(self, data, addr):
        self.context.addr = addr
        if self.recv_buffer is None:
            self.recv_buffer = list()
        self.recv_buffer.append(data)

        if self.connection.framer:
            self.loop.create_task(self.invoke_framer())
            return
        else:
            for w in self.waiters:
                if not w.done():
                    w.set_result(None)
                    return


class TcpTransport(TransportLayer):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.context = MessageContext()

    async def active_open(self, transport):
        # If there is a framer, call the start event
        if self.connection.framer:
            await self.connection.framer.handle_start(self.connection)
        self.transport = transport
        logger.info("Connected successfully on TCP.")
        self.connection.state = ConnectionState.ESTABLISHED
        self.connection.sleeper_for_racing.cancel_all()
        if self.connection.ready:
            self.loop.create_task(self.connection.ready(self.connection))
        return

    async def write(self, data):
        """ Send tcp data
        """
        logger.info("Writing TCP data.")
        if isinstance(data, str):
            data = data.encode()
        try:

            # Frame the data
            if self.connection.framer:
                data = await self.connection.framer. \
                    handle_new_sent_message(data, None, False)
            # Attempt to write data
            self.transport.write(data)
        except InterruptedError:
            logger.warn("SendError occurred.")
            if self.connection.send_error:
                self.loop.create_task(
                    self.connection.send_error(self.message_count, self.connection)
                )
            return
        logger.info("Data written successfully.")
        if self.connection.sent:
            self.loop.create_task(
                self.connection.sent(self.message_count, self.connection)
            )
        return

    async def read(self, min_incomplete_length, max_length):
        # print_time("Reading message", color)
        if self.connection.framer:
            if len(self.framer_buffer) == 0:
                await self.await_data()
            data = self.framer_buffer.pop(0)
            if self.connection.received:
                self.loop.create_task(
                    self.connection.received(data, "Context", self.connection)
                )
            return

        while self.recv_buffer is None or (
                len(self.recv_buffer) < min_incomplete_length):
            await self.await_data()
        if max_length == -1 or len(self.recv_buffer) <= max_length:
            data = self.recv_buffer
            self.recv_buffer = None
        else:
            data = self.recv_buffer[:max_length]
            self.recv_buffer = self.recv_buffer[max_length:]

        if self.at_eof:
            if self.connection.received:
                self.loop.create_task(self.connection.received(data,
                                                               self.context, self.connection))
            return
        else:
            if self.connection.received_partial:
                self.loop.create_task(self.connection.received_partial(data,
                                                                       self.context, False, self))

    async def close(self):
        logger.info("Closing connection.")
        self.transport.close()
        self.connection.state = ConnectionState.CLOSED
        if self.connection.closed:
            self.loop.create_task(self.connection.closed(self.connection))

    # Asyncio Callbacks

    """ ASYNCIO function that gets called when a new
        connection has been made, similar to TAPS ready callback.
    """

    def connection_made(self, transport):
        if self.connection.state == ConnectionState.ESTABLISHED:
            transport.close()
            return

        # Check if its an incoming or outgoing connection
        if self.connection.active:
            self.loop.create_task(self.active_open(transport))
        else:
            self.loop.create_task(self.passive_open(transport))

            # Stub code for forcefully killing connection tasks
            # Before establishment to the peer has been completed
            """
            for t in self.connection.transports:
                if t != self:
                    self.connection.transports.remove(t)

            for t in self.connection.pending:
                if t != asyncio.current_task() and not t.done():
                    print(t)
                    print(asyncio.current_task())
                    t.cancel() """

    """ ASYNCIO function that gets called when new data is made available
        by the OS. Stores new data in buffer and triggers the receive waiter
    """

    def data_received(self, data):
        logger.info("Received %d bytes" % len(data))

        # See if we already have so data buffered
        if self.recv_buffer is None:
            self.recv_buffer = data
        else:
            self.recv_buffer = self.recv_buffer + data
        if self.connection.framer:
            self.loop.create_task(self.invoke_framer())
            return
        else:
            for w in self.waiters:
                if not w.done():
                    w.set_result(None)
                    return
