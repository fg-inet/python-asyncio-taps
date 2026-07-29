API Usage
=========

Using the PyTAPS API, an application can do the following:

* Create a Preconnection with endpoints, TransportProperties, and SecurityParameters
* Perform one of the following actions on the Preconnection:
	* Initiate: Connect to another endpoint or join a multicast session
	* Listen: Listen for incoming Connections
	* Rendezvous: Simultaneously Listen and Initiate
* To send data, call Send()
* To receive data, call Receive()
* Close the Connection

Creating a Preconnection
------------------------

Before an application can create a Connection, first it has to create a *Preconnection*.
A Preconnection consists of the local and remote endpoints as well as the Transport properties and security security parameters::

	import pytaps as taps

	remote_endpoint = taps.RemoteEndpoint()
	remote_endpoint.with_hostname("example.org")
	remote_endpoint.with_port(80)

	local_endpoint = taps.LocalEndpoint()
	local_endpoint.with_port(6666)

Transport Properties specify which behavior and properties an application expects a new connection to have. This has also an impact on which transport protocol gets chosen by the TAPS system.

The default Transport Properties describe a reliable, ordered connection. The
implementation selects and races compatible transports rather than promising a
specific protocol::

	properties = taps.TransportProperties()

PyTAPS also exposes RFC 9622-style convenience profiles for common transport
intents::

	properties = taps.TransportProperties().reliable_inorder_stream()
	message_properties = taps.TransportProperties().reliable_message()
	datagram_properties = taps.TransportProperties().unreliable_datagram()

To use **TLS, add SecurityParameters** to the Preconnection.
The default SecurityParameters result in the client using the default certificate trust store of the system to validate the peer's certificate, while not setting its own identity.

To specify a Certificate Authority to trust or to set a certificate as the local identity, set SecurityParameters as follows::

	security = taps.SecurityParameters()
	security.add_trust_ca(args.trust_ca)
	security.add_identity(args.local_identity)

To request an **unreliable datagram** service, use the RFC 9622 profile::

	properties = taps.TransportProperties().unreliable_datagram()

To **join a multicast group**, configure your Preconnection :ref:`as described here<Joining a multicast group>`.

After all the prerequisite and optional objects have been configured, the preconnection itself can finally be created::

	preconnection = taps.Preconnection(
		remote_endpoints=[remote_endpoint],
		local_endpoints=[local_endpoint],
		transport_properties=properties,
		security_parameters=security,
	)


Initiating a Connection
-----------------------

To actively initiate a Connection, an application first has to create a Preconnection.

Then the application needs to set a callback on the Preconnection, which will be called once the Connection is ready::

	async def handle_ready():
		print("Connection has been successfully established")

	preconnection.on_ready(handle_ready)

Note that the callback function has to be defined as an *async* function, i.e., a Python asyncio *coroutine*. See `Design decisions <design.rst>`_ for more information on coroutines and for our reasoning why PyTAPS functions and callbacks are coroutines.
There are several other callbacks that can be set on the preconnection, see the full `API reference <reference.rst>`_

After setting the callback, the application can call Initiate. Note that Initiate is a *coroutine* and not a regular function, so it cannot be called directly.

To run a coroutine, an application can create a task from this coroutine and then run the task in an event loop::

	loop = asyncio.get_event_loop()
	loop.create_task(preconnection.initiate())
	loop.run_forever()

The above example runs the event loop until all queued tasks have completed, i.e., the Connections has been established and the on_ready callback has been called.

Alternatively, a coroutine can be called using *await* from within another coroutine::

	async def initiate_connection(preconnection):
		connection = await preconnection.initiate()
		# Returns immediately

	loop = asyncio.get_event_loop()
	loop.create_task(initiate_connection(preconnection))
	loop.run_forever()

Listening for a Connection
--------------------------

Passively listening for new connections is done in a similar way.

First, the application will have to create a Preconnection.

Once this is done, the application will have to set a callback on the Preconnection that gets called once a new connection has been received::

	async def handle_connection_received(connection):
		print("A new connection has been received.")
	
	preconnection.on_connection_received(handle_connection_received)

Similar to an active initiate, the callback is a Python *coroutine* and not a regular function. 
Now the application can get the event loop, call the listen the coroutine and then start to run the event loop::

	loop = asyncio.get_event_loop()
	loop.create_task(preconnection.listen())
	loop.run_forever()

