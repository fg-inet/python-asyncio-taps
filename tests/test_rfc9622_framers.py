import asyncio

import pytest

import pytaps as taps
from pytaps.listener import Listener, StreamHandler
from pytaps.transports import TcpTransport, UdpTransport


class FakeTcpTransport:
    def __init__(self, *, peer=("198.51.100.10", 443)):
        self.peer = peer
        self.writes = []
        self.closed = False

    def get_extra_info(self, name):
        if name == "peername":
            return self.peer
        if name == "sockname":
            return ("192.0.2.10", 54321)
        return None

    def write(self, data):
        self.writes.append(bytes(data))

    def close(self):
        self.closed = True


class FakeUdpTransport(FakeTcpTransport):
    def __init__(self, *, peer=("198.51.100.10", 443)):
        super().__init__(peer=peer)
        self.datagrams = []

    def sendto(self, data, address=None):
        self.datagrams.append((bytes(data), address))


class EnvelopeFramer(taps.Framer):
    def __init__(self, name, trace, *, use_send=False):
        super().__init__(namespace=f"example.{name.lower()}")
        self.name = name
        self.trace = trace
        self.use_send = use_send

    async def new_sent_message(
        self,
        connection,
        data,
        context,
        end_of_message,
    ):
        self.trace.append(f"{self.name}:out")
        framed = self.name.encode() + b"[" + data + b"]"
        if self.use_send:
            return await self.send(
                connection,
                framed,
                context,
                end_of_message,
            )
        return framed

    async def handle_received_data(self, connection):
        data, context, end_of_message = self.parse(
            connection,
            minimum_incomplete_length=1,
        )
        if data is None:
            return
        prefix = self.name.encode() + b"["
        if not end_of_message or not data.startswith(prefix) or not data.endswith(b"]"):
            raise taps.DeframingFailed(f"invalid {self.name} envelope")
        self.trace.append(f"{self.name}:in")
        self.advance_receive_cursor(connection, len(data))
        context.add(self, "decoded", True)
        self.deliver(
            connection,
            context,
            data[len(prefix):-1],
            True,
        )


async def open_tcp_connection(*framers, address="198.51.100.10"):
    remote = taps.RemoteEndpoint().with_address(address).with_port(443)
    preconnection = taps.Preconnection(remote_endpoint=remote)
    for framer in framers:
        preconnection.add_framer(framer)
    connection = taps.Connection(preconnection)
    connection.active = True
    transport = TcpTransport(
        connection,
        remote_endpoint=remote,
    )
    raw_transport = FakeTcpTransport(peer=(address, 443))
    await transport.active_open(raw_transport)
    return connection, transport, raw_transport


def test_framers_are_snapshotted_and_must_be_added_before_establishment():
    loop = asyncio.new_event_loop()
    first = EnvelopeFramer("FIRST", [])
    second = EnvelopeFramer("SECOND", [])
    preconnection = (
        taps.Preconnection(event_loop=loop)
        .add_framer(first)
        .add_framer(second)
    )

    snapshot = preconnection._snapshot("initiate")

    assert snapshot.framers == [first, second]
    with pytest.raises(RuntimeError, match="before creating"):
        preconnection.add_framer(EnvelopeFramer("LATE", []))
    with pytest.raises(RuntimeError, match="before creating"):
        snapshot.add_framer(EnvelopeFramer("LATE", []))
    loop.close()


@pytest.mark.asyncio
async def test_framers_are_stacked_and_metadata_is_namespaced():
    trace = []
    lower = EnvelopeFramer("LOWER", trace, use_send=True)
    upper = EnvelopeFramer("UPPER", trace)
    context = taps.MessageContext()
    context.add(lower, "type", "lower-value")
    context.add(upper, "type", "upper-value")

    assert context.get(lower, "type") == "lower-value"
    assert context.get(upper, "type") == "upper-value"
    assert context.get("example.lower", "type") == "lower-value"
    assert context.get_properties()["framerMetadata"] == {
        "example.lower": {"type": "lower-value"},
        "example.upper": {"type": "upper-value"},
    }

    connection, transport, raw = await open_tcp_connection(lower, upper)
    await connection.send(b"payload", context)
    await connection._send_drain_waiter

    assert raw.writes == [b"LOWER[UPPER[payload]]"]
    assert trace == ["UPPER:out", "LOWER:out"]

    received_context = taps.MessageContext()
    await transport._feed_framer(
        raw.writes[0],
        received_context,
        True,
    )
    message = await connection.receive(timeout=1)

    assert message.data == b"payload"
    assert trace == [
        "UPPER:out",
        "LOWER:out",
        "LOWER:in",
        "UPPER:in",
    ]
    assert message.get(lower, "decoded") is True
    assert message.get(upper, "decoded") is True

    connection.close()
    await connection.wait_closed(timeout=1)


