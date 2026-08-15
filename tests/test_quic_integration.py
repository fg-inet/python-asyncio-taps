import asyncio
import socket
import ssl
from pathlib import Path
from types import SimpleNamespace

import pytest

import pytaps as taps
import pytaps.connection_context as connection_context_module
from pytaps.transports import (
    QUIC_DATAGRAM_QUEUE_LIMIT,
    QuicAssociationError,
    QuicAssociationManager,
    QuicDatagramTransport,
    QuicStreamError,
    QuicTransport,
)
from pytaps.utility import rank_protocol_candidates


KEYS = Path(__file__).parent / "keys"
SERVER_CERTIFICATE = KEYS / "localhost.pem"
ROOT_CERTIFICATE = KEYS / "MyRootCA.pem"


def _quic_security(*, server, pin=SERVER_CERTIFICATE):
    security = taps.SecurityParameters()
    security.set_alpn_protocols(["taps-quic-test"])
    if server:
        security.add_identity(str(SERVER_CERTIFICATE))
    else:
        security.add_trust_ca(str(ROOT_CERTIFICATE))
        if pin is not None:
            security.add_pinned_server_certificate(str(pin))
        security.with_server_name("localhost")
    return security


def _quic_properties(*, multipath=None):
    properties = taps.TransportProperties()
    properties.require("multistreaming")
    if multipath is not None:
        properties.set_property("multipath", multipath)
    return properties


async def _start_quic_listener(*, connection_context=None):
    preconnection = taps.Preconnection(
        local_endpoint=(
            taps.LocalEndpoint()
            .with_address("127.0.0.1")
            .with_port(0)
        ),
        transport_properties=_quic_properties(),
        security_parameters=_quic_security(server=True),
        connection_context=connection_context,
    )
    return await preconnection.listen(timeout=3)


async def _connect_quic_stream(
    listener,
    *,
    connection_context=None,
    message_context=None,
    initiate_with_send=None,
    transport_properties=None,
):
    port = listener.quic_association.bound_port()
    preconnection = taps.Preconnection(
        remote_endpoint=(
            taps.RemoteEndpoint()
            .with_hostname("localhost")
            .with_address("127.0.0.1")
            .with_port(port)
        ),
        transport_properties=(
            transport_properties or _quic_properties()
        ),
        security_parameters=_quic_security(server=False),
        connection_context=connection_context,
    )
    if initiate_with_send is not None:
        return await preconnection.initiate_with_send(
            initiate_with_send,
            message_context,
            timeout=3,
        )
    return await preconnection.initiate(timeout=3)


async def _wait_for_client_ticket(connection_context, timeout=3):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        snapshot = (
            connection_context.get_quic_session_cache_snapshot()
        )
        if snapshot["clientTickets"]:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("QUIC client session ticket was not cached")


async def _wait_for_quic_performance(connection, timeout=3):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        association = connection.quic_association
        performance = association.performance_snapshot()
        cached = connection.connection_context.get_performance_metrics(
            protocol="quic",
            network_id="default",
        )
        if (
            performance["rttAvailable"]
            and cached is not None
            and cached["rttSamples"]
            and cached["establishmentSamples"]
        ):
            return performance, cached
        association.protocol._quic.send_ping(id(connection))
        association.protocol.transmit()
        await asyncio.sleep(0.01)
    raise AssertionError("QUIC performance metrics were not cached")


async def _bootstrap_quic_ticket(listener, connection_context):
    connection = await _connect_quic_stream(
        listener,
        connection_context=connection_context,
    )
    await connection.send(b"ticket-bootstrap")
    server_connection = await listener.accept(timeout=3)
    await server_connection.receive(
        min_incomplete_length=1,
        timeout=3,
    )
    await _wait_for_client_ticket(connection_context)
    await _close_connections(connection, server_connection)
    await asyncio.sleep(0)


async def _close_connections(*connections):
    tasks = []
    for connection in connections:
        task = connection.close()
        if task is not None:
            tasks.append(task)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def test_quic_session_ticket_caches_are_bounded_expiring_and_isolated(
    monkeypatch,
):
    now = [100.0]
    monkeypatch.setattr(
        connection_context_module.time,
        "time",
        lambda: now[0],
    )
    context = taps.ConnectionContext()
    tickets = [
        SimpleNamespace(ticket=bytes([index]), is_valid=True)
        for index in range(4)
    ]

    assert context.cache_quic_client_session_ticket(
        "first",
        tickets[0],
        capacity=2,
        lifetime=10,
    )
    assert context.cache_quic_client_session_ticket(
        "second",
        tickets[1],
        capacity=2,
        lifetime=10,
    )
    assert context.cache_quic_client_session_ticket(
        "third",
        tickets[2],
        capacity=2,
        lifetime=10,
    )
    assert context.get_quic_client_session_ticket("first") is None
    assert context.take_quic_client_session_ticket("second") is tickets[1]
    assert context.take_quic_client_session_ticket("second") is None

    assert context.cache_quic_server_session_ticket(
        tickets[3],
        capacity=1,
        lifetime=10,
    )
    assert context.take_quic_server_session_ticket(b"\x03") is tickets[3]
    assert context.take_quic_server_session_ticket(b"\x03") is None

    isolated = context.clone()
    assert isolated.get_quic_session_cache_snapshot() == {
        "clientTickets": 0,
        "serverTickets": 0,
    }

    now[0] = 111.0
    assert context.get_quic_client_session_ticket("third") is None
    assert context.get_quic_session_cache_snapshot() == {
        "clientTickets": 0,
        "serverTickets": 0,
    }


def test_quic_session_cache_configuration_is_validated():
    security = taps.SecurityParameters()

    security.set_session_cache_capacity(0)
    security.set_session_cache_lifetime(0.5)
    assert security.session_cache_capacity == 0
    assert security.session_cache_lifetime == 0.5

    with pytest.raises(ValueError, match="capacity"):
        security.set_session_cache_capacity(True)
    with pytest.raises(ValueError, match="capacity"):
        security.set_session_cache_capacity(-1)
    with pytest.raises(ValueError, match="lifetime"):
        security.set_session_cache_lifetime(-0.1)


@pytest.mark.asyncio
async def test_quic_performance_metrics_feed_future_candidate_state():
    pytest.importorskip("aioquic")
    client_context = taps.ConnectionContext()
    listener = await _start_quic_listener()
    client = await _connect_quic_stream(
        listener,
        connection_context=client_context,
    )
    await client.send(b"measure-rtt")
    server = await listener.accept(timeout=3)
    message = await server.receive(
        min_incomplete_length=1,
        timeout=3,
    )
    assert message.data == b"measure-rtt"

    performance, cached = await _wait_for_quic_performance(client)

    assert performance["latestRtt"] > 0
    assert performance["smoothedRtt"] > 0
    assert performance["minimumRtt"] > 0
    assert performance["congestionWindow"] > 0
    assert performance["bytesInFlight"] >= 0
    assert cached["latestRtt"] > 0
    assert cached["establishmentLatency"] > 0
    assert cached["successRate"] == 1
    assert (
        client.get_properties()["readOnly"]["quicAssociation"][
            "performance"
        ]
        == performance
    )

    clone = await client.clone()
    await clone.send(b"association-reuse")
    server_clone = await listener.accept(timeout=3)
    await server_clone.receive(min_incomplete_length=1, timeout=3)
    after_clone = client_context.get_performance_metrics(
        protocol="quic",
        network_id="default",
    )
    assert after_clone["establishmentSamples"] == 1
    assert after_clone["successes"] == 1
    assert client_context.protocol_cache["quic"]["successes"] == 1

    await _close_connections(
        clone,
        client,
        server_clone,
        server,
    )
    await listener.stop()


