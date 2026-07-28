API Reference
=============

This is the full API reference for the PyTAPS implementation.

.. automodule:: pytaps

Connection Context
------------------
	.. autoclass:: ConnectionContext

		.. automethod:: set_protocol_policy
		.. automethod:: set_interface_policy
		.. automethod:: set_pvd_policy
		.. automethod:: set_address_family_policy
		.. automethod:: note_alternate_remote
		.. automethod:: get_snapshot

Local Endpoint
--------------
	.. autoclass:: LocalEndpoint

		.. automethod:: with_interface
		.. automethod:: without_interface
		.. automethod:: with_ip_address
		.. automethod:: with_address
		.. automethod:: without_address
		.. automethod:: with_hostname
		.. automethod:: with_port
		.. automethod:: with_service
		.. automethod:: with_protocol
		.. automethod:: with_stun_server
		.. automethod:: with_any_source_multicast_group_ip
		.. automethod:: with_single_source_multicast_group_ip
		.. automethod:: with_hop_limit

Remote Endpoint
---------------
	.. autoclass:: RemoteEndpoint

		.. automethod:: with_ip_address
		.. automethod:: with_address
		.. automethod:: without_address
		.. automethod:: with_hostname
		.. automethod:: with_port
		.. automethod:: with_service
		.. automethod:: with_protocol
		.. automethod:: with_interface
		.. automethod:: with_multicast_group_ip
		.. automethod:: with_hop_limit

Transport Properties
--------------------
	.. autoclass:: TransportProperties

		.. automethod:: add
		.. automethod:: apply_profile
		.. automethod:: get_property
		.. automethod:: get_properties
		.. automethod:: default_property
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
		.. automethod:: set_allowed_security_protocols
		.. automethod:: add_pinned_server_certificate
		.. automethod:: set_pinned_server_certificates
		.. automethod:: add_security_algorithm
		.. automethod:: set_security_algorithms
		.. automethod:: add_alpn_protocol
		.. automethod:: set_alpn_protocols
		.. automethod:: with_server_name
		.. automethod:: set_server_name
		.. automethod:: disable_peer_authentication
		.. automethod:: enable_peer_authentication
		.. automethod:: set_cipher_suites

Preconnection
-------------
	.. autoclass:: Preconnection

		.. automethod:: initiate
		.. automethod:: initiate_with_send
		.. automethod:: listen
		.. automethod:: rendezvous
		.. automethod:: resolve
		.. automethod:: add_local_endpoint
		.. automethod:: add_remote_endpoint
		.. automethod:: add_framer
		.. automethod:: get_connection_context
		.. automethod:: set_protocol_policy
		.. automethod:: set_interface_policy
		.. automethod:: set_pvd_policy
		.. automethod:: set_address_family_policy
		.. automethod:: note_alternate_remote
		.. automethod:: separate_connection_context
		.. automethod:: get_monitoring_snapshot
		.. automethod:: get_property
		.. automethod:: default_property
		.. automethod:: on_ready
		.. automethod:: on_initiate_error
		.. automethod:: on_establishment_error
		.. automethod:: on_connection_received
		.. automethod:: on_listen_error
		.. automethod:: on_stopped
		.. automethod:: on_rendezvous_done

Connection
----------
	.. autoclass:: Connection

		.. automethod:: new_message_context
		.. automethod:: get_connection_context
		.. automethod:: set_protocol_policy
		.. automethod:: set_interface_policy
		.. automethod:: set_pvd_policy
		.. automethod:: set_address_family_policy
		.. automethod:: note_alternate_remote
		.. automethod:: get_monitoring_snapshot
		.. automethod:: get_property
		.. automethod:: get_message_properties
		.. automethod:: default_property
		.. automethod:: send
		.. automethod:: send_message
		.. automethod:: send_batch
		.. automethod:: enqueue_message
		.. automethod:: flush_messages
		.. automethod:: receive
		.. automethod:: receive_message
		.. automethod:: clone
		.. automethod:: wait_ready
		.. automethod:: wait_closed
		.. automethod:: attempt_reestablishment
		.. automethod:: enable_auto_reestablishment
		.. automethod:: disable_auto_reestablishment
		.. automethod:: get_reestablishment_candidates
		.. automethod:: note_path_change
		.. automethod:: note_soft_error
		.. automethod:: clear_path_degradation
		.. automethod:: close
		.. automethod:: get_event_history
		.. automethod:: get_group_properties
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
		.. automethod:: on_clone_error
		.. automethod:: on_reestablishment_suggested
		.. automethod:: on_reestablished
		.. automethod:: on_connection_error
		.. automethod:: on_closed

Listener
--------
	.. autoclass:: Listener

		.. automethod:: get_connection_context
		.. automethod:: set_protocol_policy
		.. automethod:: set_interface_policy
		.. automethod:: set_pvd_policy
		.. automethod:: set_address_family_policy
		.. automethod:: note_alternate_remote
		.. automethod:: get_monitoring_snapshot
		.. automethod:: get_property
		.. automethod:: set_new_connection_limit
		.. automethod:: wait_listening
		.. automethod:: accept
		.. automethod:: wait_stopped
		.. automethod:: stop
		.. automethod:: get_event_history

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
