import asyncio
from .utility import *
color = "magenta"


class Framer():

    def __init__(self):
        # Callbacks of the framer implementation
        self.new_sent_message = None
        self.handle_received_data = None
        self.start = None
        self.stop = None

    def on_start(self, a):
        self.start = a

    def on_stop(self, a):
        self.stop = a

    def on_new_sent_message(self, a):
        self.new_sent_message = a

    def on_handle_received_data(self, a):
        self.new_sent_message = a

    def ready(self):
        return

    def failed(self):
        return

    def prepend_protocol(self, framer):
        return

    def send(self, data):
        return

    def parse(self, min_incomplete_length, max_length):
        return

    def advance_receive_cursor(self, length):
        return

    def deliver_and_advance_receive_cursor(self, context, length, eom):
        return

    def deliver(self, context, data, eom):
        return


class MessageContext():

    def __init__(self):
        self.values = []
        return

    def add(self, framer, key, value):
        return

    def get(self, framer, key):
        return


class TlvFramer():

    def __init__(self):
        framer = Framer()
        framer.on_start(self.handle_start)
        framer.on_stop(self.handle_stop)
        framer.on_new_sent_message(self.handle_new_sent_message)
        framer.on_handle_received_data(self.handle_received_data)
        return

    def handle_start(self):
        return

    def handle_stop(self):
        return

    def handle_new_sent_message(self, message, message_context, eom):
        tlv = (message[0] + "/" + str(len(str(message[1]))) + "/" +
               str(message[1]))
        framer.send(tlv)

    def handle_received_data(self, a):
        print_time("Deframing " + byte_stream, color)
        try:
            tlv = byte_stream.split("/")
        except:
            print_time("Error splitting", color)
            return byte_stream, -2

        if len(tlv) < 3:
            print_time("Not enough parameters", color)
            return byte_stream, -1

        if (len(tlv[2]) < int(tlv[1])):
            print_time("Didnt receive full message", color)
            return byte_stream, -1
        len_message = len(tlv[0]) + len(tlv[1]) + int(tlv[1]) + 2
        message = (str(tlv[0]), str(tlv[2][0:int(tlv[1])]))
