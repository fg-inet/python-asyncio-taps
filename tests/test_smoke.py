import asyncio
import socket
import ssl
import types
from textwrap import dedent
from pathlib import Path

import pytest
import pytaps as taps
from pytaps.listener import Listener
from pytaps.transports import QuicTransport, TcpTransport, UdpTransport
from pytaps.utility import (
    Candidate,
    build_protocol_candidates,
    create_candidates,
    order_candidates_for_racing,
)
from pytaps.yang_validate import YANG_FMT_XML

TESTS_DIR = Path(__file__).resolve().parent


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


def test_message_context_properties_round_trip():
    context = taps.MessageContext(
        priority=7,
        ordered=False,
        reliable=False,
        final=False,
        safely_replayable=True,
        no_fragmentation=True,
        no_segmentation=True,
    )
    properties = context.get_properties()

    assert properties["msgPriority"] == 7
    assert properties["msgOrdered"] is False
    assert properties["msgReliable"] is False
    assert properties["final"] is False
    assert properties["safelyReplayable"] is True
    assert properties["noFragmentation"] is True
    assert properties["noSegmentation"] is True
    assert properties["msgLifetime"] == "Infinite"


def test_message_context_supports_rfc_style_helpers():
    context = taps.MessageContext()
    context.add("msgPriority", 5)
    context.add("msgLifetime", 3.5)
    context.remote_address = "203.0.113.10"
    context.remote_port = 443
    context.local_address = "192.0.2.10"
    context.local_port = 8443

    remote = context.get_remote_endpoint()
    local = context.get_local_endpoint()

    assert context.get("msgPriority") == 5
    assert context.get("msgLifetime") == 3.5
    assert remote.address == ["203.0.113.10"]
    assert remote.port == 443
    assert local.address == ["192.0.2.10"]
    assert local.port == 8443


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


def test_transport_property_profiles_match_rfc_style_convenience_profiles():
    stream = taps.TransportProperties().reliable_inorder_stream()
    message = taps.TransportProperties().reliable_message()
    datagram = taps.TransportProperties().unreliable_datagram()

    assert stream.get("reliability") is taps.PreferenceLevel.REQUIRE
    assert stream.get("preserveOrder") is taps.PreferenceLevel.REQUIRE
    assert stream.get("preserveMsgBoundaries") is taps.PreferenceLevel.PROHIBIT

    assert message.get("reliability") is taps.PreferenceLevel.REQUIRE
    assert message.get("preserveMsgBoundaries") is taps.PreferenceLevel.REQUIRE

    assert datagram.get("reliability") is taps.PreferenceLevel.PROHIBIT
    assert datagram.get("preserveMsgBoundaries") is taps.PreferenceLevel.REQUIRE
    assert datagram.get("congestionControl") is taps.PreferenceLevel.IGNORE


def test_transport_properties_support_single_property_get_and_default():
    properties = taps.TransportProperties()
    properties.set_property("connPriority", 5)
    assert properties.get_property("connPriority") == 5

    properties.default_property("connPriority")

    assert properties.get_property("connPriority") == 100


def test_transport_properties_report_explicit_property_sets():
    properties = taps.TransportProperties()
    properties.require("reliability")
    properties.set_property("connPriority", 5)

    reported = properties.get_properties()

    assert "reliability" in reported["explicitSelection"]
    assert "connPriority" in reported["explicitConnection"]


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


def test_grouped_connections_are_sorted_by_connection_priority():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    first = taps.Connection(preconnection)
    second = taps.Connection(preconnection)
    third = taps.Connection(preconnection)
    first.connection_group.add_connection(second)
    first.connection_group.add_connection(third)

    first.set_property("connPriority", 50)
    second.set_property("connPriority", 10)
    third.set_property("connPriority", 30)

    ordered = first.grouped_connections()
    first.loop.close()

    assert ordered == [second, third, first]


def test_connection_group_limit_is_enforced():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    first = taps.Connection(preconnection)
    second = taps.Connection(preconnection)
    third = taps.Connection(preconnection)

    first.connection_group.set_property("groupConnLimit", 2)
    first.connection_group.add_connection(second)

    with pytest.raises(RuntimeError, match="limit"):
        first.connection_group.add_connection(third)

    first.loop.close()


def test_preconnection_clone_copies_configuration():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    local = taps.LocalEndpoint().with_address("127.0.0.1").with_interface("lo0")
    preconnection = taps.Preconnection(
        local_endpoint=local,
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    preconnection.set_property("connPriority", 5)
    preconnection.set_property("msgPriority", 7)
    clone = preconnection.clone()

    assert clone is not preconnection
    assert clone.local_endpoint.address == ["127.0.0.1"]
    assert clone.local_endpoint.interface == ["lo0"]
    assert clone.get_properties()["connection"]["connPriority"] == 5
    assert clone.get_properties()["message"]["msgPriority"] == 7
    assert clone.get_connection_context() is preconnection.get_connection_context()


def test_protocol_candidates_follow_require_prefer_avoid_order():
    properties = taps.TransportProperties()
    properties.require("reliability")
    properties.prefer("congestion-control")
    properties.avoid("preserveMsgBoundaries")

    candidates = build_protocol_candidates(properties)

    assert candidates == ["quic", "tcp", "tls-tcp"]


def test_protocol_candidates_require_confidentiality():
    properties = taps.TransportProperties()
    properties.require("confidentiality")

    candidates = build_protocol_candidates(properties)

    assert candidates == ["quic", "tls-tcp"]


def test_protocol_candidates_can_prefer_quic_multistreaming():
    properties = taps.TransportProperties()
    properties.require("multistreaming")

    candidates = build_protocol_candidates(properties)

    assert candidates == ["quic"]


def test_protocol_candidates_use_cached_protocol_outcomes():
    properties = taps.TransportProperties()
    properties.require("reliability")
    properties.ignore("preserveMsgBoundaries")
    properties.ignore("zeroRttMsg")
    properties.ignore("multistreaming")

    context = taps.ConnectionContext()
    context.record_protocol_outcome("tcp", True)
    context.record_protocol_outcome("quic", False, RuntimeError("cached failure"))

    candidates = build_protocol_candidates(properties, connection_context=context)

    assert candidates == ["tcp", "tls-tcp", "quic"]


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
    assert candidates[0].protocol == "quic"
    assert candidates[0].remote_address == "2001:db8::1"
    assert candidates[-1].path == "cell"


def test_racing_order_prefers_cached_successful_candidate_path():
    remote = taps.RemoteEndpoint().with_address("203.0.113.10").with_port(443)
    local = taps.LocalEndpoint().with_address("192.0.2.1").with_port(12345)
    preconnection = taps.Preconnection(
        local_endpoint=local,
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    connection = taps.Connection(preconnection)
    connection.connection_context.record_candidate_outcome(
        ("192.0.2.10", 12345),
        ("203.0.113.10", 443),
        "tcp",
        True,
    )

    ordered = order_candidates_for_racing(
        connection,
        [
            Candidate(
                protocol="tcp",
                remote_address="203.0.113.10",
                address_family=socket.AddressFamily.AF_INET,
                path="default",
                local_address="192.0.2.20",
            ),
            Candidate(
                protocol="tcp",
                remote_address="203.0.113.10",
                address_family=socket.AddressFamily.AF_INET,
                path="default",
                local_address="192.0.2.10",
            ),
        ],
    )
    preconnection.loop.close()

    assert ordered[0].local_address == "192.0.2.10"


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


def test_connection_message_property_helpers():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    connection = taps.Connection(preconnection)
    context = connection.new_message_context(
        msgPriority=5,
        msgReliable=False,
        final=False,
        safelyReplayable=True,
    )
    connection.loop.close()

    assert connection.get_message_properties(context)["msgPriority"] == 5
    assert connection.get_message_properties(context)["msgReliable"] is False
    assert connection.get_message_properties(context)["final"] is False
    assert connection.get_message_properties(context)["safelyReplayable"] is True


def test_message_defaults_can_be_set_on_preconnection_and_connection():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    preconnection.set_property("msgPriority", 9)
    preconnection.set_property("msgCapacityProfile", "Low Latency/Interactive")

    connection = taps.Connection(preconnection)
    context = connection._coerce_message_context()
    connection.set_property("msgLifetime", 1.5)
    inherited = connection._coerce_message_context()
    connection.loop.close()

    assert preconnection.get_properties()["message"]["msgPriority"] == 9
    assert context.priority == 9
    assert context.capacity_profile == "Low Latency/Interactive"
    assert inherited.lifetime == 1.5


def test_preconnection_supports_single_property_get_and_default():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    preconnection.set_property("connPriority", 5)
    preconnection.set_property("msgPriority", 7)

    assert preconnection.get_property("connPriority") == 5
    assert preconnection.get_property("msgPriority") == 7

    preconnection.default_property("connPriority")
    preconnection.default_property("msgPriority")
    preconnection.loop.close()

    assert preconnection.get_property("connPriority") == 100
    assert preconnection.get_property("msgPriority") == 100


def test_connection_send_batch_assigns_shared_batch_id():
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
            return len(self.calls)

    transport = DummyTransport()
    connection.transports = [transport]
    first = taps.MessageContext()
    second = taps.MessageContext()

    result = connection.loop.run_until_complete(
        connection.send_batch(
            [
                ("hello", first, True),
                ("world", second, True),
            ]
        )
    )
    connection.loop.close()

    assert result == [1, 2]
    assert first.batch_id == second.batch_id
    assert first.batch_id is not None


def test_flush_messages_prefers_higher_priority():
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
            self.calls.append((data, message_context.priority))
            return len(self.calls)

    transport = DummyTransport()
    connection.transports = [transport]
    low = taps.MessageContext(priority=100, final=False)
    high = taps.MessageContext(priority=10, final=False)
    mid = taps.MessageContext(priority=50, final=False)

    connection.enqueue_message("low", low)
    connection.enqueue_message("high", high)
    connection.enqueue_message("mid", mid)
    result = connection.loop.run_until_complete(connection.flush_messages())
    connection.loop.close()

    assert result == [1, 2, 3]
    assert transport.calls == [
        (b"high", 10),
        (b"mid", 50),
        (b"low", 100),
    ]


def test_flush_messages_preserves_fifo_for_equal_priority():
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
            self.calls.append(data)
            return len(self.calls)

    transport = DummyTransport()
    connection.transports = [transport]

    connection.enqueue_message("one", taps.MessageContext(priority=1, final=False))
    connection.enqueue_message("two", taps.MessageContext(priority=1, final=False))
    connection.enqueue_message("three", taps.MessageContext(priority=1, final=False))
    connection.loop.run_until_complete(connection.flush_messages())
    connection.loop.close()

    assert transport.calls == [b"one", b"two", b"three"]


def test_flush_messages_reports_expired_queued_message():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)
    connection.state = taps.ConnectionState.ESTABLISHED
    expired = {}

    class DummyTransport:
        def __init__(self):
            self.calls = []

        def send(self, data, message_context=None, end_of_message=True):
            self.calls.append(data)
            return len(self.calls)

    async def handle_expired(context, expired_connection):
        expired["context"] = context
        expired["connection"] = expired_connection

    connection.on_expired(handle_expired)
    connection.transports = [DummyTransport()]
    connection.enqueue_message("gone", taps.MessageContext(lifetime=0))
    result = loop.run_until_complete(connection.flush_messages())
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()

    assert result == [None]
    assert expired["connection"] is connection


def test_final_message_is_sent_last_and_blocks_future_sends():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    connection = taps.Connection(preconnection)
    connection.state = taps.ConnectionState.ESTABLISHED
    send_errors = []

    class DummyTransport:
        def __init__(self):
            self.calls = []

        def send(self, data, message_context=None, end_of_message=True):
            self.calls.append((data, message_context.final))
            return len(self.calls)

    async def handle_send_error(message_ref, failed_connection):
        send_errors.append((message_ref, failed_connection))

    connection.on_send_error(handle_send_error)
    connection.transports = [DummyTransport()]
    connection.enqueue_message("body", taps.MessageContext(priority=100, final=False))
    connection.enqueue_message("trailer", taps.MessageContext(priority=0, final=True))
    connection.loop.run_until_complete(connection.flush_messages())
    blocked = connection.loop.run_until_complete(
        connection.send("after", taps.MessageContext(final=False))
    )
    connection.loop.run_until_complete(asyncio.sleep(0))
    connection.loop.close()

    assert connection.transports[0].calls == [
        (b"body", False),
        (b"trailer", True),
    ]
    assert blocked is None
    assert send_errors[0][1] is connection


def test_message_expired_callback_fires_before_send():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)
    connection.state = taps.ConnectionState.ESTABLISHED
    expired = {}

    class DummyTransport:
        def __init__(self):
            self.calls = []

        def send(self, data, message_context=None, end_of_message=True):
            self.calls.append((data, message_context, end_of_message))
            return 1

    async def handle_expired(context, expired_connection):
        expired["context"] = context
        expired["connection"] = expired_connection

    connection.on_expired(handle_expired)
    transport = DummyTransport()
    connection.transports = [transport]
    context = taps.MessageContext(lifetime=0)

    result = loop.run_until_complete(connection.send("hello", context))
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()

    assert result is None
    assert transport.calls == []
    assert expired["context"] is context
    assert expired["connection"] is connection


def test_udp_send_requires_safely_replayable():
    remote = taps.RemoteEndpoint().with_address("203.0.113.10").with_port(4444)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)
    connection.state = taps.ConnectionState.ESTABLISHED
    connection.protocol = "udp"
    send_errors = {}

    class DummyTransport:
        def send(self, data, message_context=None, end_of_message=True):
            raise AssertionError("send should not be called for invalid UDP message")

    async def handle_send_error(context, reason, failed_connection):
        send_errors["context"] = context
        send_errors["reason"] = reason
        send_errors["connection"] = failed_connection

    connection.on_send_error(handle_send_error)
    connection.transports = [DummyTransport()]
    context = taps.MessageContext(final=False, safely_replayable=False)

    result = loop.run_until_complete(connection.send("hello", context))
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()

    assert result is None
    assert send_errors["context"] is context
    assert send_errors["connection"] is connection
    assert "safely replayable" in str(send_errors["reason"]).lower()


def test_pending_message_can_expire_before_active_open():
    remote = taps.RemoteEndpoint().with_address("203.0.113.10").with_port(4444)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)
    expired = {}

    async def handle_expired(context, expired_connection):
        expired["context"] = context
        expired["connection"] = expired_connection

    connection.on_expired(handle_expired)
    context = taps.MessageContext(lifetime=0)
    loop.run_until_complete(connection.initiate_with_send("hello", context))
    transport = UdpTransport(connection=connection, remote_endpoint=remote)
    loop.run_until_complete(transport.active_open(None))
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()

    assert expired["context"] is context
    assert expired["connection"] is connection


def test_received_message_exposes_message_properties():
    context = taps.MessageContext(priority=3, final=False)
    received = taps.ReceivedMessage(b"hello", context, object())

    assert received.get_properties()["msgPriority"] == 3
    assert received.get_properties()["final"] is False


def test_received_message_exposes_receive_side_helpers():
    context = taps.MessageContext(
        priority=3,
        final=False,
        end_of_message=True,
        remote_address="203.0.113.10",
        remote_port=4444,
        local_address="192.0.2.10",
        local_port=5555,
        received_at=12.5,
        receive_sequence=7,
    )
    received = taps.ReceivedMessage(b"hello", context, object())

    assert received.get("msgPriority") == 3
    assert received.is_complete is True
    assert received.remote_endpoint.address == ["203.0.113.10"]
    assert received.remote_endpoint.port == 4444
    assert received.local_endpoint.address == ["192.0.2.10"]
    assert received.local_endpoint.port == 5555
    assert received.get_read_only_properties()["receivedAt"] == 12.5
    assert received.get_read_only_properties()["receiveSequence"] == 7


def test_connection_tracks_soft_errors_and_path_changes():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    local = taps.LocalEndpoint().with_address("192.0.2.1").with_port(1111)
    preconnection = taps.Preconnection(
        local_endpoint=local,
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    connection = taps.Connection(preconnection)
    soft_errors = {}
    path_changes = {}

    async def handle_soft_error(reason, affected_connection):
        soft_errors["reason"] = reason
        soft_errors["connection"] = affected_connection

    async def handle_path_change(previous_path, current_path, affected_connection):
        path_changes["previous"] = previous_path
        path_changes["current"] = current_path
        path_changes["connection"] = affected_connection

    connection.on_soft_error(handle_soft_error)
    connection.on_path_change(handle_path_change)
    connection.note_soft_error("ECN CE marks observed")
    connection.note_path_change(
        local_address="192.0.2.10",
        local_port=12345,
        remote_address="203.0.113.10",
        remote_port=443,
    )
    connection.loop.run_until_complete(asyncio.sleep(0))
    properties = connection.get_properties()
    connection.loop.close()

    assert soft_errors["reason"] == "ECN CE marks observed"
    assert soft_errors["connection"] is connection
    assert path_changes["previous"] == {"local": None, "remote": None}
    assert path_changes["current"]["local"] == ("192.0.2.10", 12345)
    assert path_changes["current"]["remote"] == ("203.0.113.10", 443)
    assert path_changes["connection"] is connection
    assert properties["readOnly"]["softErrors"] == ["ECN CE marks observed"]
    assert properties["readOnly"]["currentPath"]["remote"] == ("203.0.113.10", 443)
    assert properties["readOnly"]["previousPath"] == {"local": None, "remote": None}
    assert properties["readOnly"]["eventCount"] >= 2
    assert any(event["name"] == "soft_error" for event in connection.get_event_history())
    assert any(event["name"] == "path_change" for event in connection.get_event_history())


def test_group_properties_expose_shared_connection_state():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    connection = taps.Connection(preconnection)
    clone = taps.Connection(preconnection)
    connection.connection_group.add_connection(clone)
    connection.set_property("connTimeout", 15)

    properties = connection.get_group_properties()
    connection.loop.close()

    assert properties["size"] == 2
    assert clone in properties["connections"]
    assert properties["sharedConnectionProperties"]["connTimeout"] == 15
    assert properties["connectionContext"]["connectionGroups"] == 1


def test_connection_read_only_properties_expose_property_catalog_state():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    security = taps.SecurityParameters()
    security.set_alpn_protocols(["h2", "hq"])
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        security_parameters=security,
        event_loop=asyncio.new_event_loop(),
    )
    connection = taps.Connection(preconnection)
    connection.note_soft_error("queue pressure")
    read_only = connection.get_properties()["readOnly"]
    security_properties = connection.get_properties()["security"]
    connection.loop.run_until_complete(asyncio.sleep(0))
    connection.loop.close()

    assert read_only["securityAvailable"] is True
    assert read_only["softErrorCount"] == 1
    assert read_only["receiveSequence"] == 0
    assert read_only["messageDefaults"]["msgLifetime"] == "Infinite"
    assert security_properties["alpnProtocols"] == ["h2", "hq"]
    assert read_only["connectionContext"]["eventCounters"]["soft_error"] == 1


