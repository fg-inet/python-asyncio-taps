# python-asyncio-taps

This is an implementation of a transport system as described by the **TAPS (Transport Services) Working Group** in the IETF in [draft-ietf-taps-interface-03](https://tools.ietf.org/html/draft-ietf-taps-interface-03).

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

##Yang support:
### Installation
 Install libyang:
	1. git clone https://github.com/CESNET/libyang
	2. mkdir libyang/build
	3. cd libyang/build
	4. cmake -DCMAKE_INSTALL_PREFIX=$HOME/local-installs ..
	5. make && make install

Build the shared library:

  On Linux/FreeBSD/Solaris:
  
	  cd PyTAPS
	  g++ -c -fPIC -I $HOME/local-installs/include validate_yang.cxx -o validate_yang.o
	  g++ validate_yang.o -shared -L $HOME/local-installs/lib -lyang -Wl,-soname,libyangcheck.so -o libyangcheck.so
	  export LD_LIBRARY_PATH=$HOME/local-installs/lib

On MacOS:

	  cd PyTAPS
	  g++ -c -fPIC -I $HOME/local-installs/include validate_yang.cxx -o validate_yang.o
	  g++ validate_yang.o -shared -lyang -dynamiclib -o libyangcheck.so
	  export LD_LIBRARY_PATH=$HOME/local-installs/lib

### Use

To run a server with a yang model specified in `yang_model_server.json` run

	python yangServer.py -f ./yang_mode_server.json
For a client with a model specified in `yang_model_client.json` run

	python yangClient.py -f ./yang_model_client.json