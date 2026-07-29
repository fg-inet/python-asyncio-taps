import asyncio
import socket
import types

import pytest

import pytaps as taps
import pytaps.listener as listener_module
import pytaps.system_policy as system_policy_module
from pytaps.transports import QuicAssociationManager


def _interface_policy(address, *, available=True):
    return {
        "available": available,
        "addresses": [
            {
                "address": address,
                "family": "ipv6" if ":" in address else "ipv4",
                "isLoopback": False,
                "isLinkLocal": False,
            }
        ] if available else [],
        "relativeCost": "normal",
    }


def test_system_policy_snapshot_is_atomic_and_suppresses_unchanged_updates():
    context = taps.ConnectionContext()
    updates = []
    context.subscribe(updates.append)
    snapshot = taps.SystemPolicySnapshot(
        source="test-policy",
        interfaces={
            "wifi": _interface_policy("192.0.2.10"),
        },
        protocols={
            "quic": {
                "available": True,
                "preferenceAdjustment": 2,
            },
        },
        pvds={
            "home": {
                "available": True,
                "preferenceAdjustment": 1,
            },
        },
        address_families={"ipv4": 3, "ipv6": -1},
        observed_at=100.0,
    )

    assert context.apply_system_policy(snapshot) is True
    assert context.apply_system_policy(snapshot) is False

    policy = context.get_snapshot()["systemPolicy"]
    assert policy["source"] == "test-policy"
    assert policy["generation"] == 1
    assert policy["lastUpdated"] == 100.0
    assert policy["interfaces"]["wifi"]["available"] is True
    assert policy["protocols"]["quic"]["preferenceAdjustment"] == 2
    assert policy["pvds"]["home"]["preferenceAdjustment"] == 1
    assert policy["addressFamilies"] == {"ipv4": 3, "ipv6": -1}
    assert [update["trigger"] for update in updates] == [
        "system_policy_updated",
    ]


def test_supplied_policy_sections_withdraw_omitted_entries():
    context = taps.ConnectionContext()
    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            protocols={"quic": {"available": True}},
            pvds={"home": {"available": True}},
            address_families={"ipv4": 2, "ipv6": 3},
        )
    )

    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            protocols={},
            pvds={},
            address_families={"ipv4": 1},
        )
    )

    assert context.protocol_policy["quic"]["available"] is False
    assert context.pvd_policy["home"]["available"] is False
    assert context.address_family_policy == {"ipv4": 1, "ipv6": 0}


def test_invalid_system_policy_snapshot_does_not_partially_apply():
    context = taps.ConnectionContext()
    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "wifi": _interface_policy("192.0.2.10"),
            },
        )
    )
    before = context.get_snapshot()["systemPolicy"]

    with pytest.raises(KeyError, match="satnet"):
        context.apply_system_policy(
            {
                "interfaces": {
                    "cell": _interface_policy("198.51.100.20"),
                },
                "addressFamilies": {"satnet": 5},
            }
        )

    assert context.get_snapshot()["systemPolicy"] == before


def test_system_policy_addresses_become_future_candidate_paths():
    loop = asyncio.new_event_loop()
    context = taps.ConnectionContext()
    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "wifi": _interface_policy("192.0.2.10"),
                "cell": _interface_policy(
                    "198.51.100.20",
                    available=False,
                ),
            },
        )
    )
    preconnection = taps.Preconnection(
        remote_endpoint=(
            taps.RemoteEndpoint()
            .with_address("203.0.113.10")
            .with_port(443)
        ),
        connection_context=context,
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)

    first_paths = connection._expanded_local_endpoints_for_racing()
    assert [
        (endpoint.interface, endpoint.address)
        for endpoint in first_paths
    ] == [("wifi", "192.0.2.10")]

    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "cell": _interface_policy("198.51.100.20"),
            },
        )
    )
    loop.run_until_complete(asyncio.sleep(0))
    second_paths = connection._expanded_local_endpoints_for_racing()
    connection.close()
    loop.run_until_complete(connection.wait_closed(timeout=1))
    loop.close()

    assert [
        (endpoint.interface, endpoint.address)
        for endpoint in second_paths
    ] == [("cell", "198.51.100.20")]


def test_active_connection_is_advised_but_not_closed_when_interface_disappears():
    loop = asyncio.new_event_loop()
    context = taps.ConnectionContext()
    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "wifi": _interface_policy("192.0.2.10"),
            },
        )
    )
    local = (
        taps.LocalEndpoint()
        .with_interface("wifi")
        .with_address("192.0.2.10")
    )
    remote = (
        taps.RemoteEndpoint()
        .with_address("203.0.113.10")
        .with_port(443)
    )
    connection = taps.Connection(
        taps.Preconnection(
            local_endpoint=local,
            remote_endpoint=remote,
            connection_context=context,
            event_loop=loop,
        )
    )
    connection.protocol = "tcp"
    connection._mark_ready()
    connection.note_path_change(
        local_address="192.0.2.10",
        local_port=54321,
        remote_address="203.0.113.10",
        remote_port=443,
    )

    context.apply_system_policy(
        taps.SystemPolicySnapshot(interfaces={})
    )
    loop.run_until_complete(asyncio.sleep(0))

    event_names = [
        event["name"]
        for event in connection.get_event_history()
    ]
    assert connection.state is taps.ConnectionState.ESTABLISHED
    assert "system_policy_changed" in event_names
    assert "soft_error" in event_names
    assert "connection_error" not in event_names
    assert context.get_path_advisory(
        ("192.0.2.10", 54321),
        ("203.0.113.10", 443),
    ) is not None

    connection.close()
    loop.run_until_complete(connection.wait_closed(timeout=1))
    loop.close()


@pytest.mark.asyncio
async def test_system_policy_monitor_refreshes_and_stops_cleanly():
    class SequenceProvider(taps.SystemPolicyProvider):
        source = "sequence"

        def __init__(self):
            self.calls = 0

        def snapshot(self):
            self.calls += 1
            interface = "wifi" if self.calls == 1 else "cell"
            address = (
                "192.0.2.10"
                if interface == "wifi"
                else "198.51.100.20"
            )
            return taps.SystemPolicySnapshot(
                source=self.source,
                interfaces={
                    interface: _interface_policy(address),
                },
            )

    context = taps.ConnectionContext()
    provider = SequenceProvider()
    monitor = taps.SystemPolicyMonitor(
        context,
        provider,
        interval=0.01,
    )
    monitor.start()

    while context.system_policy_generation < 2:
        await asyncio.sleep(0.01)
    await monitor.stop()

    assert monitor.running is False
    assert provider.calls >= 2
    assert context.interface_policy["wifi"]["available"] is False
    assert context.interface_policy["cell"]["available"] is True


