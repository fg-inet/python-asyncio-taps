import asyncio

from .endpoint import RemoteEndpoint
from .framer import DeframingFailed
from .message import MessageContext
from .utility import ConnectionState, setup_logger

try:
    import mctx_core
except ImportError:
    mctx_core = None

logger = setup_logger(__name__, "blue")


def _require_mctx_core():
    if mctx_core is None:
        raise ImportError(
            "Multicast send support requires the optional 'mctx-core-py' package."
        )

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
        self.current_message_context = None

    def _new_message_context(self, *, end_of_message=True, framer_context=None):
        context = MessageContext(
            end_of_message=end_of_message,
            framer_context=framer_context,
        )
        if self.remote_endpoint and self.remote_endpoint.address:
            context.remote_address = self.remote_endpoint.address[0]
        if self.remote_endpoint and self.remote_endpoint.port:
            context.remote_port = self.remote_endpoint.port
        if self.local_endpoint and self.local_endpoint.address:
            context.local_address = self.local_endpoint.address[0]
        if self.local_endpoint and self.local_endpoint.port:
            context.local_port = self.local_endpoint.port
        return context

    def _coerce_message_context(self, message_context=None, *, end_of_message=True):
        if message_context is None:
            return self._new_message_context(end_of_message=end_of_message)
        message_context.end_of_message = end_of_message
        if (
            message_context.remote_address is None
            and self.remote_endpoint
            and self.remote_endpoint.address
        ):
            message_context.remote_address = self.remote_endpoint.address[0]
        if (
            message_context.remote_port is None
            and self.remote_endpoint
            and self.remote_endpoint.port
        ):
            message_context.remote_port = self.remote_endpoint.port
        if (
            message_context.local_address is None
            and self.local_endpoint
            and self.local_endpoint.address
        ):
            message_context.local_address = self.local_endpoint.address[0]
        if (
            message_context.local_port is None
            and self.local_endpoint
            and self.local_endpoint.port
        ):
            message_context.local_port = self.local_endpoint.port
        return message_context

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
            self.connection._report_receive_error(
                self.current_message_context,
                DeframingFailed("Framer could not parse received data"),
            )
            self.active_framer.set_result(None)
            self.active_framer = None
            return
        # If a message was deframed successful, modify the recv buffer,
        #  add the message to the framer buffer
        self.recv_buffer = self.recv_buffer[length:]
        if not isinstance(ctx, MessageContext):
            ctx = self._new_message_context(
                end_of_message=eom,
                framer_context=ctx,
            )
        else:
            ctx.end_of_message = eom
        self.framer_buffer.append((msg, ctx, eom))
        self.active_framer.set_result(None)
        self.active_framer = None
        for w in self.waiters:
            if not w.done():
                w.set_result(None)
                return
        # Since there might be another message that is able to be deframed,
        #  invoke the framer again
        self.loop.create_task(self.invoke_framer())

    def send(self, data, message_context=None, end_of_message=True):
        """ Function responsible for sending data.
        """
        self.message_count += 1
        if self.connection.state != ConnectionState.ESTABLISHED:
            logger.warning("SendError occurred, connection is not established.")
            if self.connection.send_error:
                self.loop.create_task(
                    self.connection.send_error(self.message_count, self.connection)
                )
            return
        context = self._coerce_message_context(
            message_context,
            end_of_message=end_of_message,
        )
        if context.message_id is None:
            context.message_id = self.message_count
        if context.is_expired():
            self.connection._report_expired(context)
            return self.message_count
        self.loop.create_task(self._write_with_expiration(data, context, end_of_message))
        return self.message_count

    async def _write_with_expiration(self, data, message_context, end_of_message):
        if message_context.is_expired():
            self.connection._report_expired(message_context)
            return
        await self.write(data, message_context, end_of_message)

    async def write(self, data, message_context, end_of_message):
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
        self.at_eof = True
        if self.current_message_context is None:
            self.current_message_context = self._new_message_context(
                end_of_message=True
            )
        else:
            self.current_message_context.end_of_message = True
        for waiter in self.waiters:
            if not waiter.done():
                waiter.set_result(None)

    """ ASYNCIO function that gets called when the connection has
        an error.
        TODO: proper error handling
    """

    def error_received(self, err):
        if type(err) is ConnectionRefusedError:
            logger.warning("Connection Error occurred.")
            self.connection._report_connection_error(err)
            return

    """ ASYNCIO function that gets called when the connection
        is lost
    """

    def connection_lost(self, exc):
        if self.recv_buffer and self.current_message_context is not None and not self.at_eof:
            self.connection._report_receive_error(
                self.current_message_context,
                exc or ConnectionError("Receive terminated before the current message completed"),
            )
        if exc is None:
            logger.warning("Connection lost without error.")
            if self.connection.state == ConnectionState.CLOSING:
                self.connection._report_closed()
            else:
                self.connection._mark_closed()
        else:
            logger.warning("Connection lost with error.")
            self.connection._report_connection_error(exc)

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
        sockname = transport.get_extra_info("sockname")
        if sockname:
            self.connection.note_path_change(
                local_address=sockname[0],
                local_port=sockname[1],
                remote_address=new_remote_endpoint.address[0],
                remote_port=new_remote_endpoint.port,
            )
        self.connection._mark_ready()
        if hasattr(self.connection._originating_preconnection, "_deliver_connection"):
            self.connection._originating_preconnection._deliver_connection(self.connection)
        return


