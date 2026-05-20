import asyncio
import ssl
from copy import deepcopy
from dataclasses import dataclass
from xml.etree.ElementTree import fromstring

from .connection import Connection
from .connection_context import ConnectionContext
from .endpoint import LocalEndpoint, RemoteEndpoint
from .listener import Listener
from .message import (
    MESSAGE_PROPERTY_DEFAULTS,
    MessageContext,
    canonicalize_message_property_name,
    is_message_property,
)
from .securityParameters import SecurityParameters
from .transportProperties import PreferenceLevel, TransportProperties, normalize_direction
from .transports import UdpTransport
from .utility import schedule_callback, setup_logger
from .yang_validate import (
    YANG_FMT_JSON,
    YANG_FMT_XML,
    YangException,
    convert,
    validate,
)

logger = setup_logger(__name__, "green")


@dataclass
class RendezvousResult:
    connection: "Connection"
    listener: "Listener"
    completed: bool = False
    failed_reason: object = None
    event_history: list = None

    def __post_init__(self):
        if self.event_history is None:
            self.event_history = []

    def _record_event(self, name, **details):
        event = {
            "name": name,
            "details": details,
        }
        self.event_history.append(event)
        return event

    def mark_done(self):
        self.completed = True
        self.failed_reason = None
        self._record_event("rendezvous_done")
        return self

    def mark_failed(self, reason):
        self.completed = False
        self.failed_reason = reason
        self._record_event("rendezvous_error", reason=str(reason))
        return self

    async def wait_ready(self, timeout=None):
        return await self.connection.wait_ready(timeout=timeout)

    async def wait_listening(self, timeout=None):
        return await self.listener.wait_listening(timeout=timeout)

    async def close(self):
        self.connection.close()
        await self.listener.stop()
        await self.connection.wait_closed()
        return self

    def get_event_history(self):
        return list(self.event_history)

    def get_properties(self):
        return {
            "completed": self.completed,
            "failedReason": (
                str(self.failed_reason)
                if self.failed_reason is not None else None
            ),
            "connection": self.connection,
            "listener": self.listener,
            "events": self.get_event_history(),
        }


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

    def __init__(self, local_endpoint=None, remote_endpoint=None,
                 transport_properties=None,
                 security_parameters=None,
                 event_loop=None,
                 connection_context=None):

        # Initializations from arguments
        self.local_endpoint = local_endpoint
        self.remote_endpoint = remote_endpoint
        self.transport_properties = transport_properties or TransportProperties()
        self.security_parameters = security_parameters
        self.message_properties = MessageContext()
        self.connection_context = connection_context or ConnectionContext()
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

        # Framer object
        self.framer = None

        if self.security_parameters:
            self._apply_security_defaults()
        self.security_context = self._build_security_context()

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

    def _build_security_context(self):
        if not self.security_parameters:
            return None

        is_listener = self.local_endpoint and not self.remote_endpoint
        if is_listener:
            security_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        else:
            security_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        if self.security_parameters.identity:
            logger.info("Identity: " + str(self.security_parameters.identity))
            security_context.load_cert_chain(self.security_parameters.identity)
        elif self.security_parameters.public_key:
            logger.info("Public key certificate: " + str(self.security_parameters.public_key))
            security_context.load_cert_chain(
                self.security_parameters.public_key,
                keyfile=self.security_parameters.private_key,
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
        security_context.check_hostname = False
        if is_listener:
            if (
                self.security_parameters.require_peer_authentication
                and self.security_parameters.trustedCA
            ):
                security_context.verify_mode = ssl.CERT_REQUIRED
            else:
                security_context.verify_mode = ssl.CERT_NONE
        elif self.security_parameters.require_peer_authentication:
            security_context.verify_mode = ssl.CERT_REQUIRED
        else:
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

        # TBD: jake 2019-05-02: this api accepts only one endpoint,
        # but the spec talks about accepting multiple endpoints.
        # not clear what to do?  yang
        # and implementation api ideally would match tho...
        # current behavior is to just take the first and stop.

        lp = None
        for node in precon.findall('taps:local-endpoints', namespaces=ns):
            if not lp:
                lp = LocalEndpoint()
            # TBD: jake 2019-05-02: mapping from ifref to interface name?
            interface_ref = node.findtext('taps:ifref', namespaces=ns)
            local_address = node.findtext('taps:local-address', namespaces=ns)
            local_port = node.findtext('taps:local-port', namespaces=ns)
            if interface_ref:
                lp.with_interface(interface_ref)
            if local_address:
                lp.with_address(local_address)
            if local_port:
                lp.with_port(local_port)
            break

        rp = None
        for node in precon.findall('taps:remote-endpoints', namespaces=ns):
            if not rp:
                rp = RemoteEndpoint()
            remote_host = node.findtext('taps:remote-host', namespaces=ns)
            remote_port = node.findtext('taps:remote-port', namespaces=ns)
            if remote_host:
                rp.with_hostname(remote_host)
            if remote_port:
                rp.with_port(remote_port)
            break

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

        self.remote_endpoint = rp
        self.local_endpoint = lp
        self.transport_properties = tp
        self.security_parameters = sp
        if self.security_parameters:
            self._apply_security_defaults()
        self.security_context = self._build_security_context()
        return self

    def clone(self):
        cloned = Preconnection(
            local_endpoint=self.local_endpoint.clone() if self.local_endpoint else None,
            remote_endpoint=self.remote_endpoint.clone() if self.remote_endpoint else None,
            transport_properties=TransportProperties(
                selection_properties=self.transport_properties.get_selection_properties(),
                connection_properties=self.transport_properties.get_connection_properties(),
            ),
            security_parameters=deepcopy(self.security_parameters),
            event_loop=self.loop,
            connection_context=self.connection_context,
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
        cloned.framer = self.framer
        return cloned

    def add_local_endpoint(self, endpoint):
        self.local_endpoint = endpoint
        return self

    def add_remote_endpoint(self, endpoint):
        self.remote_endpoint = endpoint
        return self

    def add_local_address(self, address):
        if self.local_endpoint is None:
            self.local_endpoint = LocalEndpoint()
        self.local_endpoint.with_address(address)
        return self

    def add_remote_address(self, address):
        if self.remote_endpoint is None:
            self.remote_endpoint = RemoteEndpoint()
        self.remote_endpoint.with_address(address)
        return self

    def add_framer(self, framer):
        self.framer = framer
        return self

    def set_property(self, prop, value):
        if is_message_property(prop):
            self.message_properties.set_property(prop, value)
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
            return self
        self.transport_properties.default_property(prop)
        return self

    def get_properties(self):
        return {
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
        if self.remote_endpoint is None:
            raise Exception("A remote endpoint needs "
                            "to be specified to initiate")
        logger.info("Initiating connection.")

        new_connection = Connection(self)
        # Race the candidate sets
        new_connection.race_task = self.loop.create_task(new_connection.race())
        logger.info("Returning connection object.")
        if timeout is not None:
            try:
                await new_connection.wait_ready(timeout=timeout)
            except BaseException:
                if new_connection.race_task is not None and not new_connection.race_task.done():
                    new_connection.race_task.cancel()
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
        connection = await self.initiate()
        connection._pending_message = (data, message_context, end_of_message)
        if timeout is not None:
            await connection.wait_ready(timeout=timeout)
        return connection

    async def listen(self, timeout=None):
        """ Tries to start a listener, first chooses candidate protocol and
            then tries to establish it with the appropriate asyncio function.
        """
        if self.local_endpoint is None:
            raise Exception("A local endpoint needs "
                            "to be specified to listen")
        listener = Listener(self)
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

    async def rendezvous(self, timeout=None):
        if self.local_endpoint is None:
            raise Exception("A local endpoint needs to be specified to rendezvous")
        if self.remote_endpoint is None:
            raise Exception("A remote endpoint needs to be specified to rendezvous")

        listener_preconnection = self.clone()
        connection_preconnection = self.clone()
        connection_preconnection._rendezvous_mode = True

        listener = await listener_preconnection.listen()
        await listener.wait_listening(timeout=timeout)
        connection = await connection_preconnection.initiate()
        result = RendezvousResult(connection=connection, listener=listener)
        try:
            if timeout is not None:
                await asyncio.wait_for(
                    asyncio.gather(
                        result.wait_listening(),
                        result.wait_ready(),
                    ),
                    timeout,
                )
            result.mark_done()
            connection._record_event(
                "rendezvous_done",
                listener_state=listener.state.name.title(),
            )
            schedule_callback(
                self.loop,
                self.rendezvous_done,
                (result, connection),
                (connection,),
                (self,),
                (),
            )
        except BaseException as exc:
            result.mark_failed(exc)
            connection.abort(reason="Rendezvous failed")
            await listener.stop()
            raise
        return result

    # TODO: Is this actually what the spec talks about?
    async def resolve(self):
        """ Resolve the address before initiating the connection.
        """
        if self.remote_endpoint is None:
            raise Exception("A remote endpoint needs "
                            "to be specified to resolve")
        remote_info = await self.loop.getaddrinfo(
            self.remote_endpoint.host_name, self.remote_endpoint.port)
        self.remote_endpoint.address = [remote_info[0][4][0]]
        return self.remote_endpoint.address

    # Events for active open
    def on_ready(self, callback):
        """ Set callback for ready events that get thrown once the
            connection is ready to send and receive data.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.ready = callback

    def on_initiate_error(self, callback):
        """ Set callback for initiate error events that
            get thrown if an error occurs during initiation.

        Attributes:
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

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.connection_received = callback

    def on_listen_error(self, callback):
        """ Set callback for listen error events that
            get thrown if an error occurs
            while the listener waits for new connections.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.listen_error = callback

    def on_stopped(self, callback):
        """ Set callback for stopped events that
            get thrown when the listener stopped
            accepting new connections.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.stopped = callback

    def on_rendezvous_done(self, callback):
        self.rendezvous_done = callback

    # TODO: Refactor this probably
    def got_mc(self, listener, data, port):
        """ Method that redirects incoming multicast
            data to the relevant connection object
        """
        try:
            cb_data = data
            addr = listener.remote_endpoint.address
            if port in listener.active_ports:
                listener.active_ports[port].transports[0].datagram_received(
                    cb_data, (addr, port))
            else:
                rp = RemoteEndpoint()
                rp.with_address(listener.remote_endpoint.address)
                rp.with_port(port)
                precon = Preconnection(listener.local_endpoint,
                                       rp,
                                       listener.transport_properties,
                                       listener.security_parameters,
                                       listener.loop)
                if listener.framer:
                    precon.add_framer(listener.framer)
                conn = Connection(precon)
                new_udp = UdpTransport(conn,
                                       conn.local_endpoint,
                                       conn.remote_endpoint)
                listener.active_ports[port] = conn
                listener.loop.create_task(new_udp.active_open(None))
                listener._deliver_connection(conn)
                logger.info("Delivered multicast connection to listener.")
        except Exception:
            logger.exception("Error while handling multicast datagram.")
