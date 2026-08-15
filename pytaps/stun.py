"""Minimal STUN client for NAT binding discovery (RFC 8489).

Section 7.3 of RFC 9622 lets the Resolve action discover NAT bindings so that a
Rendezvous can offer server-reflexive candidates to its peer. This module
implements the Binding Request/Response exchange needed for that, including the
short-term credential mechanism when the application supplies credentials.
"""
import asyncio
import binascii
import hashlib
import hmac
import ipaddress
import os
import socket
import struct

from .utility import setup_logger

logger = setup_logger(__name__, "blue")

MAGIC_COOKIE = 0x2112A442
_HEADER_LENGTH = 20

BINDING_REQUEST = 0x0001
BINDING_SUCCESS = 0x0101
BINDING_ERROR = 0x0111

ATTR_MAPPED_ADDRESS = 0x0001
ATTR_USERNAME = 0x0006
ATTR_MESSAGE_INTEGRITY = 0x0008
ATTR_ERROR_CODE = 0x0009
ATTR_XOR_MAPPED_ADDRESS = 0x0020
ATTR_FINGERPRINT = 0x8028

_FAMILY_IPV4 = 0x01
_FAMILY_IPV6 = 0x02

# Section 6.2.1 of RFC 8489: retransmit a Binding Request starting at RTO,
# doubling each time. The defaults here stop well short of the 39.5 s the RFC
# allows, because Resolve is an interactive call.
DEFAULT_INITIAL_RTO = 0.5
DEFAULT_ATTEMPTS = 3


class StunError(Exception):
    """A STUN transaction failed."""


def _pad(value):
    remainder = len(value) % 4
    return value if remainder == 0 else value + b"\x00" * (4 - remainder)


def _attribute(attr_type, value):
    return struct.pack("!HH", attr_type, len(value)) + _pad(value)


def _credentials(credentials):
    """Normalize the opaque credentials object into (username, password)."""
    if credentials is None:
        return None, None
    if isinstance(credentials, dict):
        return credentials.get("username"), credentials.get("password")
    if isinstance(credentials, (tuple, list)) and len(credentials) == 2:
        return credentials[0], credentials[1]
    raise StunError(
        "STUN credentials must be a (username, password) pair or a mapping"
    )


def _with_length(message, extra):
    """Rewrite the header length as if `extra` bytes were already appended."""
    length = len(message) - _HEADER_LENGTH + extra
    return message[:2] + struct.pack("!H", length) + message[4:]


def build_binding_request(transaction_id, *, credentials=None, fingerprint=True):
    """Build a Binding Request, optionally authenticated and fingerprinted."""
    if len(transaction_id) != 12:
        raise StunError("STUN transaction IDs are 96 bits")
    message = struct.pack("!HHI", BINDING_REQUEST, 0, MAGIC_COOKIE)
    message += transaction_id

    username, password = _credentials(credentials)
    if username is not None:
        message += _attribute(ATTR_USERNAME, str(username).encode("utf-8"))
    if password is not None:
        # Short-term credentials: HMAC-SHA1 keyed with the password over the
        # message whose length already counts the MESSAGE-INTEGRITY attribute.
        digest = hmac.new(
            str(password).encode("utf-8"),
            _with_length(message, 24),
            hashlib.sha1,
        ).digest()
        message += _attribute(ATTR_MESSAGE_INTEGRITY, digest)
    if fingerprint:
        crc = binascii.crc32(_with_length(message, 8)) & 0xFFFFFFFF
        message += _attribute(
            ATTR_FINGERPRINT,
            struct.pack("!I", crc ^ 0x5354554E),
        )
    return _with_length(message, 0)


def parse_message(data):
    """Parse a STUN message into (message_type, transaction_id, attributes)."""
    if len(data) < _HEADER_LENGTH:
        raise StunError("STUN message is shorter than its header")
    message_type, length, cookie = struct.unpack("!HHI", data[:8])
    if cookie != MAGIC_COOKIE:
        raise StunError("STUN magic cookie mismatch")
    if message_type & 0xC000:
        raise StunError("STUN messages start with two zero bits")
    transaction_id = data[8:_HEADER_LENGTH]
    body = data[_HEADER_LENGTH:_HEADER_LENGTH + length]
    if len(body) != length:
        raise StunError("STUN message length does not match the payload")

    attributes = {}
    offset = 0
    while offset + 4 <= len(body):
        attr_type, attr_length = struct.unpack("!HH", body[offset:offset + 4])
        offset += 4
        value = body[offset:offset + attr_length]
        if len(value) != attr_length:
            raise StunError("Truncated STUN attribute")
        attributes.setdefault(attr_type, value)
        offset += attr_length
        offset += (4 - attr_length % 4) % 4
    return message_type, transaction_id, attributes