class UdpTransport(TransportLayer):

    async def active_open(self, transport):
        # If there is a framer, call the start event
        if self.connection.framer:
            await self.connection.framer.handle_start(self.connection)
        self.transport = transport
        logger.info("Connected successfully UDP to " +
                    str(self.connection.remote_endpoint.address) +
                    ":" + str(self.connection.remote_endpoint.port) +
                    ".")
        sockname = transport.get_extra_info("sockname") if transport else None
        if sockname:
            self.connection.note_path_change(
                local_address=sockname[0],
                local_port=sockname[1],
                remote_address=self.connection.remote_endpoint.address[0],
                remote_port=self.connection.remote_endpoint.port,
            )
        self.connection._mark_ready()
        if self.connection._pending_message:
            data, context, eom = self.connection._pending_message
            self.connection._pending_message = None
            if context.is_expired():
                self.connection._report_expired(context)
            else:
                await self.write(data, context, eom)
        return

    async def write(self, data, message_context, end_of_message):
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
                        handle_new_sent_message(
                            data, message_context, end_of_message
                        )
                # Write the data
                self.transport.sendto(data)
            else:
                remote_address = self.remote_endpoint.address[0]
                remote_port = self.remote_endpoint.port
                self.transport.sendto(data, (remote_address, remote_port))
        except InterruptedError:
            logger.warning("SendError occurred.")
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
        self.connection._report_closed()

    async def read(self, min_incomplete_length, max_length):
        if self.connection.framer:
            if len(self.framer_buffer) == 0:
                await self.await_data()
            data, context, _ = self.framer_buffer.pop(0)
        else:
            if self.recv_buffer is None:
                await self.await_data()
            if len(self.recv_buffer) == 1:
                data, context = self.recv_buffer[0]
                self.recv_buffer = None
            else:
                data, context = self.recv_buffer.pop(0)
        self.connection._deliver_received(data, context)

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
        context = self._new_message_context(end_of_message=True)
        context.addr = addr
        self.current_message_context = context
        if self.recv_buffer is None:
            self.recv_buffer = list()
        self.recv_buffer.append((data, context))

        if self.connection.framer:
            self.loop.create_task(self.invoke_framer())
            return
        else:
            for w in self.waiters:
                if not w.done():
                    w.set_result(None)
                    return


