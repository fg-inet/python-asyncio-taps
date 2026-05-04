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

Yang and multicast support relies on some shared libraries.  Run the script to
download, build, and install them (if not in the default location, then in a place
where LD_LIBRARY_PATH points).

Requirements:

- gcc or clang
- cmake
- libtool
- autotools
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
sudo apt-get install -y autoconf automake libtool
~~~

Build & Install requirements on MacOS:

~~~
brew install pcre cmake autoconf automake libtool
~~~

At the time of this writing, libyang and libmcrx are not packaged and can either
be installed independently, or built with the included convenience script, but
they must also be present for the build to succeed:

~~~
INSTALL_PATH=${HOME}/local_install \
  ./build_dependencies.sh
~~~

Build and install the pytaps package:

~~~
python -m pip install .
~~~

If you want to build the optional native YANG and multicast extensions as part of installation:

~~~
PYTAPS_BUILD_EXTENSIONS=1 \
INSTALL_PATH=${HOME}/local_install \
  python -m pip install .
~~~

For local development with tests:

~~~
python -m pip install -e '.[test,dev]'
~~~

### Use

You'll need the path to load the dependent dynamic libraries set whenever pytaps is imported:

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
6. Expand interoperability coverage with automated tests for TCP, UDP, TLS, framers, multicast, and eventually QUIC or additional protocol stacks.
