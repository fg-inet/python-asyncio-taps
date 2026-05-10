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
usable RFC-facing core, but it is still a partial implementation of RFC 9622.
The status labels below are practical engineering labels rather than formal
conformance claims:

- ``implemented`` means the repo has a real, tested implementation of the
  feature area
- ``partial`` means a meaningful subset exists, but the RFC surface is broader
- ``missing`` means the feature is not meaningfully present yet

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
|                                             |             | propagation, sorting by connection priority, and limit       |
|                                             |             | enforcement now exist; broader RFC policy semantics remain.  |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Selection Properties                        | partial     | Clear split from connection properties with RFC-style        |
|                                             |             | canonical names and a stronger security property set.        |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Connection Properties                       | partial     | Query/update support exists, but the RFC catalog is not      |
|                                             |             | complete yet.                                                |
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
| ``Listen``                                  | partial     | Works for TCP, UDP, and TLS listeners with explicit          |
|                                             |             | lifecycle state.                                             |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| ``Rendezvous``                              | partial     | Implemented with simultaneous local listen and active        |
|                                             |             | initiate, but still without broader rendezvous policy        |
|                                             |             | semantics.                                                   |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| ``Clone``                                   | partial     | Exists and integrates with connection groups, but semantics  |
|                                             |             | are not fully RFC-complete.                                  |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| ``Send``                                    | partial     | Message context, expiration, batch send, queueing,           |
|                                             |             | and priority scheduling are implemented.                     |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| ``Receive``                                 | partial     | Awaitable and callback-driven receive paths both exist,      |
|                                             |             | including partial stream delivery.                           |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Close / Abort                               | partial     | Connection and group close/abort exist with improved         |
|                                             |             | lifecycle handling, but not the full RFC event surface.      |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Add/Remove Local and Remote Endpoints       | partial     | Implemented on ``Connection`` with basic endpoint merging    |
|                                             |             | and removal behavior.                                        |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Property inspection and mutation            | partial     | Connection, preconnection, listener, and message property    |
|                                             |             | accessors now cover transport, message, and convenience      |
|                                             |             | profile configuration, but the RFC map is not complete.      |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Ready / Closed / Error lifecycle events     | partial     | Much more explicit than the original code, with richer       |
|                                             |             | read-only path and advisory state; still not a complete      |
|                                             |             | RFC event matrix.                                            |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Sent / SendError / Expired events           | partial     | Sent and send-error callbacks exist, and expired messages    |
|                                             |             | now trigger real runtime behavior.                           |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Received / Partial Received events          | partial     | Present and now carry structured message context.            |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Security Parameters                         | partial     | Trust CA, identity, ALPN, SNI, cipher suites, and peer-auth  |
|                                             |             | control exist, but not the full RFC security surface.        |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Framers                                     | partial     | Supported with working helper API and message-context        |
|                                             |             | propagation; still relatively lightweight overall.           |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| QUIC / SCTP                                 | missing     | Not implemented.                                             |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| Multistreaming / Multipath runtime support  | missing     | Property names exist in part, but real transport/runtime     |
|                                             |             | support is not there yet.                                    |
+---------------------------------------------+-------------+--------------------------------------------------------------+
| YANG alignment                              | partial     | Existing YANG examples still work, but the final RFC model   |
|                                             |             | is not fully mapped.                                         |
+---------------------------------------------+-------------+--------------------------------------------------------------+

Where the repository is strongest
---------------------------------

- The core object model is now much cleaner and better structured.
- The establishment path is substantially closer to RFC 9623 than the original
  codebase.
- TLS handling is real rather than nominal, and the test PKI is current.
- Message lifecycle behavior is now materially better, including receive
  waiters, expiration, batching, and priority-aware queue flush.
- The test and lint baseline is healthy enough to support further spec work.

Largest remaining RFC 9622 gaps
-------------------------------

- Complete the RFC 9622 property catalog, especially the remaining connection
  properties and receive-side metadata properties.
- Expand the event model and advisory-error surface beyond the current subset.
- Complete the remaining event and advisory-error surface around the now
  broader establishment API.
- Decide which advanced transports are genuinely in scope for this repository,
  especially QUIC, SCTP, multistreaming, and multipath.
- Build a more systematic conformance matrix and targeted interoperability
  tests.

Recommended next steps
----------------------

1. Complete the property catalog section by section from RFC 9622.
2. Fill out the remaining event and advisory-error semantics.
3. Extend the now-present API surface with fuller RFC event and policy
   semantics, especially around groups and advisory errors.
4. Decide whether this repository will grow into a fuller transport
   implementation or remain a cleaned-up reference subset.
