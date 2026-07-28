import ipaddress
import socket
from copy import deepcopy
from dataclasses import dataclass


@dataclass(frozen=True)
class StunServer:
    address: str
    port: int
    credentials: object = None


class Endpoint:
    """Identity and constraints for one RFC 9622 network Endpoint."""

    def __init__(self):
        self.interface = None
        self.port = None
        self.service = None
        self.address = None
        self.host_name = None
        self.protocol = None
        self.multicast_group = None
        self.multicast_source = None
        self.hop_limit = None
        self.stun_server = None

    def with_port(self, port):
        """Set the 16-bit port identifier."""
        if isinstance(port, bool):
            raise ValueError("Port must be an Integer between 0 and 65535")
        if isinstance(port, int):
            normalized = port
        elif isinstance(port, str):
            try:
                normalized = int(port)
            except ValueError as exc:
                raise ValueError(
                    "Port must be an Integer between 0 and 65535"
                ) from exc
        else:
            raise ValueError("Port must be an Integer between 0 and 65535")
        if normalized < 0 or normalized > 65535:
            raise ValueError("Port must be an Integer between 0 and 65535")
        self.port = normalized
        return self

    def with_hostname(self, hostname):
        """Set the hostname identifier."""
        if not isinstance(hostname, str) or not hostname.strip():
            raise ValueError("Hostname must be a non-empty string")
        self.host_name = hostname.strip()
        return self

    def with_service(self, service):
        """Set an IANA service name or DNS SRV service identifier."""
        if not isinstance(service, str) or not service.strip():
            raise ValueError("Service must be a non-empty string")
        self.service = service.strip()
        return self

    def with_ip_address(self, address):
        """Set one IPv4 or IPv6 address identifier."""
        address_value = address
        if isinstance(address, str) and "%" in address:
            address_value, scope_id = address.rsplit("%", 1)
            if self.interface is not None and self.interface != scope_id:
                raise ValueError(
                    "IPv6 scope ID conflicts with the Endpoint interface"
                )
            self.with_interface(scope_id)
        try:
            normalized = str(ipaddress.ip_address(address_value))
        except ValueError as exc:
            raise ValueError(f"Invalid IP address: {address}") from exc
        self.address = normalized
        return self

    def with_address(self, address):
        """Python-friendly alias for :meth:`with_ip_address`."""
        return self.with_ip_address(address)

    def without_address(self, address=None):
        if address is None or self.address == str(address):
            self.address = None
        return self

    def with_protocol(self, protocol):
        """Restrict this Endpoint to one transport protocol identifier."""
        if not isinstance(protocol, str) or not protocol.strip():
            raise ValueError("Protocol must be a non-empty string")
        self.protocol = protocol.strip().lower()
        return self

    def with_interface(self, interface):
        if not isinstance(interface, str) or not interface.strip():
            raise ValueError("Interface must be a non-empty string")
        self.interface = interface.strip()
        return self

    def without_interface(self, interface=None):
        if interface is None or self.interface == interface:
            self.interface = None
        return self

    def with_hop_limit(self, hop_limit):
        if (
            not isinstance(hop_limit, int)
            or isinstance(hop_limit, bool)
            or hop_limit < 0
            or hop_limit > 255
        ):
            raise ValueError("Hop limit must be an Integer between 0 and 255")
        self.hop_limit = hop_limit
        return self

    def effective_port(self, protocol=None):
        if self.port is not None:
            return self.port
        if self.service is None:
            return None
        socket_protocol = "udp" if protocol in {"udp", "quic"} else "tcp"
        try:
            return socket.getservbyname(self.service, socket_protocol)
        except OSError as exc:
            raise ValueError(f"Unknown service: {self.service}") from exc

    def effective_address(self):
        return self.multicast_group or self.address

    def socket_address(self):
        """Return the IP literal in the form expected by socket APIs."""
        address = self.effective_address()
        if address is None or self.interface is None or "%" in address:
            return address
        parsed = ipaddress.ip_address(address)
        if parsed.version == 6 and (parsed.is_link_local or parsed.is_multicast):
            return f"{address}%{self.interface}"
        return address

    @property
    def is_multicast(self):
        return self.multicast_group is not None

    def clone(self):
        new_endpoint = self.__class__()
        new_endpoint.interface = self.interface
        new_endpoint.port = self.port
        new_endpoint.service = self.service
        new_endpoint.address = self.address
        new_endpoint.host_name = self.host_name
        new_endpoint.protocol = self.protocol
        new_endpoint.multicast_group = self.multicast_group
        new_endpoint.multicast_source = self.multicast_source
        new_endpoint.hop_limit = self.hop_limit
        new_endpoint.stun_server = deepcopy(self.stun_server)
        return new_endpoint

    def __repr__(self):
        identifiers = []
        for name in (
            "host_name",
            "port",
            "service",
            "address",
            "interface",
            "protocol",
            "multicast_group",
            "multicast_source",
            "hop_limit",
            "stun_server",
        ):
            value = getattr(self, name)
            if value is not None:
                identifiers.append(f"{name}={value!r}")
        return f"{self.__class__.__name__}({', '.join(identifiers)})"


class LocalEndpoint(Endpoint):
    """A local Endpoint and its binding constraints."""

    def with_stun_server(self, address, port, credentials=None):
        self.stun_server = StunServer(
            address=str(address),
            port=Endpoint().with_port(port).port,
            credentials=credentials,
        )
        return self

    def with_any_source_multicast_group_ip(self, group_address):
        self.multicast_group = _normalize_multicast_address(group_address)
        self.multicast_source = None
        return self

    def with_single_source_multicast_group_ip(
        self,
        group_address,
        source_address,
    ):
        self.multicast_group = _normalize_multicast_address(group_address)
        source = ipaddress.ip_address(source_address)
        if source.is_multicast:
            raise ValueError("Multicast source must be a unicast IP address")
        self.multicast_source = str(source)
        return self


class RemoteEndpoint(Endpoint):
    """A remote Endpoint to resolve or contact."""

    def with_multicast_group_ip(self, group_address):
        self.multicast_group = _normalize_multicast_address(group_address)
        return self


def _normalize_multicast_address(address):
    normalized = ipaddress.ip_address(address)
    if not normalized.is_multicast:
        raise ValueError(f"Not a multicast IP address: {address}")
    return str(normalized)
