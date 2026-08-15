import asyncio
import socket
import ssl
from pathlib import Path
from types import SimpleNamespace

import pytest

import pytaps as taps
import pytaps.connection as connection_module
import pytaps.transports as transport_module
from pytaps.listener import Listener


KEYS = Path(__file__).parent / "keys"
SERVER_CERTIFICATE = KEYS / "localhost.pem"
ROOT_CERTIFICATE = KEYS / "MyRootCA.pem"


def test_section_6_1_endpoint_has_one_identifier_of_each_type():
    endpoint = (
        taps.RemoteEndpoint()
        .with_hostname("service.example")
        .with_port(443)
        .with_ip_address("192.0.2.1")
        .with_interface("en0")
        .with_protocol("tcp")
    )

    endpoint.with_ip_address("192.0.2.2")
    endpoint.with_interface("en1")

    assert endpoint.host_name == "service.example"
    assert endpoint.port == 443
    assert endpoint.address == "192.0.2.2"
    assert endpoint.interface == "en1"
    assert endpoint.protocol == "tcp"


def test_section_6_1_scoped_ipv6_sets_the_interface_identifier():
    endpoint = taps.RemoteEndpoint().with_ip_address("fe80::1%en0")

    assert endpoint.address == "fe80::1"
    assert endpoint.interface == "en0"
    assert endpoint.socket_address() == "fe80::1%en0"


def test_section_6_1_endpoint_validates_integer_ports_and_binary_addresses():
    endpoint = taps.RemoteEndpoint().with_ip_address(b"\xc0\x00\x02\x01")

    assert endpoint.address == "192.0.2.1"
    with pytest.raises(ValueError):
        endpoint.with_port(443.5)


def test_section_6_1_protocol_qualifier_selects_service_transport(monkeypatch):
    calls = []

    def fake_getservbyname(service, protocol):
        calls.append((service, protocol))
        return 443

    monkeypatch.setattr(socket, "getservbyname", fake_getservbyname)
    endpoint = (
        taps.RemoteEndpoint()
        .with_service("https")
        .with_protocol("quic")
    )

    assert endpoint.effective_port(endpoint.protocol) == 443
    assert calls == [("https", "udp")]


def test_sections_6_1_1_and_6_1_5_multicast_identifiers():
    sender = (
        taps.RemoteEndpoint()
        .with_multicast_group_ip("233.251.240.1")
        .with_port(5353)
        .with_hop_limit(8)
    )
    receiver = (
        taps.LocalEndpoint()
        .with_single_source_multicast_group_ip(
            "233.252.0.1",
            "198.51.100.10",
        )
        .with_port(5353)
    )

    assert sender.multicast_group == "233.251.240.1"
    assert sender.hop_limit == 8
    assert receiver.multicast_group == "233.252.0.1"
    assert receiver.multicast_source == "198.51.100.10"


def test_section_6_1_stun_server_identifier():
    endpoint = taps.LocalEndpoint().with_stun_server(
        "stun.example",
        3478,
        credentials={"username": "alice"},
    )

    assert endpoint.stun_server.address == "stun.example"
    assert endpoint.stun_server.port == 3478
    assert endpoint.stun_server.credentials == {"username": "alice"}


def test_section_6_endpoint_collections_are_distinct_candidates():
    loop = asyncio.new_event_loop()
    local_endpoints = [
        taps.LocalEndpoint().with_address("192.0.2.1"),
        taps.LocalEndpoint().with_address("192.0.2.2"),
    ]
    remote_endpoints = [
        taps.RemoteEndpoint().with_address("198.51.100.1").with_port(443),
        (
            taps.RemoteEndpoint()
            .with_address("198.51.100.2")
            .with_port(8443)
            .with_protocol("quic")
        ),
    ]

    preconnection = taps.Preconnection(
        local_endpoints=local_endpoints,
        remote_endpoints=remote_endpoints,
        event_loop=loop,
    )

    assert [endpoint.address for endpoint in preconnection.local_endpoints] == [
        "192.0.2.1",
        "192.0.2.2",
    ]
    assert [endpoint.address for endpoint in preconnection.remote_endpoints] == [
        "198.51.100.1",
        "198.51.100.2",
    ]
    assert preconnection.remote_endpoints[1].protocol == "quic"
    loop.close()


