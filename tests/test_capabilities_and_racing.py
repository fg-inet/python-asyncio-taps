import asyncio
import socket

import pytest

import pytaps as taps
import pytaps.connection_context as connection_context_module
import pytaps.transports as transports_module
from pytaps.transportProperties import get_protocol_capabilities
from pytaps.transports import TcpTransport, UdpTransport
from pytaps.utility import (
    Candidate,
    CandidateBranch,
    build_protocol_candidates,
    order_candidates_for_racing,
)


def _remote(address, port, protocol=None):
    endpoint = taps.RemoteEndpoint().with_address(address).with_port(port)
    if protocol is not None:
        endpoint.with_protocol(protocol)
    return endpoint


def _candidate(protocol, address, port=443):
    remote = _remote(address, port, protocol)
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return Candidate(
        protocol=protocol,
        remote_address=address,
        address_family=family,
        remote_endpoint=remote,
    )


def _branches_for(candidates):
    return [
        CandidateBranch(
            protocol=candidate.protocol,
            path=candidate.path,
            local_endpoint=candidate.local_endpoint,
            remote_endpoint=candidate.remote_endpoint,
            branch_id=f"branch-{index}",
        )
        for index, candidate in enumerate(candidates)
    ]


def test_protocol_ranking_filters_backends_before_candidate_creation():
    properties = taps.TransportProperties()

    assert build_protocol_candidates(
        properties,
        available_protocols={"tcp", "udp"},
    ) == ["tcp"]


def test_performance_cache_is_bounded_averaged_expiring_and_isolated(
    monkeypatch,
):
    now = [100.0]
    monkeypatch.setattr(
        connection_context_module.time,
        "time",
        lambda: now[0],
    )
    context = taps.ConnectionContext()
    local_path = ("192.0.2.10", 50000)
    remote_path = ("198.51.100.10", 443)

    assert context.record_performance_observation(
        local_path,
        remote_path,
        "quic",
        network_id="wifi",
        rtt=0.1,
        rtt_variation=0.02,
        establishment_latency=0.4,
        success=True,
        capacity=2,
    )
    now[0] += 1
    assert context.record_performance_observation(
        local_path,
        remote_path,
        "quic",
        network_id="wifi",
        rtt=0.05,
        rtt_variation=0.01,
        success=False,
        capacity=2,
    )

    metrics = context.get_performance_metrics(
        local_path,
        remote_path,
        "quic",
        network_id="wifi",
    )
    assert metrics["latestRtt"] == pytest.approx(0.05)
    assert metrics["smoothedRtt"] == pytest.approx(0.0875)
    assert metrics["minimumRtt"] == pytest.approx(0.05)
    assert metrics["rttVariation"] == pytest.approx(0.0175)
    assert metrics["rttSamples"] == 2
    assert metrics["establishmentLatency"] == pytest.approx(0.4)
    assert metrics["successRate"] == pytest.approx(0.5)

    isolated = context.clone()
    assert isolated.get_performance_cache_snapshot() == []

    for index in range(2):
        context.record_performance_observation(
            None,
            (f"203.0.113.{index + 1}", 443),
            "tcp",
            network_id="cellular",
            rtt=0.2 + index,
            capacity=2,
        )
    assert {
        entry["remoteAddress"]
        for entry in context.get_performance_cache_snapshot()
    } == {"203.0.113.1", "203.0.113.2"}

    expiring = taps.ConnectionContext()
    expiring.record_performance_observation(
        local_path,
        remote_path,
        "quic",
        network_id="wifi",
        rtt=0.1,
        establishment_latency=0.2,
        success=True,
        rtt_lifetime=2,
        establishment_lifetime=3,
        success_lifetime=4,
    )
    now[0] += 2
    expired_rtt = expiring.get_performance_metrics(
        local_path,
        remote_path,
        "quic",
        network_id="wifi",
    )
    assert expired_rtt["smoothedRtt"] is None
    assert expired_rtt["establishmentLatency"] == pytest.approx(0.2)
    now[0] += 2
    assert expiring.get_performance_cache_snapshot() == []


