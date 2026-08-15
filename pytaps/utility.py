import asyncio
import contextvars
import datetime
import logging
import socket
import warnings
from dataclasses import dataclass
from enum import Enum

from pytaps.transportProperties import (
    PreferenceLevel,
    canonicalize_property_name,
    get_protocol_capabilities,
    get_protocols,
)

_CURRENT_CANDIDATE_VIEW = contextvars.ContextVar(
    "pytaps_current_candidate_view",
    default=None,
)


def current_task_or_none():
    """Return the running Task, or None when there is no running loop.

    Cleanup paths use the current Task only to avoid cancelling themselves.
    They can also run while a loop is being torn down, where asking for the
    current Task raises instead of answering.
    """
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None

# Contiguous addresses of the leading family to try before alternating, the
# "First Address Family Count" of Section 4 of RFC 8305.
FIRST_ADDRESS_FAMILY_COUNT = 1

colors = {
    "red": "\x1b[31;1m",
    "green": "\x1b[32;1m",
    "yellow": "\x1b[33;1m",
    "blue": "\x1b[34;1m",
    "magenta": "\x1b[35;1m",
    "cyan": "\x1b[36;1m",
    "grey": "\x1b[37;1m",
    "white": "\x1b[38;1m"
}


class ConnectionState(Enum):
    ESTABLISHING = 0
    ESTABLISHED = 1
    CLOSING = 2
    CLOSED = 3


def print_time(msg="", color="red"):
    warnings.warn("\x1b[31;1mprint_time is deprecated, switch to a logger (e.g. with setup_logger()).\x1b[0m", DeprecationWarning, 2)
    print(str(datetime.datetime.now()) + ": " + msg, color)


def color_emit(emit):
    def new(*args):
        level = args[0].levelno
        if level == logging.CRITICAL:
            color = "red"
        elif level == logging.WARNING:
            color = "yellow"
        elif level == logging.INFO:
            color = "green"
        else:
            color = "white"
        args[0].levelname = f'{colors[color]}{args[0].levelname}\x1b[0m'
        return emit(*args)
    return new


def setup_logger(module, color="white"):
    logger = logging.getLogger(module)
    if getattr(logger, "_pytaps_configured", False):
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(f'%(asctime)s - {colors[color]}%(name)s \x1b[0m- %(levelname)s: %(message)s'))
    ch.emit = color_emit(ch.emit)
    logger.addHandler(ch)
    logger._pytaps_configured = True
    return logger


def schedule_callback(loop, callback, *arg_variants):
    if callback is None:
        return False

    if not arg_variants:
        arg_variants = ((),)

    last_error = None
    for args in arg_variants:
        try:
            loop.create_task(callback(*args))
            return True
        except TypeError as exc:
            last_error = exc
            continue

    if last_error is not None:
        raise last_error
    return False


@dataclass(frozen=True)
class Candidate:
    protocol: str
    remote_address: str
    address_family: int
    path: str = "default"
    local_address: str | None = None
    local_endpoint: object | None = None
    remote_endpoint: object | None = None
    branch_id: str | None = None
    resolution_source: str = "configured"
    resolution_duration: float | None = None


@dataclass(frozen=True)
class CandidateBranch:
    protocol: str
    path: str
    local_endpoint: object | None
    remote_endpoint: object
    branch_id: str


def _preference_weight(property_name, transport_properties):
    if property_name in transport_properties.get_explicit_selection_properties():
        return 2
    return 1


def _supports_selection_property(protocol, property_name, property_value):
    if property_name not in protocol:
        return None
    supported_value = protocol[property_name]
    if property_name == "multipath" and isinstance(property_value, str):
        return property_value == "Disabled" or supported_value is not False
    if property_name == "advertisesAltaddr":
        return bool(supported_value) if property_value else True
    return supported_value is not False


def _protocol_details(protocol_name, transport_properties=None):
    return get_protocol_capabilities(protocol_name, transport_properties)


