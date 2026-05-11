from .utility import setup_logger

logger = setup_logger(__name__, "magenta")


class SecurityParameters:
    """ Class to handle the TAPS security parameters.

    """

    def __init__(self):
        self.identity = None
        self.trustedCA = []
        self.allowed_security_protocols = []
        self.pinned_server_certificates = []
        self.security_algorithms = []
        self.pre_shared_key = None
        self.private_key = None
        self.private_key_callback_handle = None
        self.public_key = None
        self.alpn_protocols = []
        self.server_name = None
        self.require_peer_authentication = True
        self.cipher_suites = None
        self.session_cache_capacity = None
        self.session_cache_lifetime = None

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

    def add_allowed_security_protocol(self, protocol):
        self.allowed_security_protocols.append(protocol)
        logger.info("Allowing security protocol: " + str(protocol))

    def set_allowed_security_protocols(self, protocols):
        self.allowed_security_protocols = list(protocols)
        logger.info("Setting allowed security protocols: " + str(self.allowed_security_protocols))

    def add_pinned_server_certificate(self, certificate_chain):
        self.pinned_server_certificates.append(certificate_chain)
        logger.info("Configured pinned server certificate chain.")

    def set_pinned_server_certificates(self, certificate_chains):
        self.pinned_server_certificates = list(certificate_chains)
        logger.info("Configured pinned server certificate chains.")

    def add_security_algorithm(self, algorithm):
        self.security_algorithms.append(algorithm)
        logger.info("Allowing security algorithm: " + str(algorithm))

    def set_security_algorithms(self, algorithms):
        self.security_algorithms = list(algorithms)
        logger.info("Setting security algorithms: " + str(self.security_algorithms))

    def add_pre_shared_key(self, pre_shared_key):
        self.pre_shared_key = pre_shared_key
        logger.info("Configured pre-shared key.")

    def add_private_key(self, private_key):
        self.private_key = private_key
        logger.info("Configured private key path.")

    def add_private_key_callback_handle(self, handle):
        self.private_key_callback_handle = handle
        logger.info("Configured external private key callback handle.")

    def add_public_key(self, public_key):
        self.public_key = public_key
        logger.info("Configured public key path.")

    def add_alpn_protocol(self, protocol):
        self.alpn_protocols.append(protocol)
        logger.info("Offering ALPN protocol: " + str(protocol))

    def set_alpn_protocols(self, protocols):
        self.alpn_protocols = list(protocols)
        logger.info("Setting ALPN protocols: " + str(self.alpn_protocols))

    def with_server_name(self, server_name):
        self.server_name = server_name
        logger.info("Setting server name: " + str(server_name))

    def set_server_name(self, server_name):
        self.with_server_name(server_name)

    def disable_peer_authentication(self):
        self.require_peer_authentication = False
        logger.info("Peer authentication disabled.")

    def enable_peer_authentication(self):
        self.require_peer_authentication = True
        logger.info("Peer authentication enabled.")

    def set_cipher_suites(self, cipher_suites):
        self.cipher_suites = cipher_suites
        logger.info("Configured cipher suites.")

    def set_session_cache_capacity(self, capacity):
        self.session_cache_capacity = capacity
        logger.info("Configured session cache capacity: " + str(capacity))

    def set_session_cache_lifetime(self, lifetime):
        self.session_cache_lifetime = lifetime
        logger.info("Configured session cache lifetime: " + str(lifetime))

    def get_configuration(self):
        return {
            "identity": self.identity,
            "trustedCAs": list(self.trustedCA),
            "allowedSecurityProtocols": list(self.allowed_security_protocols),
            "pinnedServerCertificate": list(self.pinned_server_certificates),
            "securityAlgorithms": list(self.security_algorithms),
            "preSharedKey": self.pre_shared_key,
            "privateKey": self.private_key,
            "privateKeyCallbackHandle": self.private_key_callback_handle,
            "publicKey": self.public_key,
            "alpnProtocols": list(self.alpn_protocols),
            "serverName": self.server_name,
            "requirePeerAuthentication": self.require_peer_authentication,
            "cipherSuites": self.cipher_suites,
            "sessionCacheCapacity": self.session_cache_capacity,
            "sessionCacheLifetime": self.session_cache_lifetime,
        }