@pytest.mark.asyncio
async def test_passive_udp_uses_the_framer_stack_in_both_directions():
    framer = EnvelopeFramer("UDP", [])
    local = taps.LocalEndpoint().with_address("192.0.2.10").with_port(9000)
    remote = (
        taps.RemoteEndpoint()
        .with_address("198.51.100.10")
        .with_port(7000)
    )
    preconnection = (
        taps.Preconnection(
            local_endpoint=local,
            remote_endpoint=remote,
        )
        .add_framer(framer)
    )
    connection = taps.Connection(preconnection)
    transport = UdpTransport(
        connection,
        local_endpoint=local,
        remote_endpoint=remote,
    )
    raw = FakeUdpTransport(peer=("198.51.100.10", 7000))
    await transport.passive_open(raw)

    transport.datagram_received(
        b"UDP[request]",
        ("198.51.100.10", 7000),
    )
    message = await connection.receive(timeout=1)
    await connection.send(
        b"response",
        taps.MessageContext(safely_replayable=True),
    )
    await connection._send_drain_waiter

    assert message.data == b"request"
    assert raw.datagrams == [
        (b"UDP[response]", ("198.51.100.10", 7000))
    ]

    connection.close()
    await connection.wait_closed(timeout=1)


@pytest.mark.asyncio
async def test_framer_start_and_stop_gate_ready_and_closed_events():
    ready_gate = asyncio.Event()
    closed_gate = asyncio.Event()
    events = []

    class LifecycleFramer(taps.Framer):
        async def start(self, connection):
            events.append("start")
            self.defer_connection_ready(connection)
            await self.send(connection, b"preface")

            async def finish_start():
                await ready_gate.wait()
                events.append("make-ready")
                self.make_connection_ready(connection)

            asyncio.create_task(finish_start())

        async def stop(self, connection):
            events.append("stop")
            self.defer_connection_closed(connection)
            await self.send(connection, b"epilogue")

            async def finish_stop():
                await closed_gate.wait()
                events.append("make-closed")
                self.make_connection_closed(connection)

            asyncio.create_task(finish_stop())

        async def handle_received_data(self, connection):
            return None

    framer = LifecycleFramer()
    remote = taps.RemoteEndpoint().with_address("198.51.100.10").with_port(443)
    preconnection = taps.Preconnection(remote_endpoint=remote).add_framer(framer)
    connection = taps.Connection(preconnection)
    connection.active = True
    transport = TcpTransport(connection, remote_endpoint=remote)
    raw = FakeTcpTransport()

    open_task = asyncio.create_task(transport.active_open(raw))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert connection.state is taps.ConnectionState.ESTABLISHING
    assert raw.writes == [b"preface"]

    ready_gate.set()
    await open_task
    assert connection.state is taps.ConnectionState.ESTABLISHED
    assert events[:2] == ["start", "make-ready"]

    close_task = connection.close()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert connection.state is taps.ConnectionState.CLOSING
    assert raw.writes == [b"preface", b"epilogue"]

    closed_gate.set()
    await close_task
    assert connection.state is taps.ConnectionState.CLOSED
    assert events == ["start", "make-ready", "stop", "make-closed"]