@pytest.mark.asyncio
async def test_system_policy_monitor_coalesces_push_events():
    class QueueEventSource(taps.SystemPolicyEventSource):
        name = "test-route-events"

        def __init__(self):
            self.queue = asyncio.Queue()
            self.drained = 0
            self.closed = False

        async def wait_for_change(self):
            return await self.queue.get()

        def drain(self):
            while True:
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    return self.drained
                self.drained += 1

        def close(self):
            self.closed = True

    class EventProvider(taps.SystemPolicyProvider):
        source = "event-provider"

        def __init__(self, event_source):
            self.event_source = event_source
            self.calls = 0
            self.loop = None

        def create_event_source(self, *, loop=None):
            self.loop = loop
            return self.event_source

        def snapshot(self):
            self.calls += 1
            return taps.SystemPolicySnapshot(
                source=self.source,
                interfaces={
                    "wifi": _interface_policy(
                        f"192.0.2.{self.calls}"
                    ),
                },
            )

    async def wait_for_calls(provider, expected):
        while provider.calls < expected:
            await asyncio.sleep(0)

    context = taps.ConnectionContext()
    event_source = QueueEventSource()
    provider = EventProvider(event_source)
    monitor = taps.SystemPolicyMonitor(
        context,
        provider,
        interval=60,
        debounce_interval=0.01,
    )
    monitor.start()
    await asyncio.wait_for(wait_for_calls(provider, 1), timeout=1)

    event_source.queue.put_nowait({"change": "address"})
    event_source.queue.put_nowait({"change": "route"})
    event_source.queue.put_nowait({"change": "link"})
    await asyncio.wait_for(wait_for_calls(provider, 2), timeout=1)
    await asyncio.sleep(0.02)

    assert provider.calls == 2
    assert provider.loop is asyncio.get_running_loop()
    assert event_source.drained == 2
    assert monitor.event_driven is True
    assert monitor.event_source_name == "test-route-events"
    assert monitor.last_event == {"change": "address"}
    assert monitor.last_trigger == "event"
    assert monitor.refresh_count == 2

    await monitor.stop()

    assert event_source.closed is True
    assert monitor.event_driven is False


@pytest.mark.asyncio
async def test_system_policy_monitor_falls_back_after_event_source_error():
    class FailingEventSource(taps.SystemPolicyEventSource):
        name = "failing-route-events"

        def __init__(self):
            self.closed = False

        async def wait_for_change(self):
            raise OSError("route socket failed")

        def close(self):
            self.closed = True

    class FallbackProvider(taps.SystemPolicyProvider):
        source = "fallback-provider"

        def __init__(self, event_source):
            self.event_source = event_source
            self.calls = 0

        def create_event_source(self, *, loop=None):
            return self.event_source

        def snapshot(self):
            self.calls += 1
            return taps.SystemPolicySnapshot(
                source=self.source,
                address_families={"ipv4": self.calls},
            )

    async def wait_for_calls(provider, expected):
        while provider.calls < expected:
            await asyncio.sleep(0)

    updates = []
    context = taps.ConnectionContext()
    context.subscribe(updates.append)
    event_source = FailingEventSource()
    provider = FallbackProvider(event_source)
    monitor = taps.SystemPolicyMonitor(
        context,
        provider,
        interval=0.01,
        debounce_interval=0,
    )
    monitor.start()
    await asyncio.wait_for(wait_for_calls(provider, 2), timeout=1)
    await monitor.stop()

    assert event_source.closed is True
    assert monitor.event_driven is False
    assert monitor.last_trigger == "poll"
    assert isinstance(monitor.last_event_source_error, OSError)
    assert any(
        update["trigger"] == "system_policy_event_source_error"
        and update["details"]["source"] == "failing-route-events"
        and update["details"]["error"] == "route socket failed"
        for update in updates
    )


@pytest.mark.asyncio
async def test_system_policy_monitor_falls_back_after_event_source_setup_error():
    class SetupFailingProvider(taps.SystemPolicyProvider):
        source = "setup-failing-provider"

        def __init__(self):
            self.calls = 0

        def create_event_source(self, *, loop=None):
            raise PermissionError("route notifications unavailable")

        def snapshot(self):
            self.calls += 1
            return taps.SystemPolicySnapshot(
                source=self.source,
                address_families={"ipv4": self.calls},
            )

    async def wait_for_calls(provider, expected):
        while provider.calls < expected:
            await asyncio.sleep(0)

    updates = []
    context = taps.ConnectionContext()
    context.subscribe(updates.append)
    provider = SetupFailingProvider()
    monitor = taps.SystemPolicyMonitor(
        context,
        provider,
        interval=0.01,
    )
    monitor.start()
    await asyncio.wait_for(wait_for_calls(provider, 2), timeout=1)
    await monitor.stop()

    assert monitor.event_driven is False
    assert monitor.last_trigger == "poll"
    assert isinstance(monitor.last_event_source_error, PermissionError)
    assert any(
        update["trigger"] == "system_policy_event_source_error"
        and update["details"]["source"] == "setup-failing-provider"
        and update["details"]["error"] == "route notifications unavailable"
        for update in updates
    )


def test_native_route_event_source_opens_linux_netlink_socket(monkeypatch):
    class FakeSocket:
        def __init__(self, *args):
            self.args = args
            self.bound_to = None
            self.blocking = None
            self.closed = False

        def bind(self, address):
            self.bound_to = address

        def setblocking(self, blocking):
            self.blocking = blocking

        def close(self):
            self.closed = True

    created = []

    def create_socket(*args):
        event_socket = FakeSocket(*args)
        created.append(event_socket)
        return event_socket

    monkeypatch.setattr(system_policy_module.sys, "platform", "linux")
    monkeypatch.setattr(
        system_policy_module.socket,
        "AF_NETLINK",
        123,
        raising=False,
    )
    monkeypatch.setattr(
        system_policy_module.socket,
        "NETLINK_ROUTE",
        456,
        raising=False,
    )
    monkeypatch.setattr(
        system_policy_module.socket,
        "socket",
        create_socket,
    )

    loop = object()
    event_source = taps.NativeRouteEventSource.open(loop=loop)

    assert created[0].args == (123, socket.SOCK_RAW, 456)
    assert created[0].bound_to == (
        0,
        taps.NativeRouteEventSource._LINUX_ROUTE_GROUPS,
    )
    assert created[0].blocking is False
    assert event_source.name == "linux-netlink-route"
    assert event_source.loop is loop

    event_source.close()

    assert created[0].closed is True


