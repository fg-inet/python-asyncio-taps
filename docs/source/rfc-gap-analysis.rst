RFC Gap Analysis
================

This repository predates the final TAPS RFC set and still tracks an older
draft-based API surface. The published specifications that should guide future
work are:

- RFC 9621: Architecture and Requirements for Transport Services
- RFC 9622: An Abstract Application Programming Interface (API) for Transport Services
- RFC 9623: Implementing Interfaces to Transport Services

Initial observations
--------------------

- The public API is centered on ``Preconnection``, ``Connection``, and
  ``Listener``, which is compatible with the overall TAPS architecture, but the
  available operations and event coverage are much smaller than the final API.
- Transport properties are still modeled using an older, smaller property set
  and do not yet include the full RFC 9622 property catalog.
- Candidate gathering and racing exist, but they currently implement a much
  simpler protocol ranking scheme than the branch-sorting and cache-informed
  candidate selection described in RFC 9623.
- Optional YANG and multicast integrations are wired into the repository, but
  they are not yet packaged as cleanly optional features for modern installs.

Suggested phases
----------------

Phase 1: Modernize the developer surface

- keep package importable on current Python versions
- declare dependencies and optional features explicitly
- make automated tests runnable in a clean environment

Phase 2: Define the spec delta

- map every RFC 9622 API object, method, callback, property, and event to
  current implementation status
- classify each item as implemented, partial, missing, or draft-only legacy

Phase 3: Reconcile the core object model

- make Preconnection state immutable after Initiate or Listen
- add a model for Connection Groups and cloned Connections
- distinguish Selection Properties from Connection Properties

Phase 4: Rework establishment and racing

- gather candidate paths, endpoints, and protocol stacks separately
- sort branches using prohibited, required, preferred, and avoided properties
- incorporate cached state and policy hooks per RFC 9623

Phase 5: Expand transfer semantics

- extend message contexts and message properties
- align send and receive events with the RFC 9622 lifecycle
- support additional establishment patterns such as InitiateWithSend and Rendezvous

Phase 6: Grow transport and security coverage

- refresh TLS handling against the RFC security parameter model
- assess protocol support for QUIC, SCTP, multipath, and multistreaming
- add targeted conformance and interoperability tests