def rank_protocol_candidates(
    transport_properties,
    connection_context=None,
    available_protocols=None,
):
    """Rank protocol branches per RFC 9623 sorting guidance."""
    available_protocols = (
        set(available_protocols)
        if available_protocols is not None
        else None
    )
    ranked_protocols = []
    for protocol in get_protocols(transport_properties):
        if (
            available_protocols is not None
            and protocol["name"] not in available_protocols
        ):
            continue
        if (
            protocol["name"] == "udp"
            and transport_properties.get("direction")
            != "Unidirectional Receive"
            and transport_properties.get_profile_message_properties().get(
                "safelyReplayable"
            )
            is not True
        ):
            continue
        quic_datagram_mode = (
            protocol["name"] == "quic"
            and transport_properties.get("_pytaps.quicTransportMode")
            == "Datagram"
        )
        if (
            connection_context is not None
            and connection_context.protocol_policy.get(protocol["name"], {}).get("available") is False
        ):
            continue
        prefer_score = 0
        avoid_score = 0
        excluded = False

        for property_name, property_value in transport_properties.selection_properties.items():
            canonical_property = canonicalize_property_name(property_name)
            if canonical_property in {"direction", "interface", "pvd"}:
                continue
            if (
                quic_datagram_mode
                and canonical_property
                in {
                    "reliability",
                    "preserveMsgBoundaries",
                    "perMsgReliability",
                    "preserveOrder",
                }
                and canonical_property
                not in transport_properties.explicit_selection_properties
            ):
                continue

            support = _supports_selection_property(protocol, canonical_property, property_value)
            if support is None:
                continue
            if canonical_property in {
                "advertisesAltaddr",
            }:
                if not support:
                    excluded = True
                    break
                continue
            if canonical_property == "multipath":
                if property_value != "Disabled" and support:
                    prefer_score += _preference_weight(
                        canonical_property,
                        transport_properties,
                    )
                continue

            weight = _preference_weight(canonical_property, transport_properties)
            if property_value is PreferenceLevel.PROHIBIT and support:
                excluded = True
                break
            if property_value is PreferenceLevel.REQUIRE and not support:
                excluded = True
                break
            if property_value is PreferenceLevel.PREFER and support:
                prefer_score += weight
            if property_value is PreferenceLevel.AVOID and support:
                avoid_score += weight

        if not excluded:
            cache_score = (
                connection_context.get_protocol_score(protocol["name"])
                if connection_context is not None else 0
            )
            ranked_protocols.append((protocol, prefer_score, avoid_score, cache_score))

    ranked_protocols.sort(
        key=lambda value: (
            -value[1],
            -value[3],
            value[2],
            value[0]["name"],
        )
    )
    return ranked_protocols


def describe_unsatisfiable_properties(
    transport_properties,
    connection_context=None,
    available_protocols=None,
):
    """Explain why no protocol matches, or return None when one does.

    Section 3.1 of RFC 9623 asks for a Property set that no available protocol
    can satisfy to be reported during preestablishment, as early as possible,
    rather than after resources have been allocated for an attempt that cannot
    succeed.
    """
    if rank_protocol_candidates(
        transport_properties,
        connection_context=connection_context,
        available_protocols=available_protocols,
    ):
        return None

    protocols = [
        protocol
        for protocol in get_protocols(transport_properties)
        if available_protocols is None
        or protocol["name"] in available_protocols
    ]
    if not protocols:
        return "no transport protocol is available"

    unmet_requirements = []
    unavoidable_prohibitions = []
    for name, value in transport_properties.selection_properties.items():
        canonical = canonicalize_property_name(name)
        if canonical in {"direction", "interface", "pvd"}:
            continue
        if value is PreferenceLevel.REQUIRE:
            if not any(
                _supports_selection_property(protocol, canonical, value)
                for protocol in protocols
            ):
                unmet_requirements.append(canonical)
        elif value is PreferenceLevel.PROHIBIT:
            if all(
                _supports_selection_property(protocol, canonical, value)
                for protocol in protocols
            ):
                unavoidable_prohibitions.append(canonical)

    reasons = []
    if unmet_requirements:
        reasons.append(
            "no available protocol provides required "
            + ", ".join(sorted(unmet_requirements))
        )
    if unavoidable_prohibitions:
        reasons.append(
            "every available protocol provides prohibited "
            + ", ".join(sorted(unavoidable_prohibitions))
        )
    if not reasons:
        # Each constraint is individually satisfiable, so the combination the
        # application asked for is what no single protocol offers.
        explicit = transport_properties.get_explicit_selection_properties()
        constraints = []
        for name in sorted(explicit):
            value = transport_properties.selection_properties.get(name)
            if value is PreferenceLevel.REQUIRE:
                constraints.append(f"require {name}")
            elif value is PreferenceLevel.PROHIBIT:
                constraints.append(f"prohibit {name}")
        reasons.append(
            "no single available protocol satisfies "
            + ", ".join(constraints)
            if constraints
            else "no available protocol satisfies the Transport Properties"
        )
    return "; ".join(reasons)