def test_native_route_event_source_opens_bsd_routing_socket(monkeypatch):
    class FakeSocket:
        def __init__(self, *args):
            self.args = args
            self.blocking = None
            self.closed = False

        def setblocking(self, blocking):
            self.blocking = blocking

        def close(self):
            self.closed = True

    created = []

    def create_socket(*args):
        event_socket = FakeSocket(*args)
        created.append(event_socket)
        return event_socket

    monkeypatch.setattr(system_policy_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        system_policy_module.socket,
        "PF_ROUTE",
        321,
        raising=False,
    )
    monkeypatch.setattr(
        system_policy_module.socket,
        "AF_UNSPEC",
        654,
    )
    monkeypatch.setattr(
        system_policy_module.socket,
        "socket",
        create_socket,
    )

    event_source = taps.NativeRouteEventSource.open()

    assert created[0].args == (321, socket.SOCK_RAW, 654)
    assert created[0].blocking is False
    assert event_source.name == "bsd-routing-socket"

    event_source.close()

    assert created[0].closed is True


@pytest.mark.asyncio
async def test_darwin_path_events_apply_authoritative_cost_policy(
    monkeypatch,
):
    class FakeNetwork:
        nw_interface_type_wifi = 1
        nw_interface_type_cellular = 2
        nw_interface_type_wired = 3
        nw_interface_type_loopback = 4
        nw_interface_type_other = 5
        nw_path_status_satisfied = 10
        nw_path_status_unsatisfied = 11
        nw_path_status_satisfiable = 12
        nw_path_status_invalid = 13

        def __init__(self):
            self.monitor = None
            self.cancelled = []

        def nw_path_monitor_create(self):
            self.monitor = types.SimpleNamespace(handler=None)
            return self.monitor

        @staticmethod
        def nw_path_monitor_create_with_type(_interface_type):
            return None

        @staticmethod
        def nw_path_monitor_set_update_handler(monitor, handler):
            monitor.handler = handler

        @staticmethod
        def nw_path_monitor_set_queue(monitor, queue):
            monitor.queue = queue

        @staticmethod
        def nw_path_monitor_start(monitor):
            monitor.handler(object())

        def nw_path_monitor_cancel(self, monitor):
            self.cancelled.append(monitor)

        @staticmethod
        def nw_path_enumerate_interfaces(_path, callback):
            interface = types.SimpleNamespace(name=b"en9", kind=1)
            assert callback(interface) is True
            assert callback(interface) is True

        @staticmethod
        def nw_interface_get_name(interface):
            return interface.name

        @staticmethod
        def nw_interface_get_type(interface):
            return interface.kind

        @staticmethod
        def nw_path_get_status(_path):
            return 10

        @staticmethod
        def nw_path_is_expensive(_path):
            return True

        @staticmethod
        def nw_path_is_constrained(_path):
            return True

        @staticmethod
        def nw_path_is_ultra_constrained(_path):
            return False

        @staticmethod
        def nw_path_has_ipv4(_path):
            return True

        @staticmethod
        def nw_path_has_ipv6(_path):
            return False

        @staticmethod
        def nw_path_has_dns(_path):
            return True

    monkeypatch.setattr(system_policy_module.sys, "platform", "darwin")
    resolver = taps.DarwinNetworkPolicyResolver()
    network = FakeNetwork()
    event_source = taps.DarwinNetworkPathEventSource.open(
        resolver,
        network_module=network,
        objc_module=object(),
        queue_factory=object,
    )

    event = await asyncio.wait_for(
        event_source.wait_for_change(),
        timeout=1,
    )
    policy = resolver.snapshot()["en9"]

    assert event == {
        "source": "darwin-network-path",
        "pathMonitor": "default",
        "interfaces": ["en9"],
    }
    assert policy == {
        "pathStatus": "satisfied",
        "expensive": True,
        "metered": True,
        "constrained": True,
        "ultraConstrained": False,
        "supportsIPv4": True,
        "supportsIPv6": False,
        "hasDNS": True,
        "isDefaultPath": True,
        "interfaceType": "wifi",
        "policySource": "darwin-network-framework",
        "relativeCost": "high",
    }

    event_source.close()

    assert network.cancelled == [network.monitor]
    assert resolver.snapshot() == {}


@pytest.mark.asyncio
async def test_darwin_callback_failure_becomes_event_source_error(
    monkeypatch,
):
    class FailingNetwork:
        nw_path_status_satisfied = 1

        def __init__(self):
            self.monitor = types.SimpleNamespace(handler=None)

        def nw_path_monitor_create(self):
            return self.monitor

        @staticmethod
        def nw_path_monitor_set_update_handler(monitor, handler):
            monitor.handler = handler

        @staticmethod
        def nw_path_monitor_set_queue(_monitor, _queue):
            return None

        @staticmethod
        def nw_path_monitor_start(monitor):
            monitor.handler(object())

        @staticmethod
        def nw_path_monitor_cancel(_monitor):
            return None

        @staticmethod
        def nw_path_enumerate_interfaces(_path, _callback):
            raise KeyError("native symbol unavailable")

        @staticmethod
        def nw_interface_get_name(_interface):
            return b"en0"

        @staticmethod
        def nw_interface_get_type(_interface):
            return 1

        @staticmethod
        def nw_path_get_status(_path):
            return 1

    monkeypatch.setattr(system_policy_module.sys, "platform", "darwin")
    resolver = taps.DarwinNetworkPolicyResolver()
    event_source = taps.DarwinNetworkPathEventSource.open(
        resolver,
        network_module=FailingNetwork(),
        objc_module=object(),
        queue_factory=object,
    )

    with pytest.raises(
        RuntimeError,
        match="Apple network path callback failed.*native symbol",
    ):
        await asyncio.wait_for(
            event_source.wait_for_change(),
            timeout=1,
        )

    event_source.close()


@pytest.mark.asyncio
async def test_darwin_interface_callback_failure_becomes_event_source_error(
    monkeypatch,
):
    class FailingNetwork:
        nw_path_status_satisfied = 1

        def __init__(self):
            self.monitor = types.SimpleNamespace(handler=None)

        def nw_path_monitor_create(self):
            return self.monitor

        @staticmethod
        def nw_path_monitor_set_update_handler(monitor, handler):
            monitor.handler = handler

        @staticmethod
        def nw_path_monitor_set_queue(_monitor, _queue):
            return None

        @staticmethod
        def nw_path_monitor_start(monitor):
            monitor.handler(object())

        @staticmethod
        def nw_path_monitor_cancel(_monitor):
            return None

        @staticmethod
        def nw_path_enumerate_interfaces(_path, callback):
            callback(object())

        @staticmethod
        def nw_interface_get_name(_interface):
            raise KeyError("lazy interface symbol unavailable")

        @staticmethod
        def nw_interface_get_type(_interface):
            return 1

        @staticmethod
        def nw_path_get_status(_path):
            return 1

    monkeypatch.setattr(system_policy_module.sys, "platform", "darwin")
    resolver = taps.DarwinNetworkPolicyResolver()
    event_source = taps.DarwinNetworkPathEventSource.open(
        resolver,
        network_module=FailingNetwork(),
        objc_module=object(),
        queue_factory=object,
    )

    with pytest.raises(
        RuntimeError,
        match="Apple network path callback failed.*lazy interface symbol",
    ):
        await asyncio.wait_for(
            event_source.wait_for_change(),
            timeout=1,
        )

    event_source.close()


