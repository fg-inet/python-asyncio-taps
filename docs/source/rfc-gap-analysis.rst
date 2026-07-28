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

- ``python -m pytest -q -rs`` reports 189 passed and 6 skipped tests.
- ``python -m ruff check .`` passes.
- Five skips concern the optional ``yang_glue`` extension, and one disables
  an external-network test.
- ``mcrx-core-py`` and ``mctx-core-py`` are installed in the audit
  environment.
- ``aioquic`` is not installed in the audit environment. QUIC behavior is
  therefore covered by controlled backend tests, not a real QUIC
  interoperability run.

The suite now has section-mapped RFC 9622 coverage for Endpoints,
configuration snapshots, Security Parameters, Transport and Message
Properties, Send and Receive, Listener, Rendezvous, ConnectionGroup, Close,
and Abort. A green suite is useful evidence, but it is not a formal RFC
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
       several real transports exist. Dynamic operating-system policy,
       backend enforcement of every advertised property, true multipath, and
       richer scoped caches remain incomplete.
   * - RFC 9622
     - ``partial``
     - The core object model and lifecycle now closely follow the final API:
       configuration snapshots, Initiate, Listen, one-Connection Rendezvous,
       Send and Receive completion, graceful Close, Abort, and group
       entanglement are section-tested. Framer semantics, true early data,
       several transport-property effects, advanced security callbacks, and
       some transport-specific capabilities remain incomplete, so the
       implementation should not yet be described as fully conformant.
   * - RFC 9623
     - ``partial``
     - Candidate gathering and racing, TCP/UDP/TLS mappings, generic cached
       state, multicast, and a QUIC stream model provide a useful
       implementation skeleton. Per-path resolution, dynamic system policy,
       pooling, migration, protocol-specific caches, and several optional
       transport mappings remain absent or shallow.

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
       but path discovery remains primarily interface based and true
       transport-independent multipath behavior is absent.
   * - Property-driven stack selection
     - ``partial``
     - Candidate filtering and ordering use final RFC names, defaults, and
       profiles. Some properties are still storage-only, so a preference can
       describe behavior that a selected backend does not fully implement.
   * - Security requirements
     - ``partial``
     - Secure requirements exclude insecure candidates, TLS verifies the
       requested peer identity, and certificate pins are checked separately
       from PKI trust. Several advanced security parameters and callbacks are
       metadata-only, and QUIC lacks a live-backend interoperability test.
   * - Peer independence from TAPS
     - ``verified``
     - TCP, UDP, TLS, QUIC, and multicast use ordinary wire protocols and do
       not require the peer to expose a TAPS API.
   * - Monitoring
     - ``partial``
     - Event history, lifecycle counts, health summaries, policy snapshots,
       path advisories, and monitoring subscriptions exist. They are mostly
       library-generated signals rather than integrations with changing
       operating-system and network policy.
   * - Cached state
     - ``partial``
     - ``ConnectionContext`` records protocol and path outcomes and influences
       future ordering. Cache keys are broad, and DNS, TLS ticket, TFO, RTT,
       latency, and throughput caches are absent.
   * - Cache and session isolation
     - ``verified``
     - Independent Initiate calls with ``isolateSession`` receive separate
       PyTAPS-managed contexts, while clones and members of the same isolated
       group continue to share that group's context.
   * - Multistreaming and multipath
     - ``partial``
     - QUIC clones model stream-per-Connection multistreaming and streams from
       one peer association are grouped. True multipath scheduling,
       migration, and transport-independent adaptation are not implemented.

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
       constraints reach socket bindings. STUN identifiers are not expanded
       into candidates, and racing remains a flattened approximation of the
       RFC 9623 candidate tree.
   * - ``InitiateWithSend``
     - ``partial``
     - The API rejects partial messages, snapshots the Message Context, and
       produces one completion event. It sends after establishment rather than
       using genuine TCP Fast Open or QUIC 0-RTT data.
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
       ``perMsgReliability``, is represented with validated names and values.
       Unknown names and invalid values are rejected.
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
       values are represented and tested. Checksum length, timeouts,
       scheduler, capacity profile, rate limits, multipath policy, and TCP user
       timeout still have incomplete or no backend effect.
   * - Read-only properties
     - ``partial``
     - State, send/receive capability, endpoints, protocol, Message defaults,
       sequence values, and useful limits are exposed. Values are sometimes
       conservative or static rather than queried dynamically from the
       backend and current path.
   * - ``ConnectionGroup``
     - ``partial``
     - Every Connection Property except ``connPriority`` is entangled;
       Message Properties remain per Connection. Peer-created member limits,
       shared isolated context, group Close, and group Abort are tested.
       Generic cross-Connection scheduling policies are not implemented by
       every backend.
   * - ``Clone``
     - ``partial``
     - Clones preserve group entanglement and isolated context. QUIC clones can
       share one association with one TAPS Connection per stream. Generic
       multistreaming backends and live ``aioquic`` CloneError scenarios are
       not covered.

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
     - All ten Section 9.1.3 send Properties have exact defaults and validated
       names, types, and enumeration values. Useful receive metadata is also
       represented. Reliability, checksum, capacity, fragmentation,
       segmentation, ECN, and early-data requests are not consistently
       enforceable by every backend.
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
     - ``surface-only``
     - A single basic Framer can transform data. Framer stacks, lifecycle
       readiness and stop behavior, metadata namespacing, prepend and
       passthrough, and effective failure handling are absent.
       ``Framer.__init__`` also shadows the ``fail_connection`` method with an
       instance attribute.

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
       cover matching and mismatching pins; the QUIC hook is structurally
       tested because ``aioquic`` is unavailable in the audit environment.
   * - Security callbacks and advanced parameters
     - ``surface-only``
     - PSK, cache, groups, and signature-related fields are partly
       represented, but peer identity challenges, trust verification
       callbacks, and several advanced handshake controls do not affect
       runtime behavior.
   * - Multicast
     - ``partial``
     - Optional mcrx/mctx bindings provide real sender and receiver paths.
       Group, source, hop limit, and interface are represented through the
       final Endpoint API. Cross-platform behavior depends on optional native
       bindings.
   * - QUIC multistreaming
     - ``partial``
     - A TAPS Connection maps to a QUIC stream, clones share their peer
       association, inbound streams create Connections, streams from one
       association share a group, and distinct peer associations are kept in
       separate groups. No real ``aioquic`` run was available for this audit.

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
     - Candidates model path, protocol, and derived remote-address dimensions
       in the recommended order. The implementation uses a flattened sequence
       rather than independently timed and cancelled branches.
   * - Endpoint gathering
     - ``partial``
     - Hostname resolution, interface addresses, multicast, and alternate
       remote hints exist. Service discovery, STUN-derived,
       server-reflexive, relayed, proxy, and richer protocol-specific
       candidates are missing.
   * - Per-path DNS and Happy Eyeballs
     - ``partial``
     - Address-family ordering and staggered attempts exist. DNS is resolved
       globally before path expansion rather than independently per path.
   * - Protocol gathering and filtering
     - ``partial``
     - Transport Properties filter and rank available implementations.
       Storage-only properties can still overstate backend support.
   * - Listener implementation
     - ``partial``
     - TCP, UDP, TLS/TCP, optional QUIC, and multicast listener paths exist.
       UDP maps peers to separate Connections, and QUIC maps streams by peer
       association. Dynamic interface and route changes are not watched.
   * - Dynamic system policy
     - ``partial``
     - Manual protocol, interface, PvD, and address-family policy inputs exist.
       There is no operating-system feed for interface, route, cost, battery,
       radio, or network-policy changes.
   * - Protocol and performance caches
     - ``partial``
     - Generic success and failure histories influence ordering. Cache scope
       is broad, and RTT, establishment latency, throughput, DNS TTL, TLS
       ticket, TFO, and other protocol-specific state are not integrated.
   * - Connection pooling
     - ``missing``
     - No general Pooled Connection abstraction or pool selection and reuse
       policy is implemented. QUIC Clone association reuse is narrower than a
       general pool.
   * - Path changes and migration
     - ``partial``
     - Advisories, path snapshots, and re-establishment suggestions exist.
       In-place migration, protocol notification, and multipath scheduling do
       not.
   * - TCP
     - ``partial``
     - Establishment, listening, transfer, partial delivery, EOF, and
       termination are real and locally integration-tested. TCP Fast Open,
       user-timeout behavior, advanced keepalive, and cached performance
       behavior are incomplete.
   * - UDP
     - ``partial``
     - Datagram establishment, listening, per-peer Connections, transfer,
       partial delivery, and multicast variants exist. Checksum, DF, ECN, and
       capacity controls are incomplete.
   * - TLS over TCP
     - ``partial``
     - Real TLS handshakes, hostname identity, and exact certificate pinning
       are tested. Resumption state, PSK behavior, and the full security
       callback surface remain incomplete.
   * - QUIC
     - ``partial``
     - The stream-per-TAPS-Connection mapping and per-peer association grouping
       follow the multiplexed transport guidance. Live backend coverage,
       early data, migration, and general pooling remain.
   * - Multicast receive and send
     - ``partial``
     - Optional native bindings provide useful runtime support through final
       Endpoint modeling. Portability and broad integration remain dependent
       on those bindings.
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
and per-association QUIC grouping all have focused tests.

The next priorities are:

1. Implement the RFC 9622 Framer stack and lifecycle, including readiness,
   stop, failure propagation, metadata namespacing, prepend, and passthrough.
2. Validate and harden QUIC with a real ``aioquic`` installation, including
   multiple simultaneous peers, Clone, close/error ordering, certificate
   validation, and early-data behavior.
3. Make capability selection and read-only values derive from actual backend
   support, then connect currently storage-only Connection and Message
   Properties where the backend can honor them.
4. Deepen RFC 9623 behavior with per-path resolution and racing, scoped
   protocol caches, dynamic system policy, pooling, and path migration.
5. Treat same-port simultaneous open, STUN/ICE/TURN, additional transport
   mappings, and broader multicast portability as deliberate scope choices
   rather than prerequisites for the abstract API surface.

YANG scope
----------

The final RFC 9622 does not define YANG as part of the abstract TAPS API.
Existing YANG examples and ``yang_glue`` support may remain useful project
features, but their completeness and optional-test skips are not RFC 9622
conformance gaps.