@pytest.mark.asyncio
async def test_performance_history_orders_equivalent_endpoints_and_paths():
    context = taps.ConnectionContext()
    context.record_performance_observation(
        None,
        ("192.0.2.20", 443),
        "tcp",
        network_id="default",
        rtt=0.8,
        establishment_latency=0.8,
        success=True,
    )
    context.record_performance_observation(
        None,
        ("198.51.100.20", 443),
        "tcp",
        network_id="default",
        rtt=0.02,
        establishment_latency=0.05,
        success=True,
    )
    connection = taps.Connection(
        taps.Preconnection(
            remote_endpoint=_remote("192.0.2.20", 443),
            connection_context=context,
        )
    )
    candidates = [
        _candidate("tcp", "192.0.2.20"),
        _candidate("tcp", "198.51.100.20"),
    ]

    ordered = order_candidates_for_racing(connection, candidates)

    assert ordered[0].remote_address == "198.51.100.20"

    context.record_performance_observation(
        ("192.0.2.30", 0),
        ("203.0.113.30", 443),
        "tcp",
        network_id="cellular",
        rtt=0.7,
    )
    context.record_performance_observation(
        ("198.51.100.30", 0),
        ("203.0.113.30", 443),
        "tcp",
        network_id="wifi",
        rtt=0.03,
    )
    path_connection = taps.Connection(
        taps.Preconnection(
            local_endpoints=[
                (
                    taps.LocalEndpoint()
                    .with_address("192.0.2.30")
                    .with_interface("cellular")
                ),
                (
                    taps.LocalEndpoint()
                    .with_address("198.51.100.30")
                    .with_interface("wifi")
                ),
            ],
            remote_endpoint=_remote("203.0.113.30", 443, "tcp"),
            connection_context=context,
        )
    )

    branches = path_connection._candidate_branches_for_racing()

    assert branches[0].path == "wifi"
    connection._fail_initiate(RuntimeError("test cleanup"))
    path_connection._fail_initiate(RuntimeError("test cleanup"))


@pytest.mark.asyncio
async def test_ready_records_establishment_performance_once():
    context = taps.ConnectionContext()
    connection = taps.Connection(
        taps.Preconnection(
            remote_endpoint=_remote("198.51.100.40", 443, "tcp"),
            connection_context=context,
        )
    )
    connection.protocol = "tcp"
    connection.note_path_change(
        local_address="192.0.2.40",
        local_port=50000,
        remote_address="198.51.100.40",
        remote_port=443,
    )
    connection._establishment_started_at = connection.loop.time() - 0.25

    connection._mark_ready()

    metrics = context.get_performance_metrics(
        ("192.0.2.40", 50000),
        ("198.51.100.40", 443),
        "tcp",
        network_id="default",
    )
    assert metrics["establishmentLatency"] >= 0.25
    assert metrics["establishmentSamples"] == 1
    assert metrics["successRate"] == 1
    connection._record_establishment_performance()
    assert context.get_performance_metrics(
        ("192.0.2.40", 50000),
        ("198.51.100.40", 443),
        "tcp",
        network_id="default",
    )["establishmentSamples"] == 1
    connection._mark_closed()


@pytest.mark.parametrize(
    ("feature", "available_protocols"),
    [
        ("softErrorNotify", None),
        ("keepAlive", {"quic"}),
    ],
)
def test_unimplemented_capabilities_cannot_satisfy_require(
    feature,
    available_protocols,
):
    properties = taps.TransportProperties()
    properties.require(feature)

    assert build_protocol_candidates(
        properties,
        available_protocols=available_protocols,
    ) == []


def test_zero_rtt_requirement_selects_quic():
    properties = taps.TransportProperties()
    properties.require("zeroRttMsg")

    assert build_protocol_candidates(properties) == ["quic"]


def test_unimplemented_alternate_address_advertising_is_not_selected():
    properties = taps.TransportProperties()
    properties.set_property("advertisesAltaddr", True)

    assert build_protocol_candidates(properties) == []


def test_unimplemented_active_multipath_falls_back_to_non_multipath_protocols():
    properties = taps.TransportProperties()
    properties.set_property("multipath", "Active")

    assert build_protocol_candidates(properties) == [
        "quic",
        "tcp",
        "tls-tcp",
    ]


def test_property_names_are_case_insensitive_and_extensions_are_namespaced():
    properties = taps.TransportProperties()
    properties.set_property("CONNTIMEOUT", 2)
    properties.set_property("_PYTAPS.QUICSTREAMTYPE", "unidirectional")
    message = taps.MessageContext()
    message.set_property("MSGLIFETIME", 3)

    assert properties.get("conntimeout") == 2
    assert (
        properties.get("_pytaps.quicstreamtype")
        == "Unidirectional"
    )
    assert message.get("msglifetime") == 3
    with pytest.raises(KeyError, match="Unknown Transport Property"):
        properties.set_property("quic.streamType", "Bidirectional")