def _rank_path_candidate(
    local_endpoint,
    transport_properties,
    connection_context,
    index,
):
    if local_endpoint is None or not local_endpoint.interface:
        performance_score = (
            connection_context.get_network_performance_score(
                connection_context.get_network_id(
                    local_address=(
                        local_endpoint.effective_address()
                        if local_endpoint is not None
                        else None
                    )
                )
            )
            if connection_context is not None
            else 0
        )
        return (
            "default",
            None,
            local_endpoint,
            0,
            0,
            0,
            performance_score,
            index,
        )
    interface_preferences = {
        interface_id: preference
        for preference, interface_id in transport_properties.selection_properties.get("interface", set())
    }

    required_interfaces = {
        interface_id
        for interface_id, preference in interface_preferences.items()
        if preference is PreferenceLevel.REQUIRE
    }
    prohibited_interfaces = {
        interface_id
        for interface_id, preference in interface_preferences.items()
        if preference is PreferenceLevel.PROHIBIT
    }

    interface_id = local_endpoint.interface
    if interface_id in prohibited_interfaces:
        return None
    if required_interfaces and interface_id not in required_interfaces:
        return None

    policy = (
        connection_context.interface_policy.get(interface_id, {})
        if connection_context is not None else {}
    )
    if policy.get("available") is False:
        return None

    pvd_preferences = {
        pvd_id: preference
        for preference, pvd_id in transport_properties.selection_properties.get("pvd", set())
    }
    required_pvds = {
        pvd_id
        for pvd_id, preference in pvd_preferences.items()
        if preference is PreferenceLevel.REQUIRE
    }
    prohibited_pvds = {
        pvd_id
        for pvd_id, preference in pvd_preferences.items()
        if preference is PreferenceLevel.PROHIBIT
    }
    interface_pvd = policy.get("pvdId")
    if interface_pvd in prohibited_pvds:
        return None
    if required_pvds and interface_pvd not in required_pvds:
        return None

    preference = interface_preferences.get(interface_id, PreferenceLevel.IGNORE)
    prefer_score = 1 if preference is PreferenceLevel.PREFER else 0
    avoid_score = 1 if preference is PreferenceLevel.AVOID else 0
    system_score = policy.get("preferenceAdjustment", 0)

    if policy.get("relativeCost") == "low":
        system_score += 1
    elif policy.get("relativeCost") == "high":
        system_score -= 1

    if (
        transport_properties.selection_properties.get("useTemporaryLocalAddress")
        is PreferenceLevel.PREFER
        and policy
        and not policy.get("supportsTemporaryAddress", True)
    ):
        system_score -= 1

    if interface_pvd in pvd_preferences:
        pvd_preference = pvd_preferences[interface_pvd]
        if pvd_preference is PreferenceLevel.PREFER:
            system_score += 2
        elif pvd_preference is PreferenceLevel.AVOID:
            system_score -= 2

    if connection_context is not None and interface_pvd in connection_context.pvd_policy:
        pvd_policy = connection_context.pvd_policy[interface_pvd]
        if not pvd_policy.get("available", True):
            return None
        system_score += pvd_policy.get("preferenceAdjustment", 0)

    performance_score = (
        connection_context.get_network_performance_score(
            connection_context.get_network_id(
                interface_id,
                local_address=local_endpoint.effective_address(),
            )
        )
        if connection_context is not None
        else 0
    )
    return (
        interface_id,
        interface_id,
        local_endpoint,
        prefer_score,
        avoid_score,
        system_score,
        performance_score,
        index,
    )


