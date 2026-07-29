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
