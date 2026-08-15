"""Exercise the Darwin policy stack against the real Network.framework.

The rest of the Darwin coverage drives a FakeNetwork stub, which cannot catch a
symbol name or constant that diverges from the framework. These tests use the
real binding on macOS and skip everywhere else.
"""
import asyncio
import sys

import pytest

import pytaps as taps
from pytaps.system_policy import (
    DarwinNetworkPathEventSource,
    DarwinNetworkPolicyResolver,
    NativeSystemPolicyProvider,
    SystemPolicyMonitor,
)

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("darwin"),
    reason="Network.framework is only available on Darwin",
)


def _network_module():
    return pytest.importorskip(
        "Network",
        reason="pyobjc-framework-Network is not installed",
    )


async def _wait_for(predicate, *, timeout=5.0, interval=0.1):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


def test_every_symbol_the_binding_needs_exists():
    """A stub cannot catch a renamed or missing framework symbol."""
    network = _network_module()

    for symbol in (
        "nw_path_monitor_create",
        "nw_path_monitor_set_update_handler",
        "nw_path_monitor_set_queue",
        "nw_path_monitor_start",
        "nw_path_monitor_cancel",
        "nw_path_enumerate_interfaces",
        "nw_interface_get_name",
        "nw_interface_get_type",
        "nw_path_get_status",
    ):
        assert hasattr(network, symbol), f"Network.{symbol} is missing"

    for constant in (
        "nw_interface_type_wifi",
        "nw_interface_type_cellular",
        "nw_interface_type_wired",
        "nw_interface_type_loopback",
        "nw_interface_type_other",
        "nw_path_status_satisfied",
        "nw_path_status_unsatisfied",
        "nw_path_status_satisfiable",
        "nw_path_status_invalid",
    ):
        assert hasattr(network, constant), f"Network.{constant} is missing"


@pytest.mark.asyncio
async def test_real_path_monitor_reports_policy_for_loopback():
    """Loopback is the one interface every machine running this test has."""
    _network_module()
    resolver = DarwinNetworkPolicyResolver()
    source = DarwinNetworkPathEventSource.open(
        resolver,
        loop=asyncio.get_running_loop(),
    )
    if source is None:
        pytest.skip("Darwin path event source unavailable in this environment")

    try:
        assert source._monitors, "no nw_path_monitor was started"
        assert await _wait_for(lambda: bool(resolver._paths)), (
            "Network.framework delivered no path updates"
        )

        policies = resolver.snapshot()
        assert "lo0" in policies, policies
        loopback = policies["lo0"]
        assert loopback["interfaceType"] == "loopback"
        assert loopback["pathStatus"] in {
            "satisfied",
            "satisfiable",
            "unsatisfied",
        }
        # Every policy value the resolver publishes is a real decoded value,
        # not a passthrough of an opaque framework object.
        for key in ("expensive", "constrained", "hasDNS", "isDefaultPath"):
            assert isinstance(loopback[key], bool), (key, loopback[key])
    finally:
        source.close()


@pytest.mark.asyncio
async def test_provider_selects_the_framework_and_drives_a_context():
    _network_module()
    provider = NativeSystemPolicyProvider()

    resolver = provider.platform_policy_resolver
    assert isinstance(resolver, DarwinNetworkPolicyResolver)
    assert resolver.source == "darwin-network-framework"

    context = taps.ConnectionContext()
    monitor = SystemPolicyMonitor(context, provider, interval=1.0)
    monitor.start()
    try:
        assert await _wait_for(lambda: monitor.event_driven, timeout=5.0), (
            "the monitor fell back to polling instead of framework events"
        )
        assert monitor.event_source_name == "darwin-network-path"
        assert monitor.last_event_source_error is None
        assert provider.last_platform_event_source_error is None

        await monitor.refresh(trigger="test")
        assert provider.last_platform_policy_error is None

        interfaces = context.get_snapshot()["systemPolicy"]["interfaces"]
        authoritative = {
            name: entry
            for name, entry in interfaces.items()
            if entry.get("interfaceType") is not None
        }
        assert authoritative, (
            "framework policy never reached the ConnectionContext"
        )
        assert "lo0" in authoritative
        assert authoritative["lo0"]["interfaceType"] == "loopback"

        # The context turns that policy into usable Local Endpoints.
        endpoints = context.get_system_local_endpoints()
        assert any(
            endpoint.interface == "lo0" for endpoint in endpoints
        ), endpoints
    finally:
        await monitor.stop()


@pytest.mark.asyncio
async def test_real_event_source_closes_cleanly():
    _network_module()
    resolver = DarwinNetworkPolicyResolver()
    source = DarwinNetworkPathEventSource.open(
        resolver,
        loop=asyncio.get_running_loop(),
    )
    if source is None:
        pytest.skip("Darwin path event source unavailable in this environment")

    await _wait_for(lambda: bool(resolver._paths))
    source.close()

    assert source.closed
    # Cancelling every monitor twice must stay harmless.
    source.close()