@pytest.mark.asyncio
async def test_framer_can_prepend_another_framer_before_readiness():
    trace = []
    prepended = EnvelopeFramer("DYNAMIC", trace)

    class PrependingFramer(EnvelopeFramer):
        async def start(self, connection):
            self.prepend_framer(connection, prepended)

    lower = PrependingFramer("LOWER", trace)
    connection, _, raw = await open_tcp_connection(lower)

    assert connection.framer_stack.framers == (lower, prepended)
    with pytest.raises(RuntimeError, match="before readiness"):
        lower.prepend_framer(
            connection,
            EnvelopeFramer("TOO-LATE", trace),
        )

    await connection.send(b"payload")
    await connection._send_drain_waiter

    assert raw.writes == [b"LOWER[DYNAMIC[payload]]"]
    assert trace == ["DYNAMIC:out", "LOWER:out"]

    connection.close()
    await connection.wait_closed(timeout=1)


@pytest.mark.asyncio
async def test_start_passthrough_forwards_current_and_future_data():
    calls = []

    class SwitchingFramer(taps.Framer):
        async def new_sent_message(
            self,
            connection,
            data,
            context,
            end_of_message,
        ):
            calls.append("out")
            return b"framed:" + data

        async def handle_received_data(self, connection):
            calls.append("in")
            self.start_passthrough(connection)

    framer = SwitchingFramer()
    connection, transport, raw = await open_tcp_connection(framer)

    await transport._feed_framer(b"switch", taps.MessageContext(), True)
    first = await connection.receive(timeout=1)
    await connection.send(b"payload")
    await connection._send_drain_waiter
    await transport._feed_framer(b"plain", taps.MessageContext(), True)
    second = await connection.receive(timeout=1)

    assert first.data == b"switch"
    assert second.data == b"plain"
    assert raw.writes == [b"payload"]
    assert calls == ["in"]

    connection.close()
    await connection.wait_closed(timeout=1)


@pytest.mark.asyncio
async def test_deliver_and_advance_can_earmark_bytes_not_yet_received():
    class LengthPrefixFramer(taps.Framer):
        async def new_sent_message(
            self,
            connection,
            data,
            context,
            end_of_message,
        ):
            return len(data).to_bytes(4, "big") + data

        async def handle_received_data(self, connection):
            header, context, _ = self.parse(
                connection,
                minimum_incomplete_length=4,
                maximum_length=4,
            )
            if header is None:
                return
            length = int.from_bytes(header, "big")
            self.advance_receive_cursor(connection, 4)
            context.add(self, "length", length)
            self.deliver_and_advance_receive_cursor(
                connection,
                context,
                length,
                True,
            )

    framer = LengthPrefixFramer(namespace="example.length")
    connection, transport, _ = await open_tcp_connection(framer)
    context = taps.MessageContext()

    await transport._feed_framer(
        b"\x00\x00\x00\x05he",
        context,
        False,
    )
    assert transport.framer_buffer == []

    await transport._feed_framer(b"llo", context, False)
    message = await connection.receive(timeout=1)

    assert message.data == b"hello"
    assert message.get(framer, "length") == 5

    connection.close()
    await connection.wait_closed(timeout=1)


@pytest.mark.asyncio
async def test_framer_failure_rejects_only_the_current_candidate():
    starts = []

    class CandidateFramer(taps.Framer):
        async def start(self, connection):
            address = connection.remote_endpoint.address
            starts.append(address)
            if address == "198.51.100.1":
                self.fail_connection(connection, "candidate rejected")

        async def handle_received_data(self, connection):
            return None

    framer = CandidateFramer()
    first_remote = (
        taps.RemoteEndpoint()
        .with_address("198.51.100.1")
        .with_port(443)
    )
    preconnection = (
        taps.Preconnection(remote_endpoint=first_remote)
        .add_framer(framer)
    )
    connection = taps.Connection(preconnection)
    connection.active = True
    first = TcpTransport(connection, remote_endpoint=first_remote)
    first_raw = FakeTcpTransport(peer=("198.51.100.1", 443))

    with pytest.raises(taps.FramerFailed, match="candidate rejected"):
        await first.active_open(first_raw)

    assert connection.state is taps.ConnectionState.ESTABLISHING
    assert first_raw.closed is True

    second_remote = (
        taps.RemoteEndpoint()
        .with_address("198.51.100.2")
        .with_port(443)
    )
    second = TcpTransport(connection, remote_endpoint=second_remote)
    second_raw = FakeTcpTransport(peer=("198.51.100.2", 443))
    await second.active_open(second_raw)

    assert connection.state is taps.ConnectionState.ESTABLISHED
    assert connection.transports[0] is second
    assert connection.framer_stack is second.framer_stack
    assert first.framer_stack is not second.framer_stack
    assert starts == ["198.51.100.1", "198.51.100.2"]

    connection.close()
    await connection.wait_closed(timeout=1)