@pytest.mark.asyncio
async def test_quic_datagram_capabilities_and_property_support_are_truthful():
    properties = taps.TransportProperties()
    properties.set_property("_pytaps.quicTransportMode", "Datagram")
    connection = taps.Connection(
        taps.Preconnection(
            remote_endpoint=_remote("2001:db8::1", 443, "quic"),
            transport_properties=properties,
        )
    )
    connection.protocol = "quic"
    connection.state = taps.ConnectionState.ESTABLISHED

    read_only = connection.get_properties()["readOnly"]

    assert read_only["securityAvailable"] is True
    assert read_only["backendCapabilities"]["reliability"] is False
    assert read_only["backendCapabilities"]["preserveMsgBoundaries"] is True
    assert read_only["backendCapabilities"]["preserveOrder"] is False
    assert read_only["backendCapabilities"]["zeroRttMsg"] is True
    assert (
        read_only["propertySupport"]["connection"]["connTimeout"]
        == "not-applicable"
    )
    assert read_only["propertySupport"]["message"]["msgLifetime"] == "enforced"
    assert (
        read_only["propertySupport"]["message"]["safelyReplayable"]
        == "enforced-for-0rtt"
    )

    connection._mark_closed()


@pytest.mark.asyncio
async def test_protocol_specific_connection_property_rejects_wrong_protocol():
    connection = taps.Connection(
        taps.Preconnection(
            remote_endpoint=_remote("192.0.2.1", 443, "udp"),
        )
    )
    connection.protocol = "udp"
    connection.state = taps.ConnectionState.ESTABLISHED

    with pytest.raises(ValueError, match="TCP-specific"):
        connection.set_property("tcp.userTimeoutValue", 5)

    connection._mark_closed()


@pytest.mark.asyncio
async def test_tcp_connection_properties_apply_available_socket_options(
    monkeypatch,
):
    keep_idle_option = 0x7F01
    user_timeout_option = 0x7F02
    monkeypatch.setattr(
        transports_module.socket,
        "TCP_KEEPIDLE",
        keep_idle_option,
        raising=False,
    )
    monkeypatch.setattr(
        transports_module.socket,
        "TCP_USER_TIMEOUT",
        user_timeout_option,
        raising=False,
    )

    properties = taps.TransportProperties()
    properties.require("keepAlive")
    properties.set_property("keepAliveTimeout", 9)
    properties.set_property("connTimeout", 2)
    connection = taps.Connection(
        taps.Preconnection(
            remote_endpoint=_remote("192.0.2.2", 443, "tcp"),
            transport_properties=properties,
        )
    )
    connection.protocol = "tcp"

    class FakeSocket:
        def __init__(self):
            self.options = []

        def setsockopt(self, level, option, value):
            self.options.append((level, option, value))

    class FakeRawTransport:
        def __init__(self):
            self.socket = FakeSocket()

        def get_extra_info(self, name):
            return self.socket if name == "socket" else None

    transport = TcpTransport(
        connection,
        remote_endpoint=connection.remote_endpoint,
    )
    transport.transport = FakeRawTransport()

    connection._apply_connection_properties()

    assert (
        socket.SOL_SOCKET,
        socket.SO_KEEPALIVE,
        1,
    ) in transport.transport.socket.options
    assert (
        socket.IPPROTO_TCP,
        keep_idle_option,
        9,
    ) in transport.transport.socket.options
    assert (
        socket.IPPROTO_TCP,
        user_timeout_option,
        2000,
    ) in transport.transport.socket.options
    effects = connection.get_properties()["readOnly"]["propertyEffects"]
    assert effects["keepAlive"] == "applied:SO_KEEPALIVE"
    assert effects["keepAliveTimeout"] == "applied:TCP_KEEPIDLE"
    assert effects["connTimeout"] == "applied:TCP_USER_TIMEOUT"
    assert effects["tcp.userTimeoutValue"] == "unsupported:RFC5482"

    connection._fail_initiate(RuntimeError("test cleanup"))


