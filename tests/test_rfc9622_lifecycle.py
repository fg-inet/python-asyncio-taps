import asyncio

import pytest

import pytaps as taps
from pytaps.transports import TcpTransport, UdpTransport


def _connection(*, protocol="tcp"):
    loop = asyncio.new_event_loop()
    remote = taps.RemoteEndpoint().with_address("203.0.113.10").with_port(443)
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)
    connection.protocol = protocol
    return loop, connection, remote


def test_section_9_1_1_send_snapshots_message_context():
    loop, connection, _remote = _connection()
    connection.state = taps.ConnectionState.ESTABLISHED
    delivered = []

    class SnapshotTransport:
        def send(
            self,
            data,
            message_context=None,
            end_of_message=True,
            send_call_id=None,
        ):
            delivered.append((data, message_context, send_call_id))
            connection._queue_send_event(
                "sent",
                message_context,
                send_call_id=send_call_id,
            )
            return message_context.message_id

    connection.transports = [SnapshotTransport()]
    source = taps.MessageContext(priority=7)
    message_id = loop.run_until_complete(connection.send(b"hello", source))
    source.set_property("msgPriority", 99)
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()

    assert message_id == source.message_id
    assert delivered[0][1] is not source
    assert delivered[0][1].priority == 7
    assert connection.get_event_history()[-1]["name"] == "sent"


def test_sections_9_2_and_11_send_before_ready_is_queued():
    loop, connection, _remote = _connection()
    writes = []

    class ReadyTransport:
        def send(
            self,
            data,
            message_context=None,
            end_of_message=True,
            send_call_id=None,
        ):
            writes.append(data)
            connection._queue_send_event(
                "sent",
                message_context,
                send_call_id=send_call_id,
            )
            return message_context.message_id

    connection.transports = [ReadyTransport()]
    message_id = loop.run_until_complete(connection.send(b"queued"))

    assert message_id == 1
    assert writes == []

    connection._mark_ready()
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()

    assert writes == [b"queued"]
    assert [event["name"] for event in connection.get_event_history()] == [
        "ready",
        "sent",
    ]


@pytest.mark.parametrize(
    ("profile_name", "protocol", "reliable", "ordered"),
    [
        ("reliable_inorder_stream", "tcp", True, True),
        ("unreliable_datagram", "udp", False, False),
    ],
)
def test_section_9_1_3_queued_send_resolves_defaults_after_selection(
    profile_name,
    protocol,
    reliable,
    ordered,
):
    loop = asyncio.new_event_loop()
    properties = getattr(taps.TransportProperties(), profile_name)()
    remote = (
        taps.RemoteEndpoint()
        .with_address("203.0.113.10")
        .with_port(443)
        .with_protocol(protocol)
    )
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        transport_properties=properties,
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)
    delivered = []

    class SelectedTransport:
        def send(
            self,
            data,
            message_context=None,
            end_of_message=True,
            send_call_id=None,
        ):
            delivered.append(message_context)
            connection._queue_send_event(
                "sent",
                message_context,
                send_call_id=send_call_id,
            )
            return message_context.message_id

    connection.transports = [SelectedTransport()]
    loop.run_until_complete(connection.send(b"select defaults"))
    queued_context = connection._pre_ready_sends[0]["context"]

    assert queued_context.reliable is None
    assert queued_context.ordered is None

    connection.protocol = protocol
    connection._mark_ready()
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()

    assert delivered[0].reliable is reliable
    assert delivered[0].ordered is ordered
    assert connection.get_event_history()[-1]["name"] == "sent"


def test_section_9_2_2_each_send_has_exactly_one_completion_event():
    loop, connection, _remote = _connection()
    connection.state = taps.ConnectionState.ESTABLISHED

    class DuplicateCompletionTransport:
        def send(
            self,
            data,
            message_context=None,
            end_of_message=True,
            send_call_id=None,
        ):
            connection._queue_send_event(
                "sent",
                message_context,
                send_call_id=send_call_id,
            )
            connection._queue_send_event(
                "send_error",
                message_context,
                RuntimeError("late duplicate"),
                send_call_id=send_call_id,
            )
            return message_context.message_id

    connection.transports = [DuplicateCompletionTransport()]
    loop.run_until_complete(connection.send(b"one event"))
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()

    completions = [
        event["name"]
        for event in connection.get_event_history()
        if event["name"] in {"sent", "expired", "send_error"}
    ]
    assert completions == ["sent"]


def test_section_9_2_2_transport_exception_becomes_send_error():
    loop, connection, _remote = _connection()
    connection.state = taps.ConnectionState.ESTABLISHED

    class FailingTransport:
        def send(
            self,
            data,
            message_context=None,
            end_of_message=True,
            send_call_id=None,
        ):
            raise OSError("write failed")

    connection.transports = [FailingTransport()]
    loop.run_until_complete(connection.send(b"failure"))
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()

    assert connection.get_event_history()[-1]["name"] == "send_error"
    assert connection.get_event_history()[-1]["details"]["reason"] == "write failed"


