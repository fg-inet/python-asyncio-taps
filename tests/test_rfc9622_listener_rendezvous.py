import asyncio
import socket

import pytest

import pytaps as taps
from pytaps.listener import Listener


def _listener(*, remote_endpoint=None):
    loop = asyncio.new_event_loop()
    local = taps.LocalEndpoint().with_address("127.0.0.1").with_port(8443)
    preconnection = taps.Preconnection(
        local_endpoint=local,
        remote_endpoint=remote_endpoint,
        event_loop=loop,
    )
    listener = Listener(preconnection)
    listener._mark_listening()
    return loop, listener


def _passive_connection(loop, *, address, port, protocol="tcp"):
    local = taps.LocalEndpoint().with_address("127.0.0.1").with_port(8443)
    remote = taps.RemoteEndpoint().with_address(address).with_port(port)
    preconnection = taps.Preconnection(
        local_endpoint=local,
        remote_endpoint=remote,
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)
    connection.protocol = protocol
    connection.remote_endpoint = remote
    connection.remote_endpoints = [remote]
    return connection


def test_section_7_2_new_connection_limit_decrements_and_can_reset():
    loop, listener = _listener()
    listener.set_new_connection_limit(1)
    first = _passive_connection(
        loop,
        address="203.0.113.10",
        port=5000,
    )
    rejected = _passive_connection(
        loop,
        address="203.0.113.11",
        port=5001,
    )

    assert listener._deliver_connection(first) is True
    assert listener._deliver_connection(rejected) is False
    assert listener.get_property("newConnectionLimit") == 0
    assert rejected.state is taps.ConnectionState.CLOSED

    listener.set_new_connection_limit("Infinite")
    third = _passive_connection(
        loop,
        address="203.0.113.12",
        port=5002,
    )
    assert listener._deliver_connection(third) is True
    assert listener.get_property("newConnectionLimit") == "Infinite"
    assert [
        event["name"]
        for event in listener.get_event_history()
        if event["name"] == "connection_received"
    ] == ["connection_received", "connection_received"]

    loop.run_until_complete(listener.stop())
    loop.close()


@pytest.mark.parametrize("value", [-1, 1.5, True, [], "unlimited"])
def test_section_7_2_new_connection_limit_rejects_invalid_values(value):
    loop, listener = _listener()
    with pytest.raises(ValueError):
        listener.set_new_connection_limit(value)
    loop.run_until_complete(listener.stop())
    loop.close()


def test_section_7_2_listener_enforces_remote_endpoint_constraints():
    constraint = (
        taps.RemoteEndpoint()
        .with_address("203.0.113.10")
        .with_port(5000)
        .with_protocol("tcp")
    )
    loop, listener = _listener(remote_endpoint=constraint)
    matching = _passive_connection(
        loop,
        address="203.0.113.10",
        port=5000,
    )
    wrong_address = _passive_connection(
        loop,
        address="203.0.113.11",
        port=5000,
    )
    wrong_port = _passive_connection(
        loop,
        address="203.0.113.10",
        port=5001,
    )
    wrong_protocol = _passive_connection(
        loop,
        address="203.0.113.10",
        port=5000,
        protocol="udp",
    )

    assert listener._deliver_connection(matching) is True
    assert listener._deliver_connection(wrong_address) is False
    assert listener._deliver_connection(wrong_port) is False
    assert listener._deliver_connection(wrong_protocol) is False
    assert listener.get_properties()["readOnly"]["pendingConnections"] == 1

    loop.run_until_complete(listener.stop())
    loop.close()


def test_section_7_2_connection_received_is_ready_without_ready_event():
    loop, listener = _listener()
    received = []
    ready = []

    async def handle_connection_received(connection):
        received.append(connection)

    async def handle_ready(connection):
        ready.append(connection)

    listener.connection_received = handle_connection_received
    listener.ready = handle_ready
    connection = _passive_connection(
        loop,
        address="203.0.113.10",
        port=5000,
    )

    assert listener._deliver_connection(connection) is True
    loop.run_until_complete(asyncio.sleep(0))

    assert connection.state is taps.ConnectionState.ESTABLISHED
    assert connection._ready_waiter.result() is connection
    assert received == [connection]
    assert ready == []
    assert all(
        event["name"] != "ready"
        for event in connection.get_event_history()
    )

    loop.run_until_complete(listener.stop())
    loop.close()