@pytest.mark.asyncio
async def test_quic_listener_stop_does_not_close_accepted_connection():
    pytest.importorskip("aioquic")
    listener = await _start_quic_listener()
    parent_association = listener.quic_association
    client = await _connect_quic_stream(listener)
    await client.send(b"before-listener-stop")
    server = await listener.accept(timeout=3)
    before = await server.receive(
        min_incomplete_length=1,
        timeout=3,
    )
    assert before.data == b"before-listener-stop"
    server_association = server.quic_association

    await listener.stop()

    assert listener.state is taps.ConnectionState.CLOSED
    assert parent_association.server is not None
    assert parent_association._listener_draining is True
    assert server_association in parent_association.child_associations
    assert client.state is taps.ConnectionState.ESTABLISHED
    assert server.state is taps.ConnectionState.ESTABLISHED

    await client.send(b"after-listener-stop")
    after = await server.receive(
        min_incomplete_length=1,
        timeout=3,
    )
    assert after.data == b"after-listener-stop"

    await _close_connections(client, server)
    for _ in range(10):
        if parent_association.server is None:
            break
        await asyncio.sleep(0)
    assert parent_association.server is None


@pytest.mark.asyncio
async def test_quic_listener_rebinds_same_port_and_drains_old_path():
    pytest.importorskip("aioquic")
    context = taps.ConnectionContext()
    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "loopback": {
                    "available": True,
                    "addresses": [{"address": "127.0.0.1"}],
                },
            },
        )
    )
    listener = taps.Listener(
        taps.Preconnection(
            local_endpoint=(
                taps.LocalEndpoint()
                .with_interface("loopback")
                .with_port(0)
            ),
            transport_properties=_quic_properties(),
            security_parameters=_quic_security(server=True),
            connection_context=context,
        )
    )
    listener._protocol_candidates = ["quic"]
    old_endpoint = (
        listener.local_endpoints[0].clone()
        .with_address("127.0.0.1")
    )
    assert await listener._start_candidate(
        "quic",
        old_endpoint,
        template_index=0,
    )
    listener._refresh_system_policy_paths(record=False)
    listener._mark_listening()
    old_parent = listener.quic_association
    port = old_parent.bound_port()

    old_client = await _connect_quic_stream(listener)
    await old_client.send(b"before-rebind")
    old_server = await listener.accept(timeout=3)
    before = await old_server.receive(
        min_incomplete_length=1,
        timeout=3,
    )
    assert before.data == b"before-rebind"

    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "loopback": {
                    "available": True,
                    "addresses": [{"address": "::1"}],
                },
            },
        )
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 3
    rebound = False
    while loop.time() < deadline:
        rebound = (
            listener.quic_association is not old_parent
            and listener.get_property("boundLocalEndpoints")[0].address
            == "::1"
        )
        if rebound:
            break
        await asyncio.sleep(0.01)
    if not rebound:
        error = listener.get_property("pathReconciliationError")
        await _close_connections(old_client, old_server)
        await listener.stop()
        pytest.fail(
            "QUIC Listener did not rebind to ::1 within 3 seconds "
            f"(last reconciliation error: {error})"
        )
    if listener._policy_reconcile_task is not None:
        await listener._policy_reconcile_task

    new_parent = listener.quic_association
    assert new_parent is not old_parent
    assert new_parent.bound_port() == port
    assert old_parent._listener_draining is True
    assert old_parent.server is not None
    assert [
        endpoint.address
        for endpoint in listener.get_property("boundLocalEndpoints")
    ] == ["::1"]
    assert [
        endpoint.address
        for endpoint in listener.get_property("drainingLocalEndpoints")
    ] == ["127.0.0.1"]

    rejected_client = await taps.Preconnection(
        remote_endpoint=(
            taps.RemoteEndpoint()
            .with_hostname("localhost")
            .with_address("127.0.0.1")
            .with_port(port)
        ),
        transport_properties=_quic_properties(),
        security_parameters=_quic_security(server=False),
    ).initiate()
    with pytest.raises(TimeoutError):
        await rejected_client.wait_ready(timeout=0.2)
    rejected_client.race_task.cancel()
    await asyncio.gather(
        rejected_client.race_task,
        return_exceptions=True,
    )
    rejected_client.abort("retired Listener path rejected association")
    if rejected_client._close_task is not None:
        await rejected_client._close_task
    await rejected_client.wait_closed(timeout=1)

    await old_client.send(b"existing-after-rebind")
    existing = await old_server.receive(
        min_incomplete_length=1,
        timeout=3,
    )
    assert existing.data == b"existing-after-rebind"

    new_client = await taps.Preconnection(
        remote_endpoint=(
            taps.RemoteEndpoint()
            .with_hostname("localhost")
            .with_address("::1")
            .with_port(port)
        ),
        transport_properties=_quic_properties(),
        security_parameters=_quic_security(server=False),
    ).initiate(timeout=3)
    await new_client.send(b"new-path")
    new_server = await listener.accept(timeout=3)
    received = await new_server.receive(
        min_incomplete_length=1,
        timeout=3,
    )
    assert received.data == b"new-path"

    await _close_connections(
        old_client,
        old_server,
        new_client,
        new_server,
    )
    for _ in range(10):
        if old_parent.server is None:
            break
        await asyncio.sleep(0)
    assert old_parent.server is None
    assert listener.get_property("drainingLocalEndpoints") == []
    await listener.stop()


def test_quic_protocol_properties_configure_quic_without_forcing_selection():
    properties = taps.TransportProperties()

    properties.set_property("_pytaps.quicStreamType", "unidirectional")
    properties.set_property("_pytaps.quicTransportMode", "datagram")

    assert properties.get("_pytaps.quicStreamType") == "Unidirectional"
    assert properties.get("_pytaps.quicTransportMode") == "Datagram"
    assert properties.get_properties()["protocolSpecific"] == {
        "_pytaps.quicStreamType": "Unidirectional",
        "_pytaps.quicTransportMode": "Datagram",
    }
    assert [
        candidate[0]["name"]
        for candidate in rank_protocol_candidates(properties)
    ] == ["quic", "tcp", "tls-tcp"]
    with pytest.raises(ValueError, match="quicStreamType"):
        properties.set_property("_pytaps.quicStreamType", "sideways")

    properties.require("reliability")
    assert [
        candidate[0]["name"]
        for candidate in rank_protocol_candidates(properties)
    ] == ["tcp", "tls-tcp"]

    properties.require("multistreaming")
    assert rank_protocol_candidates(properties) == []


@pytest.mark.asyncio
async def test_unused_association_closes_when_peer_has_no_datagram_support():
    class FakeContextManager:
        def __init__(self):
            self.closed = False

        async def __aexit__(self, exc_type, exc, traceback):
            self.closed = True

    preconnection = taps.Preconnection(
        remote_endpoint=(
            taps.RemoteEndpoint()
            .with_address("127.0.0.1")
            .with_port(4433)
        ),
        transport_properties=_quic_properties(),
    )
    connection = taps.Connection(preconnection)
    association = QuicAssociationManager(
        loop=asyncio.get_running_loop(),
    )
    context_manager = FakeContextManager()
    association.protocol = object()
    association.context_manager = context_manager

    with pytest.raises(RuntimeError, match="did not negotiate"):
        await association.open_datagram_connection(connection)

    assert context_manager.closed is True
    assert association.protocol is None
    assert association.context_manager is None
    connection._fail_initiate(RuntimeError("test cleanup"))


