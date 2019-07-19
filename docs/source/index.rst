.. PyTAPS documentation master file, created by
   sphinx-quickstart on Thu Mar 14 22:47:48 2019.
   You can adapt this file completely to your liking, but it should at least
   contain the root `toctree` directive.

Welcome to PyTAPS's documentation!
==================================

PyTAPS is an implementation of a **transport system** as described by the **TAPS (Transport Services)** Working Group in the IETF in `draft-ietf-taps-interface-04 <https://tools.ietf.org/html/draft-ietf-taps-interface-04>`_.

PyTAPS provides an asynchronous programming interface which allows applications to transmit and receive messages over transport protocols and network paths dynamically selected at runtime.

As of right now, PyTAPS supports the following features:

    - Creating Preconnection, Endpoint and Connection Objects
    - Protocol selection based on specified transport properties (UDP, TCP and TLS)
    - Actively initiating connections 
    - Passively listening for new connections
    - Configuring Preconnections and Endpoints with YANG
    - Framers, e.g., to preserve message boundaries across TCP


.. toctree::
   :maxdepth: 2
   :caption: Contents:

   api
   design
   reference
   license


Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
