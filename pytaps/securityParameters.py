from .utility import setup_logger

logger = setup_logger(__name__, "magenta")


class SecurityParameters:
    """ Class to handle the TAPS security parameters.

    """

    def __init__(self):
        self.identity = None
        self.trustedCA = []
        self.alpn_protocols = []
        self.server_name = None
        self.require_peer_authentication = True
        self.cipher_suites = None

    def add_identity(self, identity):
        """ Adds a local identity with which to
            prove ones identity to a remote.
        Attributes:
            identity (string, required): Identity to be added.
        """
        if isinstance(identity, list):
            self.identity = identity[0]
        else:
            self.identity = identity
        logger.info("Our certificate: " + str(self.identity))

    def add_trust_ca(self, cert):
        """ Adds a certificate to be trusted.
        Attributes:
            cert (string, required):  Certificate to be trusted.
        """
        self.trustedCA.append(cert)
        logger.info("Trusting certificate: " + str(cert))

    def add_alpn_protocol(self, protocol):
        self.alpn_protocols.append(protocol)
        logger.info("Offering ALPN protocol: " + str(protocol))

    def with_server_name(self, server_name):
        self.server_name = server_name
        logger.info("Setting server name: " + str(server_name))

    def disable_peer_authentication(self):
        self.require_peer_authentication = False
        logger.info("Peer authentication disabled.")

    def set_cipher_suites(self, cipher_suites):
        self.cipher_suites = cipher_suites
        logger.info("Configured cipher suites.")

    def get_configuration(self):
        return {
            "identity": self.identity,
            "trustedCAs": list(self.trustedCA),
            "alpnProtocols": list(self.alpn_protocols),
            "serverName": self.server_name,
            "requirePeerAuthentication": self.require_peer_authentication,
            "cipherSuites": self.cipher_suites,
        }
