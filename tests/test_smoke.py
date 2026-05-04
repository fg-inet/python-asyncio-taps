import asyncio

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
