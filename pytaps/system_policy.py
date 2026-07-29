import asyncio
import ctypes
import inspect
import ipaddress
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

try:
    import netifaces
except ImportError:
    netifaces = None


@dataclass(frozen=True)
class SystemPolicySnapshot:
    """One System Policy view with complete mappings for supplied sections."""

    source: str = "system"
    interfaces: Mapping[str, Mapping] | None = None
    protocols: Mapping[str, Mapping] | None = None
    pvds: Mapping[str, Mapping] | None = None
    address_families: Mapping[str, int | float] | None = None
    observed_at: float = field(default_factory=time.time)

    def as_dict(self):
        return {
            "source": self.source,
            "interfaces": (
                {
                    interface_id: dict(policy)
                    for interface_id, policy in self.interfaces.items()
                }
                if self.interfaces is not None
                else None
            ),
            "protocols": (
                {
                    protocol: dict(policy)
                    for protocol, policy in self.protocols.items()
                }
                if self.protocols is not None
                else None
            ),
            "pvds": (
                {
                    pvd_id: dict(policy)
                    for pvd_id, policy in self.pvds.items()
                }
                if self.pvds is not None
                else None
            ),
            "addressFamilies": (
                dict(self.address_families)
                if self.address_families is not None
                else None
            ),
            "observedAt": self.observed_at,
        }


class SystemPolicyProvider:
    """Implementation hook for retrieving current dynamic System Policy."""

    def snapshot(self):
        raise NotImplementedError

    def create_event_source(self, *, loop=None):
        return None


class SystemPolicyEventSource:
    """Asynchronous invalidation source for a System Policy provider."""

    name = "system-policy-events"

    async def wait_for_change(self):
        raise NotImplementedError

    def drain(self):
        return 0

    def close(self):
        return None


class InterfacePolicyResolver:
    """Optional native enrichment for policies associated with interfaces."""

    source = "interface-policy"

    def snapshot(self):
        return {}

    def create_event_source(self, *, loop=None):
        return None


class DarwinNetworkPolicyResolver(InterfacePolicyResolver):
    """Cache authoritative path policy supplied by Apple Network.framework."""

    source = "darwin-network-framework"

    def __init__(self):
        self._paths = {}

    @staticmethod
    def _merge_policy(current, incoming):
        merged = dict(current)
        for key, value in incoming.items():
            if key in {
                "constrained",
                "expensive",
                "hasDNS",
                "isDefaultPath",
                "metered",
                "supportsIPv4",
                "supportsIPv6",
                "ultraConstrained",
            }:
                merged[key] = bool(merged.get(key)) or bool(value)
            elif key == "pathStatus":
                if value == "satisfied" or key not in merged:
                    merged[key] = value
            elif (
                key == "interfaceType"
                and merged.get(key) not in {None, "other"}
            ):
                continue
            else:
                merged[key] = value
        return merged

    def update_path(self, path_id, interface_policies):
        normalized = {
            str(interface_id): dict(policy)
            for interface_id, policy in interface_policies.items()
        }
        if self._paths.get(path_id) == normalized:
            return False
        self._paths[path_id] = normalized
        return True

    def clear(self):
        self._paths.clear()

    def snapshot(self):
        interfaces = {}
        for path_id in sorted(self._paths):
            for interface_id, policy in self._paths[path_id].items():
                interfaces[interface_id] = self._merge_policy(
                    interfaces.get(interface_id, {}),
                    policy,
                )
        for policy in interfaces.values():
            policy["policySource"] = self.source
            if (
                policy.get("expensive")
                or policy.get("constrained")
                or policy.get("ultraConstrained")
            ):
                policy["relativeCost"] = "high"
        return interfaces

    def create_event_source(self, *, loop=None):
        return DarwinNetworkPathEventSource.open(
            self,
            loop=loop,
        )


