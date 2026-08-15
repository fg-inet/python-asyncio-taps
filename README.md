# python-asyncio-taps

This repository contains an early asyncio-based reference implementation of **TAPS (Transport Services)**. It originally tracked an Internet-Draft-era API and now needs to be aligned with the final published specifications:

- [RFC 9621: Transport Services Architecture](https://www.rfc-editor.org/rfc/rfc9621.html)
- [RFC 9622: Transport Services API](https://www.rfc-editor.org/rfc/rfc9622.html)
- [RFC 9623: Implementing Interfaces to Transport Services](https://www.rfc-editor.org/rfc/rfc9623.html)

The full documentation can be found on [readthedocs.io](https://pytaps.readthedocs.io/en/latest/index.html).

A **transport system** is a novel way to offer transport layer services to the application layer.
It provides an interface on top of multiple different transport protocols, such as TCP, SCTP, UDP, or QUIC. Instead of having to choose a transport protocol itself, the application only provides abstract requirements (*Transport Properties*), e.g., *Reliable Data Transfer*. The transport system maps then maps these properties to specific transport protocols, possibly trying out multiple different protocols in parallel. Furthermore, it can select between multiple local interfaces and remote IP addresses.

TAPS was standardized in the [IETF TAPS Working Group](https://datatracker.ietf.org/wg/taps/about/):

- [RFC 9621: Architecture](https://www.rfc-editor.org/rfc/rfc9621.html)
- [RFC 9622: API](https://www.rfc-editor.org/rfc/rfc9622.html)
- [RFC 9623: Implementation considerations](https://www.rfc-editor.org/rfc/rfc9623.html)

People interested in participating in TAPS can [join the mailing list](https://www.ietf.org/mailman/listinfo/taps).

## Build Dependencies:

YANG support still relies on a shared library. Multicast support now uses the
optional Python package `mcrx-core-py`.

Requirements:

- cmake
- libpcre
- Python 3.10+

You should first create and activate a virtual environment:

~~~
python3 -m venv .venv
source .venv/bin/activate
~~~

Build & Install requirements on Linux(Debian):

~~~
sudo apt-get update
sudo apt-get install -y libpcre3-dev cmake
~~~

Build & Install requirements on MacOS:

~~~
brew install pcre cmake
~~~

At the time of this writing, `libyang` is not packaged everywhere and can be
installed independently or built with the included convenience script:

~~~
INSTALL_PATH=${HOME}/local_install \
  ./build_dependencies.sh
~~~

Build and install the pytaps package:

~~~
python -m pip install .
~~~

If you want to build the optional native YANG extension as part of installation:

~~~
PYTAPS_BUILD_EXTENSIONS=1 \
INSTALL_PATH=${HOME}/local_install \
  python -m pip install .
~~~

For multicast support, install the optional Python binding package:

~~~
python -m pip install -e '.[multicast]'
~~~

That multicast extra now includes both:

- `mcrx-core-py` for multicast receive support
- `mctx-core-py` for multicast sender tooling and examples

For QUIC support, install the optional QUIC extra:

~~~
python -m pip install -e '.[quic]'
~~~

The current QUIC integration follows the RFC direction of mapping a TAPS
`Connection` to a QUIC stream, with shared association state underneath.
Clones can mix bidirectional streams, unidirectional streams, and one
association-wide RFC 9221 QUIC DATAGRAM Connection. Policy-bound session
tickets are cached in the shared `ConnectionContext`, and replay-safe
`InitiateWithSend` Messages can use genuine QUIC 0-RTT over streams or
DATAGRAMs with transparent rejection fallback. Active QUIC associations can
also validate and hand over to a new local UDP path under
`multipathPolicy=Handover`; the resulting `PathChange` is shared across the
Connection Group and failed validation rolls back to the old socket. Live
QUIC RTT plus fresh-association latency and success history feed a bounded,
expiring `ConnectionContext` performance cache for future candidate ordering.
See [`examples/quic_example`](examples/quic_example/README.md) for a runnable
two-host demo.

PyTAPS also provides a pluggable dynamic System Policy extension.
`NativeSystemPolicyProvider` refreshes interfaces, addresses, default routes,
link state where the platform exposes it, and a route-scoped network identity
through `SystemPolicyMonitor`. Linux netlink and BSD/macOS routing-socket
notifications trigger immediate, coalesced refreshes, with periodic polling
as a safety net and fallback; `PortableInterfacePolicyProvider` remains the
address-only fallback. On macOS, the optional `system-policy` extra adds
Network.framework path status, interface type, expensive-path metering, and
Low Data Mode constraints. On NetworkManager-based Linux systems, `nmcli`
supplies configured or inferred metering state. Future Connections use those
paths and costs, interface-following
Listeners reconcile TCP, UDP, TLS/TCP, and QUIC bindings make-before-break
with bounded retries, and active Connections receive a non-destructive path
advisory if their selected path becomes invalid. Retired QUIC accept sockets
drain existing associations while refusing new ones. QUIC associations
configured with `multipath=Active` and `multipathPolicy=Handover`
automatically validate a suitable replacement path when the selected
interface, address, or route-scoped network identity changes, while honoring
the application's Local Endpoint constraints. Named-interface multicast
Listeners also rejoin make-before-break when their interface address or
network identity changes; explicit multicast interface addresses remain
fixed. Custom platform and administrative policy can enrich the same
`SystemPolicyProvider` contract and event-source interface.

For local development with tests:

~~~
python -m pip install -e '.[test,dev,quic,system-policy]'
~~~

### Use

You'll need the path to load the dependent YANG dynamic libraries set whenever pytaps is imported:

	export LD_LIBRARY_PATH=${HOME}/local_install/lib

To run a server with a yang model specified in `examples/yang_example/test-server2.json` run

	python examples/yang_example/yangServer.py -f examples/yang_example/test-server2.json

For a client with a model specified in `examples/yang_example/test-client2.json` run

	python examples/yang_example/yangClient.py -f examples/yang_example/test-client2.json

## Connection Group Send Scheduling

RFC 9622 orders `connPriority` over `msgPriority` (Section 9.2.6): a Message on a
higher-priority Connection is sent before a higher-priority Message on a
lower-priority Connection of the same group. `connScheduler` (Section 8.1.5)
selects which scheduler apportions capacity, using the set from Section 3 of
[RFC 8260](https://www.rfc-editor.org/rfc/rfc8260.html). All six are
implemented:

| Scheduler | Behaviour |
| --- | --- |
| `First-Come, First-Served` | Application delivery order; priorities ignored. |
| `Round-Robin` | Cycles Connections once per Message, regardless of length. |
| `Round-Robin per Packet` | Stays on one Connection until it has filled a packet, so a lost packet affects only that Connection. |
| `Priority-Based` | Strict: drains a higher-priority Connection before starting a lower-priority one. |
| `Fair Capacity` | Equal share of *bytes* per Connection, by virtual finish time. |
| `Weighted Fair Queueing` (default) | Share of bytes proportional to weight, derived from `connPriority`. |

The capacity-aware schedulers account for Message length, not Message count. The
`connPriority` weight is the reciprocal of the priority value, so a Connection at
priority 0 receives twice the capacity of one at priority 1, as RFC 8260
Section 3.6 requires. Weighted Fair Queueing therefore interleaves 2:1 where
Priority-Based would starve the lower-priority Connection — both still satisfy
the Section 9.2.6 first-send rule. A Message marked `final` is sorted last under
every scheduler (Section 9.1.3.5).

The usual abbreviations (`wfq`, `rr-p`, …) and the `SCTP_SS_*` spellings are
accepted and canonicalized. Per RFC 8260 Section 3, the scheduler is chosen at
the sender and is never signalled to the peer.

Batch Messages with the Section 9.2.4 calls and flush the whole group through
its scheduler:

~~~python
connection.start_batch()
await connection.send(request)
await connection.send(follow_up)
await connection.end_batch()          # flushes the group via connScheduler
~~~

`Connection.flush_group_messages()` flushes without batching, and
`Connection.flush_messages()` still flushes just one Connection.

## NAT Binding Discovery (STUN)

Section 7.3 of RFC 9622 uses the Resolve action to discover NAT bindings so a
Rendezvous can offer server-reflexive candidates to its peer. PyTAPS implements
the RFC 8489 Binding Request exchange for this, including short-term
credentials when the application supplies them:

~~~python
host = taps.LocalEndpoint().with_address("192.0.2.5").with_port(9876)
reflexive = taps.LocalEndpoint().with_stun_server("stun.example", 3478)

preconnection = taps.Preconnection(local_endpoints=[host, reflexive], remote_endpoints=[])
local_candidates, _ = await preconnection.resolve()
# Signal local_candidates to the peer, then add what it returns:
preconnection.add_remote_endpoint(peer_candidate)
connection = await preconnection.rendezvous()
~~~

`Resolve` returns concrete addresses — host candidates and discovered
server-reflexive ones. A reflexive candidate carries `reflexive_local_port`,
the local port its mapping was learned on, because a NAT binding only describes
that port. A Local Endpoint whose only identifier is a STUN server resolves to
its discovered binding rather than to an address-less placeholder, and a STUN
server that cannot be reached is skipped rather than failing `Resolve`.

## Interoperability

Section 3.4 of RFC 9621 requires that a peer need not use the same API or
implementation. `tests/test_rfc9621_interop.py` holds that line by talking to
peers built without PyTAPS: raw blocking sockets over TCP and UDP, the
`openssl s_server` and `s_client` tools, and a server written directly against
aioquic's own API.

`tests/test_interop_network_framework.py` goes further and talks to Apple's
Network.framework, which Appendix C of RFC 9623 lists as an existing Transport
Services implementation — so that pairing is two independent TAPS stacks rather
than a TAPS stack and a socket. The peer lives in `tests/interop/nwpeer.swift`
and is compiled on demand; the tests skip off Darwin or without a Swift
toolchain.

One of the four directions, our TLS client against an `NWListener`, is verified
by hand rather than in the suite. `SecPKCS12Import` puts the listener's private
key in the login keychain with an ACL bound to the code signature of the binary
that imported it, and a freshly compiled, ad-hoc signed test peer is a different
application to the keychain, so the handshake stalls waiting for an
authorization no non-interactive run can supply. The module docstring carries
the manual procedure.

## Darwin System Policy

On macOS, PyTAPS reads authoritative path policy from Network.framework via
`nw_path_monitor`, which supplies interface type, expensive/constrained/metered
flags, DNS availability, and default-path status.
`tests/test_darwin_network_framework.py` exercises that binding against the real
framework rather than a stub, so a renamed symbol or a changed constant is
caught. Install it with the `system-policy` extra:

~~~
python -m pip install -e '.[system-policy]'
~~~

## Configuration-Time Errors

Section 3.1 of RFC 9623 asks for a Property set that no available protocol can
satisfy to be reported during preestablishment, "as early as possible", and
Appendix A.2 of RFC 9622 sanctions raising it synchronously. `Initiate`,
`Listen`, and `Rendezvous` therefore raise
`UnsatisfiableTransportProperties` before allocating a Connection or Listener,
naming the constraint that could not be met:

~~~
No available protocol satisfies the Transport Properties for initiate:
no single available protocol satisfies prohibit congestionControl, require reliability
~~~

## Candidate Racing

Section 4.3.2 of RFC 9623 points at the Happy Eyeballs algorithm of
[RFC 8305](https://www.rfc-editor.org/rfc/rfc8305.html) for racing between IP
addresses, so PyTAPS follows it in two respects:

- **Interleaved address families** (RFC 8305 Section 4). Resolved addresses are
  ranked by System Policy and family preference, then the two families are
  interleaved, so a long run of one family cannot stall establishment when
  connectivity over that family is impaired. The "First Address Family Count" —
  how many contiguous addresses of the leading family are attempted before
  alternating — defaults to 1 and is a parameter of `order_remote_addresses`.
- **Bounded staggered delays** (RFC 8305 Section 5). Racing is staggered, never
  simultaneous. The Connection Attempt Delay defaults to 250 ms and is scaled by
  cached path history, bounded to 100 ms at the low end and 2 s at the high end
  — the recommended minimum and maximum. Because the delay is computed from
  history, the bound matters: an attempt must never start within 10 ms of the
  previous one.
- **Failure accelerates the next candidate** (RFC 9623 Section 4.3.2). A child
  that fails before the next child's delay has expired releases that child
  immediately, so a dead address costs a round trip rather than a full stagger.
  Exactly one waiting candidate is released per failure, which keeps the race
  staggered instead of collapsing it into simultaneous racing.

## QUIC Client Socket Binding

PyTAPS binds the QUIC client socket itself rather than delegating to `aioquic`,
which always binds the IPv6 wildcard as a dual-stack socket. A dual-stack
wildcard binding can share a port number with a socket bound to a specific IPv4
address, and the host then delivers datagrams to the more specific binding — so
a Listener on the same host could silently receive a Connection's packets and
that handshake would never complete. PyTAPS binds a socket of the Remote
Endpoint's own address family instead, which makes the conflict visible to the
host, and lets the Local Endpoint constraints of RFC 9622 Section 6.1.2 (address
and interface, not just port) apply to QUIC as they do to every other protocol.

## Connection Establishment Callbacks

RFC 9622 Section 6.3.8 security callbacks block establishment until the
application decides:

~~~python
security = taps.SecurityParameters()
security.set_trust_verification_callback(lambda chain: verify(chain))
security.set_identity_challenge_callback(lambda: passphrase)
~~~

The trust verification callback receives the peer certificate chain once the
peer presents it; returning a falsy value or raising rejects that candidate. It
runs for both TLS and QUIC. The identity challenge callback is invoked when a
private key operation is needed to unlock a protected local identity.

## Running Tests

### Requirements:

- Python 3.10 or above
- `pip install -e .[test]`

### Running

By default, the core test suite runs without optional native extensions:

~~~
python -m pytest -q
~~~

Tests that depend on the optional `yang_glue` extension are skipped unless that
extension is built and importable. The legacy external HTTP check is also
skipped by default; enable it explicitly with:

~~~
python -m pytest -q --run-external
~~~

## Modernization Roadmap

The current codebase still reflects pre-RFC TAPS concepts and naming. A practical path to RFC alignment is:

1. Stabilize packaging and local development on modern Python.
2. Separate optional capabilities such as YANG validation and multicast from the core import path.
3. Audit the existing API against RFC 9622 and document gaps in object model, properties, events, and operations.
4. Introduce missing abstractions incrementally, starting with immutable preestablishment state, richer Transport Properties, and Connection Group support.
5. Rework candidate gathering and racing to follow RFC 9623 guidance on property ordering, path/protocol sorting, and cache usage.
6. Expand interoperability coverage with automated tests for TCP, UDP, TLS, framers, multicast, QUIC, and any other protocol stacks that become part of the maintained scope.