@pytest.mark.asyncio
async def test_resolution_cache_is_scoped_to_each_path(monkeypatch):
    context = taps.ConnectionContext()
    remote = taps.RemoteEndpoint().with_hostname("example.test").with_port(443)
    remote.with_protocol("tcp")
    local_endpoints = [
        (
            taps.LocalEndpoint()
            .with_address("192.0.2.10")
            .with_interface("wifi")
        ),
        (
            taps.LocalEndpoint()
            .with_address("198.51.100.10")
            .with_interface("cellular")
        ),
    ]
    preconnection = taps.Preconnection(
        local_endpoints=local_endpoints,
        remote_endpoint=remote,
        connection_context=context,
    )
    connection = taps.Connection(preconnection)
    calls = []

    async def fake_getaddrinfo(host, port, *, family, type):
        calls.append((host, port, family, type))
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("203.0.113.20", port),
            )
        ]

    monkeypatch.setattr(connection.loop, "getaddrinfo", fake_getaddrinfo)

    candidates = await connection._gather_candidate_leaves()

    assert {candidate.path for candidate in candidates} == {"wifi", "cellular"}
    assert {candidate.resolution_source for candidate in candidates} == {"dns"}
    assert len(calls) == 2
    cache = context.get_snapshot()["resolutionCache"]
    assert {entry["path"] for entry in cache} == {"wifi", "cellular"}

    cached_connection = taps.Connection(preconnection)
    monkeypatch.setattr(
        cached_connection.loop,
        "getaddrinfo",
        fake_getaddrinfo,
    )
    cached_candidates = await cached_connection._gather_candidate_leaves()

    assert len(calls) == 2
    assert {
        candidate.resolution_source for candidate in cached_candidates
    } == {"cache"}

    connection._fail_initiate(RuntimeError("test cleanup"))
    cached_connection._fail_initiate(RuntimeError("test cleanup"))


@pytest.mark.asyncio
async def test_candidate_resolution_cancellation_reaps_branch_tasks(monkeypatch):
    remote = (
        taps.RemoteEndpoint()
        .with_hostname("slow.example")
        .with_port(443)
        .with_protocol("tcp")
    )
    connection = taps.Connection(taps.Preconnection(remote_endpoint=remote))
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_getaddrinfo(host, port, *, family, type):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(
        connection.loop,
        "getaddrinfo",
        blocked_getaddrinfo,
    )

    resolution = asyncio.create_task(connection._gather_candidate_leaves())
    await asyncio.wait_for(started.wait(), 1)
    resolution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await resolution

    assert cancelled.is_set()
    assert connection._resolution_tasks == []
    connection._fail_initiate(RuntimeError("test cleanup"))


@pytest.mark.asyncio
async def test_failed_datagram_candidate_falls_back_to_stream(monkeypatch):
    connection = taps.Connection(
        taps.Preconnection(
            remote_endpoint=_remote("192.0.2.30", 443),
        )
    )
    candidates = [
        _candidate("udp", "192.0.2.30"),
        _candidate("tcp", "192.0.2.30"),
    ]
    attempts = []
    branches = _branches_for(candidates)

    async def resolve_branch(branch):
        index = int(branch.branch_id.rsplit("-", 1)[1])
        return [candidates[index]]

    async def open_candidate(candidate):
        attempts.append(candidate.protocol)
        if candidate.protocol == "udp":
            raise OSError("UDP path failed")
        connection.protocol = candidate.protocol
        connection.remote_endpoint = candidate.remote_endpoint.clone()
        connection._protocol_capabilities = get_protocol_capabilities(
            candidate.protocol,
            connection.transport_properties,
        )
        connection._mark_ready()

    monkeypatch.setattr(
        connection,
        "_candidate_branches_for_racing",
        lambda: branches,
    )
    monkeypatch.setattr(
        connection,
        "_resolve_candidate_branch",
        resolve_branch,
    )
    monkeypatch.setattr(
        connection,
        "_candidate_racing_delay",
        lambda candidate: 0,
    )
    monkeypatch.setattr(connection, "_open_candidate", open_candidate)

    await connection.race()
    await asyncio.sleep(0)

    assert attempts == ["udp", "tcp"]
    assert connection.state is taps.ConnectionState.ESTABLISHED
    assert connection.protocol == "tcp"
    assert connection.last_error is None
    assert connection.pending == []

    connection._mark_closed()


