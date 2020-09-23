class Endpoint:

    def __init__(self):
        self.interface = None
        self.port = None
        self.address = []
        self.host_name = None

    """ The TAPS endpoint class, local and remote endpoints are derived from this

    """
    def with_port(self, port):
        """Specifies which port the remote endpoint should have.

        Attributes:
            port (integer, required): Port number.
        """
        self.port = port

    def with_hostname(self, hostname):
        """Specifies which hostname the remote endpoint should have.

        Attributes:
            hostname (string, required): Host name.
        """
        self.host_name = hostname

    def with_address(self, address):
        """Specifies which address the local endpoint should use.

        Attributes:
            address (string, required): Address in the form of an IPv4
                or IPv6 address.
        """
        self.address.append(address)


class LocalEndpoint(Endpoint):
    """ A local (TAPS) Endpoint with an interface
        (address for now) and port number.
    """

    def with_interface(self, interface):
        """Specifies which interface the local endpoint should use.

        Attributes:
            interface (interface, required): Interface identifier.
        """
        self.interface = interface


class RemoteEndpoint(Endpoint):
    """ A remote (TAPS) Endpoint with an address,
        that can either be given directly
        as an IPv4 or IPv6 or that can be given
        as a name that will be resolved with DNS.
    """
