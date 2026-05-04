import asyncio

import pytest

import pytaps as taps


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


def test_preconnection_freezes_after_initiate():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=loop,
    )

    connection = loop.run_until_complete(preconnection.initiate())
    connection.race_task.cancel()
    loop.run_until_complete(asyncio.gather(connection.race_task, return_exceptions=True))
    loop.close()

    assert preconnection.is_frozen() is True
    with pytest.raises(RuntimeError):
        preconnection.set_property("connPriority", 10)


def test_connection_clone_and_group_property_propagation():
    remote = taps.RemoteEndpoint().with_hostname("localhost").with_port(443)
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=asyncio.new_event_loop(),
    )
    connection = taps.Connection(preconnection)
    clone = connection.clone()

    connection.set_property("connTimeout", 12)
    connection.set_property("connPriority", 5)

    assert clone.connection_group is connection.connection_group
    assert clone.get_properties()["connection"]["connTimeout"] == 12
    assert clone.get_properties()["connection"]["connPriority"] == 100


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