class MulticastSendTransport(TransportLayer):
    def __init__(self, connection, local_endpoint=None, remote_endpoint=None):
        super().__init__(connection, local_endpoint, remote_endpoint)
        self.message_based = True
        self.mctx_context = None
        self.publication = None
        self.async_publication = None

    async def active_open(self, transport):
        _require_mctx_core()
        if self.connection.framer:
            await self.connection.framer.handle_start(self.connection)

        source = None
        source_port = None
        if self.local_endpoint and self.local_endpoint.address:
            source = self.local_endpoint.address[0]
        if self.local_endpoint and self.local_endpoint.port:
            source_port = self.local_endpoint.port

        interface = getattr(
            self.connection._originating_preconnection,
            "multicast_interface_address",
            None,
        )
        ttl = getattr(
            self.connection._originating_preconnection,
            "multicast_ttl",
            1,
        )
        loopback = not getattr(
            self.connection._originating_preconnection,
            "multicast_disable_loopback",
            False,
        )

        self.mctx_context = mctx_core.Context()
        self.publication = self.mctx_context.add_publication(
            self.remote_endpoint.address[0],
            self.remote_endpoint.port,
            source=source,
            source_port=source_port,
            interface=interface,
            ttl=ttl,
            loopback=loopback,
        )
        self.async_publication = mctx_core.AsyncPublication(
            self.publication,
            loop=self.loop,
        )
        self.connection.multicast_open = True

        try:
            local_addr = self.publication.local_addr()
        except Exception:
            local_addr = None
        if local_addr:
            self.connection.note_path_change(
                local_address=local_addr[0],
                local_port=local_addr[1],
                remote_address=self.remote_endpoint.address[0],
                remote_port=self.remote_endpoint.port,
            )

        logger.info(
            "Connected multicast sender to %s:%s.",
            self.remote_endpoint.address[0],
            self.remote_endpoint.port,
        )
        self.connection._mark_ready()
        if self.connection._pending_message:
            data, context, eom = self.connection._pending_message
            self.connection._pending_message = None
            if context.is_expired():
                self.connection._report_expired(context)
            else:
                await self.write(data, context, eom)

    async def write(self, data, message_context, end_of_message):
        if isinstance(data, str):
            data = data.encode()
        try:
            if self.connection.framer:
                data = await self.connection.framer.handle_new_sent_message(
                    data,
                    message_context,
                    end_of_message,
                )
            report = await self.async_publication.send(data)
        except InterruptedError:
            logger.warning("SendError occurred.")
            if self.connection.send_error:
                self.loop.create_task(
                    self.connection.send_error(
                        self.message_count,
                        self.connection,
                    )
                )
            return

        if report.local_addr:
            message_context.local_address = report.local_addr[0]
            message_context.local_port = report.local_addr[1]
        if report.source_addr:
            message_context.local_address = report.source_addr
        logger.info("Multicast packet written successfully.")
        if self.connection.sent:
            self.loop.create_task(
                self.connection.sent(self.message_count, self.connection)
            )

    async def close(self):
        logger.info("Closing multicast sender.")
        if self.publication is not None:
            self.publication.remove()
        self.publication = None
        self.async_publication = None
        self.mctx_context = None
        self.connection._report_closed()

    async def read(self, min_incomplete_length, max_length):
        raise RuntimeError("Multicast sender transports do not support receive().")


class TcpTransport(TransportLayer):

    async def active_open(self, transport):
        # If there is a framer, call the start event
        if self.connection.framer:
            await self.connection.framer.handle_start(self.connection)
        self.transport = transport
        logger.info("Connected successfully on TCP.")
        sockname = transport.get_extra_info("sockname")
        if sockname:
            self.connection.note_path_change(
                local_address=sockname[0],
                local_port=sockname[1],
                remote_address=self.connection.remote_endpoint.address[0],
                remote_port=self.connection.remote_endpoint.port,
            )
        self.connection._mark_ready()
        if self.connection._pending_message:
            data, context, eom = self.connection._pending_message
            self.connection._pending_message = None
            if context.is_expired():
                self.connection._report_expired(context)
            else:
                await self.write(data, context, eom)
        return

    async def write(self, data, message_context, end_of_message):
        """ Send tcp data
        """
        logger.info("Writing TCP data.")
        if isinstance(data, str):
            data = data.encode()
        try:

            # Frame the data
            if self.connection.framer:
                data = await self.connection.framer. \
                    handle_new_sent_message(
                        data, message_context, end_of_message
                    )
            # Attempt to write data
            self.transport.write(data)
        except InterruptedError:
            logger.warning("SendError occurred.")
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
            data, context, _ = self.framer_buffer.pop(0)
            self.connection._deliver_received(data, context)
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

        if self.current_message_context is None:
            self.current_message_context = self._new_message_context(
                end_of_message=self.at_eof
            )

        context = self.current_message_context
        context.end_of_message = self.at_eof

        if self.at_eof:
            context.final = True
            self.connection._deliver_received(data, context)
            self.connection._received_final_message = True
            self.current_message_context = None
            return

        self.connection._deliver_received_partial(data, context)

    async def close(self):
        logger.info("Closing connection.")
        self.transport.close()
        self.connection._report_closed()

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
        if self.current_message_context is None:
            self.current_message_context = self._new_message_context(
                end_of_message=False
            )
        else:
            self.current_message_context.end_of_message = False

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