The Listener can cap future ``ConnectionReceived`` deliveries. The value is
decremented after each delivery and can be reset to ``"Infinite"``::

	listener = await preconnection.listen()
	listener.set_new_connection_limit(100)

Rendezvous
----------

To listen and initiate simultaneously from the same Preconnection, call
``rendezvous()``. It completes with the single winning Connection; the
internal Listener and losing candidates are not exposed::

	connection = await preconnection.rendezvous(timeout=5)

The same Connection is delivered to the ``RendezvousDone`` callback. A
connectionless candidate does not complete until its first Message arrives::

	async def handle_rendezvous_done(connection):
		await connection.send(data)

	preconnection.on_rendezvous_done(handle_rendezvous_done)

QUIC streams and datagrams
--------------------------

Install the optional ``aioquic`` backend to use QUIC::

	python -m pip install -e '.[quic]'

PyTAPS maps each stream to a TAPS ``Connection`` while keeping the QUIC
association underneath the ``ConnectionGroup``. ``Clone`` can therefore open
different stream directions on the same association. An actively created
unidirectional stream is send-only locally and receive-only at the peer::

	properties = taps.TransportProperties()
	properties.require("multistreaming")
	properties.set_property("_pytaps.quicStreamType", "Bidirectional")

	bidi = await taps.Preconnection(
		remote_endpoint=remote,
		transport_properties=properties,
		security_parameters=security,
	).initiate()

	uni = await bidi.clone(
		connection_properties={
			"_pytaps.quicStreamType": "Unidirectional",
		}
	)

The supported ``_pytaps.quicStreamType`` values are ``Auto`` (the default),
``Bidirectional``, and ``Unidirectional``. A newly allocated QUIC stream is
not visible to the peer until data or a FIN is transmitted.

RFC 9221 DATAGRAM frames can be used alongside those streams by cloning an
association-wide datagram Connection::

	datagrams = await bidi.clone(
		connection_properties={
			"_pytaps.quicTransportMode": "Datagram",
		}
	)
	await datagrams.send(b"status")

``_pytaps.quicTransportMode`` accepts ``Stream`` (the default) or ``Datagram``.
The datagram Connection reports unreliable, unordered, message-preserving
capabilities and its negotiated Message-size limit through read-only
properties. Since a raw QUIC DATAGRAM frame has no stream or application-flow
identifier, only one raw datagram Connection is exposed per association.
Applications needing several logical datagram flows must carry an appropriate
context identifier in their own protocol.

To initiate with a datagram Connection rather than cloning one, combine the
protocol-specific mode with the unreliable datagram profile::

	datagram_properties = taps.TransportProperties().unreliable_datagram()
	datagram_properties.require("multistreaming")
	datagram_properties.set_property(
		"_pytaps.quicTransportMode",
		"Datagram",
	)

These names deliberately use the RFC 9622 implementation-specific
``_pytaps`` namespace. No IETF-stream RFC currently defines standard
``quic`` Transport Properties for choosing the concrete stream type or the
association-wide DATAGRAM service.

An explicit contradictory requirement, such as required reliability, leaves
no compatible candidate.

Validated QUIC handover
~~~~~~~~~~~~~~~~~~~~~~~

The QUIC backend implements validated single-path handover. Request active
multipath before establishment and retain the default ``Handover`` Connection
Property, then call the PyTAPS ``migrate_path`` extension with a new local
Endpoint::

	properties = taps.TransportProperties()
	properties.require("multistreaming")
	properties.set_property("multipath", "Active")
	properties.set_property("multipathPolicy", "Handover")

	connection = await taps.Preconnection(
		remote_endpoint=remote,
		transport_properties=properties,
		security_parameters=security,
	).initiate()

	new_local = (
		taps.LocalEndpoint()
		.with_address("192.0.2.20")
		.with_port(0)
	)
	current_path = await connection.migrate_path(
		new_local,
		timeout=5,
	)

The old UDP socket remains usable until aioquic validates the candidate path
with QUIC ``PATH_CHALLENGE`` and ``PATH_RESPONSE``. A validation or bind
failure restores the old path and raises the failure. Success updates the
Endpoints and emits ``PathChange`` on every live stream and DATAGRAM
Connection in the group. A passive peer using the Listener default
``multipath=Passive`` observes the same validated handover.

