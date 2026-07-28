import asyncio

import pytaps as taps
from pytaps.listener import Listener
from pytaps.transportProperties import CONNECTION_PROPERTY_DEFAULTS
from pytaps.transports import QuicAssociationManager


def _connections():
    loop = asyncio.new_event_loop()
    remote = taps.RemoteEndpoint().with_address("203.0.113.10").with_port(443)
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=loop,
    )
    first = taps.Connection(preconnection)
    second = taps.Connection(preconnection)
    first.connection_group.add_connection(second)
    return loop, first, second


def test_sections_7_4_and_8_all_connection_properties_are_entangled():
    loop, first, second = _connections()
    values = {
        "recvChecksumLen": 0,
        "connTimeout": 10,
        "keepAliveTimeout": 5,
        "connScheduler": "Round Robin",
        "connCapacityProfile": "Scavenger",
        "multipathPolicy": "Aggregate",
        "minSendRate": 1,
        "minRecvRate": 2,
        "maxSendRate": 3,
        "maxRecvRate": 4,
        "groupConnLimit": 5,
        "isolateSession": True,
        "tcp.userTimeoutValue": 1000,
        "tcp.userTimeoutEnabled": True,
        "tcp.userTimeoutChangeable": False,
    }

    assert (
        set(CONNECTION_PROPERTY_DEFAULTS)
        - first.connection_group.ENTANGLED_PROPERTIES
        == {"connPriority"}
    )
    for name, value in values.items():
        first.set_property(name, value)
        assert second.get_property(name) == value

    first.set_property("connPriority", 1)
    assert second.get_property("connPriority") == 100
    loop.close()


def test_section_7_4_message_properties_remain_per_connection():
    loop, first, second = _connections()

    first.set_property("msgPriority", 7)
    first.set_property("msgLifetime", 3)

    assert first.get_property("msgPriority") == 7
    assert second.get_property("msgPriority") == 100
    assert first.get_property("msgLifetime") == 3
    assert second.get_property("msgLifetime") == "Infinite"
    loop.close()


def test_section_7_4_defaulting_an_entangled_property_updates_the_group():
    loop, first, second = _connections()
    first.set_property("connTimeout", 12)

    second.default_property("connTimeout")

    assert first.get_property("connTimeout") == "Disabled"
    assert second.get_property("connTimeout") == "Disabled"
    loop.close()


def test_section_8_1_10_isolated_initiations_do_not_share_cached_state(
    monkeypatch,
):
    loop = asyncio.new_event_loop()
    remote = taps.RemoteEndpoint().with_address("203.0.113.10").with_port(443)
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        event_loop=loop,
    )
    preconnection.set_property("isolateSession", True)

    async def establish(connection):
        connection.protocol = "tcp"
        connection._mark_ready()

    monkeypatch.setattr(taps.Connection, "race", establish)

    first = loop.run_until_complete(preconnection.initiate(timeout=1))
    second = loop.run_until_complete(preconnection.initiate(timeout=1))
    clone = loop.run_until_complete(first.clone())

    assert first.connection_context is not second.connection_context
    assert clone.connection_context is first.connection_context
    assert clone.connection_group is first.connection_group
    assert clone.get_property("isolateSession") is True

    loop.run_until_complete(first.close_group())
    second.close()
    loop.run_until_complete(second.wait_closed())
    loop.close()


def test_section_10_connection_group_close_waits_for_every_member():
    loop, first, second = _connections()
    first._mark_ready()
    second._mark_ready()

    loop.run_until_complete(first.close_group())

    assert first.state is taps.ConnectionState.CLOSED
    assert second.state is taps.ConnectionState.CLOSED
    assert first.get_event_history()[-1]["name"] == "closed"
    assert second.get_event_history()[-1]["name"] == "closed"
    loop.close()


def test_section_10_connection_group_abort_reports_each_member():
    loop, first, second = _connections()
    first._mark_ready()
    second._mark_ready()

    loop.run_until_complete(first.abort_group())

    assert first.state is taps.ConnectionState.CLOSED
    assert second.state is taps.ConnectionState.CLOSED
    assert first.get_event_history()[-1]["name"] == "connection_error"
    assert second.get_event_history()[-1]["name"] == "connection_error"
    loop.close()


def test_section_7_4_quic_associations_keep_distinct_peers_in_separate_groups():
    class FakeProtocolTransport:
        def __init__(self, peer_address):
            self.peer_address = peer_address

        def get_extra_info(self, name):
            if name == "peername":
                return (self.peer_address, 4433)
            if name == "sockname":
                return ("127.0.0.1", 4433)
            return None

    class FakeProtocol:
        def __init__(self, peer_address):
            self._transport = FakeProtocolTransport(peer_address)

    class FakeReader:
        async def read(self, size):
            await asyncio.sleep(3600)

    class FakeWriter:
        def write(self, data):
            return None

        async def drain(self):
            return None

        def close(self):
            return None

        async def wait_closed(self):
            return None

    loop = asyncio.new_event_loop()
    local = taps.LocalEndpoint().with_address("127.0.0.1").with_port(4433)
    preconnection = taps.Preconnection(
        local_endpoint=local,
        event_loop=loop,
    )
    listener = Listener(preconnection)
    listener.protocol = "quic"
    listener._mark_listening()
    first_association = QuicAssociationManager(loop=loop, listener=listener)
    second_association = QuicAssociationManager(loop=loop, listener=listener)

    async def accept_associations():
        await asyncio.gather(
            first_association.accept_inbound_stream(
                FakeReader(),
                FakeWriter(),
                FakeProtocol("198.51.100.1"),
            ),
            second_association.accept_inbound_stream(
                FakeReader(),
                FakeWriter(),
                FakeProtocol("198.51.100.2"),
            ),
        )

    loop.run_until_complete(accept_associations())
    first = loop.run_until_complete(listener.accept())
    second = loop.run_until_complete(listener.accept())

    assert first.connection_group is not second.connection_group
    assert first.quic_association is first_association
    assert second.quic_association is second_association
    assert first.remote_endpoint.address == "198.51.100.1"
    assert second.remote_endpoint.address == "198.51.100.2"

    async def close_groups():
        await asyncio.gather(first.close_group(), second.close_group())

    loop.run_until_complete(close_groups())
    loop.run_until_complete(listener.stop())
    loop.close()