def test_networkmanager_policy_resolver_parses_metering_and_state():
    calls = []
    output = "\n".join(
        (
            "GENERAL.DEVICE:wwan0",
            "GENERAL.TYPE:gsm",
            "GENERAL.STATE:100 (connected)",
            "GENERAL.NM-MANAGED:yes",
            "GENERAL.METERED:yes (guessed)",
            "GENERAL.DEVICE:eth0",
            "GENERAL.TYPE:ethernet",
            "GENERAL.STATE:100 (connected)",
            "GENERAL.NM-MANAGED:yes",
            "GENERAL.METERED:no",
            "GENERAL.DEVICE:wlan0",
            "GENERAL.TYPE:wifi",
            "GENERAL.STATE:20 (unavailable)",
            "GENERAL.NM-MANAGED:yes",
            "GENERAL.METERED:unknown",
        )
    )

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return types.SimpleNamespace(
            returncode=0,
            stdout=output,
            stderr="",
        )

    resolver = taps.NetworkManagerPolicyResolver(
        command="/usr/bin/nmcli",
        runner=run,
    )

    policies = resolver.snapshot()

    assert policies["wwan0"] == {
        "policySource": "networkmanager",
        "interfaceType": "gsm",
        "networkManagerState": "100 (connected)",
        "networkManagerStateCode": 100,
        "managed": True,
        "metered": True,
        "expensive": True,
        "meteredSource": "guessed",
        "relativeCost": "high",
    }
    assert policies["eth0"]["metered"] is False
    assert policies["eth0"]["meteredSource"] == "configured"
    assert policies["wlan0"]["available"] is False
    assert "metered" not in policies["wlan0"]
    assert calls[0][0][:5] == [
        "/usr/bin/nmcli",
        "--terse",
        "--mode",
        "multiline",
        "--fields",
    ]
    assert calls[0][1] == {
        "capture_output": True,
        "text": True,
        "timeout": 1.0,
        "check": False,
    }


def test_native_provider_prefers_platform_events_and_falls_back_to_routes(
    monkeypatch,
):
    platform_source = object()

    class PlatformResolver(taps.InterfacePolicyResolver):
        def __init__(self):
            self.event_source = platform_source

        def create_event_source(self, *, loop=None):
            return self.event_source

    route_source = object()
    monkeypatch.setattr(
        taps.NativeRouteEventSource,
        "open",
        lambda *, loop=None: route_source,
    )
    resolver = PlatformResolver()
    provider = taps.NativeSystemPolicyProvider(
        platform_policy_resolver=resolver,
    )

    assert provider.create_event_source() is platform_source

    resolver.event_source = None

    assert provider.create_event_source() is route_source


@pytest.mark.asyncio
async def test_native_provider_reports_platform_setup_error_with_route_fallback(
    monkeypatch,
):
    class RouteSource(taps.SystemPolicyEventSource):
        name = "route-fallback"

        def __init__(self):
            self.event = asyncio.Event()
            self.closed = False

        async def wait_for_change(self):
            await self.event.wait()

        def close(self):
            self.closed = True

    class FailingResolver(taps.InterfacePolicyResolver):
        source = "failing-platform-policy"

        def create_event_source(self, *, loop=None):
            raise RuntimeError("platform monitor unavailable")

    route_source = RouteSource()
    monkeypatch.setattr(
        taps.NativeRouteEventSource,
        "open",
        lambda *, loop=None: route_source,
    )
    provider = taps.NativeSystemPolicyProvider(
        platform_policy_resolver=FailingResolver(),
    )
    provider.snapshot = lambda: taps.SystemPolicySnapshot(
        source="test",
        interfaces={},
    )
    updates = []
    context = taps.ConnectionContext()
    context.subscribe(updates.append)
    monitor = taps.SystemPolicyMonitor(
        context,
        provider,
        interval=60,
    )
    monitor.start()
    while monitor.refresh_count < 1:
        await asyncio.sleep(0)

    assert monitor.event_source_name == "route-fallback"
    assert isinstance(monitor.last_event_source_error, RuntimeError)
    assert any(
        update["trigger"] == "system_policy_event_source_error"
        and update["details"]["source"] == "failing-platform-policy"
        and update["details"]["error"] == "platform monitor unavailable"
        for update in updates
    )

    await monitor.stop()

    assert route_source.closed is True


def test_portable_provider_reports_interface_addresses(monkeypatch):
    fake_netifaces = types.SimpleNamespace(
        AF_INET=socket.AF_INET,
        AF_INET6=socket.AF_INET6,
        interfaces=lambda: ["lo0", "en0"],
        ifaddresses=lambda interface_id: {
            socket.AF_INET: [
                {
                    "addr": (
                        "127.0.0.1"
                        if interface_id == "lo0"
                        else "192.0.2.10"
                    )
                }
            ],
            socket.AF_INET6: [],
        },
    )
    monkeypatch.setattr(system_policy_module, "netifaces", fake_netifaces)
    monkeypatch.setattr(
        system_policy_module.socket,
        "if_nameindex",
        lambda: [(1, "lo0"), (2, "en0")],
    )

    snapshot = taps.PortableInterfacePolicyProvider().snapshot().as_dict()

    assert snapshot["interfaces"]["lo0"]["isLoopback"] is True
    assert snapshot["interfaces"]["en0"]["isLoopback"] is False
    assert snapshot["interfaces"]["en0"]["index"] == 2
    assert snapshot["interfaces"]["en0"]["addresses"][0]["address"] == (
        "192.0.2.10"
    )