class DarwinNetworkPathEventSource(SystemPolicyEventSource):
    """Apple Network.framework path policy and change notifications."""

    name = "darwin-network-path"

    _INTERFACE_TYPE_NAMES = (
        ("wifi", "nw_interface_type_wifi"),
        ("cellular", "nw_interface_type_cellular"),
        ("wired", "nw_interface_type_wired"),
        ("loopback", "nw_interface_type_loopback"),
        ("other", "nw_interface_type_other"),
    )

    def __init__(
        self,
        policy_resolver,
        *,
        loop,
        network_module,
        dispatch_queue,
    ):
        self.policy_resolver = policy_resolver
        self.loop = loop
        self.network = network_module
        self.dispatch_queue = dispatch_queue
        self.closed = False
        self._events = asyncio.Queue()
        self._monitors = []
        self._handlers = []
        self._load_network_symbols()
        self._start_monitors()

    def _load_network_symbols(self):
        self._create_monitor = self.network.nw_path_monitor_create
        self._create_monitor_with_type = self._optional_symbol(
            self.network,
            "nw_path_monitor_create_with_type",
        )
        self._set_update_handler = (
            self.network.nw_path_monitor_set_update_handler
        )
        self._set_queue = self.network.nw_path_monitor_set_queue
        self._start_monitor = self.network.nw_path_monitor_start
        self._cancel_monitor = self.network.nw_path_monitor_cancel
        self._enumerate_interfaces = (
            self.network.nw_path_enumerate_interfaces
        )
        self._get_interface_name = (
            self.network.nw_interface_get_name
        )
        self._get_interface_type = (
            self.network.nw_interface_get_type
        )
        self._get_path_status = self.network.nw_path_get_status
        self._interface_types = {
            interface_type: name
            for name, constant_name in self._INTERFACE_TYPE_NAMES
            if (
                interface_type := self._optional_symbol(
                    self.network,
                    constant_name,
                )
            )
            is not None
        }
        statuses = (
            ("satisfied", "nw_path_status_satisfied"),
            ("unsatisfied", "nw_path_status_unsatisfied"),
            ("satisfiable", "nw_path_status_satisfiable"),
            ("invalid", "nw_path_status_invalid"),
        )
        self._path_statuses = {
            status: name
            for name, constant_name in statuses
            if (
                status := self._optional_symbol(
                    self.network,
                    constant_name,
                )
            )
            is not None
        }
        self._path_flags = {
            name: self._optional_symbol(self.network, name)
            for name in (
                "nw_path_is_expensive",
                "nw_path_is_constrained",
                "nw_path_is_ultra_constrained",
                "nw_path_has_ipv4",
                "nw_path_has_ipv6",
                "nw_path_has_dns",
            )
        }

    @staticmethod
    def _optional_symbol(module, name):
        try:
            return getattr(module, name)
        except (AttributeError, KeyError):
            return None

    @staticmethod
    def _create_dispatch_queue(objc_module):
        dispatch = ctypes.CDLL(None)
        dispatch.dispatch_get_global_queue.argtypes = [
            ctypes.c_long,
            ctypes.c_ulong,
        ]
        dispatch.dispatch_get_global_queue.restype = ctypes.c_void_p
        pointer = dispatch.dispatch_get_global_queue(0, 0)
        if not pointer:
            raise RuntimeError("Could not acquire a native dispatch queue")
        return objc_module.objc_object(c_void_p=pointer)

    @classmethod
    def open(
        cls,
        policy_resolver,
        *,
        loop=None,
        network_module=None,
        objc_module=None,
        queue_factory=None,
    ):
        if not sys.platform.startswith("darwin"):
            return None
        try:
            if network_module is None:
                import Network as network_module
            if objc_module is None:
                import objc as objc_module
        except ImportError:
            return None
        loop = loop or asyncio.get_running_loop()
        dispatch_queue = (
            queue_factory()
            if queue_factory is not None
            else cls._create_dispatch_queue(objc_module)
        )
        return cls(
            policy_resolver,
            loop=loop,
            network_module=network_module,
            dispatch_queue=dispatch_queue,
        )

    def _monitor_specs(self):
        specs = [("default", self._create_monitor)]
        if self._create_monitor_with_type is None:
            return specs
        for interface_type, name in self._interface_types.items():
            specs.append(
                (
                    name,
                    lambda value=interface_type: (
                        self._create_monitor_with_type(value)
                    ),
                )
            )
        return specs

    def _make_handler(self, path_id):
        def handle(path):
            if self.closed:
                return
            try:
                policies = self._policies_for_path(path, path_id)
            # PyObjC terminates the process if a Python exception escapes a
            # native block, so every callback failure becomes a queue event.
            except BaseException as error:
                if not self.loop.is_closed():
                    try:
                        self.loop.call_soon_threadsafe(
                            self._publish_error,
                            error,
                        )
                    except RuntimeError:
                        pass
                return
            if not self.loop.is_closed():
                try:
                    self.loop.call_soon_threadsafe(
                        self._publish,
                        path_id,
                        policies,
                    )
                except RuntimeError:
                    pass

        return handle

    def _start_monitors(self):
        try:
            for path_id, create_monitor in self._monitor_specs():
                monitor = create_monitor()
                if monitor is None:
                    continue
                handler = self._make_handler(path_id)
                self._set_update_handler(
                    monitor,
                    handler,
                )
                self._set_queue(
                    monitor,
                    self.dispatch_queue,
                )
                self._monitors.append(monitor)
                self._handlers.append(handler)
                self._start_monitor(monitor)
        except BaseException:
            self.close()
            raise
        if not self._monitors:
            raise RuntimeError("No Apple network path monitors were available")

    @staticmethod
    def _decode_name(name):
        if isinstance(name, bytes):
            return name.decode("utf-8", errors="replace")
        return str(name)

    def _interface_type_name(self, interface_type):
        return self._interface_types.get(interface_type, "other")

    def _path_status_name(self, status):
        return self._path_statuses.get(status, "unknown")

    def _optional_path_flag(self, name, path):
        function = self._path_flags.get(name)
        if function is None:
            return False
        try:
            return bool(function(path))
        except (AttributeError, TypeError, ValueError):
            return False

    def _policies_for_path(self, path, path_id):
        interfaces = {}
        callback_errors = []

        def add_interface(interface):
            try:
                interface_id = self._decode_name(
                    self._get_interface_name(interface)
                )
                interface_type = self._interface_type_name(
                    self._get_interface_type(interface)
                )
                interfaces[interface_id] = interface_type
            except BaseException as error:
                callback_errors.append(error)
                return False
            return True

        self._enumerate_interfaces(path, add_interface)
        if callback_errors:
            raise RuntimeError(
                f"Could not inspect path interface: {callback_errors[0]}"
            ) from callback_errors[0]
        expensive = self._optional_path_flag(
            "nw_path_is_expensive",
            path,
        )
        constrained = self._optional_path_flag(
            "nw_path_is_constrained",
            path,
        )
        common = {
            "pathStatus": self._path_status_name(
                self._get_path_status(path)
            ),
            "expensive": expensive,
            "metered": expensive,
            "constrained": constrained,
            "ultraConstrained": self._optional_path_flag(
                "nw_path_is_ultra_constrained",
                path,
            ),
            "supportsIPv4": self._optional_path_flag(
                "nw_path_has_ipv4",
                path,
            ),
            "supportsIPv6": self._optional_path_flag(
                "nw_path_has_ipv6",
                path,
            ),
            "hasDNS": self._optional_path_flag(
                "nw_path_has_dns",
                path,
            ),
            "isDefaultPath": path_id == "default",
        }
        return {
            interface_id: {
                **common,
                "interfaceType": interface_type,
            }
            for interface_id, interface_type in interfaces.items()
        }

    def _publish(self, path_id, policies):
        if self.closed:
            return
        if not self.policy_resolver.update_path(path_id, policies):
            return
        self._events.put_nowait(
            {
                "source": self.name,
                "pathMonitor": path_id,
                "interfaces": sorted(policies),
            }
        )

    def _publish_error(self, error):
        if self.closed:
            return
        self._events.put_nowait(
            RuntimeError(
                f"Apple network path callback failed: {error}"
            )
        )

    async def wait_for_change(self):
        if self.closed:
            raise RuntimeError("System Policy event source is closed")
        event = await self._events.get()
        if isinstance(event, BaseException):
            raise event
        return event

    def drain(self):
        drained = 0
        while True:
            try:
                self._events.get_nowait()
            except asyncio.QueueEmpty:
                return drained
            drained += 1

    def close(self):
        if self.closed:
            return
        self.closed = True
        for monitor in self._monitors:
            try:
                self._cancel_monitor(monitor)
            except (AttributeError, RuntimeError):
                pass
        self._monitors.clear()
        self._handlers.clear()
        self.policy_resolver.clear()


