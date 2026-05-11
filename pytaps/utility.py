import asyncio
import datetime
import logging
import socket
import warnings
from dataclasses import dataclass
from enum import Enum

from pytaps.transportProperties import (
    PreferenceLevel,
    canonicalize_property_name,
    get_protocols,
)

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


def rank_protocol_candidates(transport_properties, connection_context=None):
    """Rank protocol branches per RFC 9623 sorting guidance."""
    ranked_protocols = []
    for protocol in get_protocols():
        prefer_score = 0
        avoid_score = 0
        excluded = False

        for property_name, property_value in transport_properties.selection_properties.items():
            canonical_property = canonicalize_property_name(property_name)
            if canonical_property in {"direction", "interface", "pvd"}:
                continue

            support = _supports_selection_property(protocol, canonical_property, property_value)
            if support is None:
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


def rank_path_candidates(local_endpoint, transport_properties):
    if local_endpoint is None or not local_endpoint.interface:
        return [("default", None)]

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

    ranked_paths = []
    for interface_id in local_endpoint.interface:
        if interface_id in prohibited_interfaces:
            continue
        if required_interfaces and interface_id not in required_interfaces:
            continue

        preference = interface_preferences.get(interface_id, PreferenceLevel.IGNORE)
        prefer_score = 1 if preference is PreferenceLevel.PREFER else 0
        avoid_score = 1 if preference is PreferenceLevel.AVOID else 0
        ranked_paths.append((interface_id, (prefer_score, avoid_score)))

    ranked_paths.sort(key=lambda value: (-value[1][0], value[1][1], value[0]))
    return [(interface_id, interface_id) for interface_id, _score in ranked_paths]


def order_remote_addresses(remote_addrs):
    def sort_key(entry):
        family, address = entry
        family_rank = 1 if family == socket.AddressFamily.AF_INET6 else 0
        return (family_rank, address)

    return sorted(remote_addrs, key=lambda entry: (-sort_key(entry)[0], sort_key(entry)[1]))


def build_protocol_candidates(transport_properties, connection_context=None):
    return [
        protocol_info[0]["name"]
        for protocol_info in rank_protocol_candidates(
            transport_properties,
            connection_context=connection_context,
        )
    ]


def create_candidates(connection, remote_addrs=None):
    """Build leaf candidates ordered as path -> protocol -> endpoint."""
    if remote_addrs is None:
        remote_addrs = []

    ordered_paths = rank_path_candidates(connection.local_endpoint, connection.transport_properties)
    ordered_protocols = build_protocol_candidates(
        connection.transport_properties,
        connection_context=connection.connection_context,
    )
    ordered_remotes = order_remote_addresses(remote_addrs)

    candidates = []
    for path_label, _path_interface in ordered_paths:
        for protocol_name in ordered_protocols:
            for family, remote_address in ordered_remotes:
                candidates.append(
                    Candidate(
                        protocol=protocol_name,
                        remote_address=remote_address,
                        address_family=family,
                        path=path_label,
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
            (candidate.local_address, connection.local_endpoint.port)
            if candidate.local_address is not None and connection.local_endpoint is not None
            else None
        )
        remote_path = (candidate.remote_address, connection.remote_endpoint.port)
        cache_score = connection.connection_context.get_path_score(
            local_path,
            remote_path,
            protocol=candidate.protocol,
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