``migrate_path`` is an implementation extension rather than a standard RFC
9622 action. ``multipath=Disabled`` rejects active migration, as required by
RFC 9623. The backend supports ``multipathPolicy=Handover`` only; it does not
claim concurrent ``Interactive`` or ``Aggregate`` multipath.

The ``quicAssociation`` read-only snapshot includes ``currentPath``,
``previousPath``, ``networkPaths``, ``migrationInProgress``,
``pathValidationSuccesses``, and ``pathValidationFailures``. Connection Group
properties expose their shared current and previous path and path-change
count.

Measured performance state
~~~~~~~~~~~~~~~~~~~~~~~~~~

The QUIC association snapshot also exposes a ``performance`` mapping with
``latestRtt``, ``smoothedRtt``, ``minimumRtt``, ``rttVariation``,
``congestionWindow``, and ``bytesInFlight``. RTT values are seconds; the
window and in-flight values are bytes. These are read-only observations from
the selected ``aioquic`` protocol instance.

RFC 9623 performance history is kept in the shared ``ConnectionContext``.
Fresh protocol establishments contribute latency and success-rate samples,
while live QUIC acknowledgements contribute RTT samples. Opening another
stream on an existing QUIC association does not count as another QUIC
handshake. Validated handover immediately attributes a fresh observation to
the committed path.

The cache is bounded to 128 path/endpoint/protocol entries. RTT expires after
five minutes by default, establishment latency after one hour, and success
history after 24 hours. Samples are exponentially averaged. Eligible
candidates with otherwise equivalent application and system-policy ranking
use this state to order network paths and resolved endpoints and to tune
staggered-racing delays. Hard requirements and unavailable-path policy always
take precedence.

Applications and system integrations can inspect or add observations through
the PyTAPS ``ConnectionContext`` extension::

	context = connection.get_connection_context()
	context.record_performance_observation(
		("192.0.2.20", 51000),
		("198.51.100.10", 443),
		"quic",
		network_id="wifi",
		rtt=0.028,
	)
	metrics = context.get_performance_metrics(
		remote_path=("198.51.100.10", 443),
		protocol="quic",
		network_id="wifi",
	)

``get_snapshot()`` exposes the current entries as ``performanceCache``.
Creating an isolated context does not copy performance state. The cache is
in-memory and process-local. The native System Policy provider scopes automatic
network-attachment identity to an interface and its current default gateways;
an explicit interface identifier and then ``default`` are the fallbacks.

Dynamic System Policy
~~~~~~~~~~~~~~~~~~~~~

RFC 9621 defines System Policy as implementation input rather than an
application-facing RFC 9622 API. PyTAPS exposes implementation-extension hooks
for injecting or monitoring that policy. The default native provider
subscribes to Linux netlink or BSD/macOS routing-socket invalidations and
refreshes local interfaces, usable addresses, routes, and link state where
available. Event bursts are coalesced, and periodic polling remains a safety
net and the automatic fallback when native events are unsupported or fail.
With the optional ``system-policy`` extra on macOS, Network.framework also
supplies path status, interface type, expensive-path metering, Low Data Mode
constraints, and push notifications. On Linux, NetworkManager's authoritative
configured or inferred metering state is used when ``nmcli`` is available;
periodic refresh catches metering-only changes while netlink remains the push
source. Neither backend guesses battery or radio-technology state. Install the
macOS binding with::

	python -m pip install -e '.[system-policy]'

The provider falls back cleanly to route and address discovery when an
optional platform integration is unavailable::

	context = taps.ConnectionContext()
	monitor = taps.SystemPolicyMonitor(
		context,
		interval=2,
		debounce_interval=0.05,
	)

	await monitor.refresh()
	monitor.start()
	try:
		connection = await taps.Preconnection(
			remote_endpoint=remote,
			connection_context=context,
		).initiate()
	finally:
		await monitor.stop()

``event_driven`` and ``event_source_name`` report whether a native or custom
push source is active. ``last_trigger``, ``last_event``,
``last_event_source_error``, and ``refresh_count`` expose monitor diagnostics.
An event-source setup or read failure emits
``system_policy_event_source_error`` to ConnectionContext subscribers before
the monitor continues with route notifications when available or polling
otherwise. ``NativeSystemPolicyProvider.last_platform_policy_error`` and
``last_platform_event_source_error`` retain failures from the optional
enrichment layer separately.

