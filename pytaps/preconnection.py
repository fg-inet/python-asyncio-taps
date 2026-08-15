import asyncio
import ipaddress
import ssl
from copy import deepcopy
from xml.etree.ElementTree import fromstring

from .connection import Connection
from .connection_context import ConnectionContext
from .endpoint import LocalEndpoint, RemoteEndpoint
from .framer import Framer
from .listener import Listener
from .message import (
    MESSAGE_PROPERTY_DEFAULTS,
    MessageContext,
    canonicalize_message_property_name,
    is_message_property,
)
from .securityParameters import SecurityParameters
from .stun import StunError, discover_reflexive_address
from .transportProperties import PreferenceLevel, TransportProperties, normalize_direction
from . import transports as transport_impl
from .transports import UdpTransport
from .utility import (
    ConnectionState,
    describe_unsatisfiable_properties,
    schedule_callback,
    setup_logger,
)
from .yang_validate import (
    YANG_FMT_JSON,
    YANG_FMT_XML,
    YangException,
    convert,
    validate,
)

logger = setup_logger(__name__, "green")


class UnsatisfiableTransportProperties(ValueError):
    """No available protocol satisfies the configured Transport Properties.

    Raised during preestablishment, per Section 3.1 of RFC 9623.
    """


class Preconnection:
    """The TAPS preconnection class.

    Attributes:
        local_endpoint (LocalEndpoint, optional):
                        LocalEndpoint of the
                        preconnection, required if the connection
                        will be used to listen
        remote_endpoint (RemoteEndpoint, optional):
                        RemoteEndpoint of the
                        preconnection, required if a connection
                        will be initiated
        transport_properties (TransportProperties, optional):
                        Object of the transport properties
                        with specified preferenceLevels
        security_parameters (SecurityParameters, optional):
                        Security Parameters for the preconnection
        event_loop (eventLoop, optional):
                        Event loop on which all coroutines and callbacks
                        will be scheduled, if none if given the
                        one of the current thread is used by default
    """

    def __init__(
        self,
        local_endpoints=None,
        remote_endpoints=None,
        transport_properties=None,
        security_parameters=None,
        event_loop=None,
        connection_context=None,
        *,
        local_endpoint=None,
        remote_endpoint=None,
        _security_role=None,
        _register_context=True,
    ):

        # Initializations from arguments
        if local_endpoint is not None:
            if local_endpoints is not None:
                raise TypeError(
                    "Specify local_endpoints or local_endpoint, not both"
                )
            local_endpoints = local_endpoint
        if remote_endpoint is not None:
            if remote_endpoints is not None:
                raise TypeError(
                    "Specify remote_endpoints or remote_endpoint, not both"
                )
            remote_endpoints = remote_endpoint
        self.local_endpoints = self._normalize_endpoints(
            local_endpoints,
            LocalEndpoint,
            "local",
        )
        self.remote_endpoints = self._normalize_endpoints(
            remote_endpoints,
            RemoteEndpoint,
            "remote",
        )
        self.transport_properties = (
            transport_properties.clone()
            if transport_properties is not None
            else TransportProperties()
        )
        self.security_parameters = deepcopy(security_parameters)
        self.message_properties = MessageContext()
        for prop, value in self.transport_properties.get_profile_message_properties().items():
            self.message_properties.set_property(prop, value)
        self.connection_context = connection_context or ConnectionContext()
        if _register_context:
            self.connection_context.register_preconnection()
        self._security_role = _security_role
        self._rendezvous_mode = False
        self._reuse_isolated_context = False
        if event_loop is not None:
            self.loop = event_loop
        else:
            try:
                self.loop = asyncio.get_running_loop()
            except RuntimeError:
                self.loop = asyncio.get_event_loop()

        # Callbacks of the application
        self.read = None
        self.initiate_error = None
        self.connection_received = None
        self.listen_error = None
        self.stopped = None
        self.ready = None
        self.establishment_error = None
        self.rendezvous_done = None

        self.framers = []
        self._framer_configuration_locked = False

        if self.security_parameters:
            self._apply_security_defaults()
        self.security_context = self._build_security_context(
            is_listener=_security_role == "listener"
            if _security_role is not None
            else None
        )

    @staticmethod
    def _normalize_endpoints(endpoints, endpoint_type, label):
        if endpoints is None:
            return []
        if isinstance(endpoints, endpoint_type):
            endpoints = [endpoints]
        else:
            try:
                endpoints = list(endpoints)
            except TypeError as exc:
                raise TypeError(
                    f"{label}_endpoints must contain {endpoint_type.__name__} objects"
                ) from exc
        if not all(isinstance(endpoint, endpoint_type) for endpoint in endpoints):
            raise TypeError(
                f"{label}_endpoints must contain only {endpoint_type.__name__} objects"
            )
        return [endpoint.clone() for endpoint in endpoints]

    @property
    def local_endpoint(self):
        return self.local_endpoints[0] if self.local_endpoints else None

    @local_endpoint.setter
    def local_endpoint(self, endpoint):
        self.local_endpoints = self._normalize_endpoints(
            endpoint,
            LocalEndpoint,
            "local",
        )

    @property
    def remote_endpoint(self):
        return self.remote_endpoints[0] if self.remote_endpoints else None

    @remote_endpoint.setter
    def remote_endpoint(self, endpoint):
        self.remote_endpoints = self._normalize_endpoints(
            endpoint,
            RemoteEndpoint,
            "remote",
        )

    def _apply_security_defaults(self):
        for prop in (
            "confidentiality",
            "integrity",
            "peerAuthentication",
            "secureKeyExchange",
        ):
            if prop not in self.transport_properties.get_explicit_selection_properties():
                self.transport_properties.require(prop)
        self._apply_security_protocol_policy()

    def _normalized_allowed_security_protocols(self):
        if not self.security_parameters:
            return set()
        return {
            protocol.lower().replace(".", "").replace("_", "")
            for protocol in self.security_parameters.allowed_security_protocols
        }

    def _apply_security_protocol_policy(self):
        if not self.security_parameters:
            return
        allowed_protocols = self._normalized_allowed_security_protocols()
        if not allowed_protocols:
            return
        secure_tls_allowed = any(protocol.startswith("tls") for protocol in allowed_protocols)
        quic_allowed = "tls13" in allowed_protocols
        self.connection_context.set_protocol_policy(
            "tls-tcp",
            available=secure_tls_allowed,
        )
        self.connection_context.set_protocol_policy(
            "quic",
            available=quic_allowed,
        )

    def _build_security_context(self, *, is_listener=None):
        if not self.security_parameters:
            return None

        if is_listener is None:
            is_listener = bool(self.local_endpoints and not self.remote_endpoints)
        if is_listener:
            security_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        else:
            security_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        # RFC 9622 Section 6.3.8: a protected local identity needs a private key
        # operation, which is where the identity challenge callback is invoked.
        identity_challenge = self.security_parameters.identity_challenge_callback
        if self.security_parameters.identity:
            logger.info("Identity: " + str(self.security_parameters.identity))
            security_context.load_cert_chain(
                self.security_parameters.identity,
                password=identity_challenge,
            )
        elif self.security_parameters.public_key:
            logger.info("Public key certificate: " + str(self.security_parameters.public_key))
            security_context.load_cert_chain(
                self.security_parameters.public_key,
                keyfile=self.security_parameters.private_key,
                password=identity_challenge,
            )
        trust_anchors = list(self.security_parameters.trustedCA)
        if not trust_anchors and self.security_parameters.pinned_server_certificates:
            trust_anchors = list(self.security_parameters.pinned_server_certificates)
        for cert in trust_anchors:
            security_context.load_verify_locations(cert)
        if trust_anchors and hasattr(ssl, "VERIFY_X509_STRICT"):
            # The bundled test certificates predate stricter AKI/SKI validation in
            # newer OpenSSL releases, so keep chain verification enabled but relax
            # strict profile checks for explicit custom trust anchors.
            security_context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        if self.security_parameters.alpn_protocols:
            security_context.set_alpn_protocols(
                self.security_parameters.alpn_protocols
            )
        allowed_protocols = {
            protocol.lower().replace(".", "").replace("_", "")
            for protocol in self.security_parameters.allowed_security_protocols
        }
        if allowed_protocols:
            if hasattr(ssl, "TLSVersion"):
                if allowed_protocols == {"tls13"}:
                    security_context.minimum_version = ssl.TLSVersion.TLSv1_3
                    security_context.maximum_version = ssl.TLSVersion.TLSv1_3
                elif allowed_protocols == {"tls12"}:
                    security_context.minimum_version = ssl.TLSVersion.TLSv1_2
                    security_context.maximum_version = ssl.TLSVersion.TLSv1_2
        if self.security_parameters.cipher_suites:
            security_context.set_ciphers(self.security_parameters.cipher_suites)
        if is_listener:
            security_context.check_hostname = False
            if (
                self.security_parameters.require_peer_authentication
                and self.security_parameters.trustedCA
            ):
                security_context.verify_mode = ssl.CERT_REQUIRED
            else:
                security_context.verify_mode = ssl.CERT_NONE
        elif self.security_parameters.require_peer_authentication:
            security_context.verify_mode = ssl.CERT_REQUIRED
            security_context.check_hostname = True
        else:
            security_context.check_hostname = False
            security_context.verify_mode = ssl.CERT_NONE
        security_context._pytaps_pinned_server_certificates = list(
            self.security_parameters.pinned_server_certificates
        )
        security_context._pytaps_security_algorithms = list(
            self.security_parameters.security_algorithms
        )
        security_context._pytaps_allowed_security_protocols = list(
            self.security_parameters.allowed_security_protocols
        )
        security_context._pytaps_pre_shared_key = self.security_parameters.pre_shared_key
        security_context._pytaps_private_key_callback_handle = (
            self.security_parameters.private_key_callback_handle
        )
        security_context._pytaps_server_name = self.security_parameters.server_name
        security_context._pytaps_cipher_suites = self.security_parameters.cipher_suites
        security_context._pytaps_session_cache_capacity = (
            self.security_parameters.session_cache_capacity
        )
        security_context._pytaps_session_cache_lifetime = (
            self.security_parameters.session_cache_lifetime
        )
        return security_context

    def from_yang(self, frmat, text):
        if frmat == YANG_FMT_XML:
            validate(frmat, text)
            xml_text = text
        else:
            xml_text = convert(frmat, text, YANG_FMT_XML)
        root = fromstring(xml_text)
        ns = {'taps': 'urn:ietf:params:xml:ns:yang:ietf-taps-api'}

        # jake 2019-05-02: *sigh* thanks for all the hate, xml...
        if root.tag != "{urn:ietf:params:xml:ns:yang:ietf-taps-api}" + \
                "preconnection":
            logger.warning(
                "warning: unexpected root of instance: %s "
                "(instead of ietf-taps-api:preconnection)",
                root.tag,
            )
        precon = root

        local_endpoints = []
        for node in precon.findall('taps:local-endpoints', namespaces=ns):
            endpoint = LocalEndpoint()
            # TBD: jake 2019-05-02: mapping from ifref to interface name?
            interface_ref = node.findtext('taps:ifref', namespaces=ns)
            local_address = node.findtext('taps:local-address', namespaces=ns)
            local_port = node.findtext('taps:local-port', namespaces=ns)
            if interface_ref:
                endpoint.with_interface(interface_ref)
            if local_address:
                endpoint.with_address(local_address)
            if local_port:
                endpoint.with_port(local_port)
            local_endpoints.append(endpoint)

        remote_endpoints = []
        for node in precon.findall('taps:remote-endpoints', namespaces=ns):
            endpoint = RemoteEndpoint()
            remote_host = node.findtext('taps:remote-host', namespaces=ns)
            remote_port = node.findtext('taps:remote-port', namespaces=ns)
            if remote_host:
                endpoint.with_hostname(remote_host)
            if remote_port:
                endpoint.with_port(remote_port)
            remote_endpoints.append(endpoint)

        sp = None
        security = precon.find('taps:security', namespaces=ns)
        if security:
            sp = SecurityParameters()
            for cred in security.findall('taps:credentials', namespaces=ns):
                trust_ca = cred.findtext('taps:trust-ca', namespaces=ns)
                local_identity = cred.findtext('taps:identity', namespaces=ns)
                allowed_security_protocol = cred.findtext(
                    'taps:allowed-security-protocol',
                    namespaces=ns,
                )
                pinned_server_certificate = cred.findtext(
                    'taps:pinned-server-certificate',
                    namespaces=ns,
                )
                algorithm = cred.findtext('taps:algorithm', namespaces=ns)
                pre_shared_key = cred.findtext('taps:pre-shared-key', namespaces=ns)
                private_key = cred.findtext('taps:private-key', namespaces=ns)
                private_key_callback_handle = cred.findtext(
                    'taps:private-key-callback-handle',
                    namespaces=ns,
                )
                public_key = cred.findtext('taps:public-key', namespaces=ns)
                if trust_ca:
                    sp.add_trust_ca(trust_ca)
                if local_identity:
                    sp.add_identity(local_identity)
                if allowed_security_protocol:
                    sp.add_allowed_security_protocol(allowed_security_protocol)
                if pinned_server_certificate:
                    sp.add_pinned_server_certificate(pinned_server_certificate)
                if algorithm:
                    sp.add_security_algorithm(algorithm)
                if pre_shared_key:
                    sp.add_pre_shared_key(pre_shared_key)
                if private_key:
                    sp.add_private_key(private_key)
                if private_key_callback_handle:
                    sp.add_private_key_callback_handle(private_key_callback_handle)
                if public_key:
                    sp.add_public_key(public_key)
            session_cache_capacity = security.findtext(
                'taps:session-cache-capacity',
                namespaces=ns,
            )
            session_cache_lifetime = security.findtext(
                'taps:session-cache-lifetime',
                namespaces=ns,
            )
            if session_cache_capacity:
                sp.set_session_cache_capacity(int(session_cache_capacity))
            if session_cache_lifetime:
                sp.set_session_cache_lifetime(int(session_cache_lifetime))

        tp = TransportProperties()
        transport = precon.find('taps:transport-properties', namespaces=ns)
        if transport:
            fn_mapping = {
                'ignore': TransportProperties.ignore,
                'prohibit': TransportProperties.prohibit,
                'require': TransportProperties.require,
                'prefer': TransportProperties.prefer,
                'avoid': TransportProperties.avoid,
            }
            xml_prefix = '{' + ns['taps'] + '}'
            for node in transport:
                prop_name = str(node.tag)
                if prop_name.startswith(xml_prefix):
                    prop_name = prop_name[len(xml_prefix):]
                if prop_name == 'interface':
                    preference = node.findtext('taps:preference', namespaces=ns)
                    value = node.findtext('taps:value', namespaces=ns)
                    if preference and value:
                        tp.add_interface_preference(
                            value,
                            {
                                'ignore': PreferenceLevel.IGNORE,
                                'prohibit': PreferenceLevel.PROHIBIT,
                                'require': PreferenceLevel.REQUIRE,
                                'prefer': PreferenceLevel.PREFER,
                                'avoid': PreferenceLevel.AVOID,
                            }[preference],
                        )
                    continue
                if prop_name == 'pvd':
                    preference = node.findtext('taps:preference', namespaces=ns)
                    value = node.findtext('taps:value', namespaces=ns)
                    if preference and value:
                        tp.add_pvd_preference(
                            value,
                            {
                                'ignore': PreferenceLevel.IGNORE,
                                'prohibit': PreferenceLevel.PROHIBIT,
                                'require': PreferenceLevel.REQUIRE,
                                'prefer': PreferenceLevel.PREFER,
                                'avoid': PreferenceLevel.AVOID,
                            }[preference],
                        )
                    continue
                if node.text in fn_mapping:
                    fn = fn_mapping.get(node.text)
                    fn(tp, prop_name)
                elif prop_name == 'direction':
                    tp.set_property("direction", normalize_direction(node.text))
                else:
                    # TBD jake 2019-05-07: interface name/type, pvd
                    pass

        self.remote_endpoints = remote_endpoints
        self.local_endpoints = local_endpoints
        self.transport_properties = tp
        self.security_parameters = sp
        if self.security_parameters:
            self._apply_security_defaults()
        self.security_context = self._build_security_context()
        return self

    def _available_protocols(self):
        available = {"tcp", "udp"}
        if self.security_context is not None:
            available.add("tls-tcp")
        if transport_impl.aioquic_connect is not None:
            available.add("quic")
        return available

    def _check_configuration(self, action):
        """Reject a Property set no available protocol can satisfy.

        Section 3.1 of RFC 9623 asks for configuration-time errors to fail as
        early as possible, before resources are allocated. Appendix A.2 of
        RFC 9622 sanctions reporting them synchronously, as an exception raised
        when the application tries to establish a Connection.
        """
        reason = describe_unsatisfiable_properties(
            self.transport_properties.for_action(action),
            connection_context=self.connection_context,
            available_protocols=self._available_protocols(),
        )
        if reason is not None:
            raise UnsatisfiableTransportProperties(
                f"No available protocol satisfies the Transport Properties "
                f"for {action}: {reason}"
            )

    def _copy_configuration(self, *, action=None, security_role=None):
        transport_properties = (
            self.transport_properties.for_action(action)
            if action is not None
            else self.transport_properties.clone()
        )
        cloned = Preconnection(
            local_endpoints=[endpoint.clone() for endpoint in self.local_endpoints],
            remote_endpoints=[endpoint.clone() for endpoint in self.remote_endpoints],
            transport_properties=transport_properties,
            security_parameters=deepcopy(self.security_parameters),
            event_loop=self.loop,
            connection_context=self.connection_context,
            _security_role=security_role,
            _register_context=False,
        )
        cloned.message_properties = deepcopy(self.message_properties)
        cloned.read = self.read
        cloned.initiate_error = self.initiate_error
        cloned.connection_received = self.connection_received
        cloned.listen_error = self.listen_error
        cloned.stopped = self.stopped
        cloned.ready = self.ready
        cloned.establishment_error = self.establishment_error
        cloned.rendezvous_done = self.rendezvous_done
        cloned.framers = list(self.framers)
        cloned._rendezvous_mode = self._rendezvous_mode
        cloned._reuse_isolated_context = self._reuse_isolated_context
        for attribute in (
            "multicast_interface_address",
            "multicast_ttl",
            "multicast_disable_loopback",
        ):
            if hasattr(self, attribute):
                setattr(cloned, attribute, deepcopy(getattr(self, attribute)))
        return cloned

    def clone(self):
        return self._copy_configuration()

    def _snapshot(self, action):
        self._framer_configuration_locked = True
        security_role = "listener" if action == "listen" else "client"
        snapshot = self._copy_configuration(
            action=action,
            security_role=security_role,
        )
        snapshot._framer_configuration_locked = True
        return snapshot

    def add_local_endpoint(self, endpoint):
        if not isinstance(endpoint, LocalEndpoint):
            raise TypeError("Local endpoints must be LocalEndpoint objects")
        self.local_endpoints.append(endpoint.clone())
        return self

    def add_remote_endpoint(self, endpoint):
        if not isinstance(endpoint, RemoteEndpoint):
            raise TypeError("Remote endpoints must be RemoteEndpoint objects")
        self.remote_endpoints.append(endpoint.clone())
        return self

    def add_local_address(self, address):
        return self.add_local_endpoint(LocalEndpoint().with_address(address))

    def add_remote_address(self, address):
        return self.add_remote_endpoint(RemoteEndpoint().with_address(address))

    def add_framer(self, framer):
        if self._framer_configuration_locked:
            raise RuntimeError(
                "Framers must be added before creating a Connection or Listener"
            )
        if not isinstance(framer, Framer):
            raise TypeError("Framers must be Framer objects")
        self.framers.append(framer)
        return self

    def set_property(self, prop, value):
        if is_message_property(prop):
            self.message_properties.set_property(prop, value)
            canonical = canonicalize_message_property_name(prop)
            if canonical == "safely_replayable":
                self.transport_properties.profile_message_properties[
                    "safelyReplayable"
                ] = self.message_properties.safely_replayable
        else:
            self.transport_properties.set_property(prop, value)
        return self

    def get_property(self, prop, default=None):
        if is_message_property(prop):
            return self.message_properties.get(prop, default)
        return self.transport_properties.get_property(prop, default)

    def default_property(self, prop):
        if is_message_property(prop):
            canonical = canonicalize_message_property_name(prop)
            setattr(self.message_properties, canonical, MESSAGE_PROPERTY_DEFAULTS[canonical])
            self.message_properties.explicit_properties.discard(canonical)
            if canonical == "safely_replayable":
                self.transport_properties.profile_message_properties.pop(
                    "safelyReplayable",
                    None,
                )
            return self
        self.transport_properties.default_property(prop)
        return self

    def get_properties(self):
        return {
            "localEndpoints": [endpoint.clone() for endpoint in self.local_endpoints],
            "remoteEndpoints": [endpoint.clone() for endpoint in self.remote_endpoints],
            "selection": self.transport_properties.get_selection_properties(),
            "connection": self.transport_properties.get_connection_properties(),
            "message": self.message_properties.get_properties(),
            "connectionContext": self.connection_context.get_snapshot(),
            "security": (
                self.security_parameters.get_configuration()
                if self.security_parameters else {}
            ),
        }

    def get_connection_context(self):
        return self.connection_context

    def subscribe_monitoring(self, callback):
        self.connection_context.subscribe(callback, self.loop)
        return callback

    def unsubscribe_monitoring(self, callback):
        self.connection_context.unsubscribe(callback)
        return self

    def set_interface_policy(self, interface_id, **policy):
        self.connection_context.set_interface_policy(interface_id, **policy)
        return self

    def set_protocol_policy(self, protocol, **policy):
        self.connection_context.set_protocol_policy(protocol, **policy)
        return self

    def set_pvd_policy(self, pvd_id, **policy):
        self.connection_context.set_pvd_policy(pvd_id, **policy)
        return self

    def set_address_family_policy(self, family, preference_adjustment=0):
        self.connection_context.set_address_family_policy(
            family,
            preference_adjustment=preference_adjustment,
        )
        return self

    def note_alternate_remote(
        self,
        base_remote,
        alternate_remote,
        *,
        address_family=None,
        protocol=None,
        lifetime=None,
    ):
        self.connection_context.note_alternate_remote(
            base_remote,
            alternate_remote,
            address_family=address_family,
            protocol=protocol,
            lifetime=lifetime,
        )
        return self

    def separate_connection_context(self):
        self.connection_context = ConnectionContext()
        return self

    def get_monitoring_snapshot(self):
        return {
            "connectionContext": self.connection_context.get_snapshot(),
            "properties": self.get_properties(),
        }

    def from_yangfile(self, fname):
        """ Loads the configuration of a the preconnection,
            including endpoints, transport properties
            and security parameters from a yangfile.
        Attributes:
            fname (string, required): Path to yang configuration file.
        """
        with open(fname) as infile:
            text = infile.read()

        if fname.endswith('.xml'):
            precon = self.from_yang(YANG_FMT_XML, text)
        elif fname.endswith('.json'):
            precon = self.from_yang(YANG_FMT_JSON, text)
        else:
            try:
                precon = self.from_yang(YANG_FMT_JSON, text)
            except YangException:
                precon = self.from_yang(YANG_FMT_XML, text)
        return precon

    async def initiate(self, timeout=None):
        """ Initiates the preconnection, i.e. chooses candidate protocol,
            initializes security parameters if an encrypted connection
            was requested, resolves address and finally calls relevant
            connection call.
        """
        # Assertions
        if not self.remote_endpoints:
            raise Exception("A remote endpoint needs "
                            "to be specified to initiate")
        logger.info("Initiating connection.")

        action = "rendezvous" if self._rendezvous_mode else "initiate"
        self._check_configuration(action)
        snapshot = self._snapshot(action)
        if (
            snapshot.transport_properties.get("isolateSession")
            and not self._reuse_isolated_context
        ):
            snapshot.connection_context = ConnectionContext()
            snapshot.connection_context.register_preconnection()
        new_connection = Connection(snapshot)
        # Race the candidate sets
        new_connection.race_task = self.loop.create_task(new_connection.race())
        logger.info("Returning connection object.")
        if timeout is not None:
            try:
                await new_connection.wait_ready(timeout=timeout)
            except BaseException:
                if new_connection.race_task is not None and not new_connection.race_task.done():
                    new_connection.race_task.cancel()
                    await asyncio.gather(
                        new_connection.race_task,
                        return_exceptions=True,
                    )
                new_connection.abort(reason="Initiate timed out")
                raise
        return new_connection

    async def initiate_with_send(
        self,
        data,
        message_context=None,
        end_of_message=True,
        timeout=None,
    ):
        if not end_of_message:
            raise ValueError("InitiateWithSend does not support partial sends")
        connection = await self.initiate()
        await connection.initiate_with_send(
            data,
            message_context,
            end_of_message,
        )
        if timeout is not None:
            await connection.wait_ready(timeout=timeout)
        return connection

    async def listen(self, timeout=None):
        """ Tries to start a listener, first chooses candidate protocol and
            then tries to establish it with the appropriate asyncio function.
        """
        if not self.local_endpoints:
            raise Exception("A local endpoint needs "
                            "to be specified to listen")
        action = "rendezvous" if self._rendezvous_mode else "listen"
        self._check_configuration(action)
        self._framer_configuration_locked = True
        listener = Listener(self, action=action)
        # Create start_listener task so we can return right away
        listener.listen_task = self.loop.create_task(listener.start_listener())
        if timeout is not None:
            try:
                await listener.wait_listening(timeout=timeout)
            except BaseException:
                if listener.listen_task is not None and not listener.listen_task.done():
                    listener.listen_task.cancel()
                await listener.stop()
                raise
        return listener

    async def rendezvous(self, timeout=None, retry_interval=0.25):
        if not self.local_endpoints:
            raise Exception("A local endpoint needs to be specified to rendezvous")
        if not self.remote_endpoints:
            raise Exception("A remote endpoint needs to be specified to rendezvous")

        self._check_configuration("rendezvous")
        self._framer_configuration_locked = True
        listener_preconnection = self.clone()
        connection_preconnection = self.clone()
        listener_preconnection._rendezvous_mode = True
        connection_preconnection._rendezvous_mode = True

        listener = await listener_preconnection.listen()
        deadline = self.loop.time() + timeout if timeout is not None else None
        active_attempts = []
        accept_task = listener.accept()
        ambiguous_direction = False

        def remaining_time():
            if deadline is None:
                return None
            return max(0, deadline - self.loop.time())

        async def wait_with_deadline(awaitable):
            remaining = remaining_time()
            if remaining == 0:
                raise TimeoutError("Rendezvous timed out")
            if remaining is None:
                return await awaitable
            return await asyncio.wait_for(awaitable, remaining)

        async def establish_active():
            last_error = None
            while True:
                connection = await connection_preconnection.initiate()
                active_attempts.append(connection)
                try:
                    await wait_with_deadline(connection._ready_waiter)
                    if connection.protocol == "udp":
                        await wait_with_deadline(connection._first_message_waiter)
                    return connection
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    last_error = exc
                    remaining = remaining_time()
                    if remaining == 0:
                        raise last_error
                    delay = retry_interval
                    if remaining is not None:
                        delay = min(delay, remaining)
                    await asyncio.sleep(delay)

        active_task = self.loop.create_task(establish_active())
        winner = None
        try:
            await wait_with_deadline(listener.wait_listening())

            local_key = self._rendezvous_endpoint_key(
                self.local_endpoints[0],
                protocol="tcp",
            )
            remote_key = self._rendezvous_endpoint_key(
                self.remote_endpoints[0],
                protocol="tcp",
            )
            prefer_active = None
            if local_key != remote_key:
                prefer_active = local_key < remote_key
            else:
                ambiguous_direction = True

            preferred_task = (
                active_task
                if prefer_active is not False
                else accept_task
            )
            alternate_task = (
                accept_task
                if preferred_task is active_task
                else active_task
            )
            done, _pending = await asyncio.wait(
                {preferred_task, alternate_task},
                timeout=remaining_time(),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError("Rendezvous timed out")

            if prefer_active is None:
                completed = next(iter(done))
                winner = completed.result()
            elif preferred_task in done:
                winner = preferred_task.result()
            else:
                alternate = alternate_task.result()
                grace = retry_interval
                remaining = remaining_time()
                if remaining is not None:
                    grace = min(grace, remaining)
                try:
                    winner = await asyncio.wait_for(
                        asyncio.shield(preferred_task),
                        grace,
                    )
                except (TimeoutError, OSError):
                    winner = alternate

            winner._mark_rendezvous_done()
            schedule_callback(
                self.loop,
                self.rendezvous_done,
                (winner,),
                (winner, self),
                (self,),
                (),
            )
            return winner
        except BaseException as exc:
            if not isinstance(exc, asyncio.CancelledError):
                schedule_callback(
                    self.loop,
                    self.establishment_error,
                    (exc, self),
                    (exc,),
                    (self,),
                    (),
                )
            raise
        finally:
            for task in (active_task, accept_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                active_task,
                accept_task,
                return_exceptions=True,
            )
            rendezvous_connections = list(active_attempts)
            for task in (active_task, accept_task):
                if task.cancelled():
                    continue
                try:
                    connection = task.result()
                except BaseException:
                    continue
                if connection not in rendezvous_connections:
                    rendezvous_connections.append(connection)
            for connection in getattr(listener, "_accepted_connections", ()):
                if connection not in rendezvous_connections:
                    rendezvous_connections.append(connection)

            for connection in active_attempts:
                if connection is winner:
                    continue
                if connection.state is not ConnectionState.CLOSED:
                    if ambiguous_direction and winner is not None:
                        winner._rendezvous_companions.append(connection)
                    else:
                        connection.abort("Rendezvous candidate was not selected")
            if ambiguous_direction and winner is not None:
                for connection in rendezvous_connections:
                    if (
                        connection is not winner
                        and connection not in winner._rendezvous_companions
                        and connection.state is not ConnectionState.CLOSED
                    ):
                        winner._rendezvous_companions.append(connection)
            await listener.stop()

    @staticmethod
    def _rendezvous_endpoint_key(endpoint, *, protocol):
        address = endpoint.effective_address() or endpoint.host_name or ""
        try:
            parsed = ipaddress.ip_address(address)
            address_key = (parsed.version, parsed.packed)
        except ValueError:
            address_key = (0, str(address).casefold().encode())
        return (
            address_key,
            endpoint.effective_port(endpoint.protocol or protocol) or 0,
        )

    async def resolve(self):
        """Resolve configured Endpoint names into concrete Endpoint lists."""
        async def resolve_endpoints(endpoints):
            resolved = []
            for endpoint in endpoints:
                if endpoint.host_name is None or endpoint.effective_address() is not None:
                    endpoint_copy = endpoint.clone()
                    if endpoint_copy.port is None and endpoint_copy.service is not None:
                        endpoint_copy.port = endpoint_copy.effective_port(
                            endpoint_copy.protocol
                        )
                    resolved.append(endpoint_copy)
                    continue
                endpoint_port = (
                    endpoint.port
                    if endpoint.port is not None
                    else endpoint.service
                )
                endpoint_info = await self.loop.getaddrinfo(
                    endpoint.host_name,
                    endpoint_port,
                )
                seen = set()
                for info in endpoint_info:
                    key = (info[4][0], info[4][1])
                    if key in seen:
                        continue
                    seen.add(key)
                    endpoint_copy = endpoint.clone()
                    endpoint_copy.address = info[4][0]
                    endpoint_copy.port = info[4][1]
                    resolved.append(endpoint_copy)
            return resolved

        resolved_locals = await resolve_endpoints(
            [
                endpoint
                for endpoint in self.local_endpoints
                if not self._is_pure_stun_candidate(endpoint)
            ]
        )
        resolved_locals.extend(
            await self._resolve_server_reflexive_endpoints()
        )
        resolved_remotes = await resolve_endpoints(self.remote_endpoints)
        return resolved_locals, resolved_remotes

    @staticmethod
    def _is_pure_stun_candidate(endpoint):
        """True for a Local Endpoint whose only identifier is a STUN server.

        Section 7.3 of RFC 9622 has Resolve return concrete addresses. Such an
        endpoint has no concrete local address of its own; the binding it
        discovers is its concrete form.
        """
        return (
            endpoint.stun_server is not None
            and endpoint.address is None
            and endpoint.host_name is None
        )

    async def _resolve_server_reflexive_endpoints(self):
        """Discover NAT bindings for Local Endpoints that name a STUN server.

        Section 7.3 of RFC 9622: when the endpoints are suspected to be behind
        a NAT, Resolve discovers the bindings, and the resulting server
        reflexive candidates are what the application signals to its peer.
        """
        reflexive = []
        for endpoint in self.local_endpoints:
            stun_server = endpoint.stun_server
            if stun_server is None:
                continue
            try:
                address, port, local_port = await discover_reflexive_address(
                    stun_server,
                    local_address=endpoint.address,
                    local_port=endpoint.port or 0,
                    loop=self.loop,
                )
            except (StunError, OSError) as error:
                # A candidate that cannot be discovered is dropped rather than
                # failing Resolve: the host candidates remain usable.
                logger.warning(
                    "STUN binding discovery via %s:%s failed: %s",
                    stun_server.address,
                    stun_server.port,
                    error,
                )
                continue
            candidate = endpoint.clone()
            candidate.address = address
            candidate.port = port
            candidate.stun_server = None
            # The mapping only describes the local port it was learned on, so
            # record it for the Rendezvous that will bind that port.
            candidate.reflexive_local_port = local_port
            reflexive.append(candidate)
        return reflexive

    # Events for active open
    def on_ready(self, callback):
        """ Set callback for ready events that get thrown once the
            connection is ready to send and receive data.

        Args:
            callback (callback, required): Function that implements the
                callback.
        """
        self.ready = callback

    def on_initiate_error(self, callback):
        """ Set callback for initiate error events that
            get thrown if an error occurs during initiation.

        Args:
            callback (callback, required): Function that implements the
                callback.
        """
        self.initiate_error = callback

    def on_establishment_error(self, callback):
        self.establishment_error = callback

    # Events for passive open
    def on_connection_received(self, callback):
        """ Set callback for connection received events that get thrown when a
        new connection has been received by the listener.

        Args:
            callback (callback, required): Function that implements the
                callback.
        """
        self.connection_received = callback

    def on_listen_error(self, callback):
        """ Set callback for listen error events that
            get thrown if an error occurs
            while the listener waits for new connections.

        Args:
            callback (callback, required): Function that implements the
                callback.
        """
        self.listen_error = callback

    def on_stopped(self, callback):
        """ Set callback for stopped events that
            get thrown when the listener stopped
            accepting new connections.

        Args:
            callback (callback, required): Function that implements the
                callback.
        """
        self.stopped = callback

    def on_rendezvous_done(self, callback):
        self.rendezvous_done = callback

    def got_mc(self, listener, data, port, source_address=None):
        """Route a multicast datagram to its source-specific Connection."""
        try:
            cb_data = data
            source_address = (
                source_address
                or (
                    listener.remote_endpoint.effective_address()
                    if listener.remote_endpoint is not None
                    else None
                )
                or listener.local_endpoint.multicast_source
            )
            if source_address is None:
                raise ValueError("Multicast packet did not include a source address")
            remote_key = (source_address, port)
            if remote_key in listener.active_ports:
                listener.active_ports[remote_key].transports[
                    0
                ].datagram_received(
                    cb_data,
                    (source_address, port),
                )
            else:
                rp = RemoteEndpoint()
                rp.with_address(source_address)
                rp.with_port(port)
                precon = Preconnection(
                    local_endpoints=[listener.local_endpoint],
                    remote_endpoints=[rp],
                    transport_properties=listener.transport_properties,
                    security_parameters=listener.security_parameters,
                    event_loop=listener.loop,
                )
                for framer in getattr(listener, "framers", ()):
                    precon.add_framer(framer)
                conn = Connection(precon)
                new_udp = UdpTransport(conn,
                                       conn.local_endpoint,
                                       conn.remote_endpoint)
                listener.active_ports[remote_key] = conn

                async def open_and_deliver():
                    await new_udp.active_open(None)
                    new_udp.datagram_received(cb_data, (source_address, port))

                listener.loop.create_task(open_and_deliver())
                listener._deliver_connection(conn)
                logger.info("Delivered multicast connection to listener.")
        except Exception:
            logger.exception("Error while handling multicast datagram.")
