API Reference
=============

This is the full API reference for the PyTAPS implementation.

.. automodule:: PyTAPS

Local Endpoint
--------------
	.. autoclass:: LocalEndpoint

		.. automethod:: with_interface
		.. automethod:: with_address
		.. automethod:: with_port

Remote Endpoint
---------------
	.. autoclass:: RemoteEndpoint

		.. automethod:: with_address
		.. automethod:: with_hostname
		.. automethod:: with_port

Transport Properties
--------------------
	.. autoclass:: TransportProperties

		.. automethod:: add
		.. automethod:: require
		.. automethod:: prefer
		.. automethod:: ignore
		.. automethod:: avoid
		.. automethod:: prohibit
		.. automethod:: default

Security Parameters
-------------------
	.. autoclass:: SecurityParameters

		.. automethod:: addIdentity
		.. automethod:: addTrustCA

Preconnection
-------------
	.. autoclass:: Preconnection

		.. automethod:: initiate
		.. automethod:: listen
		.. automethod:: resolve
		.. automethod:: frame_with
		.. automethod:: on_ready
		.. automethod:: on_initiate_error
		.. automethod:: on_connection_received
		.. automethod:: on_listen_error
		.. automethod:: on_stopped

Connection
----------
	.. autoclass:: Connection

		.. automethod:: send_message
		.. automethod:: receive
		.. automethod:: close
		.. automethod:: on_ready
		.. automethod:: on_initiate_error
		.. automethod:: on_sent
		.. automethod:: on_send_error
		.. automethod:: on_expired
		.. automethod:: on_received
		.. automethod:: on_received_partial
		.. automethod:: on_receive_error
		.. automethod:: on_connection_error
		.. automethod:: on_closed