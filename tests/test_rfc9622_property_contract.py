import asyncio
import socket

import pytest

import pytaps as taps
from pytaps import transports as transports_module
from pytaps.transports import TcpTransport, UdpTransport
from pytaps.utility import build_protocol_candidates


def _remote(protocol=None):
    endpoint = (
        taps.RemoteEndpoint()
        .with_address("192.0.2.10")
        .with_port(443)
    )
    if protocol is not None:
        endpoint.with_protocol(protocol)
    return endpoint


def _established_connection(protocol, properties=None):
    loop = asyncio.new_event_loop()
    connection = taps.Connection(
        taps.Preconnection(
            remote_endpoint=_remote(protocol),
            transport_properties=properties,
            event_loop=loop,
        )
    )
    connection.protocol = protocol
    connection.state = taps.ConnectionState.ESTABLISHED
    return loop, connection


def test_udp_candidates_require_replay_safe_message_defaults():
    properties = taps.TransportProperties()
    properties.prohibit("reliability")
    properties.prohibit("preserveOrder")
    properties.require("preserveMsgBoundaries")
    properties.ignore("congestionControl")

    assert build_protocol_candidates(
        properties,
        available_protocols={"udp"},
    ) == []

    properties.profile_message_properties["safelyReplayable"] = True

    assert build_protocol_candidates(
        properties,
        available_protocols={"udp"},
    ) == ["udp"]


def test_receive_only_udp_candidate_does_not_require_replay_safe_messages():
    properties = taps.TransportProperties()
    properties.prohibit("reliability")
    properties.prohibit("preserveOrder")
    properties.require("preserveMsgBoundaries")
    properties.ignore("congestionControl")
    properties.set_property("direction", "Unidirectional Receive")

    assert build_protocol_candidates(
        properties,
        available_protocols={"udp"},
    ) == ["udp"]


def test_preconnection_replay_safe_default_controls_udp_eligibility():
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoint=_remote(),
        transport_properties=taps.TransportProperties().unreliable_datagram(),
        event_loop=loop,
    )

    assert preconnection.get_property("safelyReplayable") is True
    assert build_protocol_candidates(
        preconnection.transport_properties,
        available_protocols={"udp"},
    ) == ["udp"]

    preconnection.default_property("safelyReplayable")

    assert preconnection.get_property("safelyReplayable") is False
    assert build_protocol_candidates(
        preconnection.transport_properties,
        available_protocols={"udp"},
    ) == []
    loop.close()


def test_udp_rejects_ordered_message_without_writing():
    loop, connection = _established_connection("udp")
    writes = []
    errors = []

    class DummyTransport:
        def send(
            self,
            data,
            message_context=None,
            end_of_message=True,
            send_call_id=None,
        ):
            writes.append(data)

    async def handle_send_error(message_context, reason, failed_connection):
        errors.append((message_context, reason, failed_connection))

    connection.transports = [DummyTransport()]
    connection.on_send_error(handle_send_error)
    context = taps.MessageContext(
        ordered=True,
        safely_replayable=True,
    )

    loop.run_until_complete(connection.send(b"ordered", context))
    loop.run_until_complete(asyncio.sleep(0))

    assert writes == []
    assert len(errors) == 1
    assert "msgOrdered" in str(errors[0][1])
    assert errors[0][2] is connection
    loop.close()


def test_explicit_false_message_property_overrides_preconnection_default():
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoint=_remote("udp"),
        transport_properties=taps.TransportProperties().unreliable_datagram(),
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)
    connection.protocol = "udp"
    context = taps.MessageContext(safely_replayable=False)

    resolved = connection._apply_message_defaults(context)

    assert "safely_replayable" in context.explicit_properties
    assert resolved.safely_replayable is False
    loop.close()


def test_tcp_may_treat_unordered_message_as_no_op():
    loop, connection = _established_connection("tcp")
    writes = []

    class DummyTransport:
        def send(
            self,
            data,
            message_context=None,
            end_of_message=True,
            send_call_id=None,
        ):
            writes.append((data, message_context))

    connection.transports = [DummyTransport()]
    context = taps.MessageContext(ordered=False)

    loop.run_until_complete(connection.send(b"unordered-ok", context))

    assert writes[0][0] == b"unordered-ok"
    assert writes[0][1].ordered is False
    loop.close()


@pytest.mark.parametrize(
    ("property_name", "value", "error_text"),
    [
        ("maxSendRate", 1000, "application-rate shaping"),
        ("maxRecvRate", 1000, "application-rate shaping"),
        ("tcp.userTimeoutValue", 30, "RFC 5482"),
        ("tcp.userTimeoutEnabled", True, "RFC 5482"),
        ("tcp.userTimeoutChangeable", False, "RFC 5482"),
    ],
)
def test_unsupported_connection_property_does_not_mutate_or_close(
    property_name,
    value,
    error_text,
):
    loop, connection = _established_connection("tcp")
    before = connection.get_property(property_name)

    with pytest.raises(NotImplementedError, match=error_text):
        connection.set_property(property_name, value)

    assert connection.get_property(property_name) == before
    assert connection.state is taps.ConnectionState.ESTABLISHED
    loop.close()