def _rank_path_candidates_with_endpoints(
    local_endpoints,
    transport_properties,
    connection_context=None,
):
    if local_endpoints is None:
        local_endpoints = [None]
    elif not isinstance(local_endpoints, (list, tuple)):
        local_endpoints = [local_endpoints]

    ranked = []
    for index, local_endpoint in enumerate(local_endpoints):
        candidate = _rank_path_candidate(
            local_endpoint,
            transport_properties,
            connection_context,
            index,
        )
        if candidate is not None:
            ranked.append(candidate)
    ranked.sort(
        key=lambda candidate: (
            -(candidate[3] - candidate[4] + candidate[5]),
            -candidate[3],
            candidate[4],
            -candidate[6],
            candidate[7],
        )
    )
    return [
        (path, path_interface, endpoint)
        for (
            path,
            path_interface,
            endpoint,
            _prefer_score,
            _avoid_score,
            _system_score,
            _performance_score,
            _index,
        ) in ranked
    ]


def rank_path_candidates(local_endpoint, transport_properties, connection_context=None):
    """Rank one or more RFC 9622 Local Endpoint candidates."""
    return [
        (path, path_interface)
        for path, path_interface, _endpoint in _rank_path_candidates_with_endpoints(
            local_endpoint,
            transport_properties,
            connection_context=connection_context,
        )
    ]


def _interleave_address_families(ordered, first_address_family_count):
    """Interleave the two address families of an ordered address list.

    Section 4 of RFC 8305 asks the first address family to be followed by an
    address of the other family, so that a long run of one family cannot stall
    establishment when connectivity over that family is impaired.
    ``first_address_family_count`` is the number of contiguous addresses of the
    leading family to attempt before alternating.
    """
    if len(ordered) < 2:
        return list(ordered)
    leading_family = ordered[0][0]
    primary = [entry for entry in ordered if entry[0] == leading_family]
    secondary = [entry for entry in ordered if entry[0] != leading_family]
    if not secondary:
        return list(ordered)

    interleaved = []
    take = max(1, first_address_family_count)
    while primary or secondary:
        for _ in range(take):
            if primary:
                interleaved.append(primary.pop(0))
        take = 1
        if secondary:
            interleaved.append(secondary.pop(0))
    return interleaved


def order_remote_addresses(
    remote_addrs,
    connection_context=None,
    *,
    first_address_family_count=None,
):
    """Order resolved Remote Endpoint addresses for staggered racing.

    Addresses are ranked by System Policy and address family preference, then
    the two families are interleaved following Section 4 of RFC 8305, which
    Section 4.3.2 of RFC 9623 points at for racing between IP addresses.
    """
    def sort_key(entry):
        family, address = entry
        family_name = "ipv6" if family == socket.AddressFamily.AF_INET6 else "ipv4"
        family_rank = 1 if family_name == "ipv6" else 0
        policy_adjustment = (
            connection_context.address_family_policy.get(family_name, 0)
            if connection_context is not None else 0
        )
        return (-policy_adjustment, -family_rank, address)

    ordered = sorted(remote_addrs, key=sort_key)
    if first_address_family_count is None:
        first_address_family_count = FIRST_ADDRESS_FAMILY_COUNT
    return _interleave_address_families(ordered, first_address_family_count)