@pytest.mark.asyncio
async def test_quic_invalid_write_watermarks_fail_without_waiting():
    association = QuicAssociationManager(
        loop=asyncio.get_running_loop(),
    )
    association.stream_write_buffer_high_water = 0

    with pytest.raises(ValueError, match="high-water marks"):
        await association.write_stream_data(object(), b"blocked")

    assert association.resource_snapshot()["transportStateWaiters"] == 0


@pytest.mark.asyncio
async def test_real_quic_association_mixes_bidi_uni_and_datagrams():
    pytest.importorskip("aioquic")

    listener_preconnection = taps.Preconnection(
        local_endpoint=(
            taps.LocalEndpoint()
            .with_address("127.0.0.1")
            .with_port(0)
        ),
        transport_properties=_quic_properties(),
        security_parameters=_quic_security(server=True),
    )
    listener = await listener_preconnection.listen(timeout=3)
    port = listener.quic_association.bound_port()

    client_preconnection = taps.Preconnection(
        remote_endpoint=(
            taps.RemoteEndpoint()
            .with_hostname("localhost")
            .with_address("127.0.0.1")
            .with_port(port)
        ),
        transport_properties=_quic_properties(),
        security_parameters=_quic_security(server=False),
    )

    client_bidi = await client_preconnection.initiate(timeout=3)
    await client_bidi.send(b"bidi")
    server_bidi = await listener.accept(timeout=3)

    assert isinstance(client_bidi.transports[0], QuicTransport)
    assert isinstance(server_bidi.transports[0], QuicTransport)
    assert client_bidi.get_property("direction") == "Bidirectional"
    assert server_bidi.get_property("direction") == "Bidirectional"

    bidi_message = await server_bidi.receive(
        min_incomplete_length=1,
        timeout=3,
    )
    assert bidi_message.data == b"bidi"

    client_uni = await client_bidi.clone(
        connection_properties={
            "_pytaps.quicStreamType": "Unidirectional",
        }
    )
    await client_uni.send(b"uni")
    server_uni = await listener.accept(timeout=3)

    assert client_uni.get_property("direction") == "Unidirectional Send"
    assert server_uni.get_property("direction") == "Unidirectional Receive"
    assert client_uni.transports[0].stream_id & 0x02
    assert server_uni.transports[0].stream_id == client_uni.transports[0].stream_id
    assert client_uni.get_properties()["readOnly"]["canReceive"] is False
    assert server_uni.get_properties()["readOnly"]["canSend"] is False

    uni_message = await server_uni.receive(
        min_incomplete_length=1,
        timeout=3,
    )
    assert uni_message.data == b"uni"

    client_datagrams = await client_bidi.clone(
        connection_properties={
            "_pytaps.quicTransportMode": "Datagram",
        }
    )
    await client_datagrams.send(b"datagram")
    server_datagrams = await listener.accept(timeout=3)
    datagram_message = await server_datagrams.receive(timeout=3)

    assert isinstance(
        client_datagrams.transports[0],
        QuicDatagramTransport,
    )
    assert isinstance(
        server_datagrams.transports[0],
        QuicDatagramTransport,
    )
    assert datagram_message.data == b"datagram"
    assert datagram_message.get("msgReliable") is False
    assert client_datagrams.quic_association is client_bidi.quic_association
    assert server_datagrams.quic_association is server_bidi.quic_association

    await server_datagrams.send(b"datagram-reply")
    reply = await client_datagrams.receive(timeout=3)
    assert reply.data == b"datagram-reply"

    group_size = len(client_datagrams.connection_group)
    active_connections = client_datagrams.get_monitoring_snapshot()[
        "connectionContext"
    ]["connectionCounts"]["active"]
    with pytest.raises(
        RuntimeError,
        match="one association-wide datagram channel",
    ):
        await client_datagrams.clone(
            connection_properties={
                "_pytaps.quicTransportMode": "Datagram",
            }
        )
    assert len(client_datagrams.connection_group) == group_size
    assert (
        client_datagrams.get_monitoring_snapshot()["connectionContext"][
            "connectionCounts"
        ]["active"]
        == active_connections
    )

    client_bidi_again = await client_datagrams.clone(
        connection_properties={
            "_pytaps.quicTransportMode": "Stream",
            "_pytaps.quicStreamType": "Bidirectional",
        }
    )
    await client_bidi_again.send(b"bidi-again")
    server_bidi_again = await listener.accept(timeout=3)
    bidi_again_message = await server_bidi_again.receive(
        min_incomplete_length=1,
        timeout=3,
    )

    assert bidi_again_message.data == b"bidi-again"
    assert client_bidi_again.quic_association is client_bidi.quic_association
    assert server_bidi_again.quic_association is server_bidi.quic_association
    assert client_bidi_again.connection_group is client_bidi.connection_group
    assert server_bidi_again.connection_group is server_bidi.connection_group

    for connection in (
        client_bidi_again,
        client_datagrams,
        client_uni,
        client_bidi,
        server_bidi_again,
        server_datagrams,
        server_uni,
        server_bidi,
    ):
        close_task = connection.close()
        if close_task is not None:
            await close_task

    bad_pin_preconnection = taps.Preconnection(
        remote_endpoint=(
            taps.RemoteEndpoint()
            .with_hostname("localhost")
            .with_address("127.0.0.1")
            .with_port(port)
        ),
        transport_properties=_quic_properties(),
        security_parameters=_quic_security(
            server=False,
            pin=ROOT_CERTIFICATE,
        ),
    )
    with pytest.raises(
        ssl.SSLCertVerificationError,
        match="pinned",
    ):
        await bad_pin_preconnection.initiate(timeout=3)

    await listener.stop()


@pytest.mark.asyncio
async def test_real_quic_can_start_with_datagrams_then_clone_stream():
    pytest.importorskip("aioquic")

    listener_preconnection = taps.Preconnection(
        local_endpoint=(
            taps.LocalEndpoint()
            .with_address("127.0.0.1")
            .with_port(0)
        ),
        transport_properties=_quic_properties(),
        security_parameters=_quic_security(server=True),
    )
    listener = await listener_preconnection.listen(timeout=3)
    port = listener.quic_association.bound_port()

    properties = _quic_properties()
    properties.apply_profile("unreliable-datagram")
    properties.set_property("_pytaps.quicTransportMode", "Datagram")
    client_preconnection = taps.Preconnection(
        remote_endpoint=(
            taps.RemoteEndpoint()
            .with_hostname("localhost")
            .with_address("127.0.0.1")
            .with_port(port)
        ),
        transport_properties=properties,
        security_parameters=_quic_security(server=False),
    )

    client_datagrams = await client_preconnection.initiate(timeout=3)
    assert isinstance(
        client_datagrams.transports[0],
        QuicDatagramTransport,
    )
    assert client_datagrams.get_property("reliability") is False
    assert client_datagrams.get_property("preserveMsgBoundaries") is True
    assert (
        client_datagrams.get_properties()["readOnly"]["sendMsgMaxLen"]
        > 0
    )

    await client_datagrams.send(b"datagram-first")
    server_datagrams = await listener.accept(timeout=3)
    received = await server_datagrams.receive(timeout=3)
    assert received.data == b"datagram-first"

    client_stream = await client_datagrams.clone(
        connection_properties={
            "_pytaps.quicTransportMode": "Stream",
            "_pytaps.quicStreamType": "Bidirectional",
        }
    )
    await client_stream.send(b"stream-second")
    server_stream = await listener.accept(timeout=3)
    stream_received = await server_stream.receive(
        min_incomplete_length=1,
        timeout=3,
    )

    assert stream_received.data == b"stream-second"
    assert client_stream.quic_association is client_datagrams.quic_association
    assert server_stream.quic_association is server_datagrams.quic_association
    assert client_stream.connection_group is client_datagrams.connection_group
    assert server_stream.connection_group is server_datagrams.connection_group

    for connection in (
        client_stream,
        client_datagrams,
        server_stream,
        server_datagrams,
    ):
        close_task = connection.close()
        if close_task is not None:
            await close_task
    await listener.stop()