class NetworkManagerPolicyResolver(InterfacePolicyResolver):
    """Read authoritative Linux interface metering from NetworkManager."""

    source = "networkmanager"
    _FIELDS = (
        "GENERAL.DEVICE,"
        "GENERAL.TYPE,"
        "GENERAL.STATE,"
        "GENERAL.NM-MANAGED,"
        "GENERAL.METERED"
    )

    def __init__(self, *, command=None, runner=None, timeout=1.0):
        self.command = command or shutil.which("nmcli")
        self.runner = runner or subprocess.run
        self.timeout = float(timeout)

    @staticmethod
    def _unescape(value):
        result = []
        escaped = False
        for character in value:
            if escaped:
                result.append(character)
                escaped = False
            elif character == "\\":
                escaped = True
            else:
                result.append(character)
        if escaped:
            result.append("\\")
        return "".join(result)

    @classmethod
    def parse(cls, output):
        records = []
        current = {}
        for raw_line in str(output).splitlines():
            line = raw_line.strip()
            if not line:
                continue
            key, separator, value = line.partition(":")
            if not separator:
                continue
            if key == "GENERAL.DEVICE" and current:
                records.append(current)
                current = {}
            current[key] = cls._unescape(value)
        if current:
            records.append(current)

        interfaces = {}
        for record in records:
            interface_id = record.get("GENERAL.DEVICE")
            if not interface_id:
                continue
            policy = {
                "policySource": cls.source,
            }
            interface_type = record.get("GENERAL.TYPE")
            if interface_type:
                policy["interfaceType"] = interface_type

            state = record.get("GENERAL.STATE", "")
            state_code = None
            try:
                state_code = int(state.split(maxsplit=1)[0])
            except (ValueError, IndexError):
                pass
            if state:
                policy["networkManagerState"] = state
            if state_code is not None:
                policy["networkManagerStateCode"] = state_code
                if state_code == 20:
                    policy["available"] = False

            managed = record.get("GENERAL.NM-MANAGED", "").lower()
            if managed in {"yes", "no"}:
                policy["managed"] = managed == "yes"

            metered_value = record.get("GENERAL.METERED", "").lower()
            if metered_value and metered_value != "unknown":
                metered = (
                    metered_value.startswith("yes")
                    or metered_value.startswith("guess-yes")
                )
                policy["metered"] = metered
                policy["expensive"] = metered
                if "guess" in metered_value or "guessed" in metered_value:
                    policy["meteredSource"] = "guessed"
                else:
                    policy["meteredSource"] = "configured"
                if metered:
                    policy["relativeCost"] = "high"
            interfaces[interface_id] = policy
        return interfaces

    def snapshot(self):
        if not self.command:
            return {}
        completed = self.runner(
            [
                self.command,
                "--terse",
                "--mode",
                "multiline",
                "--fields",
                self._FIELDS,
                "device",
                "show",
            ],
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=False,
        )
        if completed.returncode:
            message = completed.stderr.strip() or "nmcli failed"
            raise RuntimeError(message)
        return self.parse(completed.stdout)


