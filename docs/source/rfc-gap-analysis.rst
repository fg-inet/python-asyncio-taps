RFC Gap Analysis
================

Purpose
-------

This document records an evidence-based audit of the implementation against
the final TAPS RFC set:

- `RFC 9621: Architecture and Requirements for Transport Services
  <https://www.rfc-editor.org/rfc/rfc9621.html>`_
- `RFC 9622: An Abstract Application Programming Interface (API) for
  Transport Services <https://www.rfc-editor.org/rfc/rfc9622.html>`_
- `RFC 9623: Implementing Interfaces to Transport Services
  <https://www.rfc-editor.org/rfc/rfc9623.html>`_

RFC 9621 and RFC 9622 are Standards Track specifications. RFC 9623 is an
Informational implementation guide, so its transport mappings and design
suggestions are engineering targets rather than a strict conformance
checklist.

This audit was last updated on 2026-07-29. It supersedes the earlier
draft-era and incremental implementation notes that previously appeared in
this file.

Status labels
-------------

The labels below deliberately distinguish an API name from working end-to-end
behavior:

- ``verified`` means meaningful runtime behavior exists and is covered by
  relevant automated or integration tests. It is not a formal conformance
  certification.
- ``partial`` means useful behavior exists, but important requirements,
  backend effects, or edge cases remain.
- ``surface-only`` means an API, property, or data structure exists but has
  little or no effect on transport behavior.
- ``missing`` means the feature is not meaningfully implemented.

Validation baseline
-------------------

At the time of this audit:

- ``python -m pytest -q -W error -rs`` reports 303 passed and 6 skipped tests.
- ``python -m ruff check .`` passes.
- Five skips concern the optional ``yang_glue`` extension, and one disables
  an external-network test.
- ``mcrx-core-py`` and ``mctx-core-py`` are installed in the audit
  environment.
- ``aioquic`` 1.3.0 is installed in the audit environment. Real loopback
  integration tests exercise TLS identity and pin verification, concurrent
  peers and mixed stream directions, stream-credit and flow-control
  backpressure, association-wide datagrams, member and association errors,
  deterministic shutdown, bidirectional half-close, session resumption, and
  accepted and rejected stream and DATAGRAM 0-RTT. Validated local QUIC
  handover, passive peer-path observation, group-wide PathChange, and failed
  validation rollback are also exercised. Policy-triggered QUIC handover for
  interface, address, and network-identity changes; native route snapshots;
  Linux netlink and BSD routing-socket event lifecycle and polling fallback;
  retryable make-before-break TCP, UDP, TLS/TCP, QUIC, and multicast Listener
  reconciliation; QUIC accept-socket draining; deterministic Listener
  resource shutdown; multicast source demultiplexing; live RTT and congestion
  state; establishment-latency caching; migration-aware observations; and
  route-scoped candidate ordering from performance history have focused
  coverage.

The suite now has section-mapped RFC 9622 coverage for Endpoints,
configuration snapshots, Security Parameters, Transport and Message
Properties, Send and Receive, Listener, Rendezvous, ConnectionGroup, Close,
Abort, and Message Framers. A green suite is useful evidence, but it is not a formal RFC
conformance certification.

Cross-RFC status
----------------

