import asyncio
from .utility import *
color = "magenta"


class Framer():

    def __init__(self, event_loop=asyncio.get_event_loop()):
        self.loop = event_loop

        # Callbacks of the framer implementation
        # self.new_sent_message = None
        # self.handle_received_data = None
        # self.start = None
        # self.stop = None
    
        # Waiters for framers
        self.start_waiter = None
        self.send_waiter_list = []
        self.receive_waiter_list = []
        
    """ Function that blocks until framer is after start event
    """
    async def await_framer_ready(self):
        while(True):
            if self.start_waiter is not None:
                await self.start_waiter
            else:
                break
        self.start_waiter = self.loop.create_future()
        try:
            await self.start_waiter
        finally:
            self.start_waiter = None

    """ Function that blocks until framer is after send event
    """
    async def await_framer_send(self):
        send_waiter = self.loop.create_future()
        self.send_waiter_list.append(send_waiter)
        try:
            data = await send_waiter
        finally:
            send_waiter = None
            return data

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

    async def start(self, connection):
        pass
    async def new_sent_message(self, data, context, eom):
        pass
    async def handle_received_data(self, connection):
        pass
    async def stop(self):
        pass
    
    # Fire start event and the wait until framer replies
    async def handle_start(self, connection):
        self.loop.create_task(self.start(connection))
        await self.await_framer_ready()
        return
    # Fire new_message_sent event and wait for reply of framer
    async def handle_new_sent_message(self, data, context, eom):
        self.loop.create_task(self.new_sent_message(data, context, eom))
        data = await self.await_framer_send()
        return data
    # Fire handle_received_data event and wait for reply of framer
    async def handle_received(self, connection):
        self.loop.create_task(self.handle_received_data(connection))
        context, data, eom = await self.await_framer_receive()
        return context, data, eom
    def make_connection_ready(self, connection):
        if self.start_waiter is not None:
            self.start_waiter.set_result(None)
        return

    def fail_connection(self, error):
        # 
        return

    def prepend_protocol(self, framer):
        return

    def send(self, data):
        # Send
        if len(self.send_waiter_list) > 0:
            self.send_waiter_list[0].set_result(data)
            del self.send_waiter_list[0]
        return

    def parse(self, connection, min_incomplete_length, max_length):
        return connection.recv_buffer.decode(), None, False

    def advance_receive_cursor(self, connection, length):
        connection.recv_buffer = connection.recv_buffer[length:]
        return

    def deliver_and_advance_receive_cursor(self, context, length, eom):
        return

    def deliver(self, conenction, context, data, eom):
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

class MessageContext():

    def __init__(self):
        self.values = []
        return

    def add(self, framer, key, value):
        return

    def get(self, framer, key):
        return