class NativeRouteEventSource(SystemPolicyEventSource):
    """Route-socket invalidations for interface, address, and route changes."""

    _LINUX_ROUTE_GROUPS = (
        0x0001  # RTMGRP_LINK
        | 0x0010  # RTMGRP_IPV4_IFADDR
        | 0x0040  # RTMGRP_IPV4_ROUTE
        | 0x0100  # RTMGRP_IPV6_IFADDR
        | 0x0400  # RTMGRP_IPV6_ROUTE
    )

    def __init__(self, event_socket, *, name, loop=None):
        self.socket = event_socket
        self.name = name
        self.loop = loop
        self.closed = False

    @classmethod
    def open(cls, *, loop=None):
        if sys.platform.startswith("linux"):
            family = getattr(socket, "AF_NETLINK", None)
            if family is None:
                return None
            event_socket = socket.socket(
                family,
                socket.SOCK_RAW,
                getattr(socket, "NETLINK_ROUTE", 0),
            )
            try:
                event_socket.bind((0, cls._LINUX_ROUTE_GROUPS))
                event_socket.setblocking(False)
            except BaseException:
                event_socket.close()
                raise
            return cls(
                event_socket,
                name="linux-netlink-route",
                loop=loop,
            )

        route_platforms = (
            "darwin",
            "freebsd",
            "openbsd",
            "netbsd",
            "dragonfly",
        )
        if sys.platform.startswith(route_platforms):
            family = getattr(
                socket,
                "PF_ROUTE",
                getattr(socket, "AF_ROUTE", None),
            )
            if family is None:
                return None
            event_socket = socket.socket(
                family,
                socket.SOCK_RAW,
                socket.AF_UNSPEC,
            )
            try:
                event_socket.setblocking(False)
            except BaseException:
                event_socket.close()
                raise
            return cls(
                event_socket,
                name="bsd-routing-socket",
                loop=loop,
            )
        return None

    async def wait_for_change(self):
        if self.closed:
            raise RuntimeError("System Policy event source is closed")
        loop = self.loop or asyncio.get_running_loop()
        payload = await loop.sock_recv(self.socket, 65535)
        if not payload:
            raise ConnectionError("System Policy route socket closed")
        return {
            "source": self.name,
            "bytes": len(payload),
        }

    def drain(self):
        if self.closed:
            return 0
        drained = 0
        while True:
            try:
                payload = self.socket.recv(65535)
            except (BlockingIOError, InterruptedError):
                break
            if not payload:
                break
            drained += 1
        return drained

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.socket.close()


