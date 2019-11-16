import asyncio
from .utility import *
color = "magenta"


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
        # Waiters for framers
        self.start_waiter = None
        self.receive_waiter_list = []

    """ Function that blocks until framer is after start event
    """
    async def await_framer_ready(self):
        """ Not required for now since there is only
            one start event per framer
        while(True):
            if self.start_waiter is not None:
                await self.start_waiter
            else:
                break
        """
        self.start_waiter = self.loop.create_future()
        try:
            await self.start_waiter
        finally:
            self.start_waiter = None

    """ Function that blocks until framer is after receive event
    """
    async def await_framer_receive(self):
        receive_waiter = self.loop.create_future()
        self.receive_waiter_list.append(receive_waiter)
        try:
            context, data, eom = await receive_waiter
        finally:
            receive_waiter = None
            return context, data, eom

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
        self.loop.create_task(self.start(connection))
        await self.await_framer_ready()
        return

    # Fire new_message_sent event and wait for reply of framer
    def handle_new_sent_message(self, data, context, eom):
        self.loop.create_task(self.new_sent_message(data, context, eom))

    # Fire handle_received_data event and wait for reply of framer
    async def handle_received(self, connection):
        self.loop.create_task(self.handle_received_data(connection))
        context, data, eom = await self.await_framer_receive()
        return context, data, eom

    #  Declare a connection ready
    def make_connection_ready(self, connection):
        """ Tells the connection object that the
            framer is done with handling the start event.

        Attributes:
            connection (connection, required):
                The connection object that was handled
                by the framer.
        """
        if self.start_waiter is not None:
            self.start_waiter.set_result(None)
        return

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
        return connection.recv_buffer.decode(), None, False

    def advance_receive_cursor(self, connection, length):
        """ Deletes the first length number of
            elements from the buffer of connection.

        Attributes:
            connection (connection, required):
                The connection object from which the
                buffer should be modified.
            length (integer, required):
                Number of elements to delete from
                the buffer.
        """      
        connection.recv_buffer = connection.recv_buffer[length:]
        return

    def deliver_and_advance_receive_cursor(self, connection, context, data,
                                           length, eom):
        """ Combines the functionallity of advance_receive_cursor and
            deliver.

        Attributes:
            connection (connection, required):
                The connection object from which the
                buffer should be modified.
            length (integer, required):
                Number of elements to delete from
                the buffer.
            context (context, required):
                The message context.
            data (string, required):
                Message to be delivered.
            eom (boolean, required):
                Wether or not this should
                be marked as end of Message.
        """
        connection.recv_buffer = connection.recv_buffer[length:]
        if len(self.receive_waiter_list) > 0:
            self.receive_waiter_list[0].set_result((context, data, eom))
            del self.receive_waiter_list[0]
        return

    def deliver(self, conenction, context, data, eom):
        """ Delivers data to the application via a received event.
        Attributes:
            connection (connection, required):
                The connection object which should issue
                a received event.
            context (context, required):
                The message context.
            data (string, required):
                Message to be delivered.
            eom (boolean, required):
                Wether or not this should
                be marked as end of Message.
        """
        if len(self.receive_waiter_list) > 0:
            self.receive_waiter_list[0].set_result((context, data, eom))
            del self.receive_waiter_list[0]
        return

    def on_start(self, a):
        self.start = a

    def on_stop(self, a):
        self.stop = a

    def on_new_sent_message(self, a):
        self.new_sent_message = a

    def on_handle_received_data(self, a):
        self.new_sent_message = a

