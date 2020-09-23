import ssl
from xml.etree.ElementTree import fromstring

from .connection import Connection
from .endpoint import LocalEndpoint
from .listener import Listener
from .securityParameters import SecurityParameters
from .transportProperties import TransportProperties
from .transports import *
from .yang_validate import *

logger = setup_logger(__name__, "green")


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
                 transport_properties=TransportProperties(),
                 security_parameters=None,
                 event_loop=asyncio.get_event_loop()):

        # Initializations from arguments
        self.local_endpoint = local_endpoint
        self.remote_endpoint = remote_endpoint
        self.transport_properties = transport_properties
        self.security_parameters = security_parameters

        self.loop = event_loop

        # Callbacks of the application
        self.read = None
        self.initiate_error = None
        self.connection_received = None
        self.listen_error = None
        self.stopped = None
        self.ready = None

        # Framer object
        self.framer = None

        # If security_parameters were given, initialize ssl context
        if self.security_parameters:
            self.security_context = ssl.create_default_context(
                ssl.Purpose.SERVER_AUTH)
            if self.security_parameters.identity:
                logger.info("Identity: " +
                            str(self.security_parameters.identity))
                self.security_context.load_cert_chain(
                    self.security_parameters.identity)
            for cert in self.security_parameters.trustedCA:
                self.security_context.load_verify_locations(cert)
        else:
            self.security_context = None

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
            logger.warning("warning: unexpected root of instance: %s" +
                           " (instead of ietf-taps-api:preconnection" % root.tag)
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
                if trust_ca:
                    sp.add_trust_ca(trust_ca)
                if local_identity:
                    sp.add_identity(local_identity)

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
                if node.text in fn_mapping:
                    fn = fn_mapping.get(node.text)
                    fn(tp, prop_name)
                elif prop_name == 'direction':
                    tp.properties["direction"] = node.text
                else:
                    # TBD jake 2019-05-07: interface name/type, pvd
                    pass

        self.remote_endpoint = rp
        self.local_endpoint = lp
        self.transport_properties = tp
        self.security_parameters = sp
        return self

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
        # TODO: Error handling if precon == None
        return precon

    async def initiate(self):
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
        self.loop.create_task(new_connection.race())
        logger.info("Returning connection object.")
        return new_connection

    async def listen(self):
        """ Tries to start a listener, first chooses candidate protocol and
            then tries to establish it with the appropriate asyncio function.
        """
        if self.local_endpoint is None:
            raise Exception("A local endpoint needs "
                            "to be specified to listen")
        listener = Listener(self)
        # Create start_listener task so we can return right away
        self.loop.create_task(listener.start_listener())
        return listener

    # TODO: Is this actually what the spec talks about?
    async def resolve(self):
        """ Resolve the address before initiating the connection.
        """
        if self.remote_endpoint is None:
            raise Exception("A remote endpoint needs "
                            "to be specified to resolve")
        remote_info = await self.loop.getaddrinfo(
            self.remote_endpoint.host_name, self.remote_endpoint.port)
        self.remote_endpoint.address = remote_info[0][4][0]

    # Set the framer
    # TODO: Multiple framers
    def add_framer(self, framer):
        """ Set a framer with which to frame the messages of the connection.

        Attributes:
            framer (framer, required): Class that implements a TAPS framer.
        """
        self.framer = framer

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
                if self.connection_received:
                    self.loop.create_task(
                        self.connection_received(conn))
                    logger.info("Called connection_received cb")
        except BaseException as e:
            print(e)