class PortableInterfacePolicyProvider(SystemPolicyProvider):
    """Discover usable local addresses without assigning platform-specific cost."""

    def __init__(self, *, source="portable-interfaces"):
        self.source = source

    @staticmethod
    def _addresses(interface_id):
        addresses = []
        family_specs = (
            (netifaces.AF_INET, "ipv4"),
            (netifaces.AF_INET6, "ipv6"),
        )
        try:
            interface_addresses = netifaces.ifaddresses(interface_id)
        except ValueError:
            return addresses
        for family, family_name in family_specs:
            for entry in interface_addresses.get(family, []):
                raw_address = entry.get("addr")
                if not raw_address:
                    continue
                address, separator, scope_id = raw_address.partition("%")
                try:
                    parsed = ipaddress.ip_address(address)
                except ValueError:
                    continue
                if parsed.is_unspecified or parsed.is_multicast:
                    continue
                candidate = {
                    "address": str(parsed),
                    "family": family_name,
                    "isLoopback": parsed.is_loopback,
                    "isLinkLocal": parsed.is_link_local,
                }
                if separator:
                    candidate["scopeId"] = scope_id
                if candidate not in addresses:
                    addresses.append(candidate)
        return addresses

    def snapshot(self):
        if netifaces is None:
            raise ImportError(
                "Portable interface policy requires the 'netifaces' package."
            )

        try:
            indexes = {
                interface_id: index
                for index, interface_id in socket.if_nameindex()
            }
        except (AttributeError, OSError):
            indexes = {}
        interfaces = {}
        for interface_id in netifaces.interfaces():
            addresses = self._addresses(interface_id)
            is_loopback = (
                bool(addresses)
                and all(address["isLoopback"] for address in addresses)
            )
            interfaces[interface_id] = {
                "available": bool(addresses),
                "preferenceAdjustment": -10 if is_loopback else 0,
                "pvdId": None,
                "supportsTemporaryAddress": True,
                "relativeCost": "normal",
                "index": indexes.get(interface_id),
                "isLoopback": is_loopback,
                "addresses": addresses,
            }
        return SystemPolicySnapshot(
            source=self.source,
            interfaces=interfaces,
        )


