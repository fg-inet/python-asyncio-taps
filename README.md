# python-asyncio-taps

This is an implementation of a transport system as described by the **TAPS (Transport Services) Working Group** in the IETF in [draft-ietf-taps-interface-04](https://tools.ietf.org/html/draft-ietf-taps-interface-04). The full documentation can be found on [readthedocs.io](https://pytaps.readthedocs.io/en/latest/index.html)

A **transport system** is a novel way to offer transport layer services to the application layer.
It provides an interface on top of multiple different transport protocols, such as TCP, SCTP, UDP, or QUIC. Instead of having to choose a transport protocol itself, the application only provides abstract requirements (*Transport Properties*), e.g., *Reliable Data Transfer*. The transport system maps then maps these properties to specific transport protocols, possibly trying out multiple different protocols in parallel. Furthermore, it can select between multiple local interfaces and remote IP addresses.

TAPS is currently being standardized in the [IETF TAPS Working Group](https://datatracker.ietf.org/wg/taps/about/):

- [Architecture](https://datatracker.ietf.org/doc/draft-ietf-taps-arch/)
- [Interface](https://datatracker.ietf.org/doc/draft-ietf-taps-interface/)
- [Implementation considerations](https://datatracker.ietf.org/doc/draft-ietf-taps-impl/)

People interested in participating in TAPS can [join the mailing list](https://www.ietf.org/mailman/listinfo/taps).
## Requirements:

- Python 3.7 or above
- termcolor (pip install termcolor)
- pytest, pytest-asyncio, pytest-timeout (for tests)

## Build Dependencies:

Yang support relies on some shared libraries.  Run the script to download, build,
and install them (if not in the default location, then in a place where
LD_LIBRARY_PATH points).

Requirements:

- gcc or clang
- cmake
- libtool
- autotools
- libpcre
- Python3.7+

For example, to build under Linux(Debian):

~~~
sudo apt-get update
sudo apt-get install -y libpcre3-dev cmake
sudo apt-get install -y autoconf automake libtool
~~~
Install requirements on MacOS:
~~~
brew install pcre cmake autoconf automake libtool
~~~
Setup virtual environment and install library:
~~~
INSTALL_PATH=${HOME}/local_install \
  ./build_dependencies.sh

INSTALL_PATH=${HOME}/local_install \
  python setup.py build install
~~~

### Use

You'll need the path to load the dependent dynamic libraries set whenever pytaps is imported:

	export LD_LIBRARY_PATH=${HOME}/local_install/lib

To run a server with a yang model specified in `examples/yang_example/test-server2.json` run

	python examples/yang_example/yangServer.py -f examples/yang_example/test-server2.json

For a client with a model specified in `examples/yang_example/test-client2.json` run

	python examples/yang_example/yangClient.py -f examples/yang_example/test-client2.json

## Running Tests

	cd tests/
	./run_tests.sh
