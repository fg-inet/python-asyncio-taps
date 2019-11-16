import asyncio
from .utility import *
color = "magenta"


class DeframingFailed(Exception):
    pass


class Framer():
    """The TAPS Framer class.

    Attributes:
        eventLoop (eventLoop, optional):
                        Event loop on which all coroutines and callbacks
                        will be scheduled, if none if given the
                        one of the current thread is used by default
    """

    def __init__(self, event_loop=asyncio.get_event_loop()):
        self.loop = event_loop
        self.fail_connection = None

    """ Empty functions, to be implemented by the framer implementation
    """
    async def start(self, connection):
        """ Function that gets called when a new connection
            has been created by the TAPS system. The framer
            implementation should execute any code required
            during connection establishment here.

        Attributes:
            connection (connection, required):
                The connection object that is to be
                handled by the framer.
        """
        pass
    async def new_sent_message(self, data, context, eom):
        """ Function that gets called when a new message
            has been queued for sending by the application.
            The framer should frame the message and then call
            the send() function on itself.

        Attributes:
            data (string, required):
                The data that is to be framed.
            context (context, required):
                The message context.
            eom (boolean, required):
                Marks wether or not this was marked
                as end of message by the application.
        """
        pass
    async def handle_received_data(self, connection):
        """ Function that gets called when a new message
            has arrived on the connection. The framer should
            call parse() to get access to the buffer. After it
            has sufficient data to deframe a message, it should
            either call advance_receive_cursor() and deliver() or
            deliver_and_advance_receive_cursor() which combines
            both functions.

        Attributes:
            connection (connection, required):
                The connection object on which new data has
                arrived.
        """
        pass

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

    # Fire handle_received_data event and wait for reply of framer
    async def handle_received(self, connection):
        self.loop.create_task(self.handle_received_data(connection))
        context, data, eom = await self.await_framer_receive()
        return context, data, eom

    def fail_connection(self, error):
        return

    def prepend_protocol(self, framer):
        return

    async def send(self, data):
        """ Should be called with framed data after a
            new_sent_message() event.

        Attributes:
            data (string, required):
                The framed message.
        """
        self.connection.send_data(data, -1)

    def parse(self, connection, min_incomplete_length, max_length):
        """ Returns the message buffer of the
            connection.

        Attributes:
            connection (connection, required):
                The connection object from which the
                buffer should be returned.
        """
        return connection.transports[0].recv_buffer, None, False

class MessageContext():

    def __init__(self):
        self.values = []
        return

    def add(self, framer, key, value):
        return

    def get(self, framer, key):
        return
