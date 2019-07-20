import asyncio
import pytest
import sys

sys.path.append(sys.path[0] + "/..")
import PyTAPS as taps  # noqa: E402

from threading import Thread

import time

TEST_TIMEOUT = 5

class TestClient():

	async def handle_received(self, data, context, connection):
		print("Receive message: " + str(data))
		self.received_data = data
		asyncio.get_event_loop().stop()

	async def handle_received_partial(self, data, context, end_of_message,
									  connection):
		print("Receive partial message: " + str(data))
		self.received_data = data
		asyncio.get_event_loop().stop()

	async def sent_and_receive(self, connection):
		print("Sent and receive")
		await self.connection.receive(min_incomplete_length=1)

	async def sent_and_stop(self, connection):
		print("Sent and stop")
		asyncio.get_event_loop().stop()

	async def handle_ready(self, connection):
		if self.stop_at_sent:
			connection.on_sent(self.sent_and_stop)
		else:
			connection.on_sent(self.sent_and_receive)
		self.connection.on_received(self.handle_received)
		self.connection.on_received_partial(self.handle_received_partial)
		msgref = await self.connection.send_message(self.data_to_send)

	async def main(self, data_to_send="Hello\n", stop_at_sent=True, yangfile=None):
		self.data_to_send = data_to_send
		self.stop_at_sent = stop_at_sent
		self.yangfile = yangfile

		ep = taps.RemoteEndpoint()
		ep.with_address("localhost")
		ep.with_port(6666)
		tp = taps.TransportProperties()

		tp.prohibit("reliability")
		tp.ignore("congestion-control")
		tp.ignore("preserve-order")

		self.preconnection = taps.Preconnection(remote_endpoint=ep,
												transport_properties=tp,
												event_loop=asyncio.get_event_loop(),
												yangfile=yangfile)
		self.preconnection.on_ready(self.handle_ready)
		self.connection = await self.preconnection.initiate()

@pytest.mark.timeout(TEST_TIMEOUT)
def test_sending():
	loop = asyncio.new_event_loop()
	try:
		client = TestClient()
		asyncio.set_event_loop(loop)
		task = loop.create_task(client.main())
		loop.run_forever()

		assert True
	finally:
		loop.close()

@pytest.mark.timeout(TEST_TIMEOUT)
def test_echo():
	teststring = "Hello\n"
	loop = asyncio.new_event_loop()
	try:
		client = TestClient()
		asyncio.set_event_loop(loop)
		task = loop.create_task(client.main(data_to_send=teststring, stop_at_sent=False))
		loop.run_forever()

		assert client.received_data == teststring
	finally:
		loop.close()

@pytest.mark.timeout(TEST_TIMEOUT)
def test_echo_yang():
	teststring = "Hello\n"
	yangfile_client = "udp-client.json"

	client_loop = asyncio.new_event_loop()
	try:
		client = TestClient()
		asyncio.set_event_loop(client_loop)
		task = client_loop.create_task(client.main(data_to_send=teststring, stop_at_sent=False, yangfile=yangfile_client))

		client_loop.run_forever()
		print("Started client")

		assert client.received_data == teststring
	finally:
		client_loop.close()

@pytest.mark.timeout(TEST_TIMEOUT)
def test_echo_yang_tcp():
	teststring = "Hello\n"
	yangfile = "tcp-client.json"
	loop = asyncio.new_event_loop()
	try:
		client = TestClient()
		asyncio.set_event_loop(loop)
		task = loop.create_task(client.main(data_to_send=teststring, stop_at_sent=False, yangfile=yangfile))
		loop.run_forever()

		assert client.received_data == teststring
	finally:
		loop.close()