def build_protocol_candidates(
    transport_properties,
    connection_context=None,
    available_protocols=None,
):
    return [
        protocol_info[0]["name"]
        for protocol_info in rank_protocol_candidates(
            transport_properties,
            connection_context=connection_context,
            available_protocols=available_protocols,
        )
    ]


def build_candidate_branches(
    connection,
    *,
    local_endpoints=None,
    available_protocols=None,
):
    """Build unresolved path -> protocol -> endpoint branches."""
    if local_endpoints is None:
        local_endpoints = getattr(connection, "local_endpoints", None) or [None]
    ordered_paths = _rank_path_candidates_with_endpoints(
        local_endpoints,
        connection.transport_properties,
        connection_context=connection.connection_context,
    )
    ordered_protocols = build_protocol_candidates(
        connection.transport_properties,
        connection_context=connection.connection_context,
        available_protocols=available_protocols,
    )

    branches = []
    for path_index, (path_label, _path_interface, local_endpoint) in enumerate(
        ordered_paths
    ):
        for protocol_index, protocol_name in enumerate(ordered_protocols):
            for endpoint_index, remote_endpoint in enumerate(
                connection.remote_endpoints
            ):
                if (
                    remote_endpoint.protocol is not None
                    and remote_endpoint.protocol != protocol_name
                ):
                    continue
                if remote_endpoint.is_multicast and protocol_name != "udp":
                    continue
                branch_id = (
                    f"path-{path_index}:protocol-{protocol_index}:"
                    f"endpoint-{endpoint_index}"
                )
                branches.append(
                    CandidateBranch(
                        protocol=protocol_name,
                        path=path_label,
                        local_endpoint=(
                            local_endpoint.clone()
                            if local_endpoint is not None
                            else None
                        ),
                        remote_endpoint=remote_endpoint.clone(),
                        branch_id=branch_id,
                    )
                )
    return branches