class NativeSystemPolicyProvider(PortableInterfacePolicyProvider):
    """Enrich portable interface discovery with native route and link state."""

    _UNAVAILABLE_STATES = {
        "down",
        "dormant",
        "lowerlayerdown",
        "notpresent",
    }

    def __init__(
        self,
        *,
        source="native-routes",
        link_state_reader=None,
        cost_resolver=None,
        platform_policy_resolver=None,
    ):
        super().__init__(source=source)
        self.link_state_reader = (
            link_state_reader or self._read_linux_link_state
        )
        self.cost_resolver = cost_resolver
        if platform_policy_resolver is False:
            platform_policy_resolver = None
        elif platform_policy_resolver is None:
            platform_policy_resolver = (
                self._default_platform_policy_resolver()
            )
        self.platform_policy_resolver = platform_policy_resolver
        self.last_platform_policy_error = None
        self.last_platform_event_source_error = None
        self.last_platform_event_source_name = None

    def create_event_source(self, *, loop=None):
        self.last_platform_event_source_error = None
        self.last_platform_event_source_name = None
        if self.platform_policy_resolver is not None:
            source_name = getattr(
                self.platform_policy_resolver,
                "source",
                type(self.platform_policy_resolver).__name__,
            )
            try:
                event_source = (
                    self.platform_policy_resolver.create_event_source(
                        loop=loop,
                    )
                )
            except Exception as error:
                self.last_platform_event_source_error = error
                self.last_platform_event_source_name = source_name
            else:
                if event_source is not None:
                    return event_source
        return NativeRouteEventSource.open(loop=loop)

    @staticmethod
    def _default_platform_policy_resolver():
        if sys.platform.startswith("darwin"):
            return DarwinNetworkPolicyResolver()
        if sys.platform.startswith("linux") and shutil.which("nmcli"):
            return NetworkManagerPolicyResolver()
        return None

    def _platform_policy_snapshot(self):
        if self.platform_policy_resolver is None:
            return {}
        try:
            policies = self.platform_policy_resolver.snapshot()
            if inspect.isawaitable(policies):
                raise TypeError(
                    "Interface policy resolver snapshots must be synchronous"
                )
            if not isinstance(policies, Mapping):
                raise TypeError(
                    "Interface policy resolvers must return a mapping"
                )
        except Exception as error:
            self.last_platform_policy_error = error
            return {}
        self.last_platform_policy_error = None
        return {
            str(interface_id): dict(policy)
            for interface_id, policy in policies.items()
        }

    @staticmethod
    def _read_linux_link_state(interface_id):
        """Read Linux operstate when sysfs is available."""
        path = Path("/sys/class/net") / interface_id / "operstate"
        try:
            return path.read_text(encoding="ascii").strip().lower()
        except (OSError, UnicodeError):
            return None

    @staticmethod
    def _route_entry(family_name, route, *, is_default=False):
        if not isinstance(route, (tuple, list)) or len(route) < 2:
            return None
        gateway, interface_id = route[:2]
        if not interface_id:
            return None
        return str(interface_id), {
            "family": family_name,
            "gateway": str(gateway) if gateway else None,
            "default": bool(is_default),
        }

    @classmethod
    def _routes_by_interface(cls):
        family_specs = (
            (netifaces.AF_INET, "ipv4"),
            (netifaces.AF_INET6, "ipv6"),
        )
        try:
            gateway_table = netifaces.gateways()
        except (AttributeError, OSError, ValueError):
            return {}

        default_routes = gateway_table.get("default", {})
        routes = {}
        for family, family_name in family_specs:
            default_route = cls._route_entry(
                family_name,
                default_routes.get(family),
                is_default=True,
            )
            if default_route is not None:
                interface_id, entry = default_route
                routes.setdefault(interface_id, []).append(entry)

            for route in gateway_table.get(family, ()):
                normalized = cls._route_entry(
                    family_name,
                    route,
                    is_default=(
                        len(route) > 2 and bool(route[2])
                    ),
                )
                if normalized is None:
                    continue
                interface_id, entry = normalized
                interface_routes = routes.setdefault(interface_id, [])
                matching = next(
                    (
                        existing
                        for existing in interface_routes
                        if (
                            existing["family"] == entry["family"]
                            and existing["gateway"] == entry["gateway"]
                        )
                    ),
                    None,
                )
                if matching is None:
                    interface_routes.append(entry)
                elif entry["default"]:
                    matching["default"] = True
        return routes

    @staticmethod
    def _network_id(interface_id, routes):
        default_routes = sorted(
            (
                route
                for route in routes
                if route["default"] and route["gateway"]
            ),
            key=lambda route: (route["family"], route["gateway"]),
        )
        if not default_routes:
            return interface_id
        route_identity = ",".join(
            f"{route['family']}={route['gateway']}"
            for route in default_routes
        )
        return f"{interface_id}|{route_identity}"

    def _apply_cost_policy(self, interface_id, policy):
        if self.cost_resolver is None:
            return
        resolved = self.cost_resolver(interface_id, dict(policy))
        if resolved is None:
            return
        if isinstance(resolved, str):
            policy["relativeCost"] = resolved.lower()
            return
        if not isinstance(resolved, Mapping):
            raise TypeError(
                "System Policy cost resolvers must return a mapping, "
                "relative-cost string, or None"
            )
        policy.update(resolved)

    def snapshot(self):
        portable = super().snapshot()
        routes_by_interface = self._routes_by_interface()
        platform_policies = self._platform_policy_snapshot()
        interfaces = {}
        default_families = set()

        for interface_id, portable_policy in portable.interfaces.items():
            policy = dict(portable_policy)
            routes = [
                dict(route)
                for route in routes_by_interface.get(interface_id, ())
            ]
            default_routes = [
                route for route in routes if route["default"]
            ]
            default_route_families = sorted(
                {route["family"] for route in default_routes}
            )
            default_gateways = {
                route["family"]: route["gateway"]
                for route in default_routes
                if route["gateway"]
            }
            default_families.update(default_route_families)

            operational_state = self.link_state_reader(interface_id)
            if operational_state is not None:
                operational_state = str(operational_state).strip().lower()
            if operational_state in self._UNAVAILABLE_STATES:
                policy["available"] = False

            is_default_route = bool(default_routes)
            if is_default_route and policy["available"]:
                policy["preferenceAdjustment"] += 2
            policy.update(
                {
                    "operationalState": operational_state,
                    "isDefaultRoute": is_default_route,
                    "defaultRouteFamilies": default_route_families,
                    "defaultGateways": default_gateways,
                    "routes": routes,
                    "networkId": self._network_id(
                        interface_id,
                        routes,
                    ),
                }
            )
            policy.update(
                platform_policies.get(interface_id, {})
            )
            self._apply_cost_policy(interface_id, policy)
            interfaces[interface_id] = policy

        interfaces = dict(
            sorted(
                interfaces.items(),
                key=lambda item: (
                    not item[1]["isDefaultRoute"],
                    item[1]["isLoopback"],
                    item[1]["index"] is None,
                    item[1]["index"] or 0,
                    item[0],
                ),
            )
        )
        return SystemPolicySnapshot(
            source=self.source,
            interfaces=interfaces,
            address_families={
                family: 1 if family in default_families else 0
                for family in ("ipv4", "ipv6")
            },
        )


