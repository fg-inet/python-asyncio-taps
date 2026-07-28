import asyncio

class DeframingFailed(Exception):
    pass


class Framer:
    """The TAPS Framer class.

    Attributes:
        event_loop (eventLoop, optional):
                        Event loop on which all coroutines and callbacks
                        will be scheduled, if none if given the
                        one of the current thread is used by default
    """

    def __init__(self, event_loop=None):
        if event_loop is not None:
            self.loop = event_loop
        else:
            try:
                self.loop = asyncio.get_running_loop()
            except RuntimeError:
                self.loop = asyncio.get_event_loop()
        self.fail_connection = None
        self.connection = None

    """ Empty functions, to be implemented by the framer implementation
    """

    async def start(self, connection):
        """ Function that gets called when a new connection
            has been created by the TAPS system. The framer
            implementation should execute any code required
            during connection establishment here.

        Args:
            connection (connection, required):
                The connection object that is to be
                handled by the framer.
        """
        raise NotImplementedError

    async def new_sent_message(self, data, context, eom):
        """ Function that gets called when a new message
            has been queued for sending by the application.
            The framer should frame the message and then call
            the send() function on itself.

        Args:
            data (string, required):
                The data that is to be framed.
            context (context, required):
                The message context.
            eom (boolean, required):
                Marks whether or not this was marked
                as end of message by the application.
        """
        raise NotImplementedError

    async def handle_received_data(self, connection):
        """ Function that gets called when a new message
            has arrived on the connection. The framer should
            call parse() to get access to the buffer. After it
            has sufficient data to deframe a message, it should
            either call advance_receive_cursor() and deliver() or
            deliver_and_advance_receive_cursor() which combines
            both functions.

        Args:
            connection (connection, required):
                The connection object on which new data has
                arrived.
        """
        raise NotImplementedError

    async def stop(self):
        pass

    # Fire start event and the wait until framer replies
    async def handle_start(self, connection):
        self.connection = connection
        await self.start(connection)
        return

    # Fire new_message_sent event and wait for reply of framer
    async def handle_new_sent_message(self, data, context, eom):
        data = await self.new_sent_message(data, context, eom)
        return data

    def fail_connection(self, error):
        return

    def prepend_protocol(self, framer):
        return

    def parse(self, connection, min_incomplete_length=0, max_length=0):
        return connection.parse(min_incomplete_length, max_length)

    def advance_receive_cursor(self, connection, length):
        if connection.transports[0].recv_buffer is None:
            return
        connection.transports[0].recv_buffer = connection.transports[0].recv_buffer[length:]

    def deliver(self, connection, context, data, eom=True):
        context.end_of_message = eom
        return connection._deliver_received(data, context)

    def deliver_and_advance_receive_cursor(self, connection, context, data, length, eom=True):
        self.advance_receive_cursor(connection, length)
        return self.deliver(connection, context, data, eom)

    async def send(self, data, context=None, end_of_message=True):
        """ Should be called with framed data after a
            new_sent_message() event.

        Args:
            data (string, required):
                The framed message.
        """
        return await self.connection.send(data, context, end_of_message)
