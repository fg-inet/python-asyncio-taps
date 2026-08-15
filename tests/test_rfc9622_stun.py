"""RFC 9622 Section 7.3: Resolve discovers NAT bindings via STUN (RFC 8489)."""
import asyncio
import os
import socket
import struct

import pytest

import pytaps as taps
from pytaps import stun


class StunServerProtocol(asyncio.DatagramProtocol):
    """A Binding-Request-only STUN server, enough to exercise the client."""

    def __init__(self, *, password=None, xor=True, respond=True, error=None):
        self.password = password
        self.xor = xor
        self.respond = respond
        self.error = error
        self.requests = []
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        message_type, transaction_id, attributes = stun.parse_message(data)
        self.requests.append((message_type, attributes, addr))
        if not self.respond:
            return
        if self.error is not None:
            body = self._error_attribute(self.error)
            self.transport.sendto(
                self._message(stun.BINDING_ERROR, transaction_id, body),
                addr,
            )
            return
        body = self._address_attribute(addr, transaction_id)
        self.transport.sendto(
            self._message(stun.BINDING_SUCCESS, transaction_id, body),
            addr,
        )

    @staticmethod
    def _message(message_type, transaction_id, body):
        return (
            struct.pack("!HHI", message_type, len(body), stun.MAGIC_COOKIE)
            + transaction_id
            + body
        )

    @staticmethod
    def _error_attribute(code):
        value = struct.pack("!HBB", 0, code // 100, code % 100) + b"Nope"
        return stun._attribute(stun.ATTR_ERROR_CODE, value)

    def _address_attribute(self, addr, transaction_id):
        address, port = addr[0], addr[1]
        packed = socket.inet_pton(
            socket.AF_INET6 if ":" in address else socket.AF_INET,
            address.split("%")[0],
        )
        family = stun._FAMILY_IPV6 if ":" in address else stun._FAMILY_IPV4
        if self.xor:
            port ^= stun.MAGIC_COOKIE >> 16
            mask = struct.pack("!I", stun.MAGIC_COOKIE) + transaction_id
            packed = bytes(b ^ m for b, m in zip(packed, mask))
            attr = stun.ATTR_XOR_MAPPED_ADDRESS
        else:
            attr = stun.ATTR_MAPPED_ADDRESS
        return stun._attribute(
            attr,
            struct.pack("!BBH", 0, family, port) + packed,
        )


async def _stun_server(**kwargs):
    loop = asyncio.get_running_loop()
    protocol = StunServerProtocol(**kwargs)
    transport, _ = await loop.create_datagram_endpoint(
        lambda: protocol,
        local_addr=("127.0.0.1", 0),
    )
    return transport, protocol, transport.get_extra_info("sockname")[1]


# --- message encoding ---


def test_rfc8489_binding_request_is_well_formed():
    transaction_id = os.urandom(12)
    request = stun.build_binding_request(transaction_id, fingerprint=False)

    message_type, parsed_id, attributes = stun.parse_message(request)

    assert message_type == stun.BINDING_REQUEST
    assert parsed_id == transaction_id
    assert attributes == {}
    # Two leading zero bits and the magic cookie.
    assert struct.unpack("!H", request[:2])[0] & 0xC000 == 0
    assert struct.unpack("!I", request[4:8])[0] == stun.MAGIC_COOKIE


def test_rfc8489_fingerprint_and_credentials_are_attached():
    transaction_id = os.urandom(12)
    request = stun.build_binding_request(
        transaction_id,
        credentials=("alice", "secret"),
    )

    _type, _id, attributes = stun.parse_message(request)

    assert attributes[stun.ATTR_USERNAME] == b"alice"
    assert len(attributes[stun.ATTR_MESSAGE_INTEGRITY]) == 20
    assert len(attributes[stun.ATTR_FINGERPRINT]) == 4


def test_rfc8489_rejects_a_foreign_magic_cookie():
    bogus = struct.pack("!HHI", stun.BINDING_REQUEST, 0, 0xDEADBEEF) + os.urandom(12)

    with pytest.raises(stun.StunError, match="magic cookie"):
        stun.parse_message(bogus)


def test_rfc8489_transaction_id_must_be_96_bits():
    with pytest.raises(stun.StunError, match="96 bits"):
        stun.build_binding_request(b"short")


# --- the exchange ---


@pytest.mark.asyncio
@pytest.mark.parametrize("xor", [True, False])
async def test_rfc8489_discovers_the_reflexive_address(xor):
    transport, protocol, port = await _stun_server(xor=xor)
    try:
        server = taps.LocalEndpoint().with_stun_server("127.0.0.1", port)
        address, mapped_port, local_port = await stun.discover_reflexive_address(
            server.stun_server
        )

        assert address == "127.0.0.1"
        # On loopback the mapping is the socket's own address and port.
        assert mapped_port == local_port
        assert protocol.requests, "the server saw no Binding Request"
    finally:
        transport.close()


@pytest.mark.asyncio
async def test_rfc8489_binding_is_discovered_for_a_chosen_local_port():
    """The mapping only describes the port it was learned on."""
    transport, _protocol, port = await _stun_server()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            chosen = probe.getsockname()[1]

        server = taps.LocalEndpoint().with_stun_server("127.0.0.1", port)
        _address, mapped_port, local_port = await stun.discover_reflexive_address(
            server.stun_server,
            local_address="127.0.0.1",
            local_port=chosen,
        )

        assert local_port == chosen
        assert mapped_port == chosen
    finally:
        transport.close()


@pytest.mark.asyncio
async def test_rfc8489_server_error_is_surfaced():
    transport, _protocol, port = await _stun_server(error=401)
    try:
        server = taps.LocalEndpoint().with_stun_server("127.0.0.1", port)
        with pytest.raises(stun.StunError, match="401"):
            await stun.discover_reflexive_address(server.stun_server)
    finally:
        transport.close()


@pytest.mark.asyncio
async def test_rfc8489_retransmits_then_gives_up():
    transport, protocol, port = await _stun_server(respond=False)
    try:
        server = taps.LocalEndpoint().with_stun_server("127.0.0.1", port)
        with pytest.raises(stun.StunError, match="No STUN Binding Response"):
            await stun.discover_reflexive_address(
                server.stun_server,
                attempts=3,
                initial_rto=0.01,
            )
        # Section 6.2.1 of RFC 8489: the request is retransmitted, not sent once.
        assert len(protocol.requests) == 3
    finally:
        transport.close()


# --- Section 7.3: Resolve returns local and server-reflexive candidates ---


@pytest.mark.asyncio
async def test_section_7_3_resolve_returns_reflexive_candidates():
    transport, _protocol, port = await _stun_server()
    try:
        host_candidate = taps.LocalEndpoint().with_address("127.0.0.1").with_port(0)
        stun_candidate = taps.LocalEndpoint().with_stun_server("127.0.0.1", port)
        preconnection = taps.Preconnection(
            local_endpoints=[host_candidate, stun_candidate],
            remote_endpoints=[],
        )

        locals_, remotes = await preconnection.resolve()

        assert remotes == []
        reflexive = [
            endpoint
            for endpoint in locals_
            if getattr(endpoint, "reflexive_local_port", None) is not None
        ]
        assert len(reflexive) == 1, locals_
        candidate = reflexive[0]
        assert candidate.address == "127.0.0.1"
        assert candidate.port == candidate.reflexive_local_port
        # The discovered candidate is what gets signalled, so it must not still
        # carry the STUN server it was learned from.
        assert candidate.stun_server is None
        # The host candidate survives alongside it.
        assert any(
            getattr(endpoint, "reflexive_local_port", None) is None
            for endpoint in locals_
        )
    finally:
        transport.close()


@pytest.mark.asyncio
async def test_section_7_3_unreachable_stun_server_leaves_host_candidates():
    """Resolve degrades to host candidates rather than failing outright."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]

    stun_candidate = taps.LocalEndpoint().with_stun_server("127.0.0.1", dead_port)
    preconnection = taps.Preconnection(
        local_endpoints=[
            taps.LocalEndpoint().with_address("127.0.0.1").with_port(0),
            stun_candidate,
        ],
        remote_endpoints=[],
    )

    original = stun.discover_reflexive_address

    async def fast_discovery(server, **kwargs):
        kwargs.update(attempts=2, initial_rto=0.01)
        return await original(server, **kwargs)

    import pytaps.preconnection as preconnection_module

    preconnection_module.discover_reflexive_address = fast_discovery
    try:
        locals_, _remotes = await preconnection.resolve()
    finally:
        preconnection_module.discover_reflexive_address = original

    # Resolve still succeeds; the addressed host candidate is unaffected, and
    # the undiscoverable STUN candidate simply contributes nothing.
    assert [endpoint.address for endpoint in locals_] == ["127.0.0.1"]
    assert all(
        getattr(endpoint, "reflexive_local_port", None) is None
        for endpoint in locals_
    )


@pytest.mark.asyncio
async def test_section_7_3_resolved_candidates_feed_add_remote():
    """The peer's resolved candidates are added with AddRemote."""
    transport, _protocol, port = await _stun_server()
    try:
        preconnection = taps.Preconnection(
            local_endpoints=[
                taps.LocalEndpoint().with_stun_server("127.0.0.1", port)
            ],
            remote_endpoints=[],
        )
        locals_, _remotes = await preconnection.resolve()
        # A Local Endpoint that only names a STUN server resolves to its
        # discovered binding, not to an address-less placeholder.
        assert len(locals_) == 1
        assert all(candidate.address is not None for candidate in locals_)

        # Signalled to the peer out of band, then offered back as remotes.
        for candidate in locals_:
            preconnection.add_remote_endpoint(
                taps.RemoteEndpoint()
                .with_address(candidate.address)
                .with_port(candidate.port)
            )

        assert len(preconnection.remote_endpoints) == len(locals_)
    finally:
        transport.close()


@pytest.mark.external_network
@pytest.mark.asyncio
async def test_section_7_3_public_stun_server_returns_a_binding():
    """Against a real STUN deployment, not our own server."""
    server = taps.LocalEndpoint().with_stun_server("stun.l.google.com", 19302)
    try:
        address, port, local_port = await stun.discover_reflexive_address(
            server.stun_server,
            attempts=2,
            initial_rto=1.0,
        )
    except (stun.StunError, OSError) as exc:
        pytest.skip(f"public STUN server unreachable: {exc}")

    assert port > 0 and local_port > 0
    import ipaddress

    ipaddress.ip_address(address)
