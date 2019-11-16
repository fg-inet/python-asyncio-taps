import asyncio
import ssl
from .yang_validate import *
import xml.etree.ElementTree as ET
from .connection import Connection
from .securityParameters import SecurityParameters
from .transportProperties import *
from .endpoint import LocalEndpoint, RemoteEndpoint
from .utility import *
from .transports import *
from .listener import Listener
color = "red"


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
        yangfile (file, optional):
                        File descriptor of a JSON file containing a
                        TAPS YANG configuration
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

                # Callbacks of the appliction
                self.read = None
                self.initiate_error = None
                self.connection_received = None
                self.listen_error = None
                self.stopped = None
                self.ready = None
                self.initiate_error = None
                self.connection_received = None
                self.listen_error = None
                self.stopped = None
                self.active = False
                # Framer object
                self.framer = None

    def from_yang(frmat, text, *args, **kwargs):
        self = Preconnection(*args, **kwargs)
        if frmat == YANG_FMT_XML:
            validate(frmat, text)
            xml_text = text
        else:
            xml_text = convert(frmat, text, YANG_FMT_XML)
        root = ET.fromstring(xml_text)
        ns = {'taps': 'urn:ietf:params:xml:ns:yang:ietf-taps-api'}

        # jake 2019-05-02: *sigh* thanks for all the hate, xml...
        if root.tag != '{urn:ietf:params:xml:ns:yang:ietf-taps-api}preconnection':
            print_time("warning: unexpected root of instance: %s (instead of ietf-taps-api:preconnection" % (root.tag))
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
                    sp.addTrustCA(trust_ca)
                if local_identity:
                    sp.addIdentity(local_identity)

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

    def from_yangfile(fname, *args, **kwargs):
        """ Loads the configuration of a the preconnection, including endpoints,
        transport properties and security parameters from a yangfile.
        Attributes:
            fname (string, required): Path to yang configuration file.
        """
        with open(fname) as infile:
            text = infile.read()

        if fname.endswith('.xml'):
            return Preconnection.from_yang(YANG_FMT_XML, text, *args, **kwargs)
        elif fname.endswith('.json'):
            return Preconnection.from_yang(YANG_FMT_JSON, text, *args, **kwargs)
        else:
            try:
                check = Preconnection.from_yang(YANG_FMT_JSON, text, *args, **kwargs)
                return check
            except YangException as ye:
                return Preconnection.from_yang(YANG_FMT_XML, text, *args, **kwargs)

    async def initiate(self):
        """ Initiates the preconnection, i.e. chooses candidate protocol,
        initializes security parameters if an encrypted conenction
        was requested, resolves address and finally calls relevant
        connection call.
        """
        # Assertions
        if self.remote_endpoint is None:
            raise Exception("A remote endpoint needs "
                            "to be specified to initiate")
        print_time("Initiating connection.", color)

        new_connection = Connection(self)
        # Race the candidate sets
        self.loop.create_task(new_connection.race())
        print_time("Returning connection object.", color)
        return new_connection

    async def listen(self):
        """ Tries to start a listener, first chooses candidate protcol and
        then tries to establish it with the appropriate asyncio function.
        """
        if self.local_endpoint is None:
            raise Exception("A local endpoint needs "
                            "to be specified to listen")
        # This is a passive connection
        self.active = False
        listener = Listener(self)
        # Create start_listener task so we can return right away
        self.loop.create_task(listener.start_listener())
        return listener

    async def resolve(self):
        """ Resolve the address before initating the connection.
        """
        if self.remote_endpoint is None:
            raise Exception("A remote endpoint needs "
                            "to be specified to resolve")
        remote_info = await self.loop.getaddrinfo(
            self.remote_endpoint.host_name, self.remote_endpoint.port)
        self.remote_endpoint.address = remote_info[0][4][0]

    # Set the framer
    def add_framer(self, a):
        """ Set a framer with which to frame the messages of the connection.

        Attributes:
            framer (framer, required): Class that implements a TAPS framer.
        """
        self.framer = a

    # Events for active open
    def on_ready(self, callback):
        """ Set callback for ready events that get thrown once the connection is ready
        to send and receive data.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.ready = callback

    def on_initiate_error(self, callback):
        """ Set callback for initiate error events that get thrown if an error occurs
        during initiation.

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
        """ Set callback for listen error events that get thrown if an error occurs
        while the listener waits for new connections.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.listen_error = callback

    def on_stopped(self, callback):
        """ Set callback for stopped events that get thrown when the listener stopped
        accepting new connections.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.stopped = callback
