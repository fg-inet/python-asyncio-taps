API Usage
=========

Using the PyTAPS API, an application can do the following:

* Create a Preconnection with endpoints, TransportProperties, and SecurityParameters
* Perform one of the following actions on the Preconnection:
	* Initiate: Connect to another endpoint or join a multicast session
	* Listen: Listen for incoming Connections
	* Rendezvous: Simultaneously Listen and Initiate
* To send data, call Send()
* To receive data, call Receive()
* Close the Connection

Creating a Preconnection
------------------------

Before an application can create a Connection, first it has to create a *Preconnection*.
A Preconnection consists of the local and remote endpoints as well as the Transport properties and security security parameters::

	import PyTAPS as taps

	remote_endpoint = taps.RemoteEndpoint()
	remote_endpoint.with_hostname("example.org")
	remote_endpoint.with_port("80")

	local_endpoint = taps.LocalEndpoint()
	local_endpoint.with_port("6666")

Transport Properties specify which behavior and properties an application expects a new connection to have. This has also an impact on which transport protocol gets chosen by the TAPS system.

The **default TransportProperties will result in a TCP connection**::

	properties = taps.TransportProperties()

To use **TLS, add SecurityParameters** to the Preconnection.
The default SecurityParameters result in the client using the default certificate trust store of the system to validate the peer's certificate, while not setting its own identity.

To specify a Certificate Authority to trust or to set a certificate as the local identity, set SecurityParameters as follows::

	security = taps.SecurityParameters()
	security.addTrustCA(args.trust_ca)
	security.addIdentity(args.local_identity)

To enforce using **UDP, set TransportProperties** that prohibit the use of TCP, for example:

* "congestion-control": "ignore"
* "preserve-order": "ignore"
* "reliability": "prohibit"

.. note::
	Setting all TransportProperties to "ignore" results in "racing" TCP and UDP. Here, UDP "wins" because it does not perform a handshake.

To **join a multicast group**, configure your Preconnection :ref:`as described here<Joining a multicast group>`.

After all the prerequisite and optional objects have been configured, the preconnection itself can finally be created::

	preconnection = taps.Preconnection(remote_endpoint=endpoint,
					local_endpoint=None,
					transport_properties=properties,
					security_parameters=security)


Initiating a Connection
-----------------------

To actively initiate a Connection, an application first has to create a Preconnection.

Then the application needs to set a callback on the Preconnection, which will be called once the Connection is ready::

	async def handle_ready():
		print("Connection has been successfully established")

	preconnection.on_ready(handle_ready)

Note that the callback function has to be defined as an *async* function, i.e., a Python asyncio *coroutine*. See `Design decisions <design.rst>`_ for more information on coroutines and for our reasoning why PyTAPS functions and callbacks are coroutines.
There are several other callbacks that can be set on the preconnection, see the full `API reference <reference.rst>`_

After setting the callback, the application can call Initiate. Note that Initiate is a *coroutine* and not a regular function, so it cannot be called directly.

To run a coroutine, an application can create a task from this coroutine and then run the task in an event loop::

	loop = asyncio.get_event_loop()
	loop.create_task(preconnection.initiate())
	loop.run_forever()

The above example runs the event loop until all queued tasks have completed, i.e., the Connections has been established and the on_ready callback has been called.

Alternatively, a coroutine can be called using *await* from within another coroutine::

	async def initiate_connection(preconnection):
		connection = await preconnection.initiate()
		# Returns immediately

	loop = asyncio.get_event_loop()
	loop.create_task(initiate_connection(preconnection))
	loop.run_forever()

Listening for a Connection
--------------------------

Passively listening for new connections is done in a similar way.

First, the application will have to create a Preconnection.

Once this is done, the application will have to set a callback on the Preconnection that gets called once a new connection has been received::

	async def handle_connection_received():
		print("A new connection has been received.")
	
	preconnection.on_connection_received(handle_connection_received)

Similar to an active initiate, the callback is a Python *coroutine* and not a regular function. 
Now the application can get the event loop, call the listen the coroutine and then start to run the event loop::

	loop = asyncio.get_event_loop()
	loop.create_task(preconnection.listen())
	loop.run_forever()

Sending data
------------

An application can send Messages through an established Connection as follows::

	await connection.send_message(data)

Optionally, the application can specify a callback function to be called once the message has been sent, i.e., once PyTAPS has handed the data to the underlying implementation of the used transport protocol::

	async def handle_sent(messageRef):
		print("Message has been sent")

	connection.on_sent(handle_sent)

Receiving data
--------------

PyTAPS is a message-oriented API, and by default, applications receive entire messages.
This works well with a transport protocol that supports message boundaries, such as SCTP, or when using a Deframer. However, a stream-oriented transport protocol such as TCP does not preserve message boundaries.

In this case, the application should receive partial messages. For this, the application has to set a callback to be called when it receives data, and then call receive::

	async def handle_received_partial(self, data, context, end_of_message):
		print("Received data: " + str(data))

	connection.on_received_partial(handle_received_partial)
	await connection.receive(min_incomplete_length=1)

In case the application has provided a Deframer or in case the underlying transport protocol supports the preservation of message boundaries, an application can receive full messages instead::

	async def handle_received(self, data, context):
		print("Received data: " + str(data))

	connection.on_received(handle_received)
	await connection.receive()

.. warning::

   The above code only receives entire messages. When using TCP, the message is only complete upon receiving a FIN, i.e., once the other endpoint has terminated the TCP connection.


Closing a connection
--------------------

An application can set a callback to be executed after the Connection has been closed, and then close the Connection::

	async def handle_closed():
		print("Connection has been closed")

	connection.on_closed(handle_closed)
	connection.close()

Using YANG to configure Preconnections and Endpoints
------------------------------------------------------

PyTAPS allows developers to load configurations from a JSON file that specifies them according to the TAPS YANG model.
To do so, the application calls the from_yangfile function on the preconnection and passes a YANG/JSON file containing the configuration::

	preconnection = taps.Preconnection.from_yangfile(fname)

This will configure the preconnection and endpoints according to the provided YANG file. The application can now continue as usual by setting callbacks and calling initiate/listen.

To achieve a preconnection that is configured the same as the one created in the earlier example, the yang configuration file would have to look like this::

	{
		"ietf-taps-api:preconnection":{
			"remote-endpoints":[
			{
				"id":"1",
				"remote-host":"example.org",
				"remote-port":"80"
			}
			],    "local-endpoints":[
			{
				"id":"1",
				"local-port":"6666"
			}
			]
		}
	}

Joining a multicast group
-------------------------

PyTAPS currently supports Source-Specific Multicast (SSM), which requires libmcrx, see build_dependencies.sh.

To join a multicast group and receive multicast messages, first :ref:`configure your Preconnection<Creating a Preconnection>` as follows:

* Local endpoint: IP address of the multicast group to join

* Remote endpoint: Source IP address from which to receive multicast messages

* Transport properties:

  * "direction": "unidirection-receive"

  * "congestion-control": "ignore",

  * "reliability": "prohibit",

  * "preserve-order": "ignore"

Then, :ref:`initiate the Preconnection<Initiating a Connection>`.

To test, you can set up an Automatic Multicast Tunneling (AMT) gateway by following the `instructions posted here <https://github.com/GrumpyOldTroll/libmcrx/blob/master/howto.txt>`_ 
and then joining the multicast group, e.g., using *yangClient.py -f test-mcast-receive.json*.
