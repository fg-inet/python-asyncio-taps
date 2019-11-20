import asyncio
import pytest
import sys

sys.path.append(sys.path[0] + "/..")
import pytaps as taps  # noqa: E402

import time

TEST_TIMEOUT = 5

class TestClient():

	async def handle_closed(self, conn):
		print("Closed")
		asyncio.get_event_loop().stop()

	async def handle_received(self, data, context, connection):
		print("Receive message: " + str(data))
		self.received_data = data
		await connection.close()

	async def handle_received_partial(self, data, context, end_of_message,
									  connection):
		print("Receive partial message: " + str(data))
		self.received_data = data
		await connection.close()

	async def sent_and_receive(self, message_ref, connection):
		print("Sent and receive")
		await self.connection.receive(min_incomplete_length=1)

	async def sent_and_stop(self, message_ref, connection):
		print("Sent and stop")
		asyncio.get_event_loop().stop()

	async def handle_ready(self, connection):
		if self.stop_at_sent:
			connection.on_sent(self.sent_and_stop)
		else:
			connection.on_sent(self.sent_and_receive)
		self.connection.on_received(self.handle_received)
		self.connection.on_received_partial(self.handle_received_partial)
		self.connection.on_closed(self.handle_closed)
		msgref = await self.connection.send_message(self.data_to_send)

	async def main(self, data_to_send="Hello\n", stop_at_sent=False, remote_hostname="localhost", remote_port=6666, reliable=False, yangfile=None, trust_ca=None, local_identity=None):
		self.data_to_send = data_to_send
		self.stop_at_sent = stop_at_sent
		self.yangfile = yangfile

		ep = taps.RemoteEndpoint()
		ep.with_hostname(remote_hostname)
		ep.with_port(remote_port)
		tp = taps.TransportProperties()

		if not reliable:
			tp.prohibit("reliability")
		tp.ignore("congestion-control")
		tp.ignore("preserve-order")

		if trust_ca or local_identity:
			sp = taps.SecurityParameters()
			if trust_ca:
				sp.addTrustCA(trust_ca)
			if local_identity:
				sp.addIdentity(local_identity)
		else:
			sp = None

		if self.yangfile:
			self.preconnection = taps.Preconnection.from_yangfile(self.yangfile,
													remote_endpoint=ep,
													transport_properties=tp,
													security_parameters=sp,
													event_loop=asyncio.get_event_loop())
		else:
			self.preconnection = taps.Preconnection(remote_endpoint=ep,
													transport_properties=tp,
													security_parameters=sp,
													event_loop=asyncio.get_event_loop())

		self.preconnection.on_ready(self.handle_ready)
		self.connection = await self.preconnection.initiate()

# Send something over UDP, then stop event loop
@pytest.mark.timeout(TEST_TIMEOUT)
def test_sending():
	loop = asyncio.new_event_loop()
	try:
		client = TestClient()
		asyncio.set_event_loop(loop)
		task = loop.create_task(client.main(stop_at_sent=True))
		loop.run_forever()

		assert True
	finally:
		loop.close()

# Send something over UDP, receive something, check if it's the same
# This requires an echo server to listen on UDP port 6666
# python3.7 examples/echo_example/echoServer.py --local-address=::1 --local-port=6666  --reliable both
@pytest.mark.timeout(TEST_TIMEOUT)
def test_echo_udp():
	teststring = "Hello\n"
	loop = asyncio.new_event_loop()
	try:
		client = TestClient()
		asyncio.set_event_loop(loop)
		task = loop.create_task(client.main(data_to_send=teststring))
		loop.run_forever()

		assert client.received_data.decode() == teststring
	finally:
		loop.close()

# Send something over UDP, receive something, check if it's the same
# This requires an echo server to listen on UDP port 6666
# e.g.: python3.7 examples/echo_example/echoServer.py --local-address=::1 --local-port=6666  --reliable both
@pytest.mark.timeout(TEST_TIMEOUT)
def test_echo_udp_yang():
	teststring = "Hello\n"
	yangfile_client = "udp-client.json"

	client_loop = asyncio.new_event_loop()
	try:
		client = TestClient()
		asyncio.set_event_loop(client_loop)
		task = client_loop.create_task(client.main(data_to_send=teststring, yangfile=yangfile_client))

		client_loop.run_forever()
		print("Started client")

		assert client.received_data.decode() == teststring
	finally:
		client_loop.close()

# Send something over TCP, receive something, check if it's the same
# This requires an echo server to listen on TCP port 6666
# e.g.: python3.7 examples/echo_example/echoServer.py --local-address=::1 --local-port=6666  --reliable both
@pytest.mark.timeout(TEST_TIMEOUT)
def test_echo_yang_tcp():
	teststring = "Hello\n"
	yangfile = "tcp-client.json"
	loop = asyncio.new_event_loop()
	try:
		client = TestClient()
		asyncio.set_event_loop(loop)
		task = loop.create_task(client.main(data_to_send=teststring, yangfile=yangfile))
		loop.run_forever()

		assert client.received_data.decode() == teststring
	finally:
		loop.close()

# Send something over TLS, receive something, check if it's the same
# This requires an echo server to listen on TLS port 6667
# e.g.: python3.7 examples/echo_example/echoServer.py --local-address=::1 --local-port=6667 --local-identity tests/keys/localhost.pem
@pytest.mark.timeout(TEST_TIMEOUT)
def test_echo_tls():
	teststring = "Hello\n"
	loop = asyncio.new_event_loop()
	try:
		client = TestClient()
		asyncio.set_event_loop(loop)
		task = loop.create_task(client.main(data_to_send=teststring, remote_port=6667, reliable=True, trust_ca="keys/MyRootCA.pem"))
		loop.run_forever()

		assert client.received_data.decode() == teststring
	finally:
		loop.close()

# Send something over TLS, receive something, check if it's the same
# This requires an echo server to listen on TLS port 6667
# e.g.: python3.7 examples/echo_example/echoServer.py --local-address=::1 --local-port=6667 --local-identity tests/keys/localhost.pem
@pytest.mark.timeout(TEST_TIMEOUT)
def test_echo_tls_yang():
	teststring = "Hello\n"
	yangfile = "tls-client.json"
	loop = asyncio.new_event_loop()
	try:
		client = TestClient()
		asyncio.set_event_loop(loop)
		task = loop.create_task(client.main(data_to_send=teststring, yangfile=yangfile))
		loop.run_forever()

		assert client.received_data.decode() == teststring
	finally:
		loop.close()

@pytest.mark.timeout(TEST_TIMEOUT)
def test_http():
	hostname = "www.ietf.org"
	teststring = "GET / HTTP/1.1\r\nHost: " + hostname + "\r\n\r\n"
	loop = asyncio.new_event_loop()
	try:
		client = TestClient()
		asyncio.set_event_loop(loop)
		task = loop.create_task(client.main(data_to_send=teststring, remote_hostname=hostname, remote_port=80, reliable=True))
		loop.run_forever()

		assert "HTTP" in client.received_data.decode()
	finally:
		loop.close()