@pytest.mark.asyncio
async def test_quic_bidirectional_half_close_allows_peer_reply():
    pytest.importorskip("aioquic")
    listener = await _start_quic_listener()
    client = await _connect_quic_stream(listener)

    await client.send(
        b"request",
        client.new_message_context(final=True),
    )
    server = await listener.accept(timeout=3)
    request = await server.receive(
        min_incomplete_length=1,
        timeout=3,
    )
    assert request.data == b"request"
    assert request.is_complete is True

    await server.send(
        b"response",
        server.new_message_context(final=True),
    )
    await asyncio.wait_for(
        asyncio.shield(server._send_drain_waiter),
        timeout=3,
    )
    response_chunks = []
    while True:
        response = await client.receive(
            min_incomplete_length=1,
            timeout=3,
        )
        response_chunks.append(response.data)
        if response.is_complete:
            break

    assert b"".join(response_chunks) == b"response"

    await _close_connections(client, server)
    await listener.stop()


@pytest.mark.asyncio
async def test_server_quic_group_survives_an_idle_association():
    pytest.importorskip("aioquic")

    listener_preconnection = taps.Preconnection(
        local_endpoint=(
            taps.LocalEndpoint()
            .with_address("127.0.0.1")
            .with_port(0)
        ),
        transport_properties=_quic_properties(),
        security_parameters=_quic_security(server=True),
    )
    listener = await listener_preconnection.listen(timeout=3)
    port = listener.quic_association.bound_port()

    client_preconnection = taps.Preconnection(
        remote_endpoint=(
            taps.RemoteEndpoint()
            .with_hostname("localhost")
            .with_address("127.0.0.1")
            .with_port(port)
        ),
        transport_properties=_quic_properties(),
        security_parameters=_quic_security(server=False),
    )
    first_client = await client_preconnection.initiate(timeout=3)
    await first_client.send(b"first")
    first_server = await listener.accept(timeout=3)
    await first_server.receive(
        min_incomplete_length=1,
        timeout=3,
    )
    server_group = first_server.connection_group

    await first_server.close()

    second_client = await first_client.clone(
        connection_properties={
            "_pytaps.quicStreamType": "Bidirectional",
        }
    )
    await second_client.send(b"second")
    second_server = await listener.accept(timeout=3)
    received = await second_server.receive(
        min_incomplete_length=1,
        timeout=3,
    )

    assert received.data == b"second"
    assert second_server.connection_group is server_group

    for connection in (
        second_client,
        first_client,
        second_server,
    ):
        close_task = connection.close()
        if close_task is not None:
            await close_task
    await listener.stop()


@pytest.mark.asyncio
async def test_quic_concurrent_peers_and_clones_use_unique_streams():
    pytest.importorskip("aioquic")
    listener = await _start_quic_listener()

    async def connect_and_send(index):
        connection = await _connect_quic_stream(listener)
        await connection.send(f"peer-{index}".encode())
        return connection

    clients = await asyncio.gather(
        *(connect_and_send(index) for index in range(4))
    )
    servers = await asyncio.gather(
        *(listener.accept(timeout=3) for _ in clients)
    )
    peer_messages = await asyncio.gather(
        *(
            connection.receive(min_incomplete_length=6, timeout=3)
            for connection in servers
        )
    )

    assert {message.data for message in peer_messages} == {
        f"peer-{index}".encode() for index in range(4)
    }
    assert len({id(connection.quic_association) for connection in clients}) == 4
    assert len({id(connection.quic_association) for connection in servers}) == 4

    anchor = clients[0]
    clones = await asyncio.gather(
        *(
            anchor.clone(
                connection_properties={
                    "_pytaps.quicStreamType": (
                        "Unidirectional"
                        if index % 2
                        else "Bidirectional"
                    ),
                }
            )
            for index in range(12)
        )
    )
    stream_ids = [
        connection.transports[0].stream_id
        for connection in clones
    ]
    assert len(stream_ids) == len(set(stream_ids))

    await asyncio.gather(
        *(
            connection.send(f"clone-{index}".encode())
            for index, connection in enumerate(clones)
        )
    )
    server_clones = await asyncio.gather(
        *(listener.accept(timeout=3) for _ in clones)
    )
    clone_messages = await asyncio.gather(
        *(
            connection.receive(min_incomplete_length=7, timeout=3)
            for connection in server_clones
        )
    )
    assert {message.data for message in clone_messages} == {
        f"clone-{index}".encode() for index in range(12)
    }

    await _close_connections(
        *clones,
        *clients,
        *server_clones,
        *servers,
    )
    await listener.stop()


@pytest.mark.asyncio
async def test_quic_member_close_and_abort_do_not_kill_siblings():
    pytest.importorskip("aioquic")
    listener = await _start_quic_listener()
    first_client = await _connect_quic_stream(listener)
    await first_client.send(b"first")
    first_server = await listener.accept(timeout=3)
    await first_server.receive(min_incomplete_length=5, timeout=3)

    second_client, survivor_client = await asyncio.gather(
        first_client.clone(),
        first_client.clone(),
    )
    await asyncio.gather(
        second_client.send(b"second"),
        survivor_client.send(b"survivor"),
    )
    accepted = await asyncio.gather(
        listener.accept(timeout=3),
        listener.accept(timeout=3),
    )
    received = await asyncio.gather(
        *(
            connection.receive(min_incomplete_length=6, timeout=3)
            for connection in accepted
        )
    )
    server_by_payload = {
        message.data: connection
        for message, connection in zip(received, accepted, strict=True)
    }
    second_server = server_by_payload[b"second"]
    survivor_server = server_by_payload[b"survivor"]

    await first_client.close()
    await first_server.wait_closed(timeout=3)
    assert first_server.last_error is None
    assert [
        event["name"] for event in first_server._event_history
    ].count("closed") == 1

    second_client.abort("forced member abort")
    if second_client._close_task is not None:
        await second_client._close_task
    await second_server.wait_closed(timeout=3)
    assert isinstance(second_server.last_error, QuicStreamError)
    assert [
        event["name"] for event in second_server._event_history
    ].count("connection_error") == 1

    await survivor_client.send(b"still-alive")
    still_alive = await survivor_server.receive(
        min_incomplete_length=len(b"still-alive"),
        timeout=3,
    )
    assert still_alive.data == b"still-alive"
    assert survivor_client.state.name == "ESTABLISHED"
    assert survivor_server.state.name == "ESTABLISHED"

    await _close_connections(
        survivor_client,
        survivor_server,
        second_server,
        first_server,
    )
    await listener.stop()