def test_connection_supports_single_property_get_and_default():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    connection = taps.Connection(preconnection)
    connection.set_property("connPriority", 5)
    connection.set_property("msgLifetime", 2.5)

    assert connection.get_property("connPriority") == 5
    assert connection.get_property("msgLifetime") == 2.5
    assert connection.get_property("connState") == "Establishing"

    connection.default_property("connPriority")
    connection.default_property("msgLifetime")
    connection.loop.close()

    assert connection.get_property("connPriority") == 100
    assert connection.get_property("msgLifetime") == "Infinite"


def test_connection_event_history_records_ready_and_closed():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    connection = taps.Connection(preconnection)
    connection._mark_ready()
    connection._report_closed()
    history = connection.get_event_history()
    read_only = connection.get_properties()["readOnly"]
    connection.loop.close()

    assert [event["name"] for event in history[-2:]] == ["ready", "closed"]
    assert read_only["lastEvent"]["name"] == "closed"
    assert read_only["connectionContext"]["eventCounters"]["ready"] == 1


def test_listener_event_history_records_failures_and_connections():
    local = taps.LocalEndpoint().with_address("127.0.0.1").with_port(8443)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        local_endpoint=local,
        event_loop=loop,
    )
    listener = Listener(preconnection)
    listener._mark_listening()

    remote = taps.RemoteEndpoint().with_address("203.0.113.10").with_port(443)
    connection_preconnection = taps.Preconnection(
        local_endpoint=local,
        remote_endpoint=remote,
        event_loop=loop,
    )
    connection = taps.Connection(connection_preconnection)
    listener._deliver_connection(connection)
    listener._fail_listen(RuntimeError("late failure"))
    history = listener.get_event_history()
    read_only = listener.get_properties()["readOnly"]
    loop.close()

    assert [event["name"] for event in history[:3]] == [
        "listening",
        "connection_received",
        "listen_error",
    ]
    assert read_only["eventCount"] == 3
    assert read_only["lastEvent"]["name"] == "listen_error"
    assert read_only["connectionContext"]["eventCounters"]["listening"] == 1


def test_listener_read_only_properties_expose_pending_state():
    local = taps.LocalEndpoint().with_address("127.0.0.1").with_port(8443)
    security = taps.SecurityParameters()
    preconnection = taps.Preconnection(
        local_endpoint=local,
        security_parameters=security,
        event_loop=asyncio.new_event_loop(),
    )
    listener = Listener(preconnection)
    waiter_task = listener.accept(timeout=1)
    read_only = listener.get_properties()["readOnly"]
    waiter_task.cancel()
    preconnection.loop.run_until_complete(asyncio.gather(waiter_task, return_exceptions=True))
    preconnection.loop.close()

    assert read_only["securityAvailable"] is True
    assert read_only["pendingAccepts"] == 1
    assert read_only["pendingConnections"] == 0


def test_listener_supports_single_property_get():
    local = taps.LocalEndpoint().with_address("127.0.0.1").with_port(8443)
    preconnection = taps.Preconnection(
        local_endpoint=local,
        event_loop=asyncio.new_event_loop(),
    )
    listener = Listener(preconnection)
    read_only = listener.get_property("connState")
    selection = listener.get_property("reliability")
    preconnection.loop.close()

    assert read_only == "Establishing"
    assert selection is taps.PreferenceLevel.REQUIRE


def test_quic_clone_uses_shared_association_stream_mapping(monkeypatch):
    import pytaps.transports as transport_impl

    class FakeQuicConfiguration:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.certificate = None
            self.private_key = None

        def load_cert_chain(self, certfile, keyfile=None, password=None):
            self.certificate = certfile
            self.private_key = keyfile or certfile

        def load_verify_locations(self, cafile=None, capath=None, cadata=None):
            self.cafile = cafile

    class FakeReader:
        async def read(self, size):
            return b""

    class FakeWriter:
        def __init__(self):
            self.buffer = []
            self.closed = False

        def write(self, data):
            self.buffer.append(data)

        async def drain(self):
            return None

        def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

    class FakeProtocol:
        def __init__(self):
            self.streams = []
            self.closed = False

        async def create_stream(self, is_unidirectional=False):
            stream = (FakeReader(), FakeWriter())
            self.streams.append(stream)
            return stream

        def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

    class FakeConnectContext:
        def __init__(self):
            self.protocol = FakeProtocol()

        async def __aenter__(self):
            return self.protocol

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(transport_impl, "QuicConfiguration", FakeQuicConfiguration)
    monkeypatch.setattr(
        transport_impl,
        "aioquic_connect",
        lambda *args, **kwargs: FakeConnectContext(),
    )
    monkeypatch.setattr(transport_impl, "aioquic_serve", object())

    remote = taps.RemoteEndpoint().with_address("203.0.113.10").with_port(443)
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    preconnection.transport_properties.require("multistreaming")
    connection = taps.Connection(preconnection)

    preconnection.loop.run_until_complete(connection.race())
    clone = preconnection.loop.run_until_complete(connection.clone())
    protocol = connection.quic_association.protocol
    preconnection.loop.close()

    assert connection.protocol == "quic"
    assert clone.protocol == "quic"
    assert connection.quic_association is clone.quic_association
    assert protocol is not None
    assert len(protocol.streams) == 2
    assert clone in connection.grouped_connections()