Pass ``platform_policy_resolver=False`` to disable automatic platform
enrichment, or supply an ``InterfacePolicyResolver`` implementation whose
synchronous ``snapshot()`` returns policies keyed by interface name. A custom
``cost_resolver`` remains the final override and can apply administrative
policy after native policy has been merged.

When no Local Endpoints were supplied by the application, subsequent
candidate gathering uses the available interface addresses in the current
snapshot. Explicit Local Endpoints remain explicit constraints. A complete
new interface snapshot marks interfaces omitted from it unavailable. Each
supplied policy section is a complete view; sections set to ``None`` are left
unchanged.

Policy updates are atomic and appear under ``systemPolicy`` in the
``ConnectionContext`` monitoring snapshot with their source, generation, and
last-update time. Reapplying an unchanged snapshot does not emit another
update. If an established Connection's selected interface becomes
unavailable, its selected address is withdrawn, or the interface's
route-scoped network identity changes, PyTAPS records
``system_policy_changed`` and ``soft_error``, degrades the cached path, and
refreshes re-establishment guidance without forcibly closing the Connection.
An initiating QUIC association that already uses ``multipath=Active`` and
``multipathPolicy=Handover`` also attempts a validated handover to the
highest-ranked available same-family local path that satisfies every
application-provided Local Endpoint constraint. A network identity change may
validate a fresh socket on the same local address. Fixed local ports are
retained and are never silently replaced by ephemeral ports. Validation
failure records ``path_adaptation_failed``, leaves the old association usable,
and allows configured automatic re-establishment to use the refreshed System
Policy paths. ``multipath=Disabled`` remains advisory only and never triggers
migration.

Wildcard TCP and UDP Listener sockets naturally follow operating-system
interface changes. A Listener configured with an interface but no fixed local
address maintains one TCP, UDP, TLS/TCP, or QUIC binding per usable address
from System Policy, using one port for every address of a protocol. Address
additions are established before obsolete bindings are removed. A transient
bind failure leaves the prior bindings active and schedules a bounded
exponential retry. Explicit address constraints remain explicit.

QUIC associations share their Listener's UDP socket. An obsolete QUIC binding
therefore stops accepting new associations but keeps its socket in a draining
state until existing Connections close; this also lets accepted QUIC
Connections outlive ``Listener.Stop``. ``boundLocalEndpoints`` and
``drainingLocalEndpoints`` distinguish active accept sockets from these
retired sockets. ``systemPolicyPaths``, ``listener_paths_updated``,
``pathReconciliationRetryScheduled``, and ``pathReconciliationError`` expose
the desired path set and reconciliation state.

A multicast Listener constrained by a named interface also follows dynamic
System Policy. IPv4 memberships use the interface's current local address,
while IPv6 uses the stable interface index when available. Address or
route-scoped network-identity changes join the replacement membership before
leaving the old one, and transient join failures use the same bounded retry
state as unicast Listener bindings. ``multicastSubscriptions`` reports the
active group, source, port, and backend interface selector. An explicit
``multicast_interface_address`` remains a fixed application constraint;
an unconstrained multicast Listener leaves route-based path selection to the
backend.

Platform integrations can subclass ``SystemPolicyProvider`` or directly
apply a ``SystemPolicySnapshot`` containing interface, protocol, PvD, and
address-family policy. ``NativeSystemPolicyProvider`` also accepts link-state
and cost resolver hooks. Custom providers can return a
``SystemPolicyEventSource`` from ``create_event_source()`` to trigger the same
coalesced refresh path. Authoritative metering and radio state and automatic
transport-independent migration are not yet supplied.

Session resumption and 0-RTT
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

QUIC session tickets are stored in the shared ``ConnectionContext``. Reusing
the same Preconnection, or otherwise sharing its ConnectionContext, therefore
allows a later association to resume the authenticated session. The cache is
bounded, expires tickets, consumes each ticket at most once, and keys client
tickets by peer and security policy. ``isolateSession`` creates a context that
does not inherit those tickets.