@pytest.mark.asyncio
async def test_quic_reset_and_stop_sending_are_member_local():
    pytest.importorskip("aioquic")
    listener = await _start_quic_listener()
    anchor_client = await _connect_quic_stream(listener)
    await anchor_client.send(b"anchor")
    anchor_server = await listener.accept(timeout=3)
    await anchor_server.receive(min_incomplete_length=6, timeout=3)

    reset_client, stop_client, survivor_client = await asyncio.gather(
        anchor_client.clone(),
        anchor_client.clone(),
        anchor_client.clone(),
    )
    await asyncio.gather(
        reset_client.send(b"reset"),
        stop_client.send(b"stop"),
        survivor_client.send(b"survivor"),
    )
    accepted = await asyncio.gather(
        listener.accept(timeout=3),
        listener.accept(timeout=3),
        listener.accept(timeout=3),
    )
    received = await asyncio.gather(
        *(
            connection.receive(min_incomplete_length=4, timeout=3)
            for connection in accepted
        )
    )
    server_by_payload = {
        message.data: connection
        for message, connection in zip(received, accepted, strict=True)
    }
    reset_server = server_by_payload[b"reset"]
    stop_server = server_by_payload[b"stop"]
    survivor_server = server_by_payload[b"survivor"]

    protocol = anchor_client.quic_association.protocol
    protocol._quic.reset_stream(
        reset_client.transports[0].stream_id,
        error_code=0x51,
    )
    protocol._quic.stop_stream(
        stop_client.transports[0].stream_id,
        error_code=0x52,
    )
    protocol.transmit()

    await asyncio.gather(
        reset_server.wait_closed(timeout=3),
        stop_server.wait_closed(timeout=3),
    )
    assert isinstance(reset_server.last_error, QuicStreamError)
    assert reset_server.last_error.operation == "reset"
    assert reset_server.last_error.error_code == 0x51
    assert isinstance(stop_server.last_error, QuicStreamError)
    assert stop_server.last_error.operation == "stop-sending"
    assert stop_server.last_error.error_code == 0x52

    await survivor_client.send(b"survived-wire-errors")
    survived = await survivor_server.receive(
        min_incomplete_length=len(b"survived-wire-errors"),
        timeout=3,
    )
    assert survived.data == b"survived-wire-errors"

    await _close_connections(
        reset_client,
        stop_client,
        survivor_client,
        anchor_client,
        reset_server,
        stop_server,
        survivor_server,
        anchor_server,
    )
    await listener.stop()


@pytest.mark.asyncio
async def test_quic_association_error_fans_out_once_and_releases_resources():
    pytest.importorskip("aioquic")
    listener = await _start_quic_listener()
    client_stream = await _connect_quic_stream(listener)
    await client_stream.send(b"stream")
    server_stream = await listener.accept(timeout=3)
    await server_stream.receive(min_incomplete_length=6, timeout=3)

    client_clone = await client_stream.clone()
    await client_clone.send(b"clone")
    server_clone = await listener.accept(timeout=3)
    await server_clone.receive(min_incomplete_length=5, timeout=3)

    client_datagram = await client_stream.clone(
        connection_properties={
            "_pytaps.quicTransportMode": "Datagram",
        }
    )
    await client_datagram.send(b"datagram")
    server_datagram = await listener.accept(timeout=3)
    await server_datagram.receive(timeout=3)

    client_association = client_stream.quic_association
    server_association = server_stream.quic_association
    pending_receive = asyncio.create_task(server_clone.receive())
    await asyncio.sleep(0)
    await client_association.abort_association("forced association failure")
    with pytest.raises(QuicAssociationError):
        await pending_receive

    all_connections = (
        client_stream,
        client_clone,
        client_datagram,
        server_stream,
        server_clone,
        server_datagram,
    )
    await asyncio.gather(
        *(connection.wait_closed(timeout=3) for connection in all_connections)
    )
    await client_association.wait_idle()
    await server_association.wait_idle()

    for connection in all_connections:
        assert isinstance(connection.last_error, QuicAssociationError)
        assert [
            event["name"] for event in connection._event_history
        ].count("connection_error") == 1

    server_clone_events = [
        event["name"] for event in server_clone._event_history
    ]
    assert server_clone_events.index("receive_error") < (
        server_clone_events.index("connection_error")
    )

    for association in (client_association, server_association):
        snapshot = association.resource_snapshot()
        assert snapshot["streamTransports"] == 0
        assert snapshot["streamIds"] == []
        assert snapshot["hasDatagramTransport"] is False
        assert snapshot["pendingInboundDatagrams"] == 0
        assert snapshot["pendingOutboundDatagrams"] == 0
        assert snapshot["backgroundTasks"] == 0
        assert snapshot["transportStateWaiters"] == 0
        assert snapshot["terminated"] is True

    assert server_association not in (
        listener.quic_association.child_associations
    )
    await listener.stop()


@pytest.mark.asyncio
async def test_quic_stream_credit_blocks_unblocks_and_cancels_cleanly():
    pytest.importorskip("aioquic")
    listener = await _start_quic_listener()
    client = await _connect_quic_stream(listener)
    await client.send(b"anchor")
    server = await listener.accept(timeout=3)
    await server.receive(min_incomplete_length=6, timeout=3)

    association = client.quic_association
    quic = association.protocol._quic
    original_limit = quic._remote_max_streams_bidi
    baseline_ids = set(association.streams_by_id)

    next_stream_id = quic.get_next_available_stream_id(
        is_unidirectional=False
    )
    quic._remote_max_streams_bidi = next_stream_id // 4
    blocked_clone = asyncio.create_task(client.clone())
    await asyncio.sleep(0.05)
    assert blocked_clone.done() is False

    quic._remote_max_streams_bidi += 1
    association.transport_state_changed()
    unblocked_clone = await asyncio.wait_for(blocked_clone, timeout=3)
    assert unblocked_clone.transports[0].stream_id not in baseline_ids

    await unblocked_clone.send(b"unblocked")
    unblocked_server = await listener.accept(timeout=3)
    message = await unblocked_server.receive(
        min_incomplete_length=len(b"unblocked"),
        timeout=3,
    )
    assert message.data == b"unblocked"

    next_stream_id = quic.get_next_available_stream_id(
        is_unidirectional=False
    )
    quic._remote_max_streams_bidi = next_stream_id // 4
    group_size = len(client.connection_group)
    registered_ids = set(association.streams_by_id)
    cancelled_clone = asyncio.create_task(client.clone())
    await asyncio.sleep(0.05)
    assert cancelled_clone.done() is False
    cancelled_clone.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_clone

    assert len(client.connection_group) == group_size
    assert set(association.streams_by_id) == registered_ids
    assert association.resource_snapshot()["transportStateWaiters"] == 0

    quic._remote_max_streams_bidi = max(
        original_limit,
        next_stream_id // 4 + 1,
    )
    association.transport_state_changed()
    await _close_connections(
        unblocked_clone,
        client,
        unblocked_server,
        server,
    )
    await listener.stop()