def test_quic_listener_maps_incoming_streams_to_connections(monkeypatch):
    import pytaps.transports as transport_impl

    class FakeQuicConfiguration:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.certificate = None
            self.private_key = None

        def load_cert_chain(self, certfile, keyfile=None, password=None):
            self.certificate = certfile
            self.private_key = keyfile or certfile

        def load_verify_locations(self, cafile=None, capath=None, cadata=None):
            self.cafile = cafile

    class FakeWriter:
        def __init__(self):
            self.closed = False

        def write(self, data):
            return None

        async def drain(self):
            return None

        def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

    class FakeReader:
        async def read(self, size):
            return b""

    class FakeServer:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    async def fake_serve(*args, **kwargs):
        return FakeServer()

    monkeypatch.setattr(transport_impl, "QuicConfiguration", FakeQuicConfiguration)
    monkeypatch.setattr(transport_impl, "aioquic_serve", fake_serve)
    monkeypatch.setattr(transport_impl, "aioquic_connect", object())

    local = taps.LocalEndpoint().with_address("127.0.0.1").with_port(4433)
    security = taps.SecurityParameters()
    security.add_identity(str(TESTS_DIR / "keys" / "localhost.pem"))
    preconnection = taps.Preconnection(
        local_endpoint=local,
        security_parameters=security,
        event_loop=asyncio.new_event_loop(),
    )
    listener = Listener(preconnection)
    listener.protocol = "quic"
    listener.quic_association = transport_impl.QuicAssociationManager(
        loop=preconnection.loop,
        listener=listener,
    )
    fake_protocol = object()

    preconnection.loop.run_until_complete(
        listener.quic_association.accept_inbound_stream(
            FakeReader(),
            FakeWriter(),
            fake_protocol,
        )
    )
    preconnection.loop.run_until_complete(
        listener.quic_association.accept_inbound_stream(
            FakeReader(),
            FakeWriter(),
            fake_protocol,
        )
    )
    first = preconnection.loop.run_until_complete(listener.accept())
    second = preconnection.loop.run_until_complete(listener.accept())
    preconnection.loop.close()

    assert first.protocol == "quic"
    assert second.protocol == "quic"
    assert first.connection_group is second.connection_group
    assert isinstance(first.transports[0], QuicTransport)


def test_preconnection_can_separate_connection_context():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    original_context = preconnection.get_connection_context()
    clone = preconnection.clone()

    preconnection.separate_connection_context()
    separated_context = preconnection.get_connection_context()
    preconnection.loop.close()

    assert clone.get_connection_context() is original_context
    assert separated_context is not original_context


def test_monitoring_snapshot_exposes_cached_state():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    local = taps.LocalEndpoint().with_address("192.0.2.1").with_port(1234)
    preconnection = taps.Preconnection(
        local_endpoint=local,
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    connection = taps.Connection(preconnection)
    connection.protocol = "tcp"
    connection.note_path_change(
        local_address="192.0.2.10",
        local_port=12345,
        remote_address="203.0.113.10",
        remote_port=443,
    )
    connection._mark_ready()

    snapshot = connection.get_monitoring_snapshot()
    preconnection_snapshot = preconnection.get_monitoring_snapshot()
    connection.loop.close()

    assert snapshot["connectionContext"]["protocolCache"]["tcp"]["successes"] == 1
    assert snapshot["connectionContext"]["pathCache"][0]["remote"] == ("203.0.113.10", 443)
    assert preconnection_snapshot["connectionContext"]["protocolCache"]["tcp"]["successes"] == 1


def test_framer_helper_methods_align_with_documented_api():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)
    transport = TcpTransport(connection=connection, remote_endpoint=remote)
    transport.recv_buffer = b"abcdef"
    delivered = {}

    async def handle_received(data, context, received_connection):
        delivered["data"] = data
        delivered["context"] = context
        delivered["connection"] = received_connection

    class TestFramer(taps.Framer):
        async def start(self, connection):
            return

        async def new_sent_message(self, data, context, eom):
            return data

        async def handle_received_data(self, connection):
            return None, b"", 0, True

    connection.on_received(handle_received)
    framer = TestFramer(event_loop=loop)
    parsed_buffer, parsed_context, parsed_eom = framer.parse(connection)
    context = taps.MessageContext(priority=9)
    framer.advance_receive_cursor(connection, 2)
    framer.deliver(connection, context, b"payload", True)
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()

    assert parsed_buffer == b"abcdef"
    assert parsed_context is None
    assert parsed_eom is False
    assert transport.recv_buffer == b"cdef"
    assert delivered["data"] == b"payload"
    assert delivered["context"] is context
    assert delivered["connection"] is connection


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
    assert received["context"].receive_sequence == 1
    assert received["context"].received_at is not None


def test_receive_returns_received_message_for_udp():
    remote = taps.RemoteEndpoint().with_address("203.0.113.10").with_port(4444)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)
    transport = UdpTransport(connection=connection, remote_endpoint=remote)

    receive_task = loop.create_task(connection.receive(1, -1))
    transport.datagram_received(b"payload", ("203.0.113.10", 4444))
    received_message = loop.run_until_complete(receive_task)
    loop.close()

    assert received_message.data == b"payload"
    assert received_message.context.addr == ("203.0.113.10", 4444)
    assert received_message.end_of_message is True
    assert received_message.connection is connection
    assert received_message.get_read_only_properties()["receiveSequence"] == 1


def test_receive_returns_partial_stream_delivery():
    remote = taps.RemoteEndpoint().with_address("203.0.113.10").with_port(4444)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)
    transport = TcpTransport(connection=connection, remote_endpoint=remote)

    receive_task = loop.create_task(connection.receive(1, 2))
    transport.data_received(b"hello")
    received_message = loop.run_until_complete(receive_task)
    loop.close()

    assert received_message.data == b"he"
    assert received_message.context.end_of_message is False
    assert received_message.connection is connection
    assert received_message.context.final is False
    assert received_message.get_read_only_properties()["receiveSequence"] == 1


def test_partial_stream_receive_reuses_message_context_until_eof():
    remote = taps.RemoteEndpoint().with_address("203.0.113.10").with_port(4444)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)
    transport = TcpTransport(connection=connection, remote_endpoint=remote)

    first_task = loop.create_task(connection.receive(1, 2))
    transport.data_received(b"hello")
    first_message = loop.run_until_complete(first_task)

    second_task = loop.create_task(connection.receive(1, 2))
    second_message = loop.run_until_complete(second_task)

    transport.eof_received()
    third_task = loop.create_task(connection.receive(1, -1))
    third_message = loop.run_until_complete(third_task)
    loop.close()

    assert first_message.data == b"he"
    assert second_message.data == b"ll"
    assert third_message.data == b"o"
    assert first_message.context is second_message.context
    assert second_message.context is third_message.context
    assert first_message.context.receive_sequence == 3
    assert third_message.end_of_message is True
    assert third_message.context.final is True


