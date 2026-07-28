import hashlib
import os
import re
import ssl

from .utility import setup_logger

logger = setup_logger(__name__, "magenta")

_PEM_CERTIFICATE_PATTERN = re.compile(
    br"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
    re.DOTALL,
)


def _certificate_bytes(value):
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if isinstance(value, str):
        if "-----BEGIN CERTIFICATE-----" not in value:
            try:
                is_file = os.path.isfile(value)
            except OSError:
                is_file = False
            if is_file:
                with open(value, "rb") as certificate_file:
                    return certificate_file.read()
        return value.encode("ascii")
    if isinstance(value, bytes):
        return value
    return None


def _certificate_to_der(value):
    raw = _certificate_bytes(value)
    if raw is not None:
        blocks = _PEM_CERTIFICATE_PATTERN.findall(raw)
        if blocks:
            return [
                ssl.PEM_cert_to_DER_cert(block.decode("ascii"))
                for block in blocks
            ]
        return [raw]

    public_bytes = getattr(value, "public_bytes", None)
    if not callable(public_bytes):
        raise TypeError(f"Unsupported certificate object: {type(value).__name__}")

    try:
        import _ssl

        return [public_bytes(_ssl.ENCODING_DER)]
    except (ImportError, TypeError, ValueError):
        try:
            from cryptography.hazmat.primitives.serialization import Encoding

            return [public_bytes(Encoding.DER)]
        except (ImportError, TypeError, ValueError) as exc:
            raise TypeError(
                f"Unsupported certificate object: {type(value).__name__}"
            ) from exc


def _flatten_certificates(value):
    if isinstance(value, (list, tuple)):
        certificates = []
        for certificate in value:
            certificates.extend(_flatten_certificates(certificate))
        return certificates
    return _certificate_to_der(value)


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

        Args:
            identity (string, required): Identity to be added.
        """
        if isinstance(identity, list):
            self.identity = identity[0]
        else:
            self.identity = identity
        logger.info("Our certificate: " + str(self.identity))

    def add_trust_ca(self, cert):
        """ Adds a certificate to be trusted.

        Args:
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

    def get_pinned_server_certificate_digests(self):
        digests = set()
        for certificate_chain in self.pinned_server_certificates:
            certificates = _flatten_certificates(certificate_chain)
            if certificates:
                # The first certificate identifies the server; later
                # certificates only establish its chain of trust.
                digests.add(hashlib.sha256(certificates[0]).digest())
        return digests

    def verify_pinned_server_certificates(self, peer_certificate_chain):
        if not self.pinned_server_certificates:
            return True
        if peer_certificate_chain is None:
            peer_certificate_chain = []
        if not isinstance(peer_certificate_chain, (list, tuple)):
            peer_certificate_chain = [peer_certificate_chain]

        pinned = self.get_pinned_server_certificate_digests()
        presented = _flatten_certificates(peer_certificate_chain)
        presented_leaf = (
            hashlib.sha256(presented[0]).digest()
            if presented
            else None
        )
        if presented_leaf not in pinned:
            raise ssl.SSLCertVerificationError(
                "The presented server certificate chain does not match "
                "a configured pinnedServerCertificate"
            )
        return True

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
