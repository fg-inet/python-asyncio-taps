from socket import gethostbyname


class localEndpoint:
    def __init__(self):
        self.interface = None
        self.port = None

    def withInterface(self, interface):
        self.interface = interface

    def withPort(self, portNumber):
        self.port = portNumber


class remoteEndpoint:
    def __init__(self):
        self.address = None
        self.port = None

    def withAddress(self, address):
        self.address = address

    def withHostname(self, name):
        self.address = gethostbyname(name)

    def withPort(self, portNumber):
        self.port = portNumber