@pytest.mark.asyncio
async def test_quic_stream_flow_control_applies_bounded_backpressure():
    pytest.importorskip("aioquic")
    listener = await _start_quic_listener()
    client = await _connect_quic_stream(listener)
    await client.send(b"anchor")
    server = await listener.accept(timeout=3)
    await server.receive(min_incomplete_length=6, timeout=3)
    await client.quic_association.protocol.ping()

    association = client.quic_association
    association.stream_write_buffer_high_water = 8 * 1024
    association.association_write_buffer_high_water = 16 * 1024
    payload = b"x" * (256 * 1024)
    receive_task = asyncio.create_task(
        server.receive(
            min_incomplete_length=len(payload),
            max_length=len(payload),
            timeout=5,
        )
    )

    await client.send(payload)
    await asyncio.wait_for(
        asyncio.shield(client._send_drain_waiter),
        timeout=5,
    )
    received = await receive_task

    assert received.data == payload
    assert (
        association.max_observed_stream_buffered_bytes
        <= association.stream_write_buffer_high_water
    )
    assert (
        association.max_observed_association_buffered_bytes
        <= association.association_write_buffer_high_water
    )

    await _close_connections(client, server)
    await listener.stop()


@pytest.mark.asyncio
async def test_quic_parallel_backpressured_streams_wake_every_sender():
    pytest.importorskip("aioquic")
    listener = await _start_quic_listener()
    anchor = await _connect_quic_stream(listener)
    await anchor.send(b"anchor")
    server_anchor = await listener.accept(timeout=3)
    await server_anchor.receive(min_incomplete_length=6, timeout=3)

    association = anchor.quic_association
    association.stream_write_buffer_high_water = 4 * 1024
    association.association_write_buffer_high_water = 8 * 1024
    clients = await asyncio.gather(*(anchor.clone() for _ in range(4)))
    accept_tasks = [listener.accept(timeout=5) for _ in clients]
    payloads = [
        bytes([ord("a") + index]) * (128 * 1024)
        for index in range(len(clients))
    ]

    await asyncio.gather(
        *(
            connection.send(payload)
            for connection, payload in zip(clients, payloads)
        )
    )
    servers = await asyncio.gather(*accept_tasks)
    receive_tasks = [
        asyncio.create_task(
            connection.receive(
                min_incomplete_length=len(payloads[0]),
                max_length=len(payloads[0]),
                timeout=10,
            )
        )
        for connection in servers
    ]
    await asyncio.wait_for(
        asyncio.gather(
            *(
                asyncio.shield(connection._send_drain_waiter)
                for connection in clients
            )
        ),
        timeout=10,
    )
    messages = await asyncio.gather(*receive_tasks)

    assert {message.data for message in messages} == set(payloads)
    assert association.resource_snapshot()["transportStateWaiters"] == 0

    await _close_connections(
        *clients,
        anchor,
        *servers,
        server_anchor,
    )
    await listener.stop()


@pytest.mark.asyncio
async def test_quic_datagram_limits_and_pending_queue_are_bounded():
    pytest.importorskip("aioquic")
    listener = await _start_quic_listener()
    client_stream = await _connect_quic_stream(listener)
    await client_stream.send(b"anchor")
    server_stream = await listener.accept(timeout=3)
    await server_stream.receive(min_incomplete_length=6, timeout=3)

    client_datagram = await client_stream.clone(
        connection_properties={
            "_pytaps.quicTransportMode": "Datagram",
        }
    )
    limit = client_datagram.get_property("sendMsgMaxLen")
    await client_datagram.send(b"d" * limit)
    server_datagram = await listener.accept(timeout=3)
    received = await server_datagram.receive(timeout=3)
    assert received.data == b"d" * limit

    send_error = asyncio.get_running_loop().create_future()

    async def handle_send_error(message_context, reason, connection):
        if not send_error.done():
            send_error.set_result((message_context, reason, connection))

    client_datagram.send_error = handle_send_error
    pending_before = client_datagram.quic_association.resource_snapshot()[
        "pendingOutboundDatagrams"
    ]
    await client_datagram.send(b"x" * (limit + 1))
    _, reason, failed_connection = await asyncio.wait_for(
        send_error,
        timeout=3,
    )
    assert failed_connection is client_datagram
    assert "sendMsgMaxLen" in str(reason)
    assert client_datagram.quic_association.resource_snapshot()[
        "pendingOutboundDatagrams"
    ] == pending_before

    pending_association = QuicAssociationManager(
        loop=asyncio.get_running_loop()
    )
    for index in range(QUIC_DATAGRAM_QUEUE_LIMIT + 20):
        pending_association.datagram_received(bytes([index % 256]))
    assert len(pending_association.pending_datagrams) == (
        QUIC_DATAGRAM_QUEUE_LIMIT
    )

    await _close_connections(
        client_datagram,
        client_stream,
        server_datagram,
        server_stream,
    )
    await listener.stop()


@pytest.mark.asyncio
async def test_quic_initiate_with_send_uses_accepted_zero_rtt():
    pytest.importorskip("aioquic")
    client_context = taps.ConnectionContext()
    listener = await _start_quic_listener()
    await _bootstrap_quic_ticket(listener, client_context)

    connection = await _connect_quic_stream(
        listener,
        connection_context=client_context,
        initiate_with_send=b"accepted-early-data",
        message_context=taps.MessageContext(
            safely_replayable=True,
        ),
    )
    server_connection = await listener.accept(timeout=3)
    received = await server_connection.receive(
        min_incomplete_length=1,
        timeout=3,
    )
    association = connection.quic_association

    assert received.data == b"accepted-early-data"
    assert received.get("isEarlyData") is True
    assert association.session_resumed is True
    assert association.early_data_attempted is True
    assert association.early_data_accepted is True
    assert association.early_data_rejected is False
    assert [
        event["name"]
        for event in connection.get_event_history()
    ].count("sent") == 1

    await _close_connections(connection, server_connection)
    await listener.stop()


@pytest.mark.asyncio
async def test_quic_unsafe_initiate_with_send_resumes_without_zero_rtt():
    pytest.importorskip("aioquic")
    client_context = taps.ConnectionContext()
    listener = await _start_quic_listener()
    await _bootstrap_quic_ticket(listener, client_context)

    connection = await _connect_quic_stream(
        listener,
        connection_context=client_context,
        initiate_with_send=b"send-after-handshake",
        message_context=taps.MessageContext(
            safely_replayable=False,
        ),
    )
    server_connection = await listener.accept(timeout=3)
    received = await server_connection.receive(
        min_incomplete_length=1,
        timeout=3,
    )
    association = connection.quic_association

    assert received.data == b"send-after-handshake"
    assert received.get("isEarlyData") is False
    assert association.session_resumed is True
    assert association.early_data_attempted is False
    assert association.early_data_accepted is False

    await _close_connections(connection, server_connection)
    await listener.stop()


@pytest.mark.asyncio
async def test_quic_rejected_zero_rtt_reconnects_and_sends_once():
    pytest.importorskip("aioquic")
    client_context = taps.ConnectionContext()
    listener = await _start_quic_listener()
    await _bootstrap_quic_ticket(listener, client_context)
    listener.connection_context.clear_quic_session_tickets(
        client=False,
        server=True,
    )

    connection = await _connect_quic_stream(
        listener,
        connection_context=client_context,
        initiate_with_send=b"replayed-after-rejection",
        message_context=taps.MessageContext(
            safely_replayable=True,
        ),
    )
    server_connection = await listener.accept(timeout=3)
    received = await server_connection.receive(
        min_incomplete_length=1,
        timeout=3,
    )
    association = connection.quic_association

    assert received.data == b"replayed-after-rejection"
    assert received.get("isEarlyData") is False
    assert association.session_resumed is False
    assert association.early_data_attempted is True
    assert association.early_data_accepted is False
    assert association.early_data_rejected is True
    assert listener._accepted_connections == []
    assert [
        event["name"]
        for event in connection.get_event_history()
    ].count("sent") == 1

    await _close_connections(connection, server_connection)
    await listener.stop()