def test_native_provider_adds_routes_link_state_cost_and_network_identity(
    monkeypatch,
):
    def ifaddresses(interface_id):
        addresses = {
            "en0": "192.0.2.10",
            "cell0": "198.51.100.20",
        }
        return {
            socket.AF_INET: [{"addr": addresses[interface_id]}],
            socket.AF_INET6: [],
        }

    fake_netifaces = types.SimpleNamespace(
        AF_INET=socket.AF_INET,
        AF_INET6=socket.AF_INET6,
        interfaces=lambda: ["cell0", "en0"],
        ifaddresses=ifaddresses,
        gateways=lambda: {
            "default": {
                socket.AF_INET: ("192.0.2.1", "en0"),
            },
            socket.AF_INET: [
                ("192.0.2.1", "en0", True),
                ("198.51.100.1", "cell0", False),
            ],
        },
    )
    monkeypatch.setattr(system_policy_module, "netifaces", fake_netifaces)
    monkeypatch.setattr(
        system_policy_module.socket,
        "if_nameindex",
        lambda: [(7, "en0"), (8, "cell0")],
    )

    provider = taps.NativeSystemPolicyProvider(
        link_state_reader=lambda interface_id: (
            "down" if interface_id == "cell0" else "up"
        ),
        platform_policy_resolver=types.SimpleNamespace(
            snapshot=lambda: {
                "en0": {
                    "policySource": "test-platform",
                    "expensive": True,
                    "metered": True,
                    "relativeCost": "high",
                },
            },
        ),
        cost_resolver=lambda interface_id, _policy: (
            {"relativeCost": "low", "metered": False}
            if interface_id == "en0"
            else "high"
        ),
    )
    snapshot = provider.snapshot().as_dict()

    assert list(snapshot["interfaces"]) == ["en0", "cell0"]
    assert snapshot["addressFamilies"] == {"ipv4": 1, "ipv6": 0}
    assert snapshot["interfaces"]["en0"]["isDefaultRoute"] is True
    assert snapshot["interfaces"]["en0"]["defaultGateways"] == {
        "ipv4": "192.0.2.1",
    }
    assert snapshot["interfaces"]["en0"]["networkId"] == (
        "en0|ipv4=192.0.2.1"
    )
    assert snapshot["interfaces"]["en0"]["policySource"] == "test-platform"
    assert snapshot["interfaces"]["en0"]["expensive"] is True
    assert snapshot["interfaces"]["en0"]["metered"] is False
    assert snapshot["interfaces"]["en0"]["relativeCost"] == "low"
    assert snapshot["interfaces"]["cell0"]["available"] is False
    assert snapshot["interfaces"]["cell0"]["operationalState"] == "down"


def test_network_identity_and_route_preference_flow_into_context_paths():
    context = taps.ConnectionContext()
    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "cell": {
                    **_interface_policy("198.51.100.20"),
                    "preferenceAdjustment": 0,
                    "networkId": "cell|198.51.100.1",
                },
                "wifi": {
                    **_interface_policy("192.0.2.10"),
                    "preferenceAdjustment": 2,
                    "networkId": "wifi|192.0.2.1",
                },
            },
        )
    )

    paths = context.get_system_local_endpoints()

    assert [endpoint.interface for endpoint in paths] == ["wifi", "cell"]
    assert context.get_interface_for_address("192.0.2.10") == "wifi"
    assert context.get_network_id(
        local_address="192.0.2.10"
    ) == "wifi|192.0.2.1"


@pytest.mark.asyncio
async def test_route_scoped_performance_history_orders_future_candidate_paths():
    context = taps.ConnectionContext()
    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "cell": {
                    **_interface_policy("198.51.100.20"),
                    "networkId": "cell|198.51.100.1",
                },
                "wifi": {
                    **_interface_policy("192.0.2.10"),
                    "networkId": "wifi|192.0.2.1",
                },
            },
        )
    )
    remote_path = ("203.0.113.10", 443)
    context.record_performance_observation(
        ("198.51.100.20", 0),
        remote_path,
        "tcp",
        network_id="cell|198.51.100.1",
        rtt=0.8,
    )
    context.record_performance_observation(
        ("192.0.2.10", 0),
        remote_path,
        "tcp",
        network_id="wifi|192.0.2.1",
        rtt=0.02,
    )
    connection = taps.Connection(
        taps.Preconnection(
            local_endpoints=[
                (
                    taps.LocalEndpoint()
                    .with_interface("cell")
                    .with_address("198.51.100.20")
                ),
                (
                    taps.LocalEndpoint()
                    .with_interface("wifi")
                    .with_address("192.0.2.10")
                ),
            ],
            remote_endpoint=(
                taps.RemoteEndpoint()
                .with_address("203.0.113.10")
                .with_port(443)
                .with_protocol("tcp")
            ),
            connection_context=context,
        )
    )

    branches = connection._candidate_branches_for_racing()

    assert branches[0].path == "wifi"
    connection._fail_initiate(RuntimeError("test cleanup"))
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_multicast_interface_selectors_follow_policy_and_ip_family():
    context = taps.ConnectionContext()
    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "wifi": {
                    "available": True,
                    "index": 7,
                    "addresses": [
                        {"address": "192.0.2.10"},
                        {"address": "2001:db8::10"},
                    ],
                },
            },
        )
    )
    endpoint = (
        taps.LocalEndpoint()
        .with_single_source_multicast_group_ip(
            "232.1.2.3",
            "198.51.100.10",
        )
        .with_port(5000)
        .with_interface("wifi")
    )
    listener = taps.Listener(
        taps.Preconnection(
            local_endpoint=endpoint,
            connection_context=context,
            event_loop=asyncio.get_running_loop(),
        )
    )

    assert listener._multicast_interface_selectors(endpoint) == [
        "192.0.2.10"
    ]
    ipv6_endpoint = (
        taps.LocalEndpoint()
        .with_single_source_multicast_group_ip(
            "ff3e::8000:1234",
            "2001:db8::20",
        )
        .with_port(5000)
        .with_interface("wifi")
    )
    assert listener._multicast_interface_selectors(ipv6_endpoint) == ["7"]

    explicit_endpoint = endpoint.clone()
    explicit_endpoint.address = "192.0.2.99"
    assert listener._multicast_interface_selectors(
        explicit_endpoint
    ) == ["192.0.2.99"]
    assert listener._multicast_template_is_dynamic(endpoint) is True
    assert (
        listener._multicast_template_is_dynamic(explicit_endpoint)
        is False
    )

    await listener.stop()


