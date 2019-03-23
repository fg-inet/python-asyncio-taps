import asyncio
import json
import sys
from .endpoint import LocalEndpoint, RemoteEndpoint
from .transportProperties import *
from .utility import *
color = "green"


class Connection:
    """The TAPS connection class.

    Attributes:
        localEndpoint (:obj:'localEndpoint', optional): LocalEndpoint of the
                       preconnection, required if the connection
                       will be used to listen
        remoteEndpoint (:obj:'remoteEndpoint', optional): RemoteEndpoint of the
                        preconnection, required if a connection
                        will be initiated
        transportProperties (:obj:'transportProperties', optional): object with
                             the transport properties
                             with specified preferenceLevel
        securityParams (tbd): Security Parameters for the preconnection
    """
    def __init__(self, local_endpoint=None, remote_endpoint=None,
                 transport_properties=None, security_parameters=None):
                # Assertions
                if local_endpoint is None and remote_endpoint is None:
                    raise Exception("At least one endpoint need "
                                    "to be specified")
                # Initializations
                self.local_endpoint = local_endpoint
                self.remote_endpoint = remote_endpoint
                self.transport_properties = transport_properties
                self.security_parameters = security_parameters
                self.loop = asyncio.get_event_loop()
                self.message_count = 0
                self.ready = None
                self.initiate_error = None
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
                self.msg_buffer = None

    """ Tries to create a (TCP) connection to a remote endpoint
        If a local endpoint was specified on connection class creation,
        it will be used.
    """
    async def connect(self):
        # Create set of candidate protocols
        candidate_set = self.create_candidates()
        if not candidate_set:
            print_time("Empty candidate set", color)
            if self.initiate_error:
                print_time("Protocol selection Error occured.", color)
                self.loop.create_task(self.initiate_error())
                print_time("Queued InitiateError cb.", color)
            return
        # Resolve remote endpoint
        remote_info = await self.loop.getaddrinfo(self.remote_endpoint.address,
                                                  self.remote_endpoint.port)
        self.remote_endpoint.address = remote_info[0][4][0]
        # Attempt connection
        try:
                if(self.local_endpoint is None):
                    print_time("Connecting with unspecified localEP.", color)
                    self.reader, self.writer = await asyncio.open_connection(
                                        self.remote_endpoint.address,
                                        self.remote_endpoint.port)
                else:
                    print_time("Connecting with specified localEP.", color)
                    self.reader, self.writer = await asyncio.open_connection(
                                    self.remote_endpoint.address,
                                    self.remote_endpoint.port,
                                    local_addr=(self.local_endpoint.interface,
                                                self.local_endpoint.port))
        except:
            if self.initiate_error:
                print_time("Initiate Error occured.", color)
                self.loop.create_task(self.initiate_error())
                print_time("Queued InitiateError cb.", color)
            return
        if self.ready:
            print_time("Connected successfully.", color)
            self.loop.create_task(self.ready(self))
            print_time("Queued Ready cb.", color)
        return

    """ Tries to send the (string) stored in data
    """
    async def send_data(self, data, message_count):
        print_time("Writing data.", color)
        try:
            self.writer.write(data.encode())
            await self.writer.drain()
        except:
            if self.send_error:
                print_time("SendError occured.", color)
                self.loop.create_task(self.send_error(message_count))
                print_time("Queued SendError cb.", color)
            return
        print_time("Data written successfully.", color)

        # Queue sent callback if there is one
        if self.sent:
            self.loop.create_task(self.sent(message_count))
            print_time("Queued Sent cb..", color)
        return

    """ Wrapper function that assigns MsgRef
        and then calls async helper function
        to send a message
    """
    async def send_message(self, data):
        print_time("Sending data.", color)
        self.message_count += 1
        self.loop.create_task(self.send_data(data, self.message_count))
        print_time("Returning MsgRef.", color)
        return self.message_count

    """ Queues reception of a message
    """
    async def receive_message(self, min_incomplete_length,
                              max_length):
        try:
            data = await self.reader.read(max_length)
            data = data.decode()
            if self.msg_buffer is None:
                self.msg_buffer = data
            else:
                self.msg_buffer = self.msg_buffer + data
        except:
            print_time("Connection Error", color)
            if self.connection_error is not None:
                self.loop.create_task(self.connection_error(self))
            return
        if self.reader.at_eof():
            print_time("Received full message", color)
            if self.received:
                self.loop.create_task(self.received(self.msg_buffer,
                                                    "Context", self))
                print_time("Called received cb.", color)
            self.msg_buffer = None
            return

        elif len(self.msg_buffer) > min_incomplete_length:
            print_time("Received partial message.", color)
            if self.received_partial:
                self.loop.create_task(self.received_partial(self.msg_buffer,
                                      "Context", False, self))
                print_time("Called partial_receive cb.", color)
                self.msg_buffer = None

    """ Wrapper function to make receive return immediately
    """
    async def receive(self, min_incomplete_length=float("inf"), max_length=-1):
        self.loop.create_task(self.receive_message(min_incomplete_length,
                              max_length))
    """ Tries to close the connection
        TODO: Check why port isnt always freed
    """
    async def close_connection(self):
        print_time("Closing connection.", color)
        self.writer.close()
        await self.writer.wait_closed()
        print_time("Connection closed.", color)
        if self.closed:
            self.loop.create_task(self.closed())

    """ Wrapper function for close_connection,
        required to make close return immediately
    """
    def close(self):
        self.loop.create_task(self.close_connection())
    """ Function to set reader/writer for passive open
    """
    def set_reader_writer(self, reader, writer):
        self.reader = reader
        self.writer = writer

    def create_candidates(self):
        available_protocols = get_protocols()
        candidate_protocols = dict([(row["name"], list((0, 0)))
                                   for row in available_protocols])
        for protocol in available_protocols:
            for transport_property in self.transport_properties.properties:
                if (self.transport_properties.properties[transport_property]
                        is PreferenceLevel.PROHIBIT):
                    if (protocol[transport_property] is True and
                            protocol["name"] in candidate_protocols):
                        del candidate_protocols[protocol["name"]]
                if (self.transport_properties.properties[transport_property]
                        is PreferenceLevel.REQUIRE):
                    if (protocol[transport_property] is False and
                            protocol["name"] in candidate_protocols):
                        del candidate_protocols[protocol["name"]]
                if (self.transport_properties.properties[transport_property]
                        is PreferenceLevel.PREFER):
                    if (protocol[transport_property] is True and
                            protocol["name"] in candidate_protocols):
                        candidate_protocols[protocol["name"]][0] += 1
                if (self.transport_properties.properties[transport_property]
                        is PreferenceLevel.AVOID):
                    if (protocol[transport_property] is True and
                            protocol["name"] in candidate_protocols):
                        candidate_protocols[protocol["name"]][1] -= 1

        sorted_candidates = sorted(candidate_protocols.items(),
                                   key=lambda value: (value[1][0],
                                   value[1][1]), reverse=True)

        return sorted_candidates

    # Events for active open
    def on_ready(self, a):
        self.ready = a

    def on_initiate_error(self, a):
        self.initiate_error = a

    # Events for sending messages
    def on_sent(self, a):
        self.sent = a

    def on_send_error(self, a):
        self.send_error = a

    def on_expired(self, a):
        self.expired = a

    # Events for receiving messages
    def on_received(self, a):
        self.received = a

    def on_received_partial(self, a):
        self.received_partial = a

    def on_receive_error(self, a):
        self.receive_error = a

    def on_connection_error(self, a):
        self.connection_error = a

    # Events for closing a connection
    def on_closed(self, a):
        self.closed = a
