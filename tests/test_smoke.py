import asyncio
import socket
import ssl

import pytaps as taps
from pytaps.listener import Listener
from pytaps.transports import TcpTransport, UdpTransport
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


def test_message_context_properties_round_trip():
    context = taps.MessageContext(priority=7, ordered=False, final=False, idempotent=True)
    properties = context.get_properties()

    assert properties["priority"] == 7
    assert properties["ordered"] is False
    assert properties["final"] is False
    assert properties["idempotent"] is True


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


def test_protocol_candidates_require_confidentiality():
    properties = taps.TransportProperties()
    properties.require("confidentiality")

    candidates = build_protocol_candidates(properties)

    assert candidates == ["tls-tcp"]


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


def test_connection_message_property_helpers():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    connection = taps.Connection(preconnection)
    context = connection.new_message_context(priority=5, final=False, idempotent=True)
    connection.loop.close()

    assert connection.get_message_properties(context)["priority"] == 5
    assert connection.get_message_properties(context)["final"] is False
    assert connection.get_message_properties(context)["idempotent"] is True


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
    low = taps.MessageContext(priority=0)
    high = taps.MessageContext(priority=10)
    mid = taps.MessageContext(priority=5)

    connection.enqueue_message("low", low)
    connection.enqueue_message("high", high)
    connection.enqueue_message("mid", mid)
    result = connection.loop.run_until_complete(connection.flush_messages())
    connection.loop.close()

    assert result == [1, 2, 3]
    assert transport.calls == [
        (b"high", 10),
        (b"mid", 5),
        (b"low", 0),
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

    connection.enqueue_message("one", taps.MessageContext(priority=1))
    connection.enqueue_message("two", taps.MessageContext(priority=1))
    connection.enqueue_message("three", taps.MessageContext(priority=1))
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

    assert received.get_properties()["priority"] == 3
    assert received.get_properties()["final"] is False


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
    security.add_alpn_protocol("h2")
    security.with_server_name("svc.example.com")
    security.disable_peer_authentication()

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
    assert properties["serverName"] == "svc.example.com"
    assert properties["requirePeerAuthentication"] is False


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
    assert candidates == ["tls-tcp"]


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