@pytest.mark.asyncio
async def test_multicast_rejoin_retries_without_leaving_old_subscription(
    monkeypatch,
):
    context = taps.ConnectionContext()
    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "wifi": _interface_policy("192.0.2.10"),
            },
        )
    )
    properties = taps.TransportProperties().unreliable_datagram()
    properties.set_property("direction", "Unidirectional Receive")
    properties.prohibit("reliability")
    endpoint = (
        taps.LocalEndpoint()
        .with_single_source_multicast_group_ip(
            "232.1.2.3",
            "198.51.100.10",
        )
        .with_port(5000)
        .with_interface("wifi")
    )
    resources = []
    join_attempts = []
    leaves = []
    replacement_saw_old_active = []
    listener_holder = {}

    def fake_join(
        current_listener,
        *,
        local_endpoint,
        interface,
    ):
        if listener_holder:
            assert current_listener is listener_holder["listener"]
        else:
            listener_holder["listener"] = current_listener
        join_attempts.append(interface)
        if interface == "192.0.2.11":
            replacement_saw_old_active.append(
                any(
                    not resource["closed"]
                    for resource in resources
                )
            )
            if join_attempts.count(interface) == 1:
                raise OSError("temporary multicast join failure")
        resource = {
            "interface": interface,
            "local_endpoint": local_endpoint.clone(),
            "closed": False,
        }
        resources.append(resource)
        return resource

    def fake_leave(resource):
        if resource["closed"]:
            return False
        resource["closed"] = True
        leaves.append(resource["interface"])
        return True

    monkeypatch.setattr(
        listener_module,
        "join_subscription",
        fake_join,
    )
    monkeypatch.setattr(
        listener_module,
        "leave_subscription",
        fake_leave,
    )
    monkeypatch.setattr(
        listener_module,
        "POLICY_RECONCILE_RETRY_BASE_DELAY",
        0,
    )

    listener = await taps.Preconnection(
        local_endpoint=endpoint,
        transport_properties=properties,
        connection_context=context,
        event_loop=asyncio.get_running_loop(),
    ).listen(timeout=1)
    assert listener_holder["listener"] is listener

    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "wifi": _interface_policy("192.0.2.11"),
            },
        )
    )
    for _ in range(30):
        await asyncio.sleep(0)
        subscriptions = listener.get_property(
            "multicastSubscriptions"
        )
        if (
            [item["interface"] for item in subscriptions]
            == ["192.0.2.11"]
        ):
            break

    assert join_attempts == [
        "192.0.2.10",
        "192.0.2.11",
        "192.0.2.11",
    ]
    assert replacement_saw_old_active == [True, True]
    assert leaves == ["192.0.2.10"]
    assert resources[0]["closed"] is True
    assert resources[-1]["closed"] is False
    assert listener.get_property("multicastSubscriptions") == [
        {
            "group": "232.1.2.3",
            "source": "198.51.100.10",
            "port": 5000,
            "interface": "192.0.2.11",
        }
    ]
    event_names = {
        event["name"] for event in listener.get_event_history()
    }
    assert "listener_path_reconciliation_failed" in event_names
    assert "listener_path_reconciliation_recovered" in event_names
    assert listener.get_property("pathReconciliationError") is None

    previous_subscription = resources[-1]
    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "wifi": {
                    **_interface_policy("192.0.2.11"),
                    "networkId": "wifi|gateway-b",
                },
            },
        )
    )
    for _ in range(20):
        await asyncio.sleep(0)
        if previous_subscription["closed"]:
            break

    assert join_attempts[-1] == "192.0.2.11"
    assert replacement_saw_old_active == [True, True, True]
    assert previous_subscription["closed"] is True
    assert resources[-1]["closed"] is False
    assert leaves == ["192.0.2.10", "192.0.2.11"]

    await listener.stop()
    assert leaves == [
        "192.0.2.10",
        "192.0.2.11",
        "192.0.2.11",
    ]


@pytest.mark.asyncio
async def test_listener_reconciles_interface_addresses_and_closes_resources(
    monkeypatch,
):
    class FakeServer:
        def __init__(self):
            self.closed = False
            self.waited = False

        def close(self):
            self.closed = True

        async def wait_closed(self):
            self.waited = True

    class FakeDatagramTransport:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    context = taps.ConnectionContext()
    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "wifi": _interface_policy("192.0.2.10"),
            },
        )
    )
    listener = taps.Listener(
        taps.Preconnection(
            local_endpoint=(
                taps.LocalEndpoint()
                .with_interface("wifi")
                .with_port(7777)
            ),
            connection_context=context,
            event_loop=asyncio.get_running_loop(),
        )
    )
    listener.state = taps.ConnectionState.ESTABLISHED
    listener._protocol_candidates = ["tcp"]
    listener._binding_ports[(0, "tcp")] = 7777
    old_endpoint = (
        taps.LocalEndpoint()
        .with_interface("wifi")
        .with_address("192.0.2.10")
        .with_port(7777)
        .with_protocol("tcp")
    )
    old_server = FakeServer()
    listener._servers.append(old_server)
    listener._store_binding(
        "tcp",
        old_endpoint,
        old_server,
        "server",
        0,
    )
    listener._refresh_system_policy_paths(record=False)
    new_servers = []
    replacement_started_while_old_open = []

    async def start_candidate(protocol, endpoint, *, template_index):
        replacement_started_while_old_open.append(
            not old_server.closed
        )
        endpoint.protocol = protocol
        endpoint.port = listener._binding_ports[
            (template_index, protocol)
        ]
        server = FakeServer()
        new_servers.append(server)
        listener._servers.append(server)
        listener._store_binding(
            protocol,
            endpoint,
            server,
            "server",
            template_index,
        )
        return True

    monkeypatch.setattr(listener, "_start_candidate", start_candidate)

    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "wifi": _interface_policy("192.0.2.11"),
            },
        )
    )
    for _ in range(3):
        await asyncio.sleep(0)
    if listener._policy_reconcile_task is not None:
        await listener._policy_reconcile_task

    assert old_server.closed is True
    assert old_server.waited is True
    assert replacement_started_while_old_open == [True]
    assert len(new_servers) == 1
    assert [
        record["endpoint"].address
        for record in listener._binding_records
    ] == ["192.0.2.11"]
    assert [
        endpoint.address
        for endpoint in listener.get_property("systemPolicyPaths")
    ] == ["192.0.2.11"]
    assert {
        event["name"] for event in listener.get_event_history()
    } >= {
        "listener_binding_added",
        "listener_binding_removed",
        "listener_paths_updated",
        "system_policy_changed",
    }

    datagram = FakeDatagramTransport()
    datagram_endpoint = old_endpoint.clone()
    datagram_endpoint.protocol = "udp"
    listener._datagram_transports.append(datagram)
    listener._store_binding(
        "udp",
        datagram_endpoint,
        datagram,
        "datagram",
        0,
    )
    await listener.stop()

    assert new_servers[0].closed is True
    assert new_servers[0].waited is True
    assert datagram.closed is True
    assert listener._servers == []
    assert listener._datagram_transports == []
    assert listener._binding_records == []