@pytest.mark.asyncio
async def test_section_6_1_address_and_interface_form_one_local_constraint(
    monkeypatch,
):
    class FakeNetifaces:
        AF_INET = 2
        AF_INET6 = 10

        @staticmethod
        def ifaddresses(_interface):
            return {
                FakeNetifaces.AF_INET: [{"addr": "192.0.2.99"}],
                FakeNetifaces.AF_INET6: [],
            }

    captured = {}

    class FakeTransport:
        def get_extra_info(self, name):
            if name == "sockname":
                return captured["local_addr"]
            if name == "peername":
                return ("198.51.100.1", 443)
            return None

        def close(self):
            return None

        def write(self, _data):
            return None

    async def fake_create_connection(
        protocol_factory,
        host,
        port,
        *,
        ssl=None,
        server_hostname=None,
        local_addr=None,
    ):
        captured.update(
            host=host,
            port=port,
            local_addr=local_addr,
            ssl=ssl,
            server_hostname=server_hostname,
        )
        protocol = protocol_factory()
        transport = FakeTransport()
        protocol.connection_made(transport)
        await asyncio.sleep(0)
        return transport, protocol

    monkeypatch.setattr(connection_module, "netifaces", FakeNetifaces)
    monkeypatch.setattr(
        connection_module,
        "build_protocol_candidates",
        lambda *_args, **_kwargs: ["tcp"],
    )

    preconnection = taps.Preconnection(
        local_endpoints=[
            (
                taps.LocalEndpoint()
                .with_ip_address("192.0.2.10")
                .with_interface("en0")
            )
        ],
        remote_endpoints=[
            (
                taps.RemoteEndpoint()
                .with_hostname("service.example")
                .with_ip_address("198.51.100.1")
                .with_port(443)
            )
        ],
    )
    connection = taps.Connection(preconnection)
    monkeypatch.setattr(connection.loop, "create_connection", fake_create_connection)
    monkeypatch.setattr(
        connection.loop,
        "getaddrinfo",
        lambda *_args, **_kwargs: pytest.fail(
            "An explicitly constrained IP address must not be re-resolved"
        ),
    )

    await connection.race()

    assert captured["host"] == "198.51.100.1"
    assert captured["local_addr"] == ("192.0.2.10", 0)
    assert connection.local_endpoint.address == "192.0.2.10"
    connection._mark_closed()


def test_section_3_1_2_constructor_arguments_use_call_by_value():
    loop = asyncio.new_event_loop()
    endpoint = taps.RemoteEndpoint().with_hostname("example.com").with_port(443)
    properties = taps.TransportProperties()
    security = taps.SecurityParameters()
    security.set_alpn_protocols(["h2"])

    preconnection = taps.Preconnection(
        remote_endpoints=[endpoint],
        transport_properties=properties,
        security_parameters=security,
        event_loop=loop,
    )
    endpoint.with_port(8443)
    properties.set_property("connPriority", 1)
    security.set_alpn_protocols(["h3"])

    assert preconnection.remote_endpoint.port == 443
    assert preconnection.get_property("connPriority") == 100
    assert preconnection.security_parameters.alpn_protocols == ["h2"]
    loop.close()


def test_sections_7_1_and_7_2_connection_and_listener_are_snapshots():
    loop = asyncio.new_event_loop()
    local = taps.LocalEndpoint().with_address("127.0.0.1").with_port(9000)
    remote = taps.RemoteEndpoint().with_address("192.0.2.1").with_port(443)
    security = taps.SecurityParameters()
    security.set_alpn_protocols(["h2"])
    preconnection = taps.Preconnection(
        local_endpoints=[local],
        remote_endpoints=[remote],
        security_parameters=security,
        event_loop=loop,
    )

    connection = taps.Connection(preconnection)
    listener = Listener(preconnection)
    preconnection.local_endpoint.with_port(9001)
    preconnection.remote_endpoint.with_port(8443)
    preconnection.set_property("connPriority", 1)
    preconnection.security_parameters.set_alpn_protocols(["h3"])

    assert connection.local_endpoint.port == 9000
    assert connection.remote_endpoint.port == 443
    assert connection.get_property("connPriority") == 100
    assert connection.security_parameters.alpn_protocols == ["h2"]
    assert listener.local_endpoint.port == 9000
    assert listener.remote_endpoint.port == 443
    assert listener.get_property("connPriority") == 100
    assert listener.security_parameters.alpn_protocols == ["h2"]
    assert listener.get_property("useTemporaryLocalAddress") is taps.PreferenceLevel.AVOID
    assert listener.get_property("multipath") == "Passive"
    loop.close()