def _decode_address(value, transaction_id, *, xored):
    if len(value) < 4:
        raise StunError("Truncated STUN address attribute")
    family = value[1]
    port = struct.unpack("!H", value[2:4])[0]
    address = value[4:]
    if xored:
        port ^= MAGIC_COOKIE >> 16
        mask = struct.pack("!I", MAGIC_COOKIE) + transaction_id
        address = bytes(
            byte ^ mask_byte for byte, mask_byte in zip(address, mask)
        )
    if family == _FAMILY_IPV4:
        if len(address) != 4:
            raise StunError("Malformed IPv4 STUN address")
        return str(ipaddress.IPv4Address(address)), port
    if family == _FAMILY_IPV6:
        if len(address) != 16:
            raise StunError("Malformed IPv6 STUN address")
        return str(ipaddress.IPv6Address(address)), port
    raise StunError(f"Unknown STUN address family: {family}")


def reflexive_address(attributes, transaction_id):
    """Return the (address, port) this transaction was seen to come from."""
    if ATTR_XOR_MAPPED_ADDRESS in attributes:
        return _decode_address(
            attributes[ATTR_XOR_MAPPED_ADDRESS],
            transaction_id,
            xored=True,
        )
    if ATTR_MAPPED_ADDRESS in attributes:
        return _decode_address(
            attributes[ATTR_MAPPED_ADDRESS],
            transaction_id,
            xored=False,
        )
    raise StunError("Binding response carried no mapped address")


def _error_reason(attributes):
    value = attributes.get(ATTR_ERROR_CODE)
    if not value or len(value) < 4:
        return "unspecified STUN error"
    code = value[2] * 100 + value[3]
    reason = value[4:].decode("utf-8", "replace")
    return f"{code} {reason}".strip()


class _BindingProtocol(asyncio.DatagramProtocol):
    def __init__(self, transaction_id, future):
        self.transaction_id = transaction_id
        self.future = future

    def datagram_received(self, data, addr):
        if self.future.done():
            return
        try:
            message_type, transaction_id, attributes = parse_message(data)
        except StunError:
            return  # Not for us; keep waiting.
        if transaction_id != self.transaction_id:
            return
        if message_type == BINDING_ERROR:
            self.future.set_exception(
                StunError(f"STUN server rejected the request: "
                          f"{_error_reason(attributes)}")
            )
            return
        if message_type != BINDING_SUCCESS:
            return
        try:
            self.future.set_result(
                reflexive_address(attributes, transaction_id)
            )
        except StunError as exc:
            self.future.set_exception(exc)

    def error_received(self, exc):
        if not self.future.done():
            self.future.set_exception(exc)


async def discover_reflexive_address(
    stun_server,
    *,
    local_address=None,
    local_port=0,
    loop=None,
    attempts=DEFAULT_ATTEMPTS,
    initial_rto=DEFAULT_INITIAL_RTO,
):
    """Discover the server-reflexive transport address of a local port.

    Returns ``(reflexive_address, reflexive_port, local_port)``. The local port
    is reported so the caller can bind the same port for the Connection the
    binding was discovered for; a mapping only describes the port it was
    learned on.
    """
    loop = loop or asyncio.get_running_loop()
    transaction_id = os.urandom(12)
    request = build_binding_request(
        transaction_id,
        credentials=getattr(stun_server, "credentials", None),
    )

    server_address = str(stun_server.address)
    server_port = int(stun_server.port)
    infos = await loop.getaddrinfo(
        server_address,
        server_port,
        type=socket.SOCK_DGRAM,
    )
    if not infos:
        raise StunError(f"Could not resolve STUN server {server_address}")
    family, _type, _proto, _canon, server_sockaddr = infos[0]

    # Always bind explicitly: the caller needs to know which local port the
    # discovered mapping describes, and asyncio would otherwise defer binding
    # until the first send.
    wildcard = "0.0.0.0" if family == socket.AF_INET else "::"
    local_addr = (local_address or wildcard, local_port or 0)

    future = loop.create_future()
    transport, _protocol = await loop.create_datagram_endpoint(
        lambda: _BindingProtocol(transaction_id, future),
        local_addr=local_addr,
        family=family,
        reuse_port=True,
    )
    try:
        bound_port = transport.get_extra_info("sockname")[1]
        timeout = initial_rto
        last_error = None
        for attempt in range(max(1, attempts)):
            transport.sendto(request, server_sockaddr)
            try:
                address, port = await asyncio.wait_for(
                    asyncio.shield(future),
                    timeout,
                )
                logger.info(
                    "STUN binding on local port %s is %s:%s",
                    bound_port,
                    address,
                    port,
                )
                return address, port, bound_port
            except (asyncio.TimeoutError, TimeoutError) as exc:
                last_error = exc
                timeout *= 2  # Section 6.2.1 of RFC 8489.
                continue
        raise StunError(
            f"No STUN Binding Response from {server_address}:{server_port} "
            f"after {attempts} attempts"
        ) from last_error
    finally:
        transport.close()
