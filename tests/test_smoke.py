import asyncio
import socket

import pytaps as taps
from pytaps.transports import UdpTransport
from pytaps.utility import build_protocol_candidates, create_candidates


def test_import_and_basic_objects():
    remote = taps.RemoteEndpoint()
    remote.with_hostname("localhost")
    remote.with_port(443)

    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )

    assert preconnection.remote_endpoint.host_name == "localhost"
    assert preconnection.remote_endpoint.port == 443
    assert "reliability" in preconnection.transport_properties.properties


def test_transport_property_aliases_are_canonicalized():
    properties = taps.TransportProperties()
    properties.ignore("congestion-control")
    properties.prohibit("preserve-order")
    properties.set_property("direction", "unidirection-receive")
    properties.set_property("connPriority", 5)

    assert properties.properties["congestionControl"] is taps.PreferenceLevel.IGNORE
    assert properties.properties["preserveOrder"] is taps.PreferenceLevel.PROHIBIT
    assert properties.properties["direction"] == "Unidirectional Receive"
    assert properties.connection_properties["connPriority"] == 5


def test_preconnection_is_reusable_after_initiate():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=loop,
    )

    connection = loop.run_until_complete(preconnection.initiate())
    connection.race_task.cancel()
    loop.run_until_complete(asyncio.gather(connection.race_task, return_exceptions=True))
    preconnection.set_property("connPriority", 10)
    second_connection = loop.run_until_complete(preconnection.initiate_with_send("hello"))
    second_connection.race_task.cancel()
    loop.run_until_complete(asyncio.gather(second_connection.race_task, return_exceptions=True))
    loop.close()

    assert preconnection.get_properties()["connection"]["connPriority"] == 10
    assert second_connection._pending_message[0] == "hello"


def test_connection_group_property_propagation():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    connection = taps.Connection(preconnection)
    clone = taps.Connection(preconnection)
    connection.connection_group.add_connection(clone)

    connection.set_property("connTimeout", 12)
    connection.set_property("connPriority", 5)

    assert clone.connection_group is connection.connection_group
    assert clone.get_properties()["connection"]["connTimeout"] == 12
    assert clone.get_properties()["connection"]["connPriority"] == 100
    assert len(connection.grouped_connections()) == 2


def test_preconnection_clone_copies_configuration():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    local = taps.LocalEndpoint().with_address("127.0.0.1").with_interface("lo0")
    preconnection = taps.Preconnection(
        local_endpoint=local,
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    preconnection.set_property("connPriority", 5)
    clone = preconnection.clone()

    assert clone is not preconnection
    assert clone.local_endpoint.address == ["127.0.0.1"]
    assert clone.local_endpoint.interface == ["lo0"]
    assert clone.get_properties()["connection"]["connPriority"] == 5


def test_protocol_candidates_follow_require_prefer_avoid_order():
    properties = taps.TransportProperties()
    properties.require("reliability")
    properties.prefer("congestion-control")
    properties.avoid("preserveMsgBoundaries")

    candidates = build_protocol_candidates(properties)

    assert candidates == ["tcp", "tls-tcp"]


def test_candidate_order_prefers_paths_before_protocols():
    remote = taps.RemoteEndpoint().with_address("2001:db8::1").with_address("192.0.2.1").with_port(443)
    local = taps.LocalEndpoint().with_interface("wifi").with_interface("cell")
    preconnection = taps.Preconnection(
        local_endpoint=local,
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    preconnection.transport_properties.add_interface_preference("wifi", taps.PreferenceLevel.PREFER)
    preconnection.transport_properties.add_interface_preference("cell", taps.PreferenceLevel.AVOID)
    preconnection.transport_properties.require("reliability")

    connection = taps.Connection(preconnection)
    candidates = create_candidates(
        connection,
        [
            (socket.AddressFamily.AF_INET, "192.0.2.1"),
            (socket.AddressFamily.AF_INET6, "2001:db8::1"),
        ],
    )

    assert candidates[0].path == "wifi"
    assert candidates[0].protocol == "tcp"
    assert candidates[0].remote_address == "2001:db8::1"
    assert candidates[-1].path == "cell"


def test_connection_add_and_remove_endpoints():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    local = taps.LocalEndpoint().with_address("127.0.0.1").with_interface("lo0")
    preconnection = taps.Preconnection(
        local_endpoint=local,
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    connection = taps.Connection(preconnection)

    extra_remote = taps.RemoteEndpoint().with_address("203.0.113.10")
    extra_local = taps.LocalEndpoint().with_address("192.0.2.10").with_interface("en0")
    connection.add_remote([extra_remote])
    connection.add_local([extra_local])
    connection.remove_remote([extra_remote])
    connection.remove_local([extra_local])

    assert "203.0.113.10" not in connection.remote_endpoint.address
    assert "192.0.2.10" not in connection.local_endpoint.address
    assert "en0" not in connection.local_endpoint.interface


def test_connection_send_preserves_message_context():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    connection = taps.Connection(preconnection)
    connection.state = taps.ConnectionState.ESTABLISHED

    class DummyTransport:
        def __init__(self):
            self.calls = []

        def send(self, data, message_context=None, end_of_message=True):
            self.calls.append((data, message_context, end_of_message))
            return 77

    transport = DummyTransport()
    connection.transports = [transport]
    context = taps.MessageContext(end_of_message=False)

    result = connection.loop.run_until_complete(
        connection.send("hello", context, end_of_message=False)
    )
    connection.loop.close()

    assert result == 77
    assert transport.calls[0][0] == b"hello"
    assert transport.calls[0][1] is context
    assert transport.calls[0][2] is False
    assert context.end_of_message is False


def test_udp_receive_delivers_message_context():
    remote = taps.RemoteEndpoint().with_address("203.0.113.10").with_port(4444)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)
    transport = UdpTransport(connection=connection, remote_endpoint=remote)
    received = {}

    async def handle_received(data, context, received_connection):
        received["data"] = data
        received["context"] = context
        received["connection"] = received_connection

    connection.on_received(handle_received)
    transport.datagram_received(b"payload", ("203.0.113.10", 4444))
    loop.run_until_complete(transport.read(1, -1))
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()

    assert received["data"] == b"payload"
    assert received["connection"] is connection
    assert received["context"].addr == ("203.0.113.10", 4444)
    assert received["context"].end_of_message is True
