from socket import gethostbyname


class localEndpoint:
    """ A local (TAPS) Endpoint with an interface
        (address for now) and port number.
    """
    def __init__(self):
        self.interface = None
        self.port = None

    def withInterface(self, interface):
        self.interface = interface

    def withPort(self, portNumber):
        self.port = portNumber


class remoteEndpoint:
    """ A remote (TAPS) Endpoint with an address,
        that can either be given directly
        as an IPv4 or IPv6 or that can be given
        as a name that will be resolved with DNS.
    """
    def __init__(self):
        self.address = None
        self.port = None

    def withAddress(self, address):
        self.address = address

    def withHostname(self, name):
        self.address = gethostbyname(name)

    def withPort(self, portNumber):
        self.port = portNumber
