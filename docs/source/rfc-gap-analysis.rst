RFC Gap Analysis
================

This repository predates the final TAPS RFC set and still tracks an older
draft-based API surface. The published specifications that should guide future
work are:

- RFC 9621: Architecture and Requirements for Transport Services
- RFC 9622: An Abstract Application Programming Interface (API) for Transport Services
- RFC 9623: Implementing Interfaces to Transport Services

Current status
--------------

The repository is no longer at its original draft-era baseline. It now has a
usable RFC-facing core across all three TAPS RFCs, but it is still a partial
implementation overall. The status labels below are practical engineering
labels rather than formal conformance claims:

- ``implemented`` means the repo has a real, tested implementation of the
  feature area
- ``partial`` means a meaningful subset exists, but the RFC surface is broader
- ``missing`` means the feature is not meaningfully present yet

Cross-RFC snapshot
------------------

+----------+-------------+-------------------------------------------------------------+
| RFC      | Status      | Summary                                                     |
+==========+=============+=============================================================+
| RFC 9621 | partial     | Core architecture is now recognizably aligned: event-driven |
|          |             | API, message-oriented transfer, connection groups, shared   |
|          |             | connection contexts, cached state, and monitoring snapshots |
|          |             | all exist. The main remaining gaps are depth: richer        |
|          |             | monitoring semantics and broader use of cached state and    |
|          |             | policy during ongoing connection management.                |
+----------+-------------+-------------------------------------------------------------+
| RFC 9622 | partial     | The repo now has a large subset of the abstract API:        |
|          |             | preconnections, listeners, connections, groups,            |
|          |             | rendezvous, message properties, receive/send paths, basic   |
|          |             | framing, and QUIC streams. The largest gaps are remaining   |
|          |             | property-catalog coverage, some lifecycle/event details,    |
|          |             | and a few API semantics that are present only as lighter    |
|          |             | approximations.                                             |
+----------+-------------+-------------------------------------------------------------+
| RFC 9623 | partial     | Candidate gathering, cache-aware racing, shared caches,     |
|          |             | dynamic policy inputs, QUIC stream mapping, and basic       |
|          |             | re-establishment guidance are all present. The biggest      |
|          |             | remaining gaps are richer endpoint gathering (e.g. NAT      |
|          |             | traversal candidates), deeper protocol-state caches, SCTP,  |
|          |             | and actual transport migration/multipath behavior.          |
+----------+-------------+-------------------------------------------------------------+

RFC 9621 checklist
------------------

+---------------------------------------------+-------------+--------------------------------------------------------------+
| Architecture area                           | Status      | Notes                                                        |
+=============================================+=============+==============================================================+
| Event-driven API                            | implemented | Core API is callback/awaitable driven throughout             |
|                                             |             | ``Preconnection``, ``Connection``, and ``Listener``.         |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Data transfer using Messages                | implemented | Messages, message contexts, framing, batching, expiration,   |
|                                             |             | and receive metadata are now first-class concepts.           |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Flexible implementation / protocol choice   | partial     | TCP, UDP, TLS/TCP, multicast send/receive, and QUIC         |
|                                             |             | streams participate in candidate selection; SCTP and         |
|                                             |             | broader advanced transports remain missing.                  |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Selection between equivalent protocol       | partial     | Property-driven selection, cached protocol/path history,     |
| stacks                                      |             | and dynamic policy inputs exist; the eligible transport set  |
|                                             |             | is still relatively small and some policy is heuristic.      |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Monitoring support                          | partial     | Event history, read-only properties, connection-context      |
|                                             |             | snapshots, and re-establishment advice are exposed, but the  |
|                                             |             | RFC's broader monitoring intent is not fully covered.        |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Preestablishment and establishment actions  | partial     | ``Initiate``, ``InitiateWithSend``, ``Listen``, and          |
|                                             |             | ``Rendezvous`` all exist, though rendezvous is still a       |
|                                             |             | lighter subset of the full architecture.                     |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Connection groups                           | partial     | Connection groups exist with entangled properties, shared    |
|                                             |             | connection contexts, grouped close/abort, QUIC stream-based  |
|                                             |             | cloning, and connPriority separation.                        |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Candidate gathering                         | partial     | Path, protocol, and remote-address gathering are present,    |
|                                             |             | but local NAT traversal / relay candidate gathering is not.  |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Candidate racing                            | partial     | Cache-aware, policy-aware racing exists with staggered       |
|                                             |             | attempts and re-establishment guidance; deeper racing modes  |
|                                             |             | and broader transport diversity are still limited.           |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Separating connection contexts              | implemented | Shared ``ConnectionContext`` objects now exist and can be    |
|                                             |             | cloned/forked to isolate cached state between groups.        |
+---------------------------------------------+-------------+--------------------------------------------------------------+

RFC 9622 checklist
------------------