When a ticket is available, ``InitiateWithSend`` can send its Message in
genuine QUIC 0-RTT if the Message Context marks it as safely replayable::

	first = await preconnection.initiate()
	await first.send(b"prime the association")

	# Wait for a ticket before closing in production code. The runnable
	# example demonstrates the asynchronous ticket check.
	await first.close()

	early_context = taps.MessageContext(
		safely_replayable=True,
		final=True,
	)
	resumed = await preconnection.initiate_with_send(
		b"idempotent request",
		early_context,
	)

An unsafe Message is held until handshake completion, even if the session
resumes. On early-data rejection, PyTAPS discards the speculative association
and retries without 0-RTT before completing the queued Send. This avoids
reusing stream or DATAGRAM state from the rejected attempt.

Received Messages expose ``isEarlyData`` in their Message Context. The
``quicAssociation`` read-only property reports ``handshakeComplete``,
``sessionResumed``, ``sessionTicketAvailable``, ``earlyDataAttempted``,
``earlyDataAccepted``, and ``earlyDataRejected``. Applications must still
treat replay-safe early data as potentially replayed by the network.

``SecurityParameters.set_session_cache_capacity()`` and
``set_session_cache_lifetime()`` override the default ticket-cache bounds.
Setting either value to zero disables storage.

See ``examples/quic_example`` for a two-host client/server demo with
certificate verification, both stream directions, datagrams, validated path
handover, session resumption, and 0-RTT.

Sending data
------------

An application can send Messages through an established Connection as follows::

	await connection.send(data)

Message properties can be configured on a ``MessageContext`` and inherited from
the ``Preconnection`` or ``Connection`` when set there as defaults::

	preconnection.set_property("msgPriority", 10)
	connection.set_property("msgCapacityProfile", "Low Latency/Interactive")
	context = connection.new_message_context(msgLifetime=1.5)
	await connection.send(data, context)

Property contracts
~~~~~~~~~~~~~~~~~~

PyTAPS validates properties both while gathering candidates and when an
application changes an established Connection. In particular, UDP does not
protect against duplicate delivery, so a send-capable Preconnection can select
UDP only when every Message is declared replay safe. The unreliable-datagram
profile supplies this default automatically::

	properties = taps.TransportProperties().unreliable_datagram()
	preconnection = taps.Preconnection(
		remote_endpoint=remote,
		transport_properties=properties,
	)

For a broader profile that should retain UDP as a candidate, set the Message
default explicitly before Initiate::

	preconnection.set_property("safelyReplayable", True)

Trying to send a Message with ``safelyReplayable=False`` on a selected UDP
Connection produces ``SendError``. Explicit values in a ``MessageContext``
override inherited defaults even when the explicit value equals the RFC
default.

Changing an unsupported property on an established Connection raises
``ValueError`` or ``NotImplementedError`` without closing the Connection or
partially changing an entangled Connection Group. This includes finite
application rate limits, non-default Connection schedulers, RFC 5482 TCP User
Timeout advertisement, QUIC stream Connection timeouts, and concurrent
multipath policies. Handover is the currently supported active multipath
policy.

TCP keepalive and local reliable-delivery timeout requests use operating-system
socket options where available. Capacity profiles map to the RFC-recommended
DSCP classes on ordinary TCP and UDP sockets. Priority, minimum-rate, partial
checksum, fragmentation, segmentation, and ECN requests remain advisory where
the selected backend cannot enforce them. The read-only ``propertySupport``
and ``propertyEffects`` values distinguish enforced, advisory,
platform-dependent, unsupported, and inapplicable behavior.

Optionally, the application can specify a callback function to be called once the message has been sent, i.e., once PyTAPS has handed the data to the underlying implementation of the used transport protocol::

	async def handle_sent(messageRef):
		print("Message has been sent")

	connection.on_sent(handle_sent)

Using Message Framers
---------------------

A Message Framer can preserve Message boundaries over a byte stream and add
Framer-specific metadata. Framers must be added before ``Initiate``,
``Listen``, or ``Rendezvous`` creates a Connection. If several Framers are
added, the last one added runs first for outbound Messages and last for
inbound data.