.. list-table::
   :header-rows: 1
   :widths: 14 14 72

   * - Specification
     - Status
     - Assessment
   * - RFC 9621
     - ``partial``
     - The event-driven, message-oriented object model is recognizably TAPS.
       Monitoring, shared context, candidate selection, cache isolation, and
       several real transports exist. Dynamic System Policy snapshots can
       discover and withdraw interface paths, incorporate native default-route
       and link-state data, update Listeners, and notify active Connections.
       Authoritative cost, metering, and radio feeds, backend enforcement of
       every advertised property, true concurrent multipath, and persistent
       and system-wide scoped caches remain incomplete.
   * - RFC 9622
     - ``partial``
     - The core object model and lifecycle now closely follow the final API:
       configuration snapshots, Initiate, Listen, one-Connection Rendezvous,
       Send and Receive completion, graceful Close, Abort, and group
       entanglement are section-tested. Framer stacks and lifecycle behavior
       are also implemented. Replay-safe QUIC early data is covered for
       streams and DATAGRAMs. Several transport-property effects, advanced
       security callbacks, and some transport-specific capabilities remain
       incomplete, so the implementation should not yet be described as
       fully conformant.
   * - RFC 9623
     - ``partial``
     - Candidate gathering and racing, TCP/UDP/TLS mappings, generic cached
       state, multicast, and a live QUIC association model with session
       tickets, validated handover, and measured performance state provide a
       useful implementation skeleton. Pluggable dynamic System Policy feeds
       future path gathering, live Listener bindings, active-Connection
       advisories, and automatic validated QUIC handover. Native Linux netlink
       and BSD routing-socket events drive policy refreshes. Per-path resolver
       views, pooling, concurrent multipath, non-QUIC RTT instrumentation,
       throughput estimation, and several optional transport mappings remain
       absent or shallow.

RFC 9621 audit
--------------

.. list-table::
   :header-rows: 1
   :widths: 28 14 58

   * - Architecture area
     - Status
     - Evidence and remaining work
   * - Event-driven object model
     - ``verified``
     - ``Preconnection``, ``Connection``, ``Listener``, and
       ``ConnectionGroup`` expose asynchronous actions, callbacks, waiters,
       and ordered lifecycle events.
   * - Message-oriented transfer
     - ``partial``
     - Message contexts are snapshotted at Send, and batching, expiration,
       correlation, partial delivery, message boundaries, and receive
       metadata are covered. Several requested Message Properties cannot yet
       be enforced by all selected transport backends.
   * - Flexible protocol and path choice
     - ``partial``
     - TCP, UDP, TLS/TCP, optional QUIC, and multicast participate in
       selection. Explicit local endpoints participate in candidate bindings,
       dynamic policy providers can supply current interface addresses, and
       QUIC can validate and hand over a live association to a new local path.
       Route-aware discovery and true transport-independent or concurrent
       multipath behavior are absent.
   * - Property-driven stack selection
     - ``partial``
     - Candidate filtering and ordering use final RFC names, defaults, and
       profiles. Some properties are still storage-only, so a preference can
       describe behavior that a selected backend does not fully implement.
   * - Security requirements
     - ``partial``
     - Secure requirements exclude insecure candidates. TLS/TCP and QUIC
       verify the requested peer identity, and certificate pins are checked
       separately from PKI trust in live local tests. Several advanced
       security parameters and callbacks remain metadata-only.
   * - Peer independence from TAPS
     - ``verified``
     - TCP, UDP, TLS, QUIC, and multicast use ordinary wire protocols and do
       not require the peer to expose a TAPS API.
   * - Monitoring
     - ``partial``
     - Event history, lifecycle counts, health summaries, policy snapshots,
       path advisories, and monitoring subscriptions exist. Dynamic policy
       generations notify subscribers and selected-interface withdrawal gives
       active Connections a non-destructive SoftError and path advisory.
       Default-route and available link-state data are refreshed immediately
       from native Linux netlink and BSD routing-socket invalidations, with
       burst coalescing and polling fallback. Authoritative cost, battery, and
       radio feeds are not integrated.
   * - Cached state
     - ``partial``
     - ``ConnectionContext`` records protocol and path outcomes and influences
       future ordering. QUIC client and server session tickets use bounded,
       expiring, policy-bound, single-use caches. A bounded performance cache
       averages and separately expires per-network/path/endpoint/protocol RTT,
       establishment latency, and success history; live QUIC feeds all three
       and equivalent future candidates use the result. Authoritative DNS TTL,
       TLS/TCP tickets, TFO, non-QUIC RTT, throughput, persistence, and subnet
       aggregation are absent.
   * - Cache and session isolation
     - ``verified``
     - Independent Initiate calls with ``isolateSession`` receive separate
       PyTAPS-managed contexts, while clones and members of the same isolated
       group continue to share that group's context. QUIC tickets are not
       copied into a newly isolated context, and neither is performance
       history.
   * - Multistreaming and multipath
     - ``partial``
     - QUIC clones support bidirectional and unidirectional streams plus
       association-wide datagrams in one group. QUIC ``Handover`` validates a
       new local path, rolls back failures, and propagates PathChange across
       the group. Concurrent multipath scheduling and transport-independent
       adaptation are not implemented.