def create_candidates(
    connection,
    remote_addrs=None,
    available_protocols=None,
    local_endpoints=None,
):
    """Build leaf candidates ordered as path -> protocol -> endpoint."""
    if remote_addrs is None:
        remote_addrs = []

    if local_endpoints is None:
        local_endpoints = (
            getattr(connection, "local_endpoints", None) or [None]
        )
    ordered_paths = _rank_path_candidates_with_endpoints(
        local_endpoints,
        connection.transport_properties,
        connection_context=connection.connection_context,
    )
    ordered_protocols = build_protocol_candidates(
        connection.transport_properties,
        connection_context=connection.connection_context,
        available_protocols=available_protocols,
    )

    candidates = []
    for path_label, _path_interface, local_endpoint in ordered_paths:
        for protocol_name in ordered_protocols:
            protocol_remotes = []
            for remote_entry in remote_addrs:
                if len(remote_entry) == 2:
                    family, remote_address = remote_entry
                    remote_endpoint = connection.remote_endpoint
                else:
                    family, remote_address, remote_endpoint = remote_entry
                if (
                    remote_endpoint is not None
                    and remote_endpoint.protocol is not None
                    and remote_endpoint.protocol != protocol_name
                ):
                    continue
                if (
                    remote_endpoint is not None
                    and remote_endpoint.is_multicast
                    and protocol_name != "udp"
                ):
                    continue
                protocol_remotes.append(
                    (family, remote_address, remote_endpoint)
                )
            for family, remote_address, remote_endpoint in list(
                protocol_remotes
            ):
                for alt_family, alt_remote in (
                    connection.connection_context.get_alternate_remotes(
                        remote_address,
                        protocol=protocol_name,
                    )
                ):
                    resolved_family = alt_family
                    if resolved_family is None:
                        resolved_family = (
                            socket.AddressFamily.AF_INET6
                            if ":" in alt_remote else socket.AddressFamily.AF_INET
                        )
                    protocol_remotes.append(
                        (resolved_family, alt_remote, remote_endpoint)
                    )
            ordered_remotes = order_remote_addresses(
                [
                    (family, remote_address)
                    for family, remote_address, _endpoint in protocol_remotes
                ],
                connection_context=connection.connection_context,
            )
            address_order = {}
            for index, key in enumerate(ordered_remotes):
                address_order.setdefault(key, index)
            protocol_remotes = sorted(
                protocol_remotes,
                key=lambda entry: address_order[(entry[0], entry[1])],
            )
            seen = set()
            deduped_remotes = []
            for family, remote_address, remote_endpoint in protocol_remotes:
                remote_port = (
                    remote_endpoint.effective_port(protocol_name)
                    if remote_endpoint is not None
                    else None
                )
                key = (family, remote_address, remote_port, protocol_name)
                if key in seen:
                    continue
                seen.add(key)
                deduped_remotes.append(
                    (family, remote_address, remote_endpoint)
                )
            for family, remote_address, remote_endpoint in deduped_remotes:
                candidate_remote = (
                    remote_endpoint.clone()
                    if remote_endpoint is not None
                    else None
                )
                if candidate_remote is not None:
                    candidate_remote.address = remote_address
                    candidate_remote.port = candidate_remote.effective_port(
                        protocol_name
                    )
                candidate_local = (
                    local_endpoint.clone()
                    if local_endpoint is not None
                    else None
                )
                local_address = (
                    candidate_local.address
                    if candidate_local is not None
                    else None
                )
                if (
                    local_address is not None
                    and (":" in local_address)
                    != (family == socket.AddressFamily.AF_INET6)
                ):
                    continue
                candidates.append(
                    Candidate(
                        protocol=protocol_name,
                        remote_address=remote_address,
                        address_family=family,
                        path=path_label,
                        local_address=local_address,
                        local_endpoint=candidate_local,
                        remote_endpoint=candidate_remote,
                    )
                )
    return candidates


def order_candidates_for_racing(connection, candidates):
    protocol_order = {}
    path_order = {}
    for index, candidate in enumerate(candidates):
        protocol_order.setdefault(candidate.protocol, len(protocol_order))
        path_order.setdefault(candidate.path, len(path_order))

    def sort_key(item):
        index, candidate = item
        local_path = (
            (
                candidate.local_address,
                (
                    candidate.local_endpoint.port
                    if candidate.local_endpoint is not None
                    else (
                        connection.local_endpoint.port
                        if connection.local_endpoint is not None
                        else None
                    )
                ),
            )
            if candidate.local_address is not None
            else None
        )
        remote_port = (
            candidate.remote_endpoint.port
            if candidate.remote_endpoint is not None
            else connection.remote_endpoint.port
        )
        remote_path = (candidate.remote_address, remote_port)
        network_id = connection.connection_context.get_network_id(
            (
                candidate.local_endpoint.interface
                if candidate.local_endpoint is not None
                else (
                    candidate.path
                    if candidate.path != "default"
                    else None
                )
            ),
            local_address=candidate.local_address,
        )
        cache_score = connection.connection_context.get_path_score(
            local_path,
            remote_path,
            protocol=candidate.protocol,
            network_id=network_id,
        )
        return (
            path_order[candidate.path],
            protocol_order[candidate.protocol],
            -cache_score,
            index,
        )

    return [candidate for _index, candidate in sorted(enumerate(candidates), key=sort_key)]


# Define our own sleep function which keeps track of its running calls
# so we can cancel them once the Connection is established
# https://stackoverflow.com/questions/37209864/interrupt-all-asyncio-sleep-currently-executing
class SleepClassForRacing:
    tasks = set()

    async def sleep(self, delay, result=None, *, loop=None):
        coro = asyncio.sleep(delay, result=result)
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            return result
        finally:
            self.tasks.remove(task)

    def cancel_all(self):
        for task in self.tasks:
            task.cancel()
