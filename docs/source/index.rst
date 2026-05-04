.. PyTAPS documentation master file, created by
   sphinx-quickstart on Thu Mar 14 22:47:48 2019.
   You can adapt this file completely to your liking, but it should at least
   contain the root `toctree` directive.

Welcome to PyTAPS's documentation!
==================================

PyTAPS is an implementation of a **transport system** as described by the
**TAPS (Transport Services)** Working Group in the IETF. This codebase started
against an early draft of the interface and is being modernized toward the
published RFC set:

- `RFC 9621 <https://www.rfc-editor.org/rfc/rfc9621.html>`_: Transport Services Architecture
- `RFC 9622 <https://www.rfc-editor.org/rfc/rfc9622.html>`_: Transport Services API
- `RFC 9623 <https://www.rfc-editor.org/rfc/rfc9623.html>`_: Implementing Interfaces to Transport Services

PyTAPS provides an asynchronous programming interface which allows applications to transmit and receive messages over transport protocols and network paths dynamically selected at runtime.

As of right now, PyTAPS supports the following features:

    - Creating Preconnection, Endpoint and Connection Objects
    - Protocol selection based on specified transport properties (UDP, TCP and TLS)
    - Actively initiating connections 
    - Passively listening for new connections
    - Configuring Preconnections and Endpoints with YANG
    - Framers, e.g., to preserve message boundaries across TCP
    - Multicast: Joining a multicast group with Source-Specific Multicast (SSM)


.. toctree::
   :maxdepth: 2
   :caption: Contents:

   api
   design
   rfc-gap-analysis
   reference
   license


Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
