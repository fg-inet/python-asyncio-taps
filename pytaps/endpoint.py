

class LocalEndpoint:
    """ A local (TAPS) Endpoint with an interface
        (address for now) and port number.
    """
    def __init__(self):
        self.interface = None
        self.port = None
        self.address = []
        self.host_name = None

    def with_interface(self, interface):
        """Specifies which interface the local endpoint should use.

        Attributes:
            interface (interface, required): Interface identifier.
        """
        self.interface = interface

    def with_address(self, address):
        """Specifies which address the local endpoint should use.

        Attributes:
            address (string, required): Address in the form of an IPv4
                or IPv6 address.
        """
        self.address.append(address)

    def with_hostname(self, hostname):
        """Specifies a hostname to listen on the resolved addresses.

        Attributes:
            hostname (hostname, required): Domain name, e.g., localhost.
        """
        self.host_name = hostname


    def with_port(self, portNumber):
        """Specifies which port the local endpoint should use.

        Attributes:
            portNumber (integer, required): Port number.
        """
        self.port = portNumber


class RemoteEndpoint:
    """ A remote (TAPS) Endpoint with an address,
        that can either be given directly
        as an IPv4 or IPv6 or that can be given
        as a name that will be resolved with DNS.
    """
    def __init__(self):
        self.address = []
        self.port = None
        self.host_name = None

    def with_address(self, address):
        """Specifies which address(es) the remote endpoint should have.

        Attributes:
            address (string, required): Address in the form of an IPv4
                or IPv6 address.
        """
        if isinstance(address, list):
            self.address += address
        else:
            self.address.append(address)

    def with_hostname(self, hostname):
        """Specifies which hostname the remote endpoint should have.

        Attributes:
            hostname (string, required): Host name.
        """
        self.host_name = hostname

    def with_port(self, portNumber):
        """Specifies which port the remote endpoint should have.

        Attributes:
            portNumber (integer, required): Port number.
        """
        self.port = portNumber