@pytest.mark.asyncio
async def test_listener_retries_failed_rebinding_without_dropping_old_socket(
    monkeypatch,
):
    class FakeServer:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

    context = taps.ConnectionContext()
    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "wifi": _interface_policy("192.0.2.10"),
            },
        )
    )
    listener = taps.Listener(
        taps.Preconnection(
            local_endpoint=(
                taps.LocalEndpoint()
                .with_interface("wifi")
                .with_port(7777)
            ),
            connection_context=context,
            event_loop=asyncio.get_running_loop(),
        )
    )
    listener.state = taps.ConnectionState.ESTABLISHED
    listener._protocol_candidates = ["tcp"]
    listener._binding_ports[(0, "tcp")] = 7777
    old_endpoint = (
        taps.LocalEndpoint()
        .with_interface("wifi")
        .with_address("192.0.2.10")
        .with_port(7777)
        .with_protocol("tcp")
    )
    old_server = FakeServer()
    listener._servers.append(old_server)
    listener._store_binding(
        "tcp",
        old_endpoint,
        old_server,
        "server",
        0,
    )
    listener._refresh_system_policy_paths(record=False)
    attempts = 0

    async def start_candidate(protocol, endpoint, *, template_index):
        nonlocal attempts
        attempts += 1
        assert old_server.closed is False
        if attempts == 1:
            raise OSError("temporary bind failure")
        endpoint.protocol = protocol
        endpoint.port = 7777
        server = FakeServer()
        listener._servers.append(server)
        listener._store_binding(
            protocol,
            endpoint,
            server,
            "server",
            template_index,
        )
        return True

    monkeypatch.setattr(listener, "_start_candidate", start_candidate)
    monkeypatch.setattr(
        listener_module,
        "POLICY_RECONCILE_RETRY_BASE_DELAY",
        0,
    )

    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "wifi": _interface_policy("192.0.2.11"),
            },
        )
    )
    for _ in range(20):
        await asyncio.sleep(0)
        if {
            record["endpoint"].address
            for record in listener._binding_records
        } == {"192.0.2.11"}:
            break

    assert attempts == 2
    assert old_server.closed is True
    assert [
        record["endpoint"].address
        for record in listener._binding_records
    ] == ["192.0.2.11"]
    event_names = [
        event["name"]
        for event in listener.get_event_history()
    ]
    assert "listener_path_reconciliation_failed" in event_names
    assert "listener_path_reconciliation_recovered" in event_names
    read_only = listener.get_properties()["readOnly"]
    assert read_only["pathReconciliationRetryScheduled"] is False
    assert read_only["pathReconciliationRetryAttempt"] == 0
    assert read_only["pathReconciliationError"] is None

    await listener.stop()


@pytest.mark.asyncio
async def test_quic_listener_rebinding_preserves_accepted_associations(
    monkeypatch,
):
    class FakeAssociation:
        instances = []

        def __init__(self, *, loop, listener):
            self.loop = loop
            self.listener = listener
            self.endpoint = None
            self.stopped = False
            self.child_associations = set()
            self.instances.append(self)

        async def start_listener(self, listener, *, local_endpoint):
            self.endpoint = local_endpoint.clone()
            return self

        def bound_port(self):
            return self.endpoint.port

        async def stop_listener(self, *, close_associations=False):
            self.stopped = True
            if close_associations:
                raise AssertionError("accepted associations must survive")

    context = taps.ConnectionContext()
    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "wifi": _interface_policy("192.0.2.10"),
            },
        )
    )
    listener = taps.Listener(
        taps.Preconnection(
            local_endpoint=(
                taps.LocalEndpoint()
                .with_interface("wifi")
                .with_port(7777)
            ),
            connection_context=context,
            event_loop=asyncio.get_running_loop(),
        )
    )
    monkeypatch.setattr(
        listener_module.transport_impl,
        "aioquic_serve",
        object(),
    )
    monkeypatch.setattr(
        listener_module,
        "QuicAssociationManager",
        FakeAssociation,
    )
    listener._protocol_candidates = ["quic"]
    old_endpoint = (
        listener.local_endpoints[0].clone()
        .with_address("192.0.2.10")
    )
    assert await listener._start_candidate(
        "quic",
        old_endpoint,
        template_index=0,
    )
    listener._mark_listening()
    old_association = listener.quic_association
    accepted_association = object()
    old_association.child_associations.add(accepted_association)

    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "wifi": _interface_policy("192.0.2.11"),
            },
        )
    )
    for _ in range(10):
        await asyncio.sleep(0)
    if listener._policy_reconcile_task is not None:
        await listener._policy_reconcile_task

    assert len(FakeAssociation.instances) == 2
    assert old_association.stopped is True
    assert accepted_association in old_association.child_associations
    assert listener.quic_association is FakeAssociation.instances[1]
    assert [
        endpoint.address
        for endpoint in listener.get_property("boundLocalEndpoints")
    ] == ["192.0.2.11"]

    await listener.stop()
    assert FakeAssociation.instances[1].stopped is True


@pytest.mark.asyncio
async def test_quic_listener_drain_closes_idle_and_preserves_active_children():
    class FakeServer:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeChild:
        def __init__(self, manager, *, idle):
            self.manager = manager
            self.idle = idle
            self.closed = False

        def _should_close_if_unused(self):
            return self.idle

        async def close_association(self):
            self.closed = True
            self.manager.child_associations.discard(self)

    manager = QuicAssociationManager(loop=asyncio.get_running_loop())
    manager.server = FakeServer()
    active_child = FakeChild(manager, idle=False)
    idle_child = FakeChild(manager, idle=True)
    manager.child_associations.update({active_child, idle_child})

    await manager.stop_listener()

    server = manager.server
    assert server is not None
    assert server.closed is False
    assert idle_child.closed is True
    assert idle_child not in manager.child_associations
    assert active_child.closed is False
    assert active_child in manager.child_associations

    await manager.stop_listener(close_associations=True)
    assert active_child.closed is True
    assert manager.child_associations == set()
    assert server.closed is True
    assert manager.server is None