@pytest.mark.asyncio
async def test_quic_datagram_initiate_with_send_uses_zero_rtt():
    pytest.importorskip("aioquic")
    client_context = taps.ConnectionContext()
    listener = await _start_quic_listener()
    await _bootstrap_quic_ticket(listener, client_context)
    properties = _quic_properties()
    properties.apply_profile("unreliable-datagram")
    properties.set_property(
        "_pytaps.quicTransportMode",
        "Datagram",
    )

    connection = await _connect_quic_stream(
        listener,
        connection_context=client_context,
        transport_properties=properties,
        initiate_with_send=b"early-datagram",
        message_context=taps.MessageContext(
            safely_replayable=True,
        ),
    )
    server_connection = await listener.accept(timeout=3)
    received = await server_connection.receive(timeout=3)
    association = connection.quic_association

    assert received.data == b"early-datagram"
    assert received.get("isEarlyData") is True
    assert association.session_resumed is True
    assert association.early_data_attempted is True
    assert association.early_data_accepted is True

    await _close_connections(connection, server_connection)
    await listener.stop()


@pytest.mark.asyncio
async def test_quic_rejected_zero_rtt_datagram_reconnects_and_sends_once():
    pytest.importorskip("aioquic")
    client_context = taps.ConnectionContext()
    listener = await _start_quic_listener()
    await _bootstrap_quic_ticket(listener, client_context)
    listener.connection_context.clear_quic_session_tickets(
        client=False,
        server=True,
    )
    properties = _quic_properties()
    properties.apply_profile("unreliable-datagram")
    properties.set_property(
        "_pytaps.quicTransportMode",
        "Datagram",
    )

    connection = await _connect_quic_stream(
        listener,
        connection_context=client_context,
        transport_properties=properties,
        initiate_with_send=b"datagram-after-rejection",
        message_context=taps.MessageContext(
            safely_replayable=True,
        ),
    )
    server_connection = await listener.accept(timeout=3)
    received = await server_connection.receive(timeout=3)
    association = connection.quic_association

    assert received.data == b"datagram-after-rejection"
    assert received.get("isEarlyData") is False
    assert association.session_resumed is False
    assert association.early_data_attempted is True
    assert association.early_data_accepted is False
    assert association.early_data_rejected is True
    assert listener._accepted_connections == []
    assert [
        event["name"]
        for event in connection.get_event_history()
    ].count("sent") == 1

    await _close_connections(connection, server_connection)
    await listener.stop()


async def _wait_for_path(connection, *, remote_port, timeout=3):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        path = connection.get_properties()["readOnly"]["currentPath"]
        if path["remote"] is not None and path["remote"][1] == remote_port:
            return path
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"Connection did not adopt remote port {remote_port}"
    )


@pytest.mark.asyncio
async def test_quic_validated_handover_updates_the_mixed_connection_group():
    pytest.importorskip("aioquic")
    listener = await _start_quic_listener()
    client_stream = await _connect_quic_stream(
        listener,
        transport_properties=_quic_properties(multipath="Active"),
    )
    await client_stream.send(b"stream-before-migration")
    server_stream = await listener.accept(timeout=3)
    stream_before = await server_stream.receive(
        min_incomplete_length=1,
        timeout=3,
    )
    assert stream_before.data == b"stream-before-migration"

    client_datagram = await client_stream.clone(
        connection_properties={
            "_pytaps.quicTransportMode": "Datagram",
        }
    )
    await client_datagram.send(b"before-migration")
    server_datagram = await listener.accept(timeout=3)
    before = await server_datagram.receive(timeout=3)
    assert before.data == b"before-migration"

    client_changes = []
    server_changes = []

    async def client_path_change(previous, current, connection):
        client_changes.append((previous, current, connection))

    async def server_path_change(previous, current, connection):
        server_changes.append((previous, current, connection))

    client_stream.on_path_change(client_path_change)
    client_datagram.on_path_change(client_path_change)
    server_stream.on_path_change(server_path_change)
    server_datagram.on_path_change(server_path_change)

    _live_performance, cached_before_migration = (
        await _wait_for_quic_performance(client_stream)
    )
    assert cached_before_migration["rttSamples"] >= 1
    old_path = client_stream.get_properties()["readOnly"][
        "currentPath"
    ]
    migrated_path = await client_stream.migrate_path(
        (
            taps.LocalEndpoint()
            .with_address("127.0.0.1")
            .with_interface("loopback-migration")
            .with_port(0)
        ),
        timeout=3,
    )
    new_port = migrated_path["local"][1]
    assert migrated_path["local"][0] == "127.0.0.1"
    assert new_port != old_path["local"][1]
    assert client_stream.local_endpoint.interface == "loopback-migration"
    assert client_datagram.local_endpoint.interface == "loopback-migration"

    server_path = await _wait_for_path(
        server_stream,
        remote_port=new_port,
    )
    await asyncio.sleep(0)

    assert server_path["remote"] == migrated_path["local"]
    assert len(client_changes) == 2
    assert len(server_changes) == 2
    assert {
        change[2] for change in client_changes
    } == {client_stream, client_datagram}
    assert {
        change[2] for change in server_changes
    } == {server_stream, server_datagram}
    assert (
        client_stream.connection_group.get_properties()[
            "currentPath"
        ]
        == migrated_path
    )
    assert (
        server_stream.connection_group.get_properties()[
            "currentPath"
        ]
        == server_path
    )
    assert client_stream.connection_group.path_change_count == 1
    assert server_stream.connection_group.path_change_count == 1

    client_snapshot = client_stream.quic_association.resource_snapshot()
    server_snapshot = server_stream.quic_association.resource_snapshot()
    read_only = client_stream.get_properties()["readOnly"]
    assert (
        read_only["propertySupport"]["connection"][
            "multipathPolicy"
        ]
        == "handover-only"
    )
    assert (
        read_only["propertyEffects"]["multipathPolicy"]
        == "enforced-handover"
    )
    assert client_snapshot["migrationInProgress"] is False
    assert client_snapshot["pathValidationSuccesses"] == 1
    assert server_snapshot["pathValidationSuccesses"] >= 1
    assert client_snapshot["currentPath"] == migrated_path
    assert client_snapshot["performance"]["rttAvailable"] is True
    cached_after_migration = (
        client_stream.connection_context.get_performance_metrics(
            migrated_path["local"],
            migrated_path["remote"],
            "quic",
            network_id="default",
        )
    )
    assert cached_after_migration["rttSamples"] >= 1
    assert any(
        path["active"] and path["validated"]
        for path in client_snapshot["networkPaths"]
    )

    await client_stream.send(b"stream-after-migration")
    stream_message = await server_stream.receive(
        min_incomplete_length=1,
        timeout=3,
    )
    assert stream_message.data == b"stream-after-migration"

    await client_datagram.send(b"datagram-after-migration")
    datagram_message = await server_datagram.receive(timeout=3)
    assert datagram_message.data == b"datagram-after-migration"

    await _close_connections(
        client_datagram,
        client_stream,
        server_datagram,
        server_stream,
    )
    await listener.stop()


