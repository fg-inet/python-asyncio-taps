import asyncio
from .utility import *
color = "magenta"


class TlvFramer():

    def __init__(self):
        return

    def frame(self, message):
        tlv = (message[0] + "/" + str(len(str(message[1]))) + "/" +
               str(message[1]))
        return tlv

    def deframe(self, byte_stream):
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
        return byte_stream[len_message:], message
