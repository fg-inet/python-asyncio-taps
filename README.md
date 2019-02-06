# python-asyncio-taps

This is an implementation of a transport system as described by the **TAPS (Transport Services) Working Group** in the IETF in [draft-ietf-taps-interface-02](https://tools.ietf.org/html/draft-ietf-taps-interface-02).

A **transport system** is a novel way to offer transport layer services to the application layer.
It provides an interface on top of multiple different transport protocols, such as TCP, SCTP, UDP, or QUIC. Instead of having to choose a transport protocol itself, the application only provides abstract requirements (*Transport Properties*), e.g., *Reliable Data Transfer*. The transport system maps then maps these properties to specific transport protocols, possibly trying out multiple different protocols in parallel. Furthermore, it can select between multiple local interfaces and remote IP addresses.

TAPS is currently being standardized in the [IETF TAPS Working Group](https://datatracker.ietf.org/wg/taps/about/):
	* [Architecture](https://datatracker.ietf.org/doc/draft-ietf-taps-arch/)
	* [Interface](https://datatracker.ietf.org/doc/draft-ietf-taps-interface/)
	* [Implementation considerations](https://datatracker.ietf.org/doc/draft-ietf-taps-impl/)

People interested in participating in TAPS can [join the mailing list](https://www.ietf.org/mailman/listinfo/taps).

Requirements:

- Python 3.7 or above
- termcolor (pip install termcolor)