@pytest.mark.asyncio
async def test_quic_repeated_validated_migrations_keep_stream_usable():
    pytest.importorskip("aioquic")
    listener = await _start_quic_listener()
    client = await _connect_quic_stream(
        listener,
        transport_properties=_quic_properties(multipath="Active"),
    )
    await client.send(b"before-repeated-migration")
    server = await listener.accept(timeout=3)
    await server.receive(min_incomplete_length=1, timeout=3)

    for index in range(3):
        old_port = client.quic_association.current_path["local"][1]
        path = await client.migrate_path(
            (
                taps.LocalEndpoint()
                .with_address("127.0.0.1")
                .with_port(0)
            ),
            timeout=3,
        )
        assert path["local"][1] != old_port
        await _wait_for_path(
            server,
            remote_port=path["local"][1],
        )

        payload = f"after-migration-{index}".encode()
        await client.send(payload)
        message = await server.receive(
            min_incomplete_length=1,
            timeout=3,
        )
        assert message.data == payload

    snapshot = client.quic_association.resource_snapshot()
    assert snapshot["pathValidationSuccesses"] == 3
    assert snapshot["pathValidationFailures"] == 0
    assert snapshot["pathChangeCount"] == 3
    assert len(client.quic_association._client_transports) == 1

    await _close_connections(client, server)
    await listener.stop()


@pytest.mark.asyncio
async def test_quic_migration_requires_active_multipath():
    pytest.importorskip("aioquic")
    listener = await _start_quic_listener()
    client = await _connect_quic_stream(listener)
    await client.send(b"before-policy-check")
    server = await listener.accept(timeout=3)
    await server.receive(min_incomplete_length=1, timeout=3)

    with pytest.raises(RuntimeError, match="multipath=Active"):
        await client.migrate_path(
            taps.LocalEndpoint().with_address("127.0.0.1"),
        )

    await client.send(b"still-connected")
    message = await server.receive(
        min_incomplete_length=1,
        timeout=3,
    )
    assert message.data == b"still-connected"

    await _close_connections(client, server)
    await listener.stop()


@pytest.mark.parametrize("policy", ["Interactive", "Aggregate"])
@pytest.mark.asyncio
async def test_quic_rejects_unsupported_concurrent_multipath_policy_mutation(
    policy,
):
    pytest.importorskip("aioquic")
    properties = _quic_properties(multipath="Active")
    listener = await _start_quic_listener()
    client = await _connect_quic_stream(
        listener,
        transport_properties=properties,
    )
    await client.send(b"before-policy-check")
    server = await listener.accept(timeout=3)
    await server.receive(min_incomplete_length=1, timeout=3)

    with pytest.raises(NotImplementedError, match="Handover"):
        client.set_property("multipathPolicy", policy)
    assert client.get_property("multipathPolicy") == "Handover"
    assert (
        client.get_properties()["readOnly"]["propertyEffects"][
            "multipathPolicy"
        ]
        == "enforced-handover"
    )
    await client.send(b"still-connected")
    message = await server.receive(
        min_incomplete_length=1,
        timeout=3,
    )
    assert message.data == b"still-connected"

    await _close_connections(client, server)
    await listener.stop()


@pytest.mark.asyncio
async def test_quic_failed_migration_path_validation_rolls_back(
    monkeypatch,
):
    pytest.importorskip("aioquic")
    listener = await _start_quic_listener()
    client = await _connect_quic_stream(
        listener,
        transport_properties=_quic_properties(multipath="Active"),
    )
    await client.send(b"before-rollback")
    server = await listener.accept(timeout=3)
    await server.receive(min_incomplete_length=1, timeout=3)
    association = client.quic_association
    old_path = association.current_path.copy()

    async def fail_validation(_timeout):
        raise TimeoutError("forced validation timeout")

    monkeypatch.setattr(
        association,
        "_await_migration_validation",
        fail_validation,
    )

    with pytest.raises(TimeoutError, match="forced validation timeout"):
        await client.migrate_path(
            (
                taps.LocalEndpoint()
                .with_address("127.0.0.1")
                .with_port(0)
            ),
            timeout=0.1,
        )

    assert association.current_path == old_path
    assert association.path_validation_successes == 0
    assert association.path_validation_failures == 1
    assert association.migration_in_progress is False

    await client.send(b"after-rollback")
    message = await server.receive(
        min_incomplete_length=1,
        timeout=3,
    )
    assert message.data == b"after-rollback"

    await _close_connections(client, server)
    await listener.stop()


@pytest.mark.asyncio
async def test_quic_client_socket_matches_the_remote_address_family():
    """A QUIC client must not bind the dual-stack wildcard.

    aioquic binds its client socket to the IPv6 wildcard as a dual-stack
    socket. Such a binding can share a port number with a socket bound to a
    specific IPv4 address, and the host then delivers datagrams to the more
    specific binding, so a local Listener silently receives the client's
    packets and the handshake never completes.
    """
    pytest.importorskip("aioquic")
    manager = QuicAssociationManager

    sock, family = manager._client_socket(None, "127.0.0.1")
    try:
        assert family is socket.AF_INET
        assert sock.getsockname()[0] == "0.0.0.0"
    finally:
        sock.close()

    sock, family = manager._client_socket(None, "::1")
    try:
        assert family is socket.AF_INET6
        assert sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 1
    finally:
        sock.close()


@pytest.mark.asyncio
async def test_quic_client_port_cannot_collide_with_a_local_listener():
    """The client binding must conflict with a Listener on the same port.

    This is the property that was violated: a dual-stack wildcard client could
    be assigned a port already bound by an IPv4 Listener, and the Listener then
    stole the server's replies.
    """
    pytest.importorskip("aioquic")
    listener = await _start_quic_listener()
    try:
        taken = listener.quic_association.bound_port()
        local = taps.LocalEndpoint().with_address("127.0.0.1").with_port(taken)

        with pytest.raises(OSError):
            sock, _family = (
                QuicAssociationManager._client_socket(
                    local,
                    "127.0.0.1",
                )
            )
            sock.close()
    finally:
        await listener.stop()


def test_quic_client_socket_honors_a_local_address_constraint():
    """RFC 9622 Section 6.1.2 constraints must reach the QUIC client socket.

    aioquic's own client only accepts a local port, so an address or interface
    constraint used to be dropped for QUIC alone.
    """
    pytest.importorskip("aioquic")
    local = taps.LocalEndpoint().with_address("127.0.0.1")

    sock, family = QuicAssociationManager._client_socket(local, "127.0.0.1")
    try:
        assert family is socket.AF_INET
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()

    with pytest.raises(ValueError, match="interface constraint"):
        QuicAssociationManager._client_socket(
            taps.LocalEndpoint().with_interface("lo0"),
            "127.0.0.1",
        )


@pytest.mark.asyncio
async def test_quic_client_establishes_with_a_local_address_constraint():
    """The constrained socket still completes a real handshake."""
    pytest.importorskip("aioquic")
    listener = await _start_quic_listener()
    try:
        port = listener.quic_association.bound_port()
        preconnection = taps.Preconnection(
            local_endpoint=taps.LocalEndpoint().with_address("127.0.0.1"),
            remote_endpoint=(
                taps.RemoteEndpoint()
                .with_hostname("localhost")
                .with_address("127.0.0.1")
                .with_port(port)
            ),
            transport_properties=_quic_properties(),
            security_parameters=_quic_security(server=False),
        )
        connection = await preconnection.initiate(timeout=3)
        await connection.send(b"constrained")
        server_connection = await listener.accept(timeout=3)
        message = await server_connection.receive(
            min_incomplete_length=1,
            timeout=3,
        )

        assert message.data == b"constrained"
        path = connection.get_properties()["readOnly"]["currentPath"]
        assert path["local"][0] == "127.0.0.1"

        await _close_connections(connection, server_connection)
    finally:
        await listener.stop()