+---------------------------------------------+-------------+--------------------------------------------------------------+
| API area                                    | Status      | Notes                                                        |
+=============================================+=============+==============================================================+
| ``Preconnection`` object                    | partial     | Endpoints, properties, security, YANG loading, ``initiate``, |
|                                             |             | ``listen``, and ``rendezvous`` are implemented; full RFC     |
|                                             |             | API still broader.                                           |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| ``Connection`` object                       | partial     | Send/receive/close, property access, clone support,          |
|                                             |             | lifecycle waiters, batching, and expiration exist.           |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| ``Listener`` object                         | partial     | ``wait_listening()``, ``accept()``, ``stop()``, error        |
|                                             |             | propagation, and lifecycle state are present.                |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| ``ConnectionGroup``                         | partial     | Group-wide close/abort, shared connection-property           |
|                                             |             | propagation, sorting by connection priority, limit           |
|                                             |             | enforcement, and shared connection context now exist.        |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Selection Properties                        | partial     | Clear split from connection properties with RFC-style        |
|                                             |             | canonical names and a stronger security property set.        |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Connection Properties                       | partial     | Query/update support is broader now, including explicit      |
|                                             |             | property tracking and richer read-only inspection, but the   |
|                                             |             | RFC catalog is not complete yet.                             |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Message Properties / Context                | partial     | RFC-style helpers on ``MessageContext`` and                 |
|                                             |             | ``ReceivedMessage`` now exist, along with inherited         |
|                                             |             | defaults and richer receive-side metadata.                  |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| ``Initiate``                                | partial     | Real candidate racing, failure propagation, and waiters      |
|                                             |             | exist, but not all RFC establishment behaviors.              |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| ``InitiateWithSend``                        | implemented | Present and backed by runtime behavior, including            |
|                                             |             | pre-establishment expiration handling.                       |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| ``Listen``                                  | partial     | Works for TCP, UDP, TLS, and QUIC listeners with explicit    |
|                                             |             | lifecycle state.                                             |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| ``Rendezvous``                              | partial     | Implemented with simultaneous local listen and active        |
|                                             |             | initiate, but it still does not expose the RFC's             |
|                                             |             | ``RendezvousDone`` event model directly.                     |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| ``Clone``                                   | partial     | Exists and integrates with connection groups; QUIC clones    |
|                                             |             | now open additional streams on a shared association, but     |
|                                             |             | ``CloneError`` and some failure semantics are still absent.  |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| ``Send``                                    | partial     | Message context, expiration, batch send, queueing,           |
|                                             |             | and priority scheduling are implemented, but exact event     |
|                                             |             | guarantees and partial-send semantics are still lighter.     |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| ``Receive``                                 | partial     | Awaitable and callback-driven receive paths both exist,      |
|                                             |             | including partial stream delivery and receive-side metadata. |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Close / Abort                               | partial     | Connection and group close/abort exist with improved         |
|                                             |             | lifecycle handling, but not the full RFC event surface.      |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Add/Remove Local and Remote Endpoints       | partial     | Implemented on ``Connection`` with basic endpoint merging    |
|                                             |             | and removal behavior.                                        |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Property inspection and mutation            | partial     | Connection, preconnection, listener, and message property    |
|                                             |             | accessors now cover both single-property and aggregate       |
|                                             |             | inspection, but the RFC map is not complete.                 |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Ready / Closed / Error lifecycle events     | partial     | Much more explicit than the original code, with richer       |
|                                             |             | read-only path/advisory state, inspectable event history,    |
|                                             |             | and shared monitoring snapshots; some RFC event names and    |
|                                             |             | ordering guarantees are still approximated.                  |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Sent / SendError / Expired events           | partial     | Sent and send-error callbacks exist, and expired messages    |
|                                             |             | now trigger real runtime behavior, but exact one-event-per-  |
|                                             |             | send guarantees are not exhaustively enforced/tested.        |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Received / Partial Received events          | partial     | Present and now carry structured message context.            |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Security Parameters                         | partial     | Trust CA, identity, ALPN, SNI, cipher suites, peer-auth,     |
|                                             |             | and bulk configuration helpers exist, but not the full RFC   |
|                                             |             | security surface.                                            |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Framers                                     | partial     | Supported with working helper API and message-context        |
|                                             |             | propagation; still relatively lightweight overall.           |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| QUIC / SCTP                                 | partial     | QUIC stream-based connections are now implemented with       |
|                                             |             | shared association state; SCTP is still missing.             |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Multistreaming / Multipath runtime support  | partial     | QUIC now provides real multistreaming support through        |
|                                             |             | stream-per-connection mapping; multipath support is still    |
|                                             |             | not there.                                                   |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| YANG alignment                              | partial     | Existing YANG examples still work, but the final RFC model   |
|                                             |             | is not fully mapped.                                         |
+---------------------------------------------+-------------+--------------------------------------------------------------+

RFC 9623 checklist
------------------