def test_quic_rejects_unsupported_active_multipath_policy_transactionally():
    properties = taps.TransportProperties()
    properties.set_property("multipath", "Active")
    loop, connection = _established_connection("quic", properties)

    with pytest.raises(NotImplementedError, match="Handover"):
        connection.set_property("multipathPolicy", "Aggregate")

    assert connection.get_property("multipathPolicy") == "Handover"
    assert connection.state is taps.ConnectionState.ESTABLISHED
    loop.close()


def test_preestablishment_configuration_rejects_unsupported_backend_effects():
    properties = taps.TransportProperties()
    properties.set_property("connTimeout", 2)
    loop = asyncio.new_event_loop()
    connection = taps.Connection(
        taps.Preconnection(
            remote_endpoint=_remote("quic"),
            transport_properties=properties,
            event_loop=loop,
        )
    )

    with pytest.raises(NotImplementedError, match="QUIC streams"):
        connection._validate_connection_configuration("quic")

    connection._validate_connection_configuration("tcp")
    loop.close()


def test_group_property_backend_failure_rolls_back_every_member(monkeypatch):
    user_timeout_option = 0x7F02
    monkeypatch.setattr(
        transports_module.socket,
        "TCP_USER_TIMEOUT",
        user_timeout_option,
        raising=False,
    )
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoint=_remote("tcp"),
        event_loop=loop,
    )
    first = taps.Connection(preconnection)
    second = taps.Connection(preconnection)
    first.connection_group.add_connection(second)
    first.protocol = "tcp"
    second.protocol = "tcp"
    first.state = taps.ConnectionState.ESTABLISHED
    second.state = taps.ConnectionState.ESTABLISHED

    class FakeSocket:
        def __init__(self, fail=False):
            self.fail = fail
            self.options = []

        def setsockopt(self, level, option, value):
            self.options.append((level, option, value))
            if self.fail and option == user_timeout_option and value:
                raise OSError("forced option failure")

    class FakeRawTransport:
        def __init__(self, fail=False):
            self.socket = FakeSocket(fail)

        def get_extra_info(self, name):
            return self.socket if name == "socket" else None

    first_transport = TcpTransport(
        first,
        remote_endpoint=first.remote_endpoint,
    )
    first_transport.transport = FakeRawTransport()
    second_transport = TcpTransport(
        second,
        remote_endpoint=second.remote_endpoint,
    )
    second_transport.transport = FakeRawTransport(fail=True)

    with pytest.raises(NotImplementedError, match="connTimeout"):
        first.set_property("connTimeout", 2)

    assert first.get_property("connTimeout") == "Disabled"
    assert second.get_property("connTimeout") == "Disabled"
    assert (
        first.get_group_properties()["sharedConnectionProperties"][
            "connTimeout"
        ]
        == "Disabled"
    )
    assert (
        socket.IPPROTO_TCP,
        user_timeout_option,
        2000,
    ) in first_transport.transport.socket.options
    assert (
        socket.IPPROTO_TCP,
        user_timeout_option,
        0,
    ) in first_transport.transport.socket.options
    assert first.state is taps.ConnectionState.ESTABLISHED
    assert second.state is taps.ConnectionState.ESTABLISHED
    loop.close()


def test_tcp_conn_timeout_updates_entangled_uto_changeable(monkeypatch):
    user_timeout_option = 0x7F02
    monkeypatch.setattr(
        transports_module.socket,
        "TCP_USER_TIMEOUT",
        user_timeout_option,
        raising=False,
    )
    loop, connection = _established_connection("tcp")

    class FakeSocket:
        def setsockopt(self, level, option, value):
            return None

    class FakeRawTransport:
        def get_extra_info(self, name):
            return FakeSocket() if name == "socket" else None

    transport = TcpTransport(
        connection,
        remote_endpoint=connection.remote_endpoint,
    )
    transport.transport = FakeRawTransport()

    connection.set_property("connTimeout", 2)

    assert connection.get_property("connTimeout") == 2
    assert connection.get_property("tcp.userTimeoutChangeable") is False
    assert (
        connection.get_group_properties()["sharedConnectionProperties"][
            "tcp.userTimeoutChangeable"
        ]
        is False
    )
    loop.close()


@pytest.mark.parametrize(
    ("protocol", "transport_type"),
    [
        ("tcp", TcpTransport),
        ("udp", UdpTransport),
    ],
)
def test_capacity_profile_applies_recommended_dscp(
    protocol,
    transport_type,
):
    loop, connection = _established_connection(protocol)

    class FakeSocket:
        family = socket.AF_INET

        def __init__(self):
            self.options = []

        def setsockopt(self, level, option, value):
            self.options.append((level, option, value))

    class FakeRawTransport:
        def __init__(self):
            self.socket = FakeSocket()

        def get_extra_info(self, name):
            return self.socket if name == "socket" else None

    transport = transport_type(
        connection,
        remote_endpoint=connection.remote_endpoint,
    )
    transport.transport = FakeRawTransport()

    connection.set_property(
        "connCapacityProfile",
        "Low Latency/Interactive",
    )

    assert (
        socket.IPPROTO_IP,
        socket.IP_TOS,
        34 << 2,
    ) in transport.transport.socket.options
    assert (
        connection.get_properties()["readOnly"]["propertyEffects"][
            "connCapacityProfile"
        ]
        == "applied:IP_TOS:DSCP=34"
    )
    loop.close()