@pytest.mark.asyncio
async def test_concurrent_framer_starts_get_task_local_candidate_endpoints():
    observed = []
    both_started = asyncio.Event()

    class CandidateFramer(taps.Framer):
        async def start(self, connection):
            observed.append(("before", connection.remote_endpoint.address))
            if len(observed) == 2:
                both_started.set()
            await both_started.wait()
            observed.append(("after", connection.remote_endpoint.address))

        async def handle_received_data(self, connection):
            return None

    first_remote = (
        taps.RemoteEndpoint()
        .with_address("198.51.100.10")
        .with_port(443)
    )
    second_remote = (
        taps.RemoteEndpoint()
        .with_address("198.51.100.20")
        .with_port(443)
    )
    preconnection = (
        taps.Preconnection(remote_endpoint=first_remote)
        .add_framer(CandidateFramer())
    )
    connection = taps.Connection(preconnection)
    first = TcpTransport(connection, remote_endpoint=first_remote)
    second = TcpTransport(connection, remote_endpoint=second_remote)

    await asyncio.gather(
        first._start_framers(),
        second._start_framers(),
    )

    assert {
        address
        for phase, address in observed
        if phase == "before"
    } == {"198.51.100.10", "198.51.100.20"}
    assert {
        address
        for phase, address in observed
        if phase == "after"
    } == {"198.51.100.10", "198.51.100.20"}
    connection._fail_initiate(RuntimeError("test cleanup"))


@pytest.mark.asyncio
async def test_runtime_framer_failure_closes_the_connection():
    class FailingFramer(taps.Framer):
        async def handle_received_data(self, connection):
            self.fail_connection(connection, ValueError("bad framing state"))

    connection, transport, raw = await open_tcp_connection(FailingFramer())
    await transport._feed_framer(b"bad", taps.MessageContext(), True)

    assert connection.state is taps.ConnectionState.CLOSED
    assert isinstance(connection.last_error, taps.FramerFailed)
    assert "bad framing state" in str(connection.last_error)
    assert raw.closed is True


@pytest.mark.asyncio
async def test_passive_connection_is_not_delivered_before_framer_ready():
    gate = asyncio.Event()

    class PassiveFramer(taps.Framer):
        async def start(self, connection):
            self.defer_connection_ready(connection)

            async def finish():
                await gate.wait()
                self.make_connection_ready(connection)

            asyncio.create_task(finish())

        async def handle_received_data(self, connection):
            data, context, end_of_message = self.parse(
                connection,
                minimum_incomplete_length=1,
            )
            if data is None:
                return
            self.deliver_and_advance_receive_cursor(
                connection,
                context,
                len(data),
                end_of_message,
            )

    local = taps.LocalEndpoint().with_address("127.0.0.1").with_port(4433)
    preconnection = (
        taps.Preconnection(local_endpoint=local)
        .add_framer(PassiveFramer())
    )
    listener = Listener(preconnection)
    listener._mark_listening()
    handler = StreamHandler(listener, "tcp")
    raw = FakeTcpTransport(peer=("127.0.0.1", 55555))
    accept_task = listener.accept(timeout=1)

    handler.connection_made(raw)
    handler.data_received(b"hello")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not accept_task.done()
    assert handler.connection.state is taps.ConnectionState.ESTABLISHING

    gate.set()
    accepted = await accept_task
    message = await accepted.receive(timeout=1)

    assert accepted is handler.connection
    assert message.data == b"hello"

    accepted.close()
    await accepted.wait_closed(timeout=1)
    await listener.stop()