@pytest.mark.asyncio
async def test_successful_candidate_cancels_delayed_siblings(monkeypatch):
    connection = taps.Connection(
        taps.Preconnection(
            remote_endpoint=_remote("192.0.2.40", 443),
        )
    )
    candidates = [
        _candidate("tcp", "192.0.2.40"),
        _candidate("udp", "192.0.2.40"),
    ]
    attempts = []
    branches = _branches_for(candidates)

    async def resolve_branch(branch):
        index = int(branch.branch_id.rsplit("-", 1)[1])
        return [candidates[index]]

    async def open_candidate(candidate):
        attempts.append(candidate.protocol)
        connection.protocol = candidate.protocol
        connection.remote_endpoint = candidate.remote_endpoint.clone()
        connection._protocol_capabilities = get_protocol_capabilities(
            candidate.protocol,
            connection.transport_properties,
        )
        connection._mark_ready()

    monkeypatch.setattr(
        connection,
        "_candidate_branches_for_racing",
        lambda: branches,
    )
    monkeypatch.setattr(
        connection,
        "_resolve_candidate_branch",
        resolve_branch,
    )
    monkeypatch.setattr(
        connection,
        "_candidate_racing_delay",
        lambda candidate: 60,
    )
    monkeypatch.setattr(connection, "_open_candidate", open_candidate)

    await connection.race()
    await asyncio.sleep(0)

    assert attempts == ["tcp"]
    assert connection.pending == []
    connection._mark_closed()


@pytest.mark.asyncio
async def test_resolved_branch_can_win_while_sibling_resolution_is_blocked(
    monkeypatch,
):
    connection = taps.Connection(
        taps.Preconnection(
            remote_endpoint=_remote("192.0.2.45", 443),
        )
    )
    candidates = [
        _candidate("tcp", "192.0.2.45"),
        _candidate("tcp", "198.51.100.45"),
    ]
    branches = _branches_for(candidates)
    slow_started = asyncio.Event()
    slow_cancelled = asyncio.Event()

    async def resolve_branch(branch):
        if branch.branch_id == "branch-0":
            slow_started.set()
            try:
                await asyncio.Future()
            finally:
                slow_cancelled.set()
        return [candidates[1]]

    async def open_candidate(candidate):
        connection.protocol = candidate.protocol
        connection.remote_endpoint = candidate.remote_endpoint.clone()
        connection._protocol_capabilities = get_protocol_capabilities(
            candidate.protocol,
            connection.transport_properties,
        )
        connection._mark_ready()

    monkeypatch.setattr(
        connection,
        "_candidate_branches_for_racing",
        lambda: branches,
    )
    monkeypatch.setattr(
        connection,
        "_resolve_candidate_branch",
        resolve_branch,
    )
    monkeypatch.setattr(
        connection,
        "_candidate_racing_delay",
        lambda candidate: 0,
    )
    monkeypatch.setattr(connection, "_open_candidate", open_candidate)

    await asyncio.wait_for(connection.race(), 1)

    assert slow_started.is_set()
    assert slow_cancelled.is_set()
    assert connection.remote_endpoint.address == "198.51.100.45"
    assert connection._resolution_tasks == []
    connection._mark_closed()


@pytest.mark.asyncio
async def test_late_transport_cannot_overwrite_candidate_winner():
    connection = taps.Connection(
        taps.Preconnection(
            remote_endpoint=_remote("192.0.2.50", 443),
        )
    )

    class FakeRawTransport:
        def __init__(self, sockname):
            self.sockname = sockname
            self.closed = False

        def get_extra_info(self, name):
            if name == "sockname":
                return self.sockname
            return None

        def close(self):
            self.closed = True

    udp_remote = _remote("192.0.2.50", 443, "udp")
    tcp_remote = _remote("198.51.100.50", 443, "tcp")
    udp = UdpTransport(connection, remote_endpoint=udp_remote)
    tcp = TcpTransport(connection, remote_endpoint=tcp_remote)
    udp.transport = FakeRawTransport(("192.0.2.60", 50000))
    tcp.transport = FakeRawTransport(("198.51.100.60", 50001))

    assert await udp._activate_candidate() is True
    connection._mark_ready()
    assert await tcp._activate_candidate() is False

    assert connection.protocol == "udp"
    assert connection.local_endpoint.address == "192.0.2.60"
    assert connection.remote_endpoint.address == "192.0.2.50"
    assert connection.transports[0] is udp
    assert tcp.transport.closed is True

    connection._mark_closed()