@pytest.mark.asyncio
@pytest.mark.parametrize("migration_fails", [False, True])
async def test_active_quic_adapts_to_policy_path_without_forced_close(
    monkeypatch,
    migration_fails,
):
    context = taps.ConnectionContext()
    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "wifi": _interface_policy("192.0.2.10"),
            },
        )
    )
    properties = taps.TransportProperties()
    properties.set_property("multipath", "Active")
    connection = taps.Connection(
        taps.Preconnection(
            remote_endpoint=(
                taps.RemoteEndpoint()
                .with_address("203.0.113.10")
                .with_port(443)
            ),
            transport_properties=properties,
            connection_context=context,
            event_loop=asyncio.get_running_loop(),
        )
    )
    connection.local_endpoint = (
        taps.LocalEndpoint()
        .with_interface("wifi")
        .with_address("192.0.2.10")
    )
    connection.protocol = "quic"
    association = types.SimpleNamespace(
        anchor_connection=connection,
        _handshake_owner=connection,
    )
    connection.quic_association = association
    connection._mark_ready()
    connection.enable_auto_reestablishment(
        triggers={"soft_error"},
    )
    connection.note_path_change(
        local_address="192.0.2.10",
        local_port=50000,
        remote_address="203.0.113.10",
        remote_port=443,
    )
    attempted = []
    reestablishment_attempts = []

    async def migrate_path(local_endpoint, *, timeout):
        attempted.append((local_endpoint.clone(), timeout))
        if migration_fails:
            raise TimeoutError("validation timed out")
        connection.local_endpoint = local_endpoint.clone()
        return {
            "local": (local_endpoint.address, 50001),
            "remote": ("203.0.113.10", 443),
        }

    monkeypatch.setattr(connection, "migrate_path", migrate_path)

    async def attempt_reestablishment(timeout=None):
        reestablishment_attempts.append(timeout)
        return object()

    monkeypatch.setattr(
        connection,
        "attempt_reestablishment",
        attempt_reestablishment,
    )

    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "cell": _interface_policy("198.51.100.20"),
            },
        )
    )
    for _ in range(3):
        await asyncio.sleep(0)
    if connection._system_policy_adaptation_task is not None:
        await connection._system_policy_adaptation_task
    for _ in range(3):
        await asyncio.sleep(0)
    if connection._auto_reestablishment_task is not None:
        await connection._auto_reestablishment_task

    event_names = [
        event["name"]
        for event in connection.get_event_history()
    ]
    assert connection.state is taps.ConnectionState.ESTABLISHED
    assert attempted[0][0].interface == "cell"
    assert attempted[0][0].address == "198.51.100.20"
    assert "path_adaptation_started" in event_names
    expected = (
        "path_adaptation_failed"
        if migration_fails
        else "path_adaptation_succeeded"
    )
    assert expected in event_names
    assert "connection_error" not in event_names
    assert reestablishment_attempts == (
        [5] if migration_fails else []
    )

    connection.close()
    await connection.wait_closed(timeout=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_policy", "updated_policy", "expected_address"),
    [
        (
            _interface_policy("192.0.2.10"),
            _interface_policy("192.0.2.11"),
            "192.0.2.11",
        ),
        (
            {
                **_interface_policy("192.0.2.10"),
                "networkId": "wifi|gateway-a",
            },
            {
                **_interface_policy("192.0.2.10"),
                "networkId": "wifi|gateway-b",
            },
            "192.0.2.10",
        ),
    ],
)
async def test_active_quic_adapts_when_address_or_network_identity_changes(
    monkeypatch,
    initial_policy,
    updated_policy,
    expected_address,
):
    context = taps.ConnectionContext()
    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={"wifi": initial_policy},
        )
    )
    properties = taps.TransportProperties()
    properties.set_property("multipath", "Active")
    connection = taps.Connection(
        taps.Preconnection(
            remote_endpoint=(
                taps.RemoteEndpoint()
                .with_address("203.0.113.10")
                .with_port(443)
            ),
            transport_properties=properties,
            connection_context=context,
            event_loop=asyncio.get_running_loop(),
        )
    )
    connection.local_endpoint = (
        taps.LocalEndpoint()
        .with_interface("wifi")
        .with_address("192.0.2.10")
    )
    connection.protocol = "quic"
    connection.quic_association = types.SimpleNamespace(
        anchor_connection=connection,
        _handshake_owner=connection,
    )
    connection._mark_ready()
    connection.note_path_change(
        local_address="192.0.2.10",
        local_port=50000,
        remote_address="203.0.113.10",
        remote_port=443,
    )
    attempted = []

    async def migrate_path(local_endpoint, *, timeout):
        attempted.append((local_endpoint.clone(), timeout))
        return {
            "local": (local_endpoint.address, 50001),
            "remote": ("203.0.113.10", 443),
        }

    monkeypatch.setattr(connection, "migrate_path", migrate_path)

    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={"wifi": updated_policy},
        )
    )
    for _ in range(3):
        await asyncio.sleep(0)
    if connection._system_policy_adaptation_task is not None:
        await connection._system_policy_adaptation_task

    assert attempted[0][0].interface == "wifi"
    assert attempted[0][0].address == expected_address
    assert attempted[0][1] == 5
    assert "path_adaptation_succeeded" in {
        event["name"] for event in connection.get_event_history()
    }

    connection.close()
    await connection.wait_closed(timeout=1)


@pytest.mark.asyncio
async def test_system_policy_does_not_migrate_when_multipath_is_disabled(
    monkeypatch,
):
    context = taps.ConnectionContext()
    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "wifi": _interface_policy("192.0.2.10"),
            },
        )
    )
    connection = taps.Connection(
        taps.Preconnection(
            remote_endpoint=(
                taps.RemoteEndpoint()
                .with_address("203.0.113.10")
                .with_port(443)
            ),
            connection_context=context,
            event_loop=asyncio.get_running_loop(),
        )
    )
    connection.local_endpoint = (
        taps.LocalEndpoint()
        .with_interface("wifi")
        .with_address("192.0.2.10")
    )
    connection.protocol = "quic"
    connection.quic_association = types.SimpleNamespace(
        anchor_connection=connection,
        _handshake_owner=connection,
    )
    connection._mark_ready()
    attempted = []

    async def migrate_path(local_endpoint, *, timeout):
        attempted.append((local_endpoint, timeout))

    monkeypatch.setattr(connection, "migrate_path", migrate_path)

    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "wifi": _interface_policy("192.0.2.11"),
            },
        )
    )
    for _ in range(3):
        await asyncio.sleep(0)

    assert attempted == []
    assert connection._system_policy_adaptation_task is None
    assert "soft_error" in {
        event["name"] for event in connection.get_event_history()
    }

    connection.close()
    await connection.wait_closed(timeout=1)


@pytest.mark.asyncio
async def test_system_policy_handover_honors_explicit_local_constraints(
    monkeypatch,
):
    context = taps.ConnectionContext()
    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "wifi": _interface_policy("192.0.2.10"),
            },
        )
    )
    properties = taps.TransportProperties()
    properties.set_property("multipath", "Active")
    connection = taps.Connection(
        taps.Preconnection(
            local_endpoint=(
                taps.LocalEndpoint()
                .with_interface("wifi")
                .with_address("192.0.2.10")
            ),
            remote_endpoint=(
                taps.RemoteEndpoint()
                .with_address("203.0.113.10")
                .with_port(443)
            ),
            transport_properties=properties,
            connection_context=context,
            event_loop=asyncio.get_running_loop(),
        )
    )
    connection.protocol = "quic"
    connection.quic_association = types.SimpleNamespace(
        anchor_connection=connection,
        _handshake_owner=connection,
    )
    connection._mark_ready()
    connection.note_path_change(
        local_address="192.0.2.10",
        local_port=50000,
        remote_address="203.0.113.10",
        remote_port=443,
    )
    attempted = []

    async def migrate_path(local_endpoint, *, timeout):
        attempted.append((local_endpoint, timeout))

    monkeypatch.setattr(connection, "migrate_path", migrate_path)

    context.apply_system_policy(
        taps.SystemPolicySnapshot(
            interfaces={
                "cell": _interface_policy("198.51.100.20"),
            },
        )
    )
    for _ in range(3):
        await asyncio.sleep(0)

    assert attempted == []
    assert connection._system_policy_adaptation_task is None
    assert connection.state is taps.ConnectionState.ESTABLISHED
    assert "soft_error" in {
        event["name"] for event in connection.get_event_history()
    }

    connection.close()
    await connection.wait_closed(timeout=1)