def test_receive_timeout_cleans_up_waiter():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)

    class DummyTransport:
        def receive(self, min_incomplete_length, max_length):
            return None

    connection.transports = [DummyTransport()]

    with pytest.raises(asyncio.TimeoutError):
        loop.run_until_complete(connection.receive(1, -1, timeout=0.01))
    loop.close()

    assert connection._receive_waiters == []


def test_listener_wait_listening_and_accept():
    local = taps.LocalEndpoint().with_address("127.0.0.1").with_port(8443)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        local_endpoint=local,
        event_loop=loop,
    )
    listener = Listener(preconnection)
    accepted_connection = taps.Connection(preconnection)

    wait_task = loop.create_task(listener.wait_listening())
    accept_task = listener.accept()
    listener._mark_listening()
    listener._deliver_connection(accepted_connection)
    waited_listener = loop.run_until_complete(wait_task)
    accepted = loop.run_until_complete(accept_task)
    loop.close()

    assert waited_listener is listener
    assert accepted is accepted_connection
    assert listener.get_properties()["readOnly"]["state"] == "ESTABLISHED"


def test_listener_accept_timeout_cleans_up_waiter():
    local = taps.LocalEndpoint().with_address("127.0.0.1").with_port(8443)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        local_endpoint=local,
        event_loop=loop,
    )
    listener = Listener(preconnection)
    accept_task = listener.accept(timeout=0.01)

    with pytest.raises(asyncio.TimeoutError):
        loop.run_until_complete(accept_task)
    loop.close()

    assert listener._connection_waiters == []


def test_listener_stop_transitions_to_closed():
    local = taps.LocalEndpoint().with_address("127.0.0.1").with_port(8443)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        local_endpoint=local,
        event_loop=loop,
    )
    listener = Listener(preconnection)
    stopped = {"called": False}

    async def handle_stopped():
        stopped["called"] = True

    listener.stopped = handle_stopped
    loop.run_until_complete(listener.stop())
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()

    assert listener.state is taps.ConnectionState.CLOSED
    assert stopped["called"] is True


def test_listener_wait_stopped_and_accept_failure():
    local = taps.LocalEndpoint().with_address("127.0.0.1").with_port(8443)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        local_endpoint=local,
        event_loop=loop,
    )
    listener = Listener(preconnection)
    accept_waiter = listener.accept()
    stop_task = loop.create_task(listener.wait_stopped())
    loop.run_until_complete(listener.stop())
    stopped_listener = loop.run_until_complete(stop_task)
    loop.close()

    assert stopped_listener is listener
    assert accept_waiter.done() is True
    assert isinstance(accept_waiter.exception(), ConnectionAbortedError)


def test_connection_wait_closed_completes():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)
    wait_task = loop.create_task(connection.wait_closed())
    connection._report_closed()
    closed_connection = loop.run_until_complete(wait_task)
    loop.close()

    assert closed_connection is connection
    assert connection.state is taps.ConnectionState.CLOSED


def test_wait_helpers_support_timeout():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    local = taps.LocalEndpoint().with_address("127.0.0.1").with_port(8443)
    loop = asyncio.new_event_loop()

    connection = taps.Connection(
        taps.Preconnection(remote_endpoint=remote, event_loop=loop)
    )
    listener = Listener(
        taps.Preconnection(local_endpoint=local, event_loop=loop)
    )

    with pytest.raises(asyncio.TimeoutError):
        loop.run_until_complete(connection.wait_ready(timeout=0.01))
    with pytest.raises(asyncio.TimeoutError):
        loop.run_until_complete(listener.wait_listening(timeout=0.01))
    with pytest.raises(asyncio.TimeoutError):
        loop.run_until_complete(listener.wait_stopped(timeout=0.01))
    loop.close()


def test_connection_read_only_properties_follow_rfc_names():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)
    read_only = connection.get_properties()["readOnly"]
    loop.close()

    assert read_only["connState"] == "Establishing"
    assert read_only["canSend"] is False
    assert read_only["canReceive"] is False
    assert read_only["sendMsgMaxLen"] == 0
    assert read_only["recvMsgMaxLen"] == 0


def test_failed_initiate_reports_error_and_closes():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)
    callback = {}

    async def handle_initiate_error(error, failed_connection):
        callback["error"] = error
        callback["connection"] = failed_connection

    connection.on_initiate_error(handle_initiate_error)
    ready_task = loop.create_task(connection.wait_ready())
    failure = RuntimeError("boom")
    connection._fail_initiate(failure)
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()

    assert ready_task.done() is True
    assert isinstance(ready_task.exception(), RuntimeError)
    assert callback["error"] is failure
    assert callback["connection"] is connection
    assert connection.state is taps.ConnectionState.CLOSED


def test_establishment_error_callback_alias_fires():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)
    callback = {}
    ready_task = loop.create_task(connection.wait_ready())

    async def handle_establishment_error(error, failed_connection):
        callback["error"] = error
        callback["connection"] = failed_connection

    connection.on_establishment_error(handle_establishment_error)
    failure = RuntimeError("boom")
    connection._fail_initiate(failure)
    loop.run_until_complete(asyncio.sleep(0))
    assert isinstance(ready_task.exception(), RuntimeError)
    loop.close()

    assert callback["error"] is failure
    assert callback["connection"] is connection


def test_failed_listener_reports_error():
    local = taps.LocalEndpoint().with_address("127.0.0.1").with_port(8443)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        local_endpoint=local,
        event_loop=loop,
    )
    listener = Listener(preconnection)
    callback = {}

    async def handle_listen_error(error, failed_listener):
        callback["error"] = error
        callback["listener"] = failed_listener

    listener.listen_error = handle_listen_error
    listen_task = loop.create_task(listener.wait_listening())
    failure = RuntimeError("listen failed")
    listener._fail_listen(failure)
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()

    assert listen_task.done() is True
    assert isinstance(listen_task.exception(), RuntimeError)
    assert callback["error"] is failure
    assert callback["listener"] is listener
    assert listener.state is taps.ConnectionState.CLOSED