RFC 9622 audit
--------------

Endpoint and pre-establishment objects
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 27 14 59

   * - API area
     - Status
     - Evidence and remaining work
   * - Endpoint model
     - ``verified``
     - Each Endpoint stores at most one identifier of each type. Hostname, IP,
       port, service, interface, protocol qualifier, STUN server, and explicit
       multicast group/source/hop-limit methods are represented and
       validated. Repeated setters replace the identifier.
   * - Endpoint collections
     - ``verified``
     - ``Preconnection`` accepts arrays of Local and Remote Endpoints, and add
       operations append distinct cloned Endpoints. Empty Remote Endpoint
       arrays and wildcard Local Endpoints are supported where the action
       permits them.
   * - Configuration snapshot
     - ``verified``
     - Constructor inputs use call-by-value semantics, and Initiate, Listen,
       and Rendezvous snapshot Endpoints, Transport Properties, Security
       Parameters, Message defaults, callbacks, and Framer configuration.
       Later Preconnection mutation does not alter created objects.
   * - ``Initiate``
     - ``partial``
     - Candidate selection, racing, pre-Ready Send queueing, completion, and
       establishment errors exist, and explicit Local IP/interface
       constraints reach socket bindings. Path/protocol/remote branches resolve
       and race independently, with winner-only state commitment and complete
       sibling cancellation. STUN identifiers and richer derived Endpoint
       types are not expanded into candidates.
   * - ``InitiateWithSend``
     - ``verified``
     - The API rejects partial messages, snapshots the Message Context, and
       produces one completion event. QUIC stream and DATAGRAM candidates can
       transmit a ``safelyReplayable`` Message in genuine 0-RTT. Unsafe or
       fresh-session Messages wait for establishment. Rejected early data
       discards speculative association state and completes the queued Send
       once on a fresh authenticated association.
   * - ``Listen``
     - ``verified``
     - Listeners enforce optional Remote Endpoint constraints, decrement and
       reset the New Connection Limit, emit ``EstablishmentError`` only after
       all candidates fail, deliver established Connections without a
       duplicate Ready event, and stop without closing accepted Connections.
       Real optional-backend coverage is assessed separately below.
   * - ``Rendezvous``
     - ``partial``
     - Rendezvous returns and emits exactly one winning Connection, suppresses
       Ready and ConnectionReceived for that result, waits for the first
       Message on connectionless transports, and is tested between two live
       loopback peers. The implementation uses dual Listen/Initiate with
       deterministic collision resolution; it is not a complete same-port
       TCP simultaneous-open, ICE, STUN, TURN, or NAT-traversal system.