def test_section_7_2_listener_failure_emits_establishment_error():
    loop, listener = _listener()
    callbacks = []

    async def handle_establishment_error(error, failed_listener):
        callbacks.append((error, failed_listener))

    listener.establishment_error = handle_establishment_error
    failure = RuntimeError("cannot listen")

    listener._fail_listen(failure)
    loop.run_until_complete(asyncio.sleep(0))

    assert listener.get_event_history()[-1]["name"] == "establishment_error"
    assert callbacks == [(failure, listener)]
    assert listener.state is taps.ConnectionState.CLOSED
    loop.close()


def test_section_7_3_udp_rendezvous_waits_for_first_message(monkeypatch):
    loop = asyncio.new_event_loop()
    local = taps.LocalEndpoint().with_address("192.0.2.1").with_port(5000)
    remote = taps.RemoteEndpoint().with_address("198.51.100.1").with_port(5000)
    preconnection = taps.Preconnection(
        local_endpoint=local,
        remote_endpoint=remote,
        event_loop=loop,
    )
    created = []

    class FakeListener:
        def __init__(self):
            self.stopped = False
            self._accept_waiter = loop.create_future()

        async def wait_listening(self, timeout=None):
            return self

        def accept(self):
            return loop.create_task(self._wait_for_connection())

        async def _wait_for_connection(self):
            return await self._accept_waiter

        async def stop(self):
            self.stopped = True

    fake_listener = FakeListener()

    async def fake_listen(self, timeout=None):
        return fake_listener

    async def fake_initiate(self, timeout=None):
        connection = taps.Connection(self)
        connection.protocol = "udp"
        connection._mark_ready()
        created.append(connection)
        return connection

    monkeypatch.setattr(taps.Preconnection, "listen", fake_listen)
    monkeypatch.setattr(taps.Preconnection, "initiate", fake_initiate)

    rendezvous_task = loop.create_task(preconnection.rendezvous(timeout=1))
    loop.run_until_complete(asyncio.sleep(0))
    assert created
    assert rendezvous_task.done() is False

    created[0]._mark_first_message()
    connection = loop.run_until_complete(rendezvous_task)
    loop.close()

    assert connection is created[0]
    assert fake_listener.stopped is True
    assert [
        event["name"]
        for event in connection.get_event_history()
        if event["name"] in {"ready", "rendezvous_done"}
    ] == ["rendezvous_done"]


def test_section_7_3_two_peers_select_a_usable_connection():
    loop = asyncio.new_event_loop()
    unhandled = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    first_port = probe.getsockname()[1]
    probe.close()
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    second_port = probe.getsockname()[1]
    probe.close()

    first = taps.Preconnection(
        local_endpoint=(
            taps.LocalEndpoint()
            .with_address("127.0.0.1")
            .with_port(first_port)
        ),
        remote_endpoint=(
            taps.RemoteEndpoint()
            .with_address("127.0.0.1")
            .with_port(second_port)
        ),
        event_loop=loop,
    )
    second = taps.Preconnection(
        local_endpoint=(
            taps.LocalEndpoint()
            .with_address("127.0.0.1")
            .with_port(second_port)
        ),
        remote_endpoint=(
            taps.RemoteEndpoint()
            .with_address("127.0.0.1")
            .with_port(first_port)
        ),
        event_loop=loop,
    )
    first.transport_properties.require("reliability")
    second.transport_properties.require("reliability")

    async def rendezvous_both():
        return await asyncio.gather(
            first.rendezvous(timeout=2),
            second.rendezvous(timeout=2),
        )

    first_connection, second_connection = loop.run_until_complete(
        rendezvous_both()
    )
    receive_task = loop.create_task(second_connection.receive(1, 5))
    loop.run_until_complete(first_connection.send(b"hello"))
    message = loop.run_until_complete(
        asyncio.wait_for(receive_task, 1)
    )

    assert first_connection.state is taps.ConnectionState.ESTABLISHED
    assert second_connection.state is taps.ConnectionState.ESTABLISHED
    assert message.data == b"hello"
    assert [
        event["name"]
        for event in first_connection.get_event_history()
        if event["name"] in {"ready", "connection_received", "rendezvous_done"}
    ] == ["rendezvous_done"]
    assert [
        event["name"]
        for event in second_connection.get_event_history()
        if event["name"] in {"ready", "connection_received", "rendezvous_done"}
    ] == ["rendezvous_done"]

    first_connection.close()
    second_connection.close()
    async def wait_for_both_closed():
        await asyncio.gather(
            first_connection.wait_closed(),
            second_connection.wait_closed(),
        )

    loop.run_until_complete(wait_for_both_closed())
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()

    assert unhandled == []