+---------------------------------------------+-------------+--------------------------------------------------------------+
| Implementation area                         | Status      | Notes                                                        |
+=============================================+=============+==============================================================+
| Candidate tree structure                    | implemented | Establishment candidates are explicitly modeled as           |
|                                             |             | path/protocol/endpoint combinations.                         |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Endpoint candidate gathering                | partial     | Hostname resolution, interface-local addresses, multicast,   |
|                                             |             | and alternate remote hints are present; server-reflexive     |
|                                             |             | and relayed candidates are not.                              |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Protocol candidate gathering                | partial     | Property-driven protocol filtering and ordering exist for    |
|                                             |             | TCP, UDP, TLS/TCP, QUIC, and multicast roles.               |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Path candidate gathering                    | partial     | Interface- and PvD-aware path selection exists; per-path     |
|                                             |             | endpoint resolution and richer system-derived paths remain   |
|                                             |             | limited.                                                     |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Candidate racing strategy                   | partial     | Staggered racing, cache-aware ordering, protocol/path bias,  |
|                                             |             | and retry pacing exist; fuller simultaneous/failover         |
|                                             |             | strategies are still limited.                                |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Dynamic system policy                       | partial     | Explicit protocol/interface/PvD/address-family policy        |
|                                             |             | inputs exist, but more external signals such as battery,     |
|                                             |             | radio state, and richer system heuristics are absent.        |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Cached protocol state                       | partial     | Shared protocol/path caches and policy history exist, but    |
|                                             |             | protocol-specific caches such as DNS, TLS tickets, and TFO   |
|                                             |             | are not deeply integrated into behavior.                     |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Separate caches for connection groups       | implemented | ``ConnectionContext`` separation now provides explicit       |
|                                             |             | cache boundaries for grouped or cloned connections.          |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| TCP mapping                                 | partial     | Initiate/listen/send/receive/close are implemented, but the  |
|                                             |             | mapping is still simpler than the full RFC narrative.        |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| UDP mapping                                 | partial     | Datagram send/receive and multicast variants are present;    |
|                                             |             | ancillary controls like DSCP/DF/ECN setting are still thin.  |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| TLS over TCP mapping                        | partial     | Real TLS establishment and security parameter integration    |
|                                             |             | exist, but not every protocol-specific nuance is surfaced.   |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| QUIC / multistreaming mapping               | partial     | TAPS connections map to QUIC streams, listener-side inbound  |
|                                             |             | streams become new connections, and clones share an          |
|                                             |             | association. Migration and broader QUIC features remain      |
|                                             |             | limited.                                                     |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| SCTP mapping                                | missing     | No SCTP transport exists in the repository today.            |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Re-establishment / path adaptation          | partial     | Re-establishment advice, cached path degradation, alternate  |
|                                             |             | remotes, and opt-in automatic re-establishment exist; actual |
|                                             |             | in-place transport migration is not implemented.             |
+---------------------------------------------+-------------+--------------------------------------------------------------+

Where the repository is strongest
---------------------------------

- The core object model is now much cleaner and better structured.
- The establishment path is substantially closer to RFC 9623 than the original
  codebase, including cache-aware protocol/path ordering and pacing, explicit
  protocol policy, and alternate-remote candidate expansion for transports
  that can advertise alternate addresses, plus basic re-establishment guidance
  after path degradation and connection failure, with optional automatic
  re-establishment on top of that guidance.
- TLS handling is real rather than nominal, and the test PKI is current.
- Message lifecycle behavior is now materially better, including receive
  waiters, expiration, batching, and priority-aware queue flush.
- Shared connection context and monitoring snapshots now provide a concrete
  base for RFC 9621 cached-state and monitoring concepts, including explicit
  protocol/interface/PvD/address-family policy inputs and alternate-remote
  hints for establishment ordering, candidate gathering, and ongoing path
  management with automatic re-establishment advice and opt-in action.
- The test and lint baseline is healthy enough to support further spec work.

Largest remaining gaps across the RFC set
-----------------------------------------

- Complete the RFC 9622 property catalog, especially the remaining connection,
  read-only, and receive-side metadata properties.
- Expand the RFC 9622 event surface where the current implementation still
  approximates the abstract API, especially ``RendezvousDone``,
  ``CloneError``, and some event-ordering guarantees.
- Deepen RFC 9623 endpoint gathering to cover server-reflexive and relayed
  candidates, not just local addresses, resolved remotes, and alternate
  remote hints.
- Use cached state more concretely for protocol-specific behavior, not only
  generic ranking; TLS session resumption and similar caches are still mostly
  configuration state rather than active transport heuristics.
- Decide whether SCTP and true multipath transport behavior are in scope for
  this repository. They remain the largest transport-level omissions.
- Build a more systematic conformance matrix and targeted interoperability
  tests that directly map implementation behavior back to the RFC text.

Recommended next steps
----------------------

1. Close the remaining RFC 9622 event/property gaps section by section.
2. Decide whether NAT traversal candidates are in scope, then either add
   them or explicitly mark that part of RFC 9623 as out of scope.
3. Decide whether SCTP and true multipath behavior are in scope, or keep the
   implementation intentionally focused on the current transport set.
4. Build a stricter conformance matrix that tracks each remaining gap as
   ``implemented``, ``partial``, or ``intentionally out of scope``.