Properties and groups
~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 27 14 59

   * - API area
     - Status
     - Evidence and remaining work
   * - Selection Property catalog
     - ``verified``
     - The complete Section 6.2 catalog, including
       ``perMsgReliability``, is represented with case-insensitive validated
       names and values. Unknown names and invalid values are rejected.
   * - Selection Property defaults
     - ``verified``
     - Section 6.2 defaults are tested property by property, including the
       action-specific ``useTemporaryLocalAddress`` and ``multipath`` defaults
       for Initiate, Listen, and Rendezvous.
   * - Transport Property profiles
     - ``verified``
     - All Appendix B.2 profiles use the exact RFC values. The unreliable
       datagram profile also supplies its ``safelyReplayable`` Message
       default.
   * - Selection Property immutability
     - ``verified``
     - Established Connections expose selected results as read-only values and
       reject attempts to mutate Selection Properties.
   * - Connection Property catalog
     - ``partial``
     - The complete Sections 8.1 and 8.2 tables, defaults, types, and enum
       values are represented and tested. TCP keepalive and reliable-delivery
       timeout requests are applied to OS socket options where available, and
       capacity profiles apply the recommended DSCP classes to ordinary TCP
       and UDP sockets. Unsupported finite rate limits, alternative
       schedulers, RFC 5482 TCP User Timeout advertisement, QUIC stream
       Connection timeouts, and concurrent multipath policies are rejected
       explicitly without closing an established Connection. Checksum,
       minimum-rate, and some platform-specific effects remain advisory.
   * - Read-only properties
     - ``partial``
     - State, send/receive capability, endpoints, protocol, Message defaults,
       sequence values, and useful limits are exposed. The selected
       mode-specific capability set, per-property support classification,
       applied OS effects, and actual bound socket Endpoint are also exposed.
       Some limits and path values remain conservative rather than queried
       dynamically from every backend.
   * - ``ConnectionGroup``
     - ``partial``
     - Every Connection Property except ``connPriority`` is entangled;
       Message Properties remain per Connection. Peer-created member limits,
       shared isolated context, group Close, and group Abort are tested.
       Property mutation validates every member before applying an entangled
       change and rolls the whole group back if a backend rejects it. Generic
       cross-Connection scheduling policies are not implemented.
   * - ``Clone``
     - ``verified``
     - Clones preserve group entanglement and isolated context. Live QUIC
       tests mix bidirectional streams, unidirectional streams, and one raw
       datagram Connection on one association, clone a stream from the
       datagram member, and cover duplicate-datagram Clone failure. Concrete
       QUIC stream and DATAGRAM selection uses the RFC-compliant
       implementation namespace ``_pytaps`` because no IETF-stream RFC defines
       standard properties for those choices.

Data transfer and lifecycle
~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 27 14 59

   * - API area
     - Status
     - Evidence and remaining work
   * - Message Property catalog
     - ``partial``
     - All ten Section 9.1.3 send Properties have exact defaults,
       case-insensitive validated names, types, and enumeration values. Useful
       receive metadata and per-property backend support classifications are
       also represented. Explicit values override inherited defaults even when
       equal to the RFC default. Candidate filtering and Send enforce UDP
       replay safety, ordered-delivery contradictions, and QUIC 0-RTT replay
       safety. Reliability, checksum, capacity, fragmentation, segmentation,
       and ECN requests are not consistently enforceable by every backend.
       QUIC reports received ``isEarlyData`` metadata.
   * - ``Send``
     - ``verified``
     - Send snapshots its Message Context, queues while Establishing,
       preserves action ordering, checks expiry, and emits exactly one of Sent,
       Expired, or SendError even when a backend raises or reports a duplicate
       result.
   * - Partial ``Send``
     - ``partial``
     - Continuations retain Message identity and context, and invalid
       context-free partial sends are rejected. Transport-specific partial
       write reporting and recovery have not been exercised across every
       backend.
   * - ``Receive``
     - ``verified``
     - Awaitable and callback-driven receives, length validation, serialized
       reads, partial stream delivery, EOF completion, UDP ``maxLength``
       splitting, Message Context continuity, and receive metadata are
       section-tested.
   * - Event model
     - ``verified``
     - Ready, RendezvousDone, ConnectionReceived, EstablishmentError, Sent,
       Expired, SendError, Received, ReceivedPartial, ReceiveError, Closed, and
       ConnectionError have section-tested lifecycle ordering for the core
       actions.
   * - ``Close`` and ``Abort``
     - ``verified``
     - Graceful Close drains accepted Sends before transport shutdown. Abort
       fails pending Sends before ConnectionError, and both operations are
       idempotent and available for Connection groups.
   * - Framers
     - ``verified``
     - Per-candidate Framer stacks implement the RFC ordering in both
       directions. Start and Stop can send control data and defer Ready or
       Closed; Framer metadata is namespaced in Message Contexts; prepend,
       passthrough, receive-cursor delivery, and fatal candidate-local failure
       are section-tested. A live TCP client/server demo exercises the same
       path with metadata carried in a framing header.