class SystemPolicyMonitor:
    """Refresh a ConnectionContext from provider events and periodic polling."""

    def __init__(
        self,
        connection_context,
        provider=None,
        *,
        loop=None,
        interval=5.0,
        debounce_interval=0.05,
    ):
        interval = float(interval)
        if interval <= 0:
            raise ValueError("System Policy refresh interval must be positive")
        debounce_interval = float(debounce_interval)
        if debounce_interval < 0:
            raise ValueError(
                "System Policy debounce interval cannot be negative"
            )
        self.connection_context = connection_context
        self.provider = provider or NativeSystemPolicyProvider()
        self.loop = loop
        self.interval = interval
        self.debounce_interval = debounce_interval
        self.last_error = None
        self.last_event_source_error = None
        self.last_event = None
        self.last_trigger = None
        self.refresh_count = 0
        self._event_source = None
        self._task = None

    @property
    def running(self):
        return self._task is not None and not self._task.done()

    @property
    def event_driven(self):
        return self._event_source is not None

    @property
    def event_source_name(self):
        if self._event_source is None:
            return None
        return getattr(
            self._event_source,
            "name",
            type(self._event_source).__name__,
        )

    async def refresh(self, *, trigger="manual"):
        snapshot = self.provider.snapshot()
        if inspect.isawaitable(snapshot):
            snapshot = await snapshot
        changed = self.connection_context.apply_system_policy(snapshot)
        self.last_error = None
        self.last_trigger = trigger
        self.refresh_count += 1
        return changed

    def _provider_name(self):
        return getattr(
            self.provider,
            "source",
            type(self.provider).__name__,
        )

    def _notify_event_source_error(
        self,
        error,
        event_source=None,
        *,
        source=None,
    ):
        self.last_event_source_error = error
        if source is None:
            source = (
                getattr(event_source, "name", None)
                if event_source is not None
                else None
            )
        self.connection_context._notify_subscribers(
            "system_policy_event_source_error",
            source=source or self._provider_name(),
            error=str(error),
        )

    async def _open_event_source(self):
        factory = getattr(self.provider, "create_event_source", None)
        if factory is None:
            return None
        event_source = factory(loop=self.loop)
        if inspect.isawaitable(event_source):
            event_source = await event_source
        if event_source is not None:
            self.last_event_source_error = None
        platform_error = getattr(
            self.provider,
            "last_platform_event_source_error",
            None,
        )
        if platform_error is not None:
            self._notify_event_source_error(
                platform_error,
                source=getattr(
                    self.provider,
                    "last_platform_event_source_name",
                    None,
                ),
            )
        return event_source

    async def _close_event_source(self, event_source):
        if event_source is None:
            return
        try:
            result = event_source.close()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._notify_event_source_error(error, event_source)

    async def _refresh_safely(self, trigger):
        try:
            await self.refresh(trigger=trigger)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.last_error = error
            self.connection_context._notify_subscribers(
                "system_policy_error",
                source=self._provider_name(),
                error=str(error),
            )

    async def _drain_event_source(self, event_source):
        if self.debounce_interval:
            await asyncio.sleep(self.debounce_interval)
        result = event_source.drain()
        if inspect.isawaitable(result):
            await result

    async def _run(self):
        event_source = None
        try:
            try:
                event_source = await self._open_event_source()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._notify_event_source_error(error)
            self._event_source = event_source

            trigger = "start"
            while True:
                await self._refresh_safely(trigger)
                if event_source is None:
                    await asyncio.sleep(self.interval)
                    trigger = "poll"
                    continue

                try:
                    event = await asyncio.wait_for(
                        event_source.wait_for_change(),
                        timeout=self.interval,
                    )
                except asyncio.TimeoutError:
                    trigger = "poll"
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._notify_event_source_error(error, event_source)
                    await self._close_event_source(event_source)
                    event_source = None
                    self._event_source = None
                    await asyncio.sleep(self.interval)
                    trigger = "poll"
                    continue

                self.last_event = event
                trigger = "event"
                try:
                    await self._drain_event_source(event_source)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._notify_event_source_error(error, event_source)
                    await self._close_event_source(event_source)
                    event_source = None
                    self._event_source = None
        finally:
            await self._close_event_source(event_source)
            self._event_source = None

    def start(self):
        if self.running:
            return self._task
        loop = self.loop or asyncio.get_running_loop()
        self.loop = loop
        self._task = loop.create_task(self._run())
        return self._task

    async def stop(self):
        if self._task is None:
            return
        task = self._task
        self._task = None
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