def test_security_parameters_build_context_and_properties():
    remote = taps.RemoteEndpoint().with_hostname("example.com").with_port(443)
    security = taps.SecurityParameters()
    cert_path = str(TESTS_DIR / "keys" / "localhost.pem")
    security.add_allowed_security_protocol("TLS1.3")
    security.add_pinned_server_certificate(cert_path)
    security.add_security_algorithm("TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256")
    security.add_pre_shared_key("shared-secret")
    security.add_private_key(cert_path)
    security.add_private_key_callback_handle("pkcb")
    security.add_public_key(cert_path)
    security.add_alpn_protocol("h2")
    security.with_server_name("svc.example.com")
    security.disable_peer_authentication()
    security.set_session_cache_capacity(16)
    security.set_session_cache_lifetime(3600)

    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        security_parameters=security,
        event_loop=asyncio.new_event_loop(),
    )
    properties = preconnection.get_properties()["security"]
    preconnection.loop.close()

    assert preconnection.security_context is not None
    assert preconnection.security_context.verify_mode == ssl.CERT_NONE
    assert properties["alpnProtocols"] == ["h2"]
    assert properties["allowedSecurityProtocols"] == ["TLS1.3"]
    assert properties["pinnedServerCertificate"] == [cert_path]
    assert properties["serverName"] == "svc.example.com"
    assert properties["requirePeerAuthentication"] is False
    assert properties["securityAlgorithms"] == ["TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256"]
    assert properties["preSharedKey"] == "shared-secret"
    assert properties["privateKey"] == cert_path
    assert properties["privateKeyCallbackHandle"] == "pkcb"
    assert properties["publicKey"] == cert_path
    assert properties["sessionCacheCapacity"] == 16
    assert properties["sessionCacheLifetime"] == 3600


def test_security_parameters_bulk_setters_override_configuration():
    security = taps.SecurityParameters()
    security.set_allowed_security_protocols(["TLS1.2"])
    security.set_pinned_server_certificates(["chain-a", "chain-b"])
    security.set_security_algorithms(["TLS_AES_128_GCM_SHA256"])
    security.set_alpn_protocols(["h3"])
    security.set_server_name("api.example.com")
    security.disable_peer_authentication()
    security.enable_peer_authentication()

    properties = security.get_configuration()

    assert properties["allowedSecurityProtocols"] == ["TLS1.2"]
    assert properties["pinnedServerCertificate"] == ["chain-a", "chain-b"]
    assert properties["securityAlgorithms"] == ["TLS_AES_128_GCM_SHA256"]
    assert properties["alpnProtocols"] == ["h3"]
    assert properties["serverName"] == "api.example.com"
    assert properties["requirePeerAuthentication"] is True


def test_security_parameters_require_secure_transport_by_default():
    remote = taps.RemoteEndpoint().with_hostname("example.com").with_port(443)
    security = taps.SecurityParameters()

    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        security_parameters=security,
        event_loop=asyncio.new_event_loop(),
    )
    candidates = build_protocol_candidates(preconnection.transport_properties)
    preconnection.loop.close()

    assert preconnection.transport_properties.get("confidentiality") is taps.PreferenceLevel.REQUIRE
    assert preconnection.transport_properties.get("integrity") is taps.PreferenceLevel.REQUIRE
    assert candidates == ["quic", "tls-tcp"]


def test_preconnection_rendezvous_returns_listener_and_connection():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    local = taps.LocalEndpoint().with_address("127.0.0.1").with_port(port)
    remote = taps.RemoteEndpoint().with_hostname("127.0.0.1").with_port(port)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        local_endpoint=local,
        remote_endpoint=remote,
        event_loop=loop,
    )
    preconnection.transport_properties.require("reliability")
    events = {}

    async def handle_rendezvous_done(connection):
        events["rendezvous"] = connection

    preconnection.on_rendezvous_done(handle_rendezvous_done)
    result = loop.run_until_complete(preconnection.rendezvous(timeout=1))
    loop.run_until_complete(asyncio.sleep(0.05))
    result.connection.close()
    loop.run_until_complete(result.connection.wait_closed(timeout=1))
    loop.run_until_complete(result.listener.stop())
    loop.close()

    assert isinstance(result, taps.RendezvousResult)
    assert result.connection.state is taps.ConnectionState.CLOSED
    assert result.listener.state is taps.ConnectionState.CLOSED
    assert events["rendezvous"] is result.connection


def test_receive_error_fires_for_incomplete_stream_termination():
    remote = taps.RemoteEndpoint().with_address("203.0.113.10").with_port(4444)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)
    transport = TcpTransport(connection=connection, remote_endpoint=remote)
    callback = {}

    async def handle_receive_error(context, reason, failed_connection):
        callback["context"] = context
        callback["reason"] = reason
        callback["connection"] = failed_connection

    connection.on_receive_error(handle_receive_error)
    transport.data_received(b"partial")
    transport.connection_lost(RuntimeError("stream aborted"))
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()

    assert callback["context"] is not None
    assert callback["connection"] is connection
    assert "aborted" in str(callback["reason"]).lower()


def test_connection_and_listener_preserve_security_context():
    remote = taps.RemoteEndpoint().with_hostname("example.com").with_port(443)
    local = taps.LocalEndpoint().with_address("127.0.0.1").with_port(8443)
    security = taps.SecurityParameters()

    preconnection = taps.Preconnection(
        local_endpoint=local,
        remote_endpoint=remote,
        security_parameters=security,
        event_loop=asyncio.new_event_loop(),
    )
    connection = taps.Connection(preconnection)
    listener = Listener(preconnection)
    preconnection.loop.close()

    assert connection.security_context is preconnection.security_context
    assert listener.security_context is preconnection.security_context