def test_section_6_resolve_allows_an_empty_remote_endpoint_list():
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        local_endpoints=[taps.LocalEndpoint().with_port(9876)],
        remote_endpoints=[],
        event_loop=loop,
    )

    resolved_local, resolved_remote = loop.run_until_complete(
        preconnection.resolve()
    )

    assert len(resolved_local) == 1
    assert resolved_local[0].port == 9876
    assert resolved_remote == []
    loop.close()


def test_section_6_3_3_certificate_pin_helper_matches_exact_certificate():
    security = taps.SecurityParameters()
    security.add_pinned_server_certificate(SERVER_CERTIFICATE.read_text())

    security.verify_pinned_server_certificates([SERVER_CERTIFICATE.read_bytes()])
    with pytest.raises(ssl.SSLCertVerificationError):
        security.verify_pinned_server_certificates([ROOT_CERTIFICATE])


@pytest.mark.asyncio
async def test_section_6_3_3_quic_pin_failure_closes_the_association(
    monkeypatch,
):
    class FakeQuicConfiguration:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        def load_verify_locations(self, **_kwargs):
            return None

    class FakeConnectContext:
        def __init__(self):
            self.exited = False

            async def wait_connected():
                return None

            self.protocol = SimpleNamespace(
                _quic=SimpleNamespace(
                    tls=SimpleNamespace(
                        _peer_certificate=ROOT_CERTIFICATE.read_bytes(),
                        _peer_certificate_chain=[],
                    )
                ),
                transmit=lambda: None,
                wait_connected=wait_connected,
            )

        async def __aenter__(self):
            return self.protocol

        async def __aexit__(self, _exc_type, _exc, _traceback):
            self.exited = True

    context = FakeConnectContext()
    monkeypatch.setattr(transport_module, "QuicConfiguration", FakeQuicConfiguration)
    monkeypatch.setattr(
        transport_module.QuicAssociationManager,
        "_connect_client_protocol",
        lambda *_args, **_kwargs: context,
    )
    monkeypatch.setattr(transport_module, "aioquic_serve", object())

    security = taps.SecurityParameters()
    security.add_trust_ca(str(ROOT_CERTIFICATE))
    security.add_pinned_server_certificate(str(SERVER_CERTIFICATE))
    preconnection = taps.Preconnection(
        remote_endpoints=[
            taps.RemoteEndpoint().with_ip_address("192.0.2.1").with_port(443)
        ],
        security_parameters=security,
    )
    connection = taps.Connection(preconnection)
    association = transport_module.QuicAssociationManager(
        loop=asyncio.get_running_loop()
    )

    with pytest.raises(ssl.SSLCertVerificationError, match="pinned"):
        await association.connect_client(connection)

    assert context.exited is True
    assert association.protocol is None
    assert association.context_manager is None


@pytest.mark.asyncio
async def test_section_6_3_tls_hostname_and_exact_pin_verification():
    tls_properties = taps.TransportProperties()
    tls_properties.prohibit("multistreaming")
    server_security = taps.SecurityParameters()
    server_security.add_identity(str(SERVER_CERTIFICATE))
    server = taps.Preconnection(
        local_endpoints=[
            taps.LocalEndpoint().with_address("127.0.0.1").with_port(0)
        ],
        transport_properties=tls_properties,
        security_parameters=server_security,
    )
    listener = await server.listen(timeout=2)
    server_port = listener._servers[0].sockets[0].getsockname()[1]

    async def connect(*, endpoint_hostname, pin):
        security = taps.SecurityParameters()
        security.add_trust_ca(str(ROOT_CERTIFICATE))
        security.add_pinned_server_certificate(str(pin))
        preconnection = taps.Preconnection(
            remote_endpoints=[
                (
                    taps.RemoteEndpoint()
                    .with_hostname(endpoint_hostname)
                    .with_address("127.0.0.1")
                    .with_port(server_port)
                )
            ],
            transport_properties=tls_properties,
            security_parameters=security,
        )
        return await preconnection.initiate(timeout=2)

    connection = await connect(
        endpoint_hostname="localhost",
        pin=SERVER_CERTIFICATE,
    )
    assert connection.protocol == "tls-tcp"
    connection.close()
    await connection.wait_closed(timeout=2)

    with pytest.raises(ssl.SSLCertVerificationError):
        await connect(
            endpoint_hostname="wrong.example",
            pin=SERVER_CERTIFICATE,
        )

    with pytest.raises(ssl.SSLCertVerificationError, match="pinned"):
        await connect(
            endpoint_hostname="localhost",
            pin=ROOT_CERTIFICATE,
        )

    await listener.stop()
