import inspect
import ipaddress
import math
import time
import weakref
from collections import OrderedDict
from collections.abc import Mapping

from .endpoint import LocalEndpoint
from .utility import schedule_callback


DEFAULT_QUIC_SESSION_CACHE_CAPACITY = 32
DEFAULT_QUIC_SESSION_CACHE_LIFETIME = 24 * 60 * 60
DEFAULT_PERFORMANCE_CACHE_CAPACITY = 128
DEFAULT_RTT_CACHE_LIFETIME = 5 * 60
DEFAULT_ESTABLISHMENT_CACHE_LIFETIME = 60 * 60
DEFAULT_SUCCESS_CACHE_LIFETIME = 24 * 60 * 60
PERFORMANCE_EWMA_ALPHA = 0.25
PERFORMANCE_SAMPLE_LIMIT = 64


class ConnectionContext:
    """Shared cached state and monitoring for related Connections."""

    def __init__(self):
        self.created_at = time.time()
        self.protocol_cache = {}
        self.path_cache = {}
        self.resolution_cache = {}
        self.event_counters = {}
        self.recent_events = []
        self.template_creations = 0
        self.connection_counts = {
            "active": 0,
            "ready": 0,
            "closed": 0,
        }
        self.listener_counts = {
            "active": 0,
            "listening": 0,
            "closed": 0,
        }
        self.connection_groups = 0
        self.protocol_policy = {}
        self.interface_policy = {}
        self.pvd_policy = {}
        self.address_family_policy = {
            "ipv4": 0,
            "ipv6": 0,
        }
        self.system_policy_source = "manual"
        self.system_policy_generation = 0
        self.system_policy_updated_at = None
        self.alternate_remotes = {}
        self.path_advisories = {}
        self.path_transitions = {}
        self.subscribers = []
        self._quic_client_session_tickets = OrderedDict()
        self._quic_server_session_tickets = OrderedDict()
        self._performance_cache = OrderedDict()
        self._connections = weakref.WeakSet()
        self._listeners = weakref.WeakSet()

    @staticmethod
    def _performance_cache_capacity(value):
        if value is None:
            return DEFAULT_PERFORMANCE_CACHE_CAPACITY
        if isinstance(value, bool):
            raise ValueError("Performance cache capacity must be an integer")
        capacity = int(value)
        if capacity < 0:
            raise ValueError("Performance cache capacity cannot be negative")
        return capacity

    @staticmethod
    def _performance_lifetime(value, default):
        if value is None:
            return default
        if isinstance(value, bool):
            raise ValueError("Performance cache lifetime must be numeric")
        lifetime = float(value)
        if not math.isfinite(lifetime) or lifetime < 0:
            raise ValueError(
                "Performance cache lifetime must be finite and non-negative"
            )
        return lifetime

    @staticmethod
    def _performance_metric(name, value):
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError(f"{name} must be numeric")
        metric = float(value)
        if not math.isfinite(metric) or metric < 0:
            raise ValueError(f"{name} must be finite and non-negative")
        return metric

    @staticmethod
    def _performance_path_parts(path):
        if path is None:
            return None, None
        if isinstance(path, (tuple, list)):
            address = path[0] if path else None
            port = path[1] if len(path) > 1 else None
            return address, port
        return path, None

    @classmethod
    def _performance_cache_key(
        cls,
        local_path,
        remote_path,
        protocol,
        network_id,
    ):
        local_address, _local_port = cls._performance_path_parts(
            local_path
        )
        remote_address, remote_port = cls._performance_path_parts(
            remote_path
        )
        return (
            network_id or "default",
            local_address,
            remote_address,
            remote_port,
            protocol,
        )

    @staticmethod
    def _new_performance_entry(key):
        (
            network_id,
            local_address,
            remote_address,
            remote_port,
            protocol,
        ) = key
        return {
            "network": network_id,
            "localAddress": local_address,
            "remoteAddress": remote_address,
            "remotePort": remote_port,
            "protocol": protocol,
            "latestRtt": None,
            "smoothedRtt": None,
            "minimumRtt": None,
            "rttVariation": None,
            "rttSamples": 0,
            "rttLastUpdated": None,
            "rttExpiresAt": None,
            "establishmentLatency": None,
            "establishmentSamples": 0,
            "establishmentLastUpdated": None,
            "establishmentExpiresAt": None,
            "successes": 0,
            "failures": 0,
            "successRate": None,
            "lastOutcome": None,
            "outcomeLastUpdated": None,
            "outcomeExpiresAt": None,
            "lastSource": None,
            "lastUpdated": None,
        }

    @staticmethod
    def _clear_expired_performance_metrics(entry, now):
        rtt_expires_at = entry.get("rttExpiresAt")
        if rtt_expires_at is not None and rtt_expires_at <= now:
            entry.update(
                {
                    "latestRtt": None,
                    "smoothedRtt": None,
                    "minimumRtt": None,
                    "rttVariation": None,
                    "rttSamples": 0,
                    "rttLastUpdated": None,
                    "rttExpiresAt": None,
                }
            )

        establishment_expires_at = entry.get(
            "establishmentExpiresAt"
        )
        if (
            establishment_expires_at is not None
            and establishment_expires_at <= now
        ):
            entry.update(
                {
                    "establishmentLatency": None,
                    "establishmentSamples": 0,
                    "establishmentLastUpdated": None,
                    "establishmentExpiresAt": None,
                }
            )

        outcome_expires_at = entry.get("outcomeExpiresAt")
        if (
            outcome_expires_at is not None
            and outcome_expires_at <= now
        ):
            entry.update(
                {
                    "successes": 0,
                    "failures": 0,
                    "successRate": None,
                    "lastOutcome": None,
                    "outcomeLastUpdated": None,
                    "outcomeExpiresAt": None,
                }
            )

    def _prune_performance_cache(self):
        now = time.time()
        for key, entry in list(self._performance_cache.items()):
            self._clear_expired_performance_metrics(entry, now)
            if (
                not entry["rttSamples"]
                and not entry["establishmentSamples"]
                and not (entry["successes"] + entry["failures"])
            ):
                self._performance_cache.pop(key, None)

    def record_performance_observation(
        self,
        local_path,
        remote_path,
        protocol,
        *,
        network_id=None,
        rtt=None,
        rtt_variation=None,
        establishment_latency=None,
        success=None,
        source=None,
        capacity=None,
        rtt_lifetime=None,
        establishment_lifetime=None,
        success_lifetime=None,
    ):
        """Record bounded, expiring RFC 9623 performance history."""
        if protocol is None:
            return False
        if success is not None and not isinstance(success, bool):
            raise ValueError("success must be a boolean")

        rtt = self._performance_metric("RTT", rtt)
        rtt_variation = self._performance_metric(
            "RTT variation",
            rtt_variation,
        )
        establishment_latency = self._performance_metric(
            "Establishment latency",
            establishment_latency,
        )
        if rtt is None and rtt_variation is not None:
            raise ValueError("RTT variation requires an RTT observation")
        if (
            rtt is None
            and establishment_latency is None
            and success is None
        ):
            return False

        capacity = self._performance_cache_capacity(capacity)
        rtt_lifetime = self._performance_lifetime(
            rtt_lifetime,
            DEFAULT_RTT_CACHE_LIFETIME,
        )
        establishment_lifetime = self._performance_lifetime(
            establishment_lifetime,
            DEFAULT_ESTABLISHMENT_CACHE_LIFETIME,
        )
        success_lifetime = self._performance_lifetime(
            success_lifetime,
            DEFAULT_SUCCESS_CACHE_LIFETIME,
        )
        if capacity == 0:
            return False
        if (
            (rtt is None or rtt_lifetime == 0)
            and (
                establishment_latency is None
                or establishment_lifetime == 0
            )
            and (success is None or success_lifetime == 0)
        ):
            return False

        self._prune_performance_cache()
        key = self._performance_cache_key(
            local_path,
            remote_path,
            protocol,
            network_id,
        )
        entry = self._performance_cache.setdefault(
            key,
            self._new_performance_entry(key),
        )
        now = time.time()

        if rtt is not None and rtt_lifetime > 0:
            previous_smoothed = entry["smoothedRtt"]
            previous_variation = entry["rttVariation"]
            if previous_smoothed is None:
                smoothed = rtt
                variation = (
                    rtt_variation
                    if rtt_variation is not None
                    else 0.0
                )
                minimum = rtt
            else:
                delta = rtt - previous_smoothed
                smoothed = (
                    previous_smoothed
                    + PERFORMANCE_EWMA_ALPHA * delta
                )
                observed_variation = (
                    rtt_variation
                    if rtt_variation is not None
                    else abs(delta)
                )
                variation = (
                    previous_variation
                    + PERFORMANCE_EWMA_ALPHA
                    * (observed_variation - previous_variation)
                )
                minimum = min(entry["minimumRtt"], rtt)
            entry.update(
                {
                    "latestRtt": rtt,
                    "smoothedRtt": smoothed,
                    "minimumRtt": minimum,
                    "rttVariation": variation,
                    "rttSamples": min(
                        PERFORMANCE_SAMPLE_LIMIT,
                        entry["rttSamples"] + 1,
                    ),
                    "rttLastUpdated": now,
                    "rttExpiresAt": now + rtt_lifetime,
                }
            )

        if (
            establishment_latency is not None
            and establishment_lifetime > 0
        ):
            previous = entry["establishmentLatency"]
            averaged = (
                establishment_latency
                if previous is None
                else (
                    previous
                    + PERFORMANCE_EWMA_ALPHA
                    * (establishment_latency - previous)
                )
            )
            entry.update(
                {
                    "establishmentLatency": averaged,
                    "establishmentSamples": min(
                        PERFORMANCE_SAMPLE_LIMIT,
                        entry["establishmentSamples"] + 1,
                    ),
                    "establishmentLastUpdated": now,
                    "establishmentExpiresAt": (
                        now + establishment_lifetime
                    ),
                }
            )

        if success is not None and success_lifetime > 0:
            outcome_name = "success" if success else "failure"
            outcome_key = "successes" if success else "failures"
            if (
                entry["successes"] + entry["failures"]
                >= PERFORMANCE_SAMPLE_LIMIT
            ):
                entry["successes"] //= 2
                entry["failures"] //= 2
            entry[outcome_key] += 1
            outcome_count = entry["successes"] + entry["failures"]
            entry.update(
                {
                    "successRate": (
                        entry["successes"] / outcome_count
                    ),
                    "lastOutcome": outcome_name,
                    "outcomeLastUpdated": now,
                    "outcomeExpiresAt": now + success_lifetime,
                }
            )

        entry["lastSource"] = source
        entry["lastUpdated"] = now
        self._performance_cache.move_to_end(key)
        while len(self._performance_cache) > capacity:
            self._performance_cache.popitem(last=False)
        return True

    def _matching_performance_entries(
        self,
        local_path,
        remote_path,
        protocol,
        network_id,
    ):
        self._prune_performance_cache()
        local_address, _local_port = self._performance_path_parts(
            local_path
        )
        remote_address, remote_port = self._performance_path_parts(
            remote_path
        )
        candidates = []
        for key, entry in self._performance_cache.items():
            if network_id is not None and entry["network"] != network_id:
                continue
            if protocol is not None and entry["protocol"] != protocol:
                continue
            if (
                remote_address is not None
                and entry["remoteAddress"] != remote_address
            ):
                continue
            if (
                remote_port is not None
                and entry["remotePort"] != remote_port
            ):
                continue
            candidates.append((key, entry))

        if local_address is not None:
            exact = [
                candidate
                for candidate in candidates
                if candidate[1]["localAddress"] == local_address
            ]
            if exact:
                candidates = exact
        for key, _entry in candidates:
            self._performance_cache.move_to_end(key)
        return [entry for _key, entry in candidates]

    def get_performance_metrics(
        self,
        local_path=None,
        remote_path=None,
        protocol=None,
        *,
        network_id=None,
    ):
        """Return averaged, unexpired performance metrics for a scope."""
        entries = self._matching_performance_entries(
            local_path,
            remote_path,
            protocol,
            network_id,
        )
        if not entries:
            return None

        rtt_entries = [
            entry for entry in entries if entry["rttSamples"]
        ]
        establishment_entries = [
            entry
            for entry in entries
            if entry["establishmentSamples"]
        ]
        rtt_samples = sum(
            entry["rttSamples"] for entry in rtt_entries
        )
        establishment_samples = sum(
            entry["establishmentSamples"]
            for entry in establishment_entries
        )
        successes = sum(entry["successes"] for entry in entries)
        failures = sum(entry["failures"] for entry in entries)
        outcomes = successes + failures

        latest_rtt_entry = max(
            rtt_entries,
            key=lambda entry: entry["rttLastUpdated"],
            default=None,
        )
        return {
            "latestRtt": (
                latest_rtt_entry["latestRtt"]
                if latest_rtt_entry is not None
                else None
            ),
            "smoothedRtt": (
                sum(
                    entry["smoothedRtt"] * entry["rttSamples"]
                    for entry in rtt_entries
                )
                / rtt_samples
                if rtt_samples
                else None
            ),
            "minimumRtt": (
                min(entry["minimumRtt"] for entry in rtt_entries)
                if rtt_entries
                else None
            ),
            "rttVariation": (
                sum(
                    entry["rttVariation"] * entry["rttSamples"]
                    for entry in rtt_entries
                )
                / rtt_samples
                if rtt_samples
                else None
            ),
            "rttSamples": rtt_samples,
            "establishmentLatency": (
                sum(
                    entry["establishmentLatency"]
                    * entry["establishmentSamples"]
                    for entry in establishment_entries
                )
                / establishment_samples
                if establishment_samples
                else None
            ),
            "establishmentSamples": establishment_samples,
            "successes": successes,
            "failures": failures,
            "successRate": successes / outcomes if outcomes else None,
            "matchCount": len(entries),
            "lastUpdated": max(
                entry["lastUpdated"] for entry in entries
            ),
        }

    def get_performance_score(
        self,
        local_path=None,
        remote_path=None,
        protocol=None,
        *,
        network_id=None,
    ):
        """Return a bounded preference adjustment for matching history."""
        metrics = self.get_performance_metrics(
            local_path,
            remote_path,
            protocol,
            network_id=network_id,
        )
        if metrics is None:
            return 0

        score = 0
        outcomes = metrics["successes"] + metrics["failures"]
        success_rate = metrics["successRate"]
        if outcomes:
            weight = 2 if outcomes >= 2 else 1
            if success_rate >= 0.8:
                score += weight
            elif success_rate <= 0.3:
                score -= weight

        rtt = metrics["smoothedRtt"]
        if rtt is not None:
            if rtt <= 0.05:
                score += 3
            elif rtt <= 0.15:
                score += 2
            elif rtt <= 0.4:
                score += 1
            elif rtt >= 1.0:
                score -= 2
            elif rtt >= 0.6:
                score -= 1

        establishment = metrics["establishmentLatency"]
        if establishment is not None:
            if establishment <= 0.1:
                score += 2
            elif establishment <= 0.3:
                score += 1
            elif establishment >= 2.0:
                score -= 2
            elif establishment >= 1.0:
                score -= 1
        return max(-6, min(6, score))

    def get_network_performance_score(self, network_id):
        return self.get_performance_score(network_id=network_id)

    def get_performance_cache_snapshot(self):
        """Return current performance entries without expired metrics."""
        self._prune_performance_cache()
        return [
            dict(entry)
            for entry in self._performance_cache.values()
        ]

    @staticmethod
    def _session_cache_capacity(value):
        if value is None:
            return DEFAULT_QUIC_SESSION_CACHE_CAPACITY
        return max(0, int(value))

    @staticmethod
    def _session_cache_lifetime(value):
        if value is None:
            return DEFAULT_QUIC_SESSION_CACHE_LIFETIME
        return max(0, float(value))

    @staticmethod
    def _session_ticket_is_valid(entry):
        ticket = entry["ticket"]
        if not getattr(ticket, "is_valid", False):
            return False
        expires_at = entry.get("expiresAt")
        return expires_at is None or time.time() <= expires_at

    def _prune_quic_session_cache(self, cache):
        for key, entry in list(cache.items()):
            if not self._session_ticket_is_valid(entry):
                cache.pop(key, None)

    def _cache_quic_session_ticket(
        self,
        cache,
        key,
        ticket,
        *,
        capacity=None,
        lifetime=None,
    ):
        capacity = self._session_cache_capacity(capacity)
        lifetime = self._session_cache_lifetime(lifetime)
        self._prune_quic_session_cache(cache)
        if capacity == 0 or lifetime == 0:
            return False
        cache.pop(key, None)
        cache[key] = {
            "ticket": ticket,
            "storedAt": time.time(),
            "expiresAt": time.time() + lifetime,
        }
        while len(cache) > capacity:
            cache.popitem(last=False)
        return True

    def cache_quic_client_session_ticket(
        self,
        key,
        ticket,
        *,
        capacity=None,
        lifetime=None,
    ):
        return self._cache_quic_session_ticket(
            self._quic_client_session_tickets,
            key,
            ticket,
            capacity=capacity,
            lifetime=lifetime,
        )

    def get_quic_client_session_ticket(self, key):
        self._prune_quic_session_cache(
            self._quic_client_session_tickets
        )
        entry = self._quic_client_session_tickets.get(key)
        if entry is None:
            return None
        self._quic_client_session_tickets.move_to_end(key)
        return entry["ticket"]

    def take_quic_client_session_ticket(self, key):
        self._prune_quic_session_cache(
            self._quic_client_session_tickets
        )
        entry = self._quic_client_session_tickets.pop(key, None)
        return entry["ticket"] if entry is not None else None

    def cache_quic_server_session_ticket(
        self,
        ticket,
        *,
        capacity=None,
        lifetime=None,
    ):
        return self._cache_quic_session_ticket(
            self._quic_server_session_tickets,
            bytes(ticket.ticket),
            ticket,
            capacity=capacity,
            lifetime=lifetime,
        )

    def get_quic_server_session_ticket(self, ticket_id):
        self._prune_quic_session_cache(
            self._quic_server_session_tickets
        )
        key = bytes(ticket_id)
        entry = self._quic_server_session_tickets.get(key)
        if entry is None:
            return None
        self._quic_server_session_tickets.move_to_end(key)
        return entry["ticket"]

    def take_quic_server_session_ticket(self, ticket_id):
        self._prune_quic_session_cache(
            self._quic_server_session_tickets
        )
        entry = self._quic_server_session_tickets.pop(
            bytes(ticket_id),
            None,
        )
        return entry["ticket"] if entry is not None else None

    def clear_quic_session_tickets(self, *, client=True, server=True):
        if client:
            self._quic_client_session_tickets.clear()
        if server:
            self._quic_server_session_tickets.clear()

    def get_quic_session_cache_snapshot(self):
        self._prune_quic_session_cache(
            self._quic_client_session_tickets
        )
        self._prune_quic_session_cache(
            self._quic_server_session_tickets
        )
        return {
            "clientTickets": len(self._quic_client_session_tickets),
            "serverTickets": len(self._quic_server_session_tickets),
        }

    def subscribe(self, callback, loop=None):
        self.subscribers.append((callback, loop))
        return callback

    def unsubscribe(self, callback):
        self.subscribers = [
            (registered, loop)
            for registered, loop in self.subscribers
            if registered is not callback
        ]

    def _notify_subscribers(self, trigger, **details):
        if not self.subscribers:
            return
        update = {
            "trigger": trigger,
            "details": dict(details),
            "snapshot": self.get_snapshot(),
        }
        for callback, loop in list(self.subscribers):
            if loop is not None:
                schedule_callback(loop, callback, (update,), ())
                continue
            result = callback(update)
            if inspect.iscoroutine(result):
                raise RuntimeError(
                    "ConnectionContext monitoring subscribers that return coroutines require a loop."
                )

    def register_preconnection(self):
        self.template_creations += 1
        self._notify_subscribers("preconnection_registered")

    def attach_connection(self, connection=None):
        self.connection_counts["active"] += 1
        if connection is not None:
            self._connections.add(connection)
        self._notify_subscribers("connection_attached")

    def mark_connection_ready(self):
        self.connection_counts["ready"] += 1
        self._notify_subscribers("connection_ready")

    def detach_connection(self, connection=None, *, was_ready=False):
        if self.connection_counts["active"] > 0:
            self.connection_counts["active"] -= 1
        if was_ready and self.connection_counts["ready"] > 0:
            self.connection_counts["ready"] -= 1
        if connection is not None:
            self._connections.discard(connection)
        self.connection_counts["closed"] += 1
        self._notify_subscribers("connection_detached", was_ready=was_ready)

    def detach_connection_for_transfer(self, connection=None, *, was_ready=False):
        if self.connection_counts["active"] > 0:
            self.connection_counts["active"] -= 1
        if was_ready and self.connection_counts["ready"] > 0:
            self.connection_counts["ready"] -= 1
        if connection is not None:
            self._connections.discard(connection)
        self._notify_subscribers("connection_context_transferred")

    def attach_listener(self, listener=None):
        self.listener_counts["active"] += 1
        if listener is not None:
            self._listeners.add(listener)
        self._notify_subscribers("listener_attached")

    def mark_listener_listening(self):
        self.listener_counts["listening"] += 1
        self._notify_subscribers("listener_listening")

    def detach_listener(self, listener=None, *, was_listening=False):
        if self.listener_counts["active"] > 0:
            self.listener_counts["active"] -= 1
        if was_listening and self.listener_counts["listening"] > 0:
            self.listener_counts["listening"] -= 1
        if listener is not None:
            self._listeners.discard(listener)
        self.listener_counts["closed"] += 1
        self._notify_subscribers("listener_detached", was_listening=was_listening)

    def _record_recent_event(self, name, *, source=None, state=None, details=None):
        event = {
            "timestamp": time.time(),
            "name": name,
            "source": source,
            "state": state,
            "details": dict(details or {}),
        }
        self.recent_events.append(event)
        if len(self.recent_events) > 50:
            self.recent_events = self.recent_events[-50:]
        return event

    def set_protocol_policy(
        self,
        protocol,
        *,
        available=True,
        preference_adjustment=0,
        racing_cooldown=0,
    ):
        self.protocol_policy[protocol] = {
            "available": available,
            "preferenceAdjustment": preference_adjustment,
            "racingCooldown": racing_cooldown,
            "lastUpdated": time.time(),
        }
        self._mark_manual_policy_update()
        self._notify_subscribers("protocol_policy_updated", protocol=protocol)

    def set_interface_policy(
        self,
        interface_id,
        *,
        available=True,
        preference_adjustment=0,
        pvd_id=None,
        supports_temporary_address=True,
        relative_cost="normal",
    ):
        self.interface_policy[interface_id] = {
            "available": available,
            "preferenceAdjustment": preference_adjustment,
            "pvdId": pvd_id,
            "supportsTemporaryAddress": supports_temporary_address,
            "relativeCost": relative_cost,
            "lastUpdated": time.time(),
        }
        self._mark_manual_policy_update()
        self._notify_subscribers("interface_policy_updated", interface=interface_id)

    def set_pvd_policy(self, pvd_id, *, available=True, preference_adjustment=0):
        self.pvd_policy[pvd_id] = {
            "available": available,
            "preferenceAdjustment": preference_adjustment,
            "lastUpdated": time.time(),
        }
        self._mark_manual_policy_update()
        self._notify_subscribers("pvd_policy_updated", pvd=pvd_id)

    def set_address_family_policy(self, family, preference_adjustment=0):
        normalized = family.lower()
        if normalized not in {"ipv4", "ipv6"}:
            raise KeyError(f"Unsupported address family policy: {family}")
        self.address_family_policy[normalized] = preference_adjustment
        self._mark_manual_policy_update()
        self._notify_subscribers("address_family_policy_updated", family=normalized)

    def _mark_manual_policy_update(self):
        self.system_policy_source = "manual"
        self.system_policy_generation += 1
        self.system_policy_updated_at = time.time()

    @staticmethod
    def _policy_value(policy, canonical, python_name, default):
        if canonical in policy:
            return policy[canonical]
        return policy.get(python_name, default)

    @staticmethod
    def _updated_policy_entry(previous, values, now):
        previous_comparable = {
            key: value
            for key, value in (previous or {}).items()
            if key != "lastUpdated"
        }
        if previous_comparable == values:
            return dict(previous), False
        return {**values, "lastUpdated": now}, True

    def _apply_interface_snapshot(self, interfaces, now):
        updated = {}
        changes = {}
        for interface_id, raw_policy in interfaces.items():
            policy = dict(raw_policy)
            values = {
                **policy,
                "available": self._policy_value(
                    policy,
                    "available",
                    "available",
                    True,
                ),
                "preferenceAdjustment": self._policy_value(
                    policy,
                    "preferenceAdjustment",
                    "preference_adjustment",
                    0,
                ),
                "pvdId": self._policy_value(
                    policy,
                    "pvdId",
                    "pvd_id",
                    None,
                ),
                "supportsTemporaryAddress": self._policy_value(
                    policy,
                    "supportsTemporaryAddress",
                    "supports_temporary_address",
                    True,
                ),
                "relativeCost": self._policy_value(
                    policy,
                    "relativeCost",
                    "relative_cost",
                    "normal",
                ),
            }
            for python_name in {
                "preference_adjustment",
                "pvd_id",
                "supports_temporary_address",
                "relative_cost",
            }:
                values.pop(python_name, None)
            previous = self.interface_policy.get(interface_id)
            entry, changed = self._updated_policy_entry(
                previous,
                values,
                now,
            )
            updated[interface_id] = entry
            if changed:
                changes[interface_id] = {
                    "previous": dict(previous) if previous else None,
                    "current": dict(entry),
                }

        for interface_id, previous in self.interface_policy.items():
            if interface_id in updated:
                continue
            values = {
                **{
                    key: value
                    for key, value in previous.items()
                    if key != "lastUpdated"
                },
                "available": False,
                "addresses": [],
            }
            entry, changed = self._updated_policy_entry(
                previous,
                values,
                now,
            )
            updated[interface_id] = entry
            if changed:
                changes[interface_id] = {
                    "previous": dict(previous),
                    "current": dict(entry),
                }
        self.interface_policy = updated
        return changes

    def _apply_protocol_snapshot(self, protocols, now):
        updated = {}
        changes = {}
        for protocol, raw_policy in protocols.items():
            policy = dict(raw_policy)
            values = {
                "available": self._policy_value(
                    policy,
                    "available",
                    "available",
                    True,
                ),
                "preferenceAdjustment": self._policy_value(
                    policy,
                    "preferenceAdjustment",
                    "preference_adjustment",
                    0,
                ),
                "racingCooldown": self._policy_value(
                    policy,
                    "racingCooldown",
                    "racing_cooldown",
                    0,
                ),
            }
            previous = self.protocol_policy.get(protocol)
            entry, changed = self._updated_policy_entry(
                previous,
                values,
                now,
            )
            updated[protocol] = entry
            if changed:
                changes[protocol] = {
                    "previous": dict(previous) if previous else None,
                    "current": dict(entry),
                }
        for protocol, previous in self.protocol_policy.items():
            if protocol in updated:
                continue
            values = {
                **{
                    key: value
                    for key, value in previous.items()
                    if key != "lastUpdated"
                },
                "available": False,
            }
            entry, changed = self._updated_policy_entry(
                previous,
                values,
                now,
            )
            updated[protocol] = entry
            if changed:
                changes[protocol] = {
                    "previous": dict(previous),
                    "current": dict(entry),
                }
        self.protocol_policy = updated
        return changes

    def _apply_pvd_snapshot(self, pvds, now):
        updated = {}
        changes = {}
        for pvd_id, raw_policy in pvds.items():
            policy = dict(raw_policy)
            values = {
                "available": self._policy_value(
                    policy,
                    "available",
                    "available",
                    True,
                ),
                "preferenceAdjustment": self._policy_value(
                    policy,
                    "preferenceAdjustment",
                    "preference_adjustment",
                    0,
                ),
            }
            previous = self.pvd_policy.get(pvd_id)
            entry, changed = self._updated_policy_entry(
                previous,
                values,
                now,
            )
            updated[pvd_id] = entry
            if changed:
                changes[pvd_id] = {
                    "previous": dict(previous) if previous else None,
                    "current": dict(entry),
                }
        for pvd_id, previous in self.pvd_policy.items():
            if pvd_id in updated:
                continue
            values = {
                **{
                    key: value
                    for key, value in previous.items()
                    if key != "lastUpdated"
                },
                "available": False,
            }
            entry, changed = self._updated_policy_entry(
                previous,
                values,
                now,
            )
            updated[pvd_id] = entry
            if changed:
                changes[pvd_id] = {
                    "previous": dict(previous),
                    "current": dict(entry),
                }
        self.pvd_policy = updated
        return changes

    def apply_system_policy(self, snapshot):
        """Atomically apply one implementation-provided System Policy view."""
        if hasattr(snapshot, "as_dict"):
            policy = snapshot.as_dict()
        elif isinstance(snapshot, Mapping):
            policy = dict(snapshot)
        else:
            raise TypeError(
                "System Policy snapshots must be mappings or "
                "SystemPolicySnapshot objects"
            )

        now = float(policy.get("observedAt", time.time()))
        source = str(policy.get("source", "system"))
        interfaces = policy.get("interfaces")
        if interfaces is not None:
            interfaces = {
                interface_id: dict(values)
                for interface_id, values in interfaces.items()
            }
        protocols = policy.get("protocols")
        if protocols is not None:
            protocols = {
                protocol: dict(values)
                for protocol, values in protocols.items()
            }
        pvds = policy.get("pvds")
        if pvds is not None:
            pvds = {
                pvd_id: dict(values)
                for pvd_id, values in pvds.items()
            }
        address_families = policy.get(
            "addressFamilies",
            policy.get("address_families"),
        )
        normalized_families = None
        if address_families is not None:
            normalized_families = {
                family.lower(): adjustment
                for family, adjustment in address_families.items()
            }
            unknown_families = (
                set(normalized_families) - {"ipv4", "ipv6"}
            )
            if unknown_families:
                family = sorted(unknown_families)[0]
                raise KeyError(
                    f"Unsupported address family policy: {family}"
                )

        changes = {
            "interfaces": {},
            "protocols": {},
            "pvds": {},
            "addressFamilies": {},
        }
        if interfaces is not None:
            changes["interfaces"] = self._apply_interface_snapshot(
                interfaces,
                now,
            )
        if protocols is not None:
            changes["protocols"] = self._apply_protocol_snapshot(
                protocols,
                now,
            )
        if pvds is not None:
            changes["pvds"] = self._apply_pvd_snapshot(
                pvds,
                now,
            )
        if normalized_families is not None:
            for normalized in {"ipv4", "ipv6"}:
                adjustment = normalized_families.get(normalized, 0)
                previous = self.address_family_policy.get(normalized, 0)
                if previous != adjustment:
                    changes["addressFamilies"][normalized] = {
                        "previous": previous,
                        "current": adjustment,
                    }
                self.address_family_policy[normalized] = adjustment

        changed = any(changes.values())
        self.system_policy_source = source
        self.system_policy_updated_at = now
        if not changed:
            return False

        self.system_policy_generation += 1
        self._notify_subscribers(
            "system_policy_updated",
            source=source,
            generation=self.system_policy_generation,
            changes=changes,
        )
        for connection in list(self._connections):
            if connection.loop.is_closed():
                continue
            connection.loop.call_soon_threadsafe(
                connection._handle_system_policy_update,
                changes,
            )
        for listener in list(self._listeners):
            if listener.loop.is_closed():
                continue
            listener.loop.call_soon_threadsafe(
                listener._handle_system_policy_update,
                changes,
            )
        return True

    @staticmethod
    def _interface_preference(policy):
        score = policy.get("preferenceAdjustment", 0)
        relative_cost = str(policy.get("relativeCost", "normal")).lower()
        if relative_cost == "low":
            score += 2
        elif relative_cost == "high":
            score -= 2
        if policy.get("isLoopback"):
            score -= 10
        return score

    def get_system_local_endpoints(self):
        endpoints = []
        interfaces = sorted(
            self.interface_policy.items(),
            key=lambda item: (
                -self._interface_preference(item[1]),
                item[0],
            ),
        )
        for interface_id, policy in interfaces:
            if policy.get("available") is False:
                continue
            for address_entry in policy.get("addresses", ()):
                address = (
                    address_entry.get("address")
                    if isinstance(address_entry, dict)
                    else address_entry
                )
                if not address:
                    continue
                try:
                    parsed = ipaddress.ip_address(address)
                except ValueError:
                    continue
                if (
                    parsed.is_unspecified
                    or parsed.is_multicast
                    or parsed.is_link_local
                ):
                    continue
                endpoint = (
                    LocalEndpoint()
                    .with_interface(interface_id)
                    .with_address(str(parsed))
                )
                if not any(
                    existing.__dict__ == endpoint.__dict__
                    for existing in endpoints
                ):
                    endpoints.append(endpoint)
        return endpoints

    def get_interface_for_address(self, address):
        """Return the interface currently or previously owning an address."""
        if not address:
            return None
        try:
            normalized = str(
                ipaddress.ip_address(str(address).split("%", 1)[0])
            )
        except ValueError:
            return None
        for interface_id, policy in self.interface_policy.items():
            for address_entry in policy.get("addresses", ()):
                candidate = (
                    address_entry.get("address")
                    if isinstance(address_entry, dict)
                    else address_entry
                )
                if not candidate:
                    continue
                try:
                    candidate = str(
                        ipaddress.ip_address(
                            str(candidate).split("%", 1)[0]
                        )
                    )
                except ValueError:
                    continue
                if candidate == normalized:
                    return interface_id
        return None

    def get_network_id(self, interface_id=None, *, local_address=None):
        """Return the policy-provided network attachment identity."""
        if interface_id is None:
            interface_id = self.get_interface_for_address(local_address)
        if interface_id is None:
            return "default"
        policy = self.interface_policy.get(interface_id, {})
        return policy.get("networkId") or interface_id

    def note_alternate_remote(
        self,
        base_remote,
        alternate_remote,
        *,
        address_family=None,
        protocol=None,
        lifetime=None,
    ):
        expires_at = time.time() + lifetime if lifetime is not None else None
        self.alternate_remotes.setdefault(base_remote, []).append(
            {
                "address": alternate_remote,
                "family": address_family,
                "protocol": protocol,
                "expiresAt": expires_at,
                "lastUpdated": time.time(),
            }
        )
        self._notify_subscribers(
            "alternate_remote_noted",
            base_remote=base_remote,
            alternate_remote=alternate_remote,
            protocol=protocol,
        )

    def degrade_path(
        self,
        local_path,
        remote_path,
        *,
        reason=None,
        penalty=2,
        lifetime=60,
    ):
        key = (local_path, remote_path)
        now = time.time()
        expires_at = now + lifetime if lifetime is not None else None
        entry = self.path_advisories.setdefault(
            key,
            {
                "penalty": 0,
                "reasons": [],
                "expiresAt": None,
                "lastUpdated": None,
            },
        )
        entry["penalty"] = max(entry["penalty"], penalty)
        if reason is not None:
            entry["reasons"].append(str(reason))
        entry["expiresAt"] = expires_at
        entry["lastUpdated"] = now
        self._notify_subscribers(
            "path_degraded",
            local_path=local_path,
            remote_path=remote_path,
            penalty=entry["penalty"],
        )

    def clear_path_degradation(self, local_path, remote_path):
        self.path_advisories.pop((local_path, remote_path), None)
        self._notify_subscribers(
            "path_degradation_cleared",
            local_path=local_path,
            remote_path=remote_path,
        )

    def get_path_advisory(self, local_path, remote_path):
        candidates = []
        exact_key = (local_path, remote_path)
        exact_advisory = self.path_advisories.get(exact_key)
        if exact_advisory is not None:
            candidates.append((exact_key, exact_advisory))
        if local_path is None:
            for key, values in self.path_advisories.items():
                if key[1] == remote_path and (key, values) not in candidates:
                    candidates.append((key, values))

        best = None
        best_penalty = -1
        now = time.time()
        for key, advisory in candidates:
            expires_at = advisory.get("expiresAt")
            if expires_at is not None and expires_at < now:
                self.path_advisories.pop(key, None)
                continue
            penalty = advisory.get("penalty", 0)
            if penalty > best_penalty:
                best = dict(advisory)
                best_penalty = penalty
        return best

    def record_path_transition(self, previous_path, current_path, protocol=None):
        key = (
            previous_path.get("local"),
            previous_path.get("remote"),
            current_path.get("local"),
            current_path.get("remote"),
            protocol,
        )
        entry = self.path_transitions.setdefault(
            key,
            {
                "count": 0,
                "lastUpdated": None,
            },
        )
        entry["count"] += 1
        entry["lastUpdated"] = time.time()
        self._notify_subscribers(
            "path_transition_recorded",
            previous_path=previous_path,
            current_path=current_path,
            protocol=protocol,
        )

    def attach_group(self):
        self.connection_groups += 1
        self._notify_subscribers("group_attached")

    def detach_group(self):
        if self.connection_groups > 0:
            self.connection_groups -= 1
        self._notify_subscribers("group_detached")

    def record_event(self, name, *, source=None, state=None, details=None):
        self.event_counters[name] = self.event_counters.get(name, 0) + 1
        self._record_recent_event(
            name,
            source=source,
            state=state,
            details=details,
        )
        self._notify_subscribers(
            "event_recorded",
            name=name,
            source=source,
            state=state,
        )

    def record_protocol_outcome(self, protocol, success, error=None):
        if protocol is None:
            return
        entry = self.protocol_cache.setdefault(
            protocol,
            {
                "successes": 0,
                "failures": 0,
                "consecutiveFailures": 0,
                "lastError": None,
                "lastOutcome": None,
                "lastUpdated": None,
            },
        )
        if success:
            entry["successes"] += 1
            entry["consecutiveFailures"] = 0
            entry["lastOutcome"] = "success"
            entry["lastError"] = None
        else:
            entry["failures"] += 1
            entry["consecutiveFailures"] += 1
            entry["lastOutcome"] = "failure"
            entry["lastError"] = str(error) if error is not None else None
        entry["lastUpdated"] = time.time()
        self._notify_subscribers(
            "protocol_outcome_recorded",
            protocol=protocol,
            success=success,
        )

    def record_candidate_outcome(
        self,
        local_path,
        remote_path,
        protocol,
        success,
        error=None,
    ):
        self.record_protocol_outcome(protocol, success, error)
        key = (local_path, remote_path)
        entry = self.path_cache.setdefault(
            key,
            {
                "uses": 0,
                "successes": 0,
                "failures": 0,
                "consecutiveFailures": 0,
                "lastProtocol": None,
                "lastOutcome": None,
                "lastError": None,
                "lastUpdated": None,
            },
        )
        if success:
            entry["successes"] += 1
            entry["consecutiveFailures"] = 0
            entry["lastOutcome"] = "success"
            entry["lastError"] = None
        else:
            entry["failures"] += 1
            entry["consecutiveFailures"] += 1
            entry["lastOutcome"] = "failure"
            entry["lastError"] = str(error) if error is not None else None
        entry["lastProtocol"] = protocol
        entry["lastUpdated"] = time.time()

    def record_path_use(self, local_path, remote_path, protocol=None):
        key = (local_path, remote_path)
        entry = self.path_cache.setdefault(
            key,
            {
                "uses": 0,
                "successes": 0,
                "failures": 0,
                "consecutiveFailures": 0,
                "lastProtocol": None,
                "lastOutcome": None,
                "lastError": None,
                "lastUpdated": None,
            },
        )
        entry["uses"] += 1
        entry["lastProtocol"] = protocol
        entry["lastUpdated"] = time.time()
        self._notify_subscribers(
            "path_use_recorded",
            local_path=local_path,
            remote_path=remote_path,
            protocol=protocol,
        )

    def get_protocol_score(self, protocol):
        entry = self.protocol_cache.get(protocol)
        policy = self.protocol_policy.get(protocol, {})
        score = policy.get("preferenceAdjustment", 0)
        if entry is None:
            return score
        score += (entry["successes"] * 3) - (entry["failures"] * 2)
        if entry["lastOutcome"] == "success":
            score += 1
        elif entry["lastOutcome"] == "failure":
            score -= 1
            cooldown = policy.get("racingCooldown", 0)
            if cooldown and entry["lastUpdated"] is not None:
                if (time.time() - entry["lastUpdated"]) < cooldown:
                    score -= max(1, entry["consecutiveFailures"])
        return score

    def get_path_score(
        self,
        local_path,
        remote_path,
        protocol=None,
        *,
        network_id=None,
    ):
        advisory_penalty = 0
        advisory_candidates = []
        exact_advisory = self.path_advisories.get((local_path, remote_path))
        if exact_advisory is not None:
            advisory_candidates.append(((local_path, remote_path), exact_advisory))
        if local_path is None:
            for key, values in self.path_advisories.items():
                if key[1] == remote_path and (key, values) not in advisory_candidates:
                    advisory_candidates.append((key, values))
        for key, advisory in advisory_candidates:
            expires_at = advisory.get("expiresAt")
            if expires_at is not None and expires_at < time.time():
                self.path_advisories.pop(key, None)
                continue
            advisory_penalty = max(advisory_penalty, advisory.get("penalty", 0))
        candidates = []
        exact = self.path_cache.get((local_path, remote_path))
        if exact is not None:
            candidates.append(exact)
        if local_path is None:
            for (cached_local, cached_remote), values in self.path_cache.items():
                if cached_remote == remote_path and values not in candidates:
                    candidates.append(values)
        best_score = None
        for entry in candidates:
            score = (entry["successes"] * 3) - (entry["failures"] * 2)
            if entry["lastOutcome"] == "success":
                score += 1
            elif entry["lastOutcome"] == "failure":
                score -= 1
                if entry["lastUpdated"] is not None and (
                    (time.time() - entry["lastUpdated"]) < 30
                ):
                    score -= max(1, entry["consecutiveFailures"])
            if protocol is not None and entry["lastProtocol"] == protocol:
                score += 1
            if best_score is None or score > best_score:
                best_score = score
        performance_score = self.get_performance_score(
            local_path,
            remote_path,
            protocol,
            network_id=network_id,
        )
        return (best_score or 0) + performance_score - advisory_penalty

    def get_alternate_remotes(self, base_remote, protocol=None):
        entries = self.alternate_remotes.get(base_remote, [])
        now = time.time()
        filtered = []
        retained = []
        for entry in entries:
            expires_at = entry.get("expiresAt")
            if expires_at is not None and expires_at < now:
                continue
            retained.append(entry)
            if protocol is None or entry.get("protocol") in {None, protocol}:
                filtered.append(
                    (
                        entry.get("family"),
                        entry["address"],
                    )
                )
        self.alternate_remotes[base_remote] = retained
        return filtered

    def record_resolution(
        self,
        path,
        host_name,
        protocol,
        address_family,
        addresses,
        *,
        duration,
        error=None,
    ):
        key = (path, host_name, protocol, address_family)
        self.resolution_cache[key] = {
            "addresses": list(addresses),
            "duration": duration,
            "error": str(error) if error is not None else None,
            "lastUpdated": time.time(),
        }
        self._notify_subscribers(
            "resolution_recorded",
            path=path,
            host_name=host_name,
            protocol=protocol,
            address_family=address_family,
            success=error is None,
        )

    def get_cached_resolution(
        self,
        path,
        host_name,
        protocol,
        address_family,
        *,
        max_age=30,
    ):
        key = (path, host_name, protocol, address_family)
        entry = self.resolution_cache.get(key)
        if entry is None:
            return None
        if (time.time() - entry["lastUpdated"]) > max_age:
            self.resolution_cache.pop(key, None)
            return None
        if entry.get("error") is not None:
            return None
        return list(entry["addresses"])

    def get_health_summary(self):
        degraded_paths = 0
        now = time.time()
        for key, advisory in list(self.path_advisories.items()):
            expires_at = advisory.get("expiresAt")
            if expires_at is not None and expires_at < now:
                self.path_advisories.pop(key, None)
                continue
            degraded_paths += 1
        protocol_failures = sum(
            values.get("consecutiveFailures", 0)
            for values in self.protocol_cache.values()
        )
        unavailable_interfaces = sum(
            1 for values in self.interface_policy.values()
            if values.get("available") is False
        )
        severity = "healthy"
        if degraded_paths or protocol_failures or unavailable_interfaces:
            severity = "warning"
        if degraded_paths >= 2 or protocol_failures >= 2:
            severity = "degraded"
        return {
            "severity": severity,
            "degradedPathCount": degraded_paths,
            "protocolFailureCount": protocol_failures,
            "unavailableInterfaceCount": unavailable_interfaces,
            "activeConnectionCount": self.connection_counts["active"],
            "activeListenerCount": self.listener_counts["active"],
        }

    def get_operational_guidance(self):
        guidance = []
        health = self.get_health_summary()
        if health["degradedPathCount"]:
            guidance.append("One or more cached paths are degraded.")
        if health["protocolFailureCount"]:
            guidance.append("Recent protocol failures should influence new connection attempts.")
        if health["unavailableInterfaceCount"]:
            guidance.append("Some interfaces are currently unavailable per system policy.")
        if self.connection_counts["active"] > self.connection_counts["ready"]:
            guidance.append("There are connections still establishing or recovering.")
        return guidance

    def get_adaptive_policy(self):
        protocol_recommendations = {}
        for protocol, values in self.protocol_cache.items():
            consecutive_failures = values.get("consecutiveFailures", 0)
            recommendation = "normal"
            if consecutive_failures >= 2:
                recommendation = "cooldown"
            elif values.get("lastOutcome") == "success":
                recommendation = "preferred"
            protocol_recommendations[protocol] = {
                "recommendation": recommendation,
                "consecutiveFailures": consecutive_failures,
                "lastOutcome": values.get("lastOutcome"),
            }

        path_recommendations = []
        for (local_path, remote_path), values in self.path_cache.items():
            score = self.get_path_score(local_path, remote_path, values.get("lastProtocol"))
            advisory = self.get_path_advisory(local_path, remote_path)
            path_recommendations.append(
                {
                    "local": local_path,
                    "remote": remote_path,
                    "recommendation": "avoid" if advisory is not None else "normal",
                    "score": score,
                    "lastProtocol": values.get("lastProtocol"),
                }
            )

        return {
            "protocols": protocol_recommendations,
            "paths": path_recommendations,
        }

    def get_snapshot(self):
        return {
            "createdAt": self.created_at,
            "templateCreations": self.template_creations,
            "connectionGroups": self.connection_groups,
            "connectionCounts": dict(self.connection_counts),
            "listenerCounts": dict(self.listener_counts),
            "eventCounters": dict(self.event_counters),
            "recentEvents": list(self.recent_events),
            "healthSummary": self.get_health_summary(),
            "operationalGuidance": self.get_operational_guidance(),
            "adaptivePolicy": self.get_adaptive_policy(),
            "systemPolicy": {
                "source": self.system_policy_source,
                "generation": self.system_policy_generation,
                "lastUpdated": self.system_policy_updated_at,
                "protocols": {
                    protocol: dict(values)
                    for protocol, values in self.protocol_policy.items()
                },
                "interfaces": {
                    interface_id: dict(values)
                    for interface_id, values in self.interface_policy.items()
                },
                "pvds": {
                    pvd_id: dict(values)
                    for pvd_id, values in self.pvd_policy.items()
                },
                "addressFamilies": dict(self.address_family_policy),
            },
            "alternateRemotes": {
                base_remote: [
                    dict(values)
                    for values in entries
                ]
                for base_remote, entries in self.alternate_remotes.items()
            },
            "pathAdvisories": [
                {
                    "local": local_path,
                    "remote": remote_path,
                    **values,
                }
                for (local_path, remote_path), values in self.path_advisories.items()
            ],
            "pathTransitions": [
                {
                    "previousLocal": previous_local,
                    "previousRemote": previous_remote,
                    "currentLocal": current_local,
                    "currentRemote": current_remote,
                    "protocol": protocol,
                    **values,
                }
                for (
                    previous_local,
                    previous_remote,
                    current_local,
                    current_remote,
                    protocol,
                ), values in self.path_transitions.items()
            ],
            "protocolCache": {
                protocol: dict(values)
                for protocol, values in self.protocol_cache.items()
            },
            "pathCache": [
                {
                    "local": local_path,
                    "remote": remote_path,
                    **values,
                }
                for (local_path, remote_path), values in self.path_cache.items()
            ],
            "resolutionCache": [
                {
                    "path": path,
                    "hostName": host_name,
                    "protocol": protocol,
                    "addressFamily": address_family,
                    **values,
                }
                for (
                    path,
                    host_name,
                    protocol,
                    address_family,
                ), values in self.resolution_cache.items()
            ],
            "performanceCache": self.get_performance_cache_snapshot(),
            "quicSessionCache": self.get_quic_session_cache_snapshot(),
        }

    def clone(self):
        cloned = ConnectionContext()
        cloned.created_at = self.created_at
        cloned.protocol_cache = {
            protocol: dict(values)
            for protocol, values in self.protocol_cache.items()
        }
        cloned.path_cache = {
            key: dict(values)
            for key, values in self.path_cache.items()
        }
        cloned.resolution_cache = {
            key: {
                **values,
                "addresses": list(values["addresses"]),
            }
            for key, values in self.resolution_cache.items()
        }
        cloned.event_counters = dict(self.event_counters)
        cloned.recent_events = [dict(values) for values in self.recent_events]
        cloned.template_creations = self.template_creations
        cloned.connection_counts = dict(self.connection_counts)
        cloned.listener_counts = dict(self.listener_counts)
        cloned.connection_groups = self.connection_groups
        cloned.protocol_policy = {
            protocol: dict(values)
            for protocol, values in self.protocol_policy.items()
        }
        cloned.interface_policy = {
            interface_id: dict(values)
            for interface_id, values in self.interface_policy.items()
        }
        cloned.pvd_policy = {
            pvd_id: dict(values)
            for pvd_id, values in self.pvd_policy.items()
        }
        cloned.address_family_policy = dict(self.address_family_policy)
        cloned.system_policy_source = self.system_policy_source
        cloned.system_policy_generation = self.system_policy_generation
        cloned.system_policy_updated_at = self.system_policy_updated_at
        cloned.alternate_remotes = {
            base_remote: [dict(values) for values in entries]
            for base_remote, entries in self.alternate_remotes.items()
        }
        cloned.path_advisories = {
            key: dict(values)
            for key, values in self.path_advisories.items()
        }
        cloned.path_transitions = {
            key: dict(values)
            for key, values in self.path_transitions.items()
        }
        # Privacy-sensitive session and performance caches are not copied into
        # an isolated context.
        cloned.subscribers = list(self.subscribers)
        return cloned