Security and transport-specific behavior
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 27 14 59

   * - API area
     - Status
     - Evidence and remaining work
   * - TLS security
     - ``verified``
     - CA and identity loading, TLS versions, cipher configuration, SNI, ALPN,
       and hostname identity verification are connected to real TLS contexts.
       A live local TLS test verifies matching and mismatching names.
   * - Certificate pinning
     - ``verified``
     - Certificate pins are checked as exact leaf-certificate matches,
       independently of normal trust and hostname validation. Live TLS tests
       cover matching and mismatching pins. A live QUIC test covers matching
       and mismatching pins after normal CA and hostname validation.
   * - Security callbacks and advanced parameters
     - ``surface-only``
     - PSK, groups, and signature-related fields are partly represented, but
       peer identity challenges, trust verification callbacks, and several
       advanced handshake controls do not affect runtime behavior. QUIC
       session-cache capacity and lifetime do affect the live ticket cache.
   * - Multicast
     - ``partial``
     - Optional mcrx/mctx bindings provide real sender and receiver paths.
       Group, source, hop limit, and interface are represented through the
       final Endpoint API. Cross-platform behavior depends on optional native
       bindings.
   * - QUIC association services
     - ``verified``
     - A TAPS Connection maps to a QUIC stream, mixed bidirectional and
       unidirectional clones share their peer association, and inbound streams
       create grouped Connections with the correct local direction. The group
       survives an idle association between streams. One raw RFC 9221
       datagram Connection can coexist on that association with negotiated
       limits and unreliable, unordered Message metadata. Concurrent Clone
       allocation, peer stream limits, bounded flow-control buffering,
       DATAGRAM size and queue bounds, member-local FIN/Abort/RESET_STREAM/
       STOP_SENDING, association-error fan-out, and resource cleanup are
       covered by live tests. Policy-bound session resumption, replay-safe
       ``InitiateWithSend`` 0-RTT for streams and DATAGRAMs, received
       ``isEarlyData`` metadata, and clean rejection fallback are also covered.
       Peer-created streams on an association that was opened only through
       active ``Initiate`` are rejected because RFC 9622 defines no application
       event through which to deliver them.

RFC 9623 audit
--------------

RFC 9623 is implementation guidance. A ``missing`` item below can be an
intentional scope choice rather than an RFC 9621 or RFC 9622 violation.

