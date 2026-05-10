API Reference
=============

This is the full API reference for the PyTAPS implementation.

.. automodule:: pytaps

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
		.. automethod:: apply_profile
		.. automethod:: require
		.. automethod:: prefer
		.. automethod:: ignore
		.. automethod:: avoid
		.. automethod:: prohibit
		.. automethod:: default
		.. automethod:: reliable_inorder_stream
		.. automethod:: reliable_message
		.. automethod:: unreliable_datagram

Security Parameters
-------------------
	.. autoclass:: SecurityParameters

		.. automethod:: add_identity
		.. automethod:: add_trust_ca
		.. automethod:: add_allowed_security_protocol
		.. automethod:: add_pinned_server_certificate
		.. automethod:: add_alpn_protocol
		.. automethod:: with_server_name
		.. automethod:: disable_peer_authentication
		.. automethod:: set_cipher_suites

Preconnection
-------------
	.. autoclass:: Preconnection

		.. automethod:: initiate
		.. automethod:: initiate_with_send
		.. automethod:: listen
		.. automethod:: rendezvous
		.. automethod:: resolve
		.. automethod:: add_framer
		.. automethod:: on_ready
		.. automethod:: on_initiate_error
		.. automethod:: on_establishment_error
		.. automethod:: on_connection_received
		.. automethod:: on_listen_error
		.. automethod:: on_stopped
		.. automethod:: on_rendezvous_done

Rendezvous Result
-----------------
	.. autoclass:: RendezvousResult

		.. automethod:: wait_ready
		.. automethod:: wait_listening
		.. automethod:: close

Connection
----------
	.. autoclass:: Connection

		.. automethod:: new_message_context
		.. automethod:: get_message_properties
		.. automethod:: send
		.. automethod:: send_message
		.. automethod:: send_batch
		.. automethod:: enqueue_message
		.. automethod:: flush_messages
		.. automethod:: receive
		.. automethod:: receive_message
		.. automethod:: wait_ready
		.. automethod:: wait_closed
		.. automethod:: note_path_change
		.. automethod:: close
		.. automethod:: on_ready
		.. automethod:: on_initiate_error
		.. automethod:: on_establishment_error
		.. automethod:: on_rendezvous_done
		.. automethod:: on_sent
		.. automethod:: on_send_error
		.. automethod:: on_expired
		.. automethod:: on_received
		.. automethod:: on_received_partial
		.. automethod:: on_receive_error
		.. automethod:: on_soft_error
		.. automethod:: on_path_change
		.. automethod:: on_connection_error
		.. automethod:: on_closed

Listener
--------
	.. autoclass:: Listener

		.. automethod:: wait_listening
		.. automethod:: accept
		.. automethod:: wait_stopped
		.. automethod:: stop

Connection Group
----------------
	.. autoclass:: ConnectionGroup

		.. automethod:: close
		.. automethod:: abort

Message Context
---------------
	.. autoclass:: MessageContext

		.. automethod:: add
		.. automethod:: get
		.. automethod:: get_local_endpoint
		.. automethod:: get_remote_endpoint

Received Message
----------------
	.. autoclass:: ReceivedMessage

		.. automethod:: get
		.. automethod:: get_properties
		.. automethod:: get_read_only_properties

Framer
------
	.. autoclass:: Framer

		.. automethod:: start
		.. automethod:: new_sent_message
		.. automethod:: handle_received_data
		.. automethod:: send
		.. automethod:: parse
		.. automethod:: advance_receive_cursor
		.. automethod:: deliver_and_advance_receive_cursor
		.. automethod:: deliver