def test_sections_10_and_11_close_waits_for_accepted_send():
    loop, connection, _remote = _connection()
    connection.state = taps.ConnectionState.ESTABLISHED
    release_send = asyncio.Event()

    class DelayedTransport:
        def send(
            self,
            data,
            message_context=None,
            end_of_message=True,
            send_call_id=None,
        ):
            async def complete():
                await release_send.wait()
                connection._queue_send_event(
                    "sent",
                    message_context,
                    send_call_id=send_call_id,
                )

            loop.create_task(complete())
            return message_context.message_id

        async def close(self):
            connection._report_closed()

    connection.transports = [DelayedTransport()]
    loop.run_until_complete(connection.send(b"drain me"))
    close_task = connection.close()
    loop.run_until_complete(asyncio.sleep(0))

    assert connection.state is taps.ConnectionState.CLOSING
    assert close_task.done() is False

    release_send.set()
    loop.run_until_complete(close_task)
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()

    assert [event["name"] for event in connection.get_event_history()] == [
        "sent",
        "closed",
    ]


def test_section_10_close_waits_for_os_transport_shutdown():
    loop, connection, remote = _connection()
    connection.state = taps.ConnectionState.ESTABLISHED
    transport = TcpTransport(connection=connection, remote_endpoint=remote)

    class DeferredCloseTransport(asyncio.Transport):
        def __init__(self):
            super().__init__()
            self.close_called = False

        def close(self):
            self.close_called = True
            loop.call_later(0.01, transport.connection_lost, None)

    raw_transport = DeferredCloseTransport()
    transport.transport = raw_transport

    close_task = connection.close()
    loop.run_until_complete(asyncio.sleep(0))

    assert raw_transport.close_called is True
    assert close_task.done() is False
    assert connection.state is taps.ConnectionState.CLOSING

    loop.run_until_complete(close_task)
    loop.close()

    assert connection.state is taps.ConnectionState.CLOSED
    assert connection.get_event_history()[-1]["name"] == "closed"


def test_section_10_abort_completes_pending_send_before_connection_error():
    loop, connection, _remote = _connection()
    connection.state = taps.ConnectionState.ESTABLISHED

    class PendingTransport:
        transport = None

        def send(
            self,
            data,
            message_context=None,
            end_of_message=True,
            send_call_id=None,
        ):
            return message_context.message_id

        async def close(self):
            return None

    connection.transports = [PendingTransport()]
    loop.run_until_complete(connection.send(b"pending"))
    connection.abort()
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()

    assert [event["name"] for event in connection.get_event_history()] == [
        "send_error",
        "connection_error",
    ]


def test_sections_9_3_1_and_9_3_2_default_receive_completes_at_eof():
    loop, connection, remote = _connection()
    transport = TcpTransport(connection=connection, remote_endpoint=remote)
    connection._mark_ready()

    receive_task = loop.create_task(connection.receive())
    transport.data_received(b"complete at eof")
    loop.run_until_complete(asyncio.sleep(0))

    assert receive_task.done() is False

    transport.eof_received()
    message = loop.run_until_complete(receive_task)
    loop.close()

    assert message.data == b"complete at eof"
    assert message.end_of_message is True
    assert message.context.final is True
    assert connection.get_event_history()[-1]["name"] == "received"


def test_sections_9_3_1_and_9_3_2_udp_honors_max_length():
    loop, connection, remote = _connection(protocol="udp")
    transport = UdpTransport(connection=connection, remote_endpoint=remote)
    connection._mark_ready()

    first_task = loop.create_task(connection.receive(1, 3))
    transport.datagram_received(b"hello", ("203.0.113.10", 443))
    first = loop.run_until_complete(first_task)
    second = loop.run_until_complete(connection.receive(1, 3))
    loop.close()

    assert first.data == b"hel"
    assert first.end_of_message is False
    assert second.data == b"lo"
    assert second.end_of_message is True
    assert first.context is second.context
    assert [
        event["name"]
        for event in connection.get_event_history()
        if event["name"].startswith("received")
    ] == ["received_partial", "received_partial"]


def test_section_9_3_receive_consumes_losing_transport_exception():
    loop, connection, _remote = _connection()
    connection.state = taps.ConnectionState.ESTABLISHED
    unhandled = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

    class SimultaneousCloseTransport:
        def receive(self, min_incomplete_length, max_length):
            read_result = loop.create_future()
            read_result.set_exception(EOFError("simultaneous EOF"))
            connection._report_closed()
            return read_result

    connection.transports = [SimultaneousCloseTransport()]

    with pytest.raises(
        ConnectionError,
        match="Connection closed before the Receive completed",
    ):
        loop.run_until_complete(connection.receive(1, 10))
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()

    assert unhandled == []


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [
        (-2, 10),
        (1.5, 10),
        (1, 0),
        (11, 10),
    ],
)
def test_section_9_3_1_receive_rejects_invalid_lengths(minimum, maximum):
    loop, connection, _remote = _connection()
    connection.state = taps.ConnectionState.ESTABLISHED

    with pytest.raises(ValueError):
        loop.run_until_complete(connection.receive(minimum, maximum))
    loop.close()