The following abbreviated Framer adds a four-byte payload length::

	class LengthPrefixFramer(taps.Framer):
		async def new_sent_message(
			self, connection, data, context, end_of_message
		):
			return len(data).to_bytes(4, "big") + data

		async def handle_received_data(self, connection):
			header, context, _ = self.parse(
				connection,
				minimum_incomplete_length=4,
				maximum_length=4,
			)
			if header is None:
				return
			length = int.from_bytes(header, "big")
			self.advance_receive_cursor(connection, 4)
			context.add(self, "payloadLength", length)
			self.deliver_and_advance_receive_cursor(
				connection, context, length, True
			)

	framer = LengthPrefixFramer(namespace="example.length-prefix")
	preconnection.add_framer(framer)

Framer metadata is namespaced by Framer, so different protocol layers can use
the same key without collisions::

	context = taps.MessageContext()
	context.add(framer, "messageType", "request")
	value = context.get(framer, "messageType")

Framers can delay Connection readiness or closure for a handshake, send
control data during ``start`` or ``stop``, fail an unsuitable candidate,
prepend another Framer before readiness, and enter passthrough mode. See the
``examples/framer_example`` client/server pair for a complete runnable
example.

Receiving data
--------------

PyTAPS is a message-oriented API, and by default, applications receive entire messages.
This works well with a transport protocol that supports message boundaries, such as SCTP, or when using a Framer. However, a stream-oriented transport protocol such as TCP does not preserve message boundaries.

In this case, the application should receive partial messages. For this, the application can either await ``receive()`` directly or set a callback to be called when it receives data, and then call receive::

	async def handle_received_partial(self, data, context, end_of_message):
		print("Received data: " + str(data))

	connection.on_received_partial(handle_received_partial)
	await connection.receive(min_incomplete_length=1)

In case the application has provided a Framer or the underlying transport protocol supports the preservation of message boundaries, an application can receive full messages instead::

	async def handle_received(self, data, context):
		print("Received data: " + str(data))

	connection.on_received(handle_received)
	await connection.receive()

.. warning::

   Without a Framer, a TCP Message is only complete upon receiving a FIN,
   i.e., once the peer has terminated its sending side of the Connection.


Closing a connection
--------------------

An application can set a callback to be executed after the Connection has been closed, and then close the Connection::

	async def handle_closed():
		print("Connection has been closed")

	connection.on_closed(handle_closed)
	connection.close()

Using YANG to configure Preconnections and Endpoints
------------------------------------------------------

PyTAPS allows developers to load configurations from a JSON file that specifies them according to the TAPS YANG model.
To do so, the application calls the from_yangfile function on the preconnection and passes a YANG/JSON file containing the configuration::

	preconnection = taps.Preconnection.from_yangfile(fname)

This will configure the preconnection and endpoints according to the provided YANG file. The application can now continue as usual by setting callbacks and calling initiate/listen.

To achieve a preconnection that is configured the same as the one created in the earlier example, the yang configuration file would have to look like this::

	{
		"ietf-taps-api:preconnection":{
			"remote-endpoints":[
			{
				"id":"1",
				"remote-host":"example.org",
				"remote-port":"80"
			}
			],    "local-endpoints":[
			{
				"id":"1",
				"local-port":"6666"
			}
			]
		}
	}

Joining a multicast group
-------------------------

PyTAPS currently supports Source-Specific Multicast (SSM) through the optional
`mcrx-core-py` Python bindings.

Use the explicit RFC 9622 multicast Endpoint identifiers rather than
overloading ordinary IP-address fields::

	local_endpoint = (
		taps.LocalEndpoint()
		.with_port(5001)
		.with_single_source_multicast_group_ip(
			"ff3e::8000:1234",
			"2001:db8::1",
		)
		.with_interface("en0")
	)
	properties = taps.TransportProperties().unreliable_datagram()
	properties.set_property("direction", "Unidirectional Receive")
	preconnection = taps.Preconnection(
		local_endpoints=[local_endpoint],
		transport_properties=properties,
	)
	listener = await preconnection.listen()
	await listener.wait_listening()

With a dynamic ``SystemPolicyMonitor``, the named interface above follows
address and network-attachment changes make-before-break. Applications that
instead set ``preconnection.multicast_interface_address`` select one fixed
local address. Received multicast flows are keyed by source address and source
port, so distinct senders create distinct Connections even when they use the
same port.

To test against a live multicast source, start the multicast receiver example
with the desired group, source, port, and interface-address values, then send
traffic with the multicast sender example or another multicast-capable sender.
