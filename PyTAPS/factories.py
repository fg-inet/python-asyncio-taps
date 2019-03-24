import asyncio
from .connection import Connection
from .transportProperties import *
from .endpoint import LocalEndpoint, RemoteEndpoint
from .utility import *
color = "yellow"


class PassiveOpenFactory(asyncio.Protocol):
    def __init__(self, preconnection):
        self.preconnection = preconnection
        # self.connection = None

    def connection_made(self, transport):
        print_time("New connection", color)
        new_remote_endpoint = RemoteEndpoint()
        print_time("Received new connection.", color)
        new_remote_endpoint.with_address(transport.get_extra_info("peername")[0])
        new_remote_endpoint.with_port(transport.get_extra_info("peername")[1])
        new_connection = Connection(self.preconnection.local_endpoint,
                                    new_remote_endpoint,
                                    self.preconnection.transport_properties,
                                    self.preconnection.security_parameters)
        self.connection = new_connection
        # new_connection.set_reader_writer(reader, writer)
        print_time("Created new connection object.", color)
        if self.preconnection.connection_received:
            self.preconnection.loop.create_task(
                                self.preconnection.connection_received(
                                    new_connection))
            print_time("Called connection_received cb", color)
        return

    def data_received(self, data):
        if self.connection.recv_buffer is None:
            self.connection.recv_buffer = data
        else:
            self.connection.recv_buffer = self.connection.recv_buffer + data
        print("Received " + self.connection.recv_buffer.decode())

    def connection_lost(self, exc):
        print("Connection lost")
    
    def eof_received(self):
        self.connection.at_eof = True