def test_multicast_adapter_uses_mcrx_core(monkeypatch):
    import pytaps.multicast as multicast

    calls = {}

    class FakeReaderHandle:
        def close(self):
            calls["closed"] = True

    class FakeSubscription:
        def join(self):
            calls["joined"] = True

        def leave(self):
            calls["left"] = True

    class FakeContext:
        def add_subscription(self, group, port, source=None, interface=None):
            calls["subscription"] = {
                "group": group,
                "port": port,
                "source": source,
                "interface": interface,
            }
            return FakeSubscription()

    def fake_add_reader(subscription, callback, loop=None):
        calls["subscription_obj"] = subscription
        calls["loop"] = loop
        calls["callback"] = callback
        return FakeReaderHandle()

    fake_module = types.SimpleNamespace(
        Context=FakeContext,
        add_reader=fake_add_reader,
    )
    monkeypatch.setattr(multicast, "mcrx_core", fake_module)

    loop = asyncio.new_event_loop()
    listener = types.SimpleNamespace(
        loop=loop,
        local_endpoint=types.SimpleNamespace(
            address=["232.1.2.3"],
            port=5000,
            interface=["192.0.2.10"],
        ),
        remote_endpoint=types.SimpleNamespace(address=["198.51.100.10"]),
        preconnection=types.SimpleNamespace(got_mc=lambda *args: None),
    )

    assert multicast.do_join(listener) is True
    assert calls["joined"] is True
    assert calls["subscription"] == {
        "group": "232.1.2.3",
        "port": 5000,
        "source": "198.51.100.10",
        "interface": "192.0.2.10",
    }

    packet = types.SimpleNamespace(payload=b"hello", source_port=6000)
    forwarded = {}
    listener.preconnection = types.SimpleNamespace(
        got_mc=lambda current_listener, data, port: forwarded.update(
            {
                "listener": current_listener,
                "data": data,
                "port": port,
            }
        )
    )
    calls["callback"](packet)
    multicast.do_leave(listener)
    loop.close()

    assert forwarded == {
        "listener": listener,
        "data": b"hello",
        "port": 6000,
    }
    assert calls["left"] is True
    assert calls["closed"] is True


def test_multicast_send_uses_mctx_core(monkeypatch):
    import pytaps.transports as transports

    calls = {}

    class FakeSendReport:
        publication_id = 1
        destination = ("ff3e::8000:1234", 5001)
        local_addr = ("fd06::1", 5001)
        source_addr = "fd06::1"
        bytes_sent = 5

    class FakeAsyncPublication:
        def __init__(self, publication, loop=None):
            calls["loop"] = loop
            self.publication = publication

        async def send(self, payload):
            calls.setdefault("payloads", []).append(payload)
            return FakeSendReport()

    class FakePublication:
        def local_addr(self):
            return ("fd06::1", 5001)

        def remove(self):
            calls["removed"] = True

    class FakeContext:
        def add_publication(
            self,
            group,
            port,
            source=None,
            source_port=None,
            interface=None,
            ttl=1,
            loopback=True,
        ):
            calls["publication"] = {
                "group": group,
                "port": port,
                "source": source,
                "source_port": source_port,
                "interface": interface,
                "ttl": ttl,
                "loopback": loopback,
            }
            return FakePublication()

    fake_module = types.SimpleNamespace(
        Context=FakeContext,
        AsyncPublication=FakeAsyncPublication,
    )
    monkeypatch.setattr(transports, "mctx_core", fake_module)

    loop = asyncio.new_event_loop()
    local = taps.LocalEndpoint().with_address("fd06::1").with_port(5001)
    remote = taps.RemoteEndpoint().with_address("ff3e::8000:1234").with_port(5001)
    preconnection = taps.Preconnection(
        local_endpoint=local,
        remote_endpoint=remote,
        event_loop=loop,
    )
    preconnection.multicast_interface_address = "fd06::1"
    preconnection.multicast_ttl = 5
    connection = taps.Connection(preconnection)
    transport = transports.MulticastSendTransport(
        connection,
        local_endpoint=local,
        remote_endpoint=remote,
    )
    connection.protocol = "udp"
    connection.state = taps.ConnectionState.ESTABLISHED
    sent = {}

    async def handle_sent(message_ref, sent_connection):
        sent["message_ref"] = message_ref
        sent["connection"] = sent_connection

    connection.on_sent(handle_sent)
    loop.run_until_complete(transport.active_open(None))
    context = connection.new_message_context(safelyReplayable=True, final=False)
    loop.run_until_complete(transport.write(b"hello", context, True))
    loop.run_until_complete(asyncio.sleep(0))
    loop.run_until_complete(transport.close())
    loop.close()

    assert calls["publication"] == {
        "group": "ff3e::8000:1234",
        "port": 5001,
        "source": "fd06::1",
        "source_port": 5001,
        "interface": "fd06::1",
        "ttl": 5,
        "loopback": True,
    }
    assert calls["payloads"] == [b"hello"]
    assert context.local_address == "fd06::1"
    assert context.local_port == 5001
    assert sent["connection"] is connection
    assert calls["removed"] is True


def test_from_yang_reads_extended_security_credentials():
    pytest.importorskip("yang_glue")
    cert_path = str(TESTS_DIR / "keys" / "localhost.pem")
    ca_path = str(TESTS_DIR / "keys" / "MyRootCA.pem")
    xml_text = dedent(
        """\
        <preconnection xmlns="urn:ietf:params:xml:ns:yang:ietf-taps-api">
          <remote-endpoints>
            <id>remote-1</id>
            <remote-host>example.com</remote-host>
            <remote-port>443</remote-port>
          </remote-endpoints>
            <security>
              <credentials>
                <id>cred-1</id>
                <identity>{cert_path}</identity>
                <trust-ca>{ca_path}</trust-ca>
                <algorithm>TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256</algorithm>
                <pre-shared-key>shared-secret</pre-shared-key>
                <private-key>{cert_path}</private-key>
                <private-key-callback-handle>pkcb</private-key-callback-handle>
                <public-key>{cert_path}</public-key>
              </credentials>
              <session-cache-capacity>32</session-cache-capacity>
              <session-cache-lifetime>900</session-cache-lifetime>
            </security>
          </preconnection>
            """
    ).format(cert_path=cert_path, ca_path=ca_path)
    preconnection = taps.Preconnection(event_loop=asyncio.new_event_loop())
    preconnection = preconnection.from_yang(YANG_FMT_XML, xml_text)
    security = preconnection.security_parameters.get_configuration()
    preconnection.loop.close()

    assert security["identity"] == cert_path
    assert security["trustedCAs"] == [ca_path]
    assert security["securityAlgorithms"] == ["TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256"]
    assert security["preSharedKey"] == "shared-secret"
    assert security["privateKey"] == cert_path
    assert security["privateKeyCallbackHandle"] == "pkcb"
    assert security["publicKey"] == cert_path
    assert security["sessionCacheCapacity"] == 32
    assert security["sessionCacheLifetime"] == 900