.. list-table::
   :header-rows: 1
   :widths: 28 14 58

   * - Implementation area
     - Status
     - Evidence and remaining work
   * - Candidate tree
     - ``partial``
     - Explicit path, protocol, and unresolved Remote Endpoint branches resolve
       concurrently and feed independently staggered leaf attempts. A winner
       atomically commits its transport and cancels/reaps sibling attempts,
       resolver tasks, QUIC associations, and Framer stacks. Service discovery
       and richer nested derivation layers remain absent.
   * - Endpoint gathering
     - ``partial``
     - Hostname resolution, interface addresses, multicast, and alternate
       remote hints exist. Service discovery, STUN-derived,
       server-reflexive, relayed, proxy, and richer protocol-specific
       candidates are missing.
   * - Per-path DNS and Happy Eyeballs
     - ``partial``
     - Resolution is independently scheduled and cached by path, protocol, and
       address family; resolved leaves can race without waiting for slower
       branches, and address-family policy controls ordering. Python's system
       resolver does not expose portable interface-bound DNS views, so distinct
       paths may still query the same underlying resolver configuration.
   * - Protocol gathering and filtering
     - ``verified``
     - Selection Properties filter mode-specific capability descriptions and
       rank only runtime-available backends. Value-sensitive constraints also
       prevent a send-capable profile from selecting UDP unless all Messages
       default to replay safe. Protocol-specific Properties configure a
       selected protocol but do not force its selection. Read-only
       support/effect maps distinguish enforced, advisory, platform-dependent,
       unsupported, no-op, and inapplicable properties.
   * - Listener implementation
     - ``partial``
     - TCP, UDP, TLS/TCP, optional QUIC, and multicast listener paths exist.
       UDP maps peers to separate Connections, and QUIC maps streams by peer
       association. Wildcard sockets follow operating-system changes, while
       interface-only TCP, UDP, TLS/TCP, and QUIC bindings reconcile address
       additions and removals from dynamic System Policy make-before-break
       with bounded retries. Named-interface multicast memberships likewise
       rejoin on address or network-identity changes without dropping the old
       membership before replacement succeeds. Retired QUIC sockets drain
       accepted associations before closing. Explicit addresses correctly
       remain constraints.
   * - Message Framers
     - ``verified``
     - Framer events and actions are serialized per candidate. The
       implementation supports setup and teardown gates, pre-Ready and
       pre-Closed control writes, outbound and inbound stacks, cursor parsing,
       earmarked delivery, dynamic prepend, passthrough, and failure-driven
       candidate rejection.
   * - Dynamic system policy
     - ``partial``
     - Manual and provider-backed protocol, interface, PvD, and address-family
       snapshots are atomically applied and observable. A cancellable monitor
       discovers interface addresses, default routes, and available link
       state; derives route-scoped network identity; withdraws disappeared
       interfaces; changes future candidate and Listener paths; and advises
       affected active Connections without closing them. Linux netlink and BSD
       routing-socket invalidations trigger coalesced refreshes, while polling
       remains a safety net and failure fallback. Apple Network.framework adds
       push-driven path status, interface type, expensive-path metering, and
       Low Data Mode constraints; NetworkManager adds configured or inferred
       metering on Linux. Battery, radio-technology, broader operating-system,
       and administrative policy integrations remain platform work.
   * - Protocol and performance caches
     - ``partial``
     - Generic success and failure histories influence ordering, and DNS
       results are scoped by path, protocol, and address family. QUIC session
       tickets are integrated with ConnectionContext isolation and security
       policy. A bounded, expiring performance cache averages RTT,
       establishment latency, and establishment success per network, local
       address, Remote Endpoint, and protocol. Live QUIC acknowledgement and
       establishment state feeds the cache; candidate path/endpoint ordering
       and racing delays consume it. Throughput, subnet aggregation,
       persistent state, authoritative DNS TTL, TLS/TCP tickets, TFO, and
       non-QUIC RTT sources are not integrated.
   * - Connection pooling
     - ``missing``
     - No general Pooled Connection abstraction or pool selection and reuse
       policy is implemented. QUIC Clone association reuse is narrower than a
       general pool.
   * - Path changes and migration
     - ``partial``
     - Advisories, path snapshots, and re-establishment suggestions exist.
       Dynamic interface withdrawal, selected-address withdrawal, and
       route-scoped network-identity changes produce a SoftError and path
       advisory without aggressively disconnecting a stateful transport.
       QUIC supports active local UDP rebinding with connection-ID rotation,
       PATH_CHALLENGE/PATH_RESPONSE validation, rollback, passive peer-path
       observation, group-wide PathChange, and automatic handover to a
       policy-ranked, constraint-compatible path when active multipath is
       requested. Migration remains disabled when the Selection Property says
       so. Other migration-capable transport mappings, NAT-rebinding
       classification, and concurrent multipath scheduling remain absent.
   * - TCP
     - ``partial``
     - Establishment, listening, transfer, partial delivery, EOF, and
       termination are real and locally integration-tested. Keepalive and
       local reliable-delivery timeout socket options and capacity-profile
       DSCP markings are applied where supported. TCP Fast Open, RFC 5482 User
       Timeout advertisement, broader keepalive controls, kernel RTT sampling,
       and throughput caching are incomplete; generic establishment latency
       and success are cached.
   * - UDP
     - ``partial``
     - Datagram establishment, listening, per-peer Connections, transfer,
       partial delivery, multicast variants, replay-safety enforcement, and
       capacity-profile DSCP markings exist. Checksum, DF, and ECN controls
       are incomplete.
   * - TLS over TCP
     - ``partial``
     - Real TLS handshakes, hostname identity, and exact certificate pinning
       are tested. Resumption state, PSK behavior, and the full security
       callback surface remain incomplete.
   * - QUIC
     - ``partial``
     - The live ``aioquic`` backend implements stream-per-TAPS-Connection
       mapping, mixed stream directions, per-peer grouping, RFC 9221
       datagrams, TLS identity verification, certificate pinning, and
       deterministic member and association lifecycle. Stream-credit waits,
       bounded write buffering, DATAGRAM bounds, concurrent peer isolation,
       reset/error ordering, bidirectional half-close, session resumption, and
       accepted and rejected replay-safe 0-RTT over streams and DATAGRAMs are
       integration-tested. Validated ``Handover`` migration and failure
       rollback are integration-tested across mixed stream and DATAGRAM
       groups. Live RTT and congestion state are exposed, and migration-aware
       RTT plus fresh-association latency and success feed future candidate
       selection without counting cloned streams as new handshakes.
       Concurrent multipath, throughput estimation, general pooling, and
       broader peer interoperability remain.
   * - Multicast receive and send
     - ``partial``
     - Optional native bindings provide useful runtime support through final
       Endpoint modeling. Named-interface Listener memberships track dynamic
       System Policy make-before-break, and received flows are separated by
       source address and port. Portability and broad live integration remain
       dependent on those bindings.
   * - MPTCP
     - ``missing``
     - No MPTCP transport mapping is implemented.
   * - UDP-Lite
     - ``missing``
     - No UDP-Lite transport mapping is implemented.
   * - SCTP
     - ``missing``
     - No SCTP transport mapping is implemented.

The absence of MPTCP, UDP-Lite, or SCTP does not by itself make the abstract
TAPS API nonconformant. Protocol selection must instead advertise and select
only capabilities that the available transport implementations can actually
provide.

Remaining high-priority work
----------------------------

The previous core-lifecycle blockers are now covered: pre-Ready Send, exactly
one Send completion, EOF and UDP partial Receive behavior, graceful Close,
Listener limits and constraints, one-Connection Rendezvous, group isolation,
per-association QUIC grouping, truthful backend capability reporting, and
branch-oriented establishment all have focused tests.

The next priorities are:

1. Broaden QUIC interoperability beyond the local ``aioquic`` peer, including
   migration, early data, stream limits, and failure behavior.
2. Implement the remaining Connection and Message Property effects where
   backend and platform APIs can honor them, especially application rate
   shaping, cross-Connection scheduling, checksum, fragmentation,
   segmentation, and ECN controls. RFC 5482 User Timeout advertisement
   remains a separate optional TCP feature.
3. Deepen RFC 9623 behavior with battery, radio-technology, broader
   operating-system and administrative policy providers, interface-specific
   resolver views, service discovery, pooling, broader protocol caches, and
   additional migration-capable transport mappings.
4. Treat same-port simultaneous open, STUN/ICE/TURN, additional transport
   mappings, and broader multicast portability as deliberate scope choices
   rather than prerequisites for the abstract API surface.

YANG scope
----------

The final RFC 9622 does not define YANG as part of the abstract TAPS API.
Existing YANG examples and ``yang_glue`` support may remain useful project
features, but their completeness and optional-test skips are not RFC 9622
conformance gaps.
