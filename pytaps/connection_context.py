import inspect
import time

from .utility import schedule_callback


class ConnectionContext:
    """Shared cached state and monitoring for related Connections."""

    def __init__(self):
        self.created_at = time.time()
        self.protocol_cache = {}
        self.path_cache = {}
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
        self.alternate_remotes = {}
        self.path_advisories = {}
        self.path_transitions = {}
        self.subscribers = []

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

    def attach_connection(self):
        self.connection_counts["active"] += 1
        self._notify_subscribers("connection_attached")

    def mark_connection_ready(self):
        self.connection_counts["ready"] += 1
        self._notify_subscribers("connection_ready")

    def detach_connection(self, *, was_ready=False):
        if self.connection_counts["active"] > 0:
            self.connection_counts["active"] -= 1
        if was_ready and self.connection_counts["ready"] > 0:
            self.connection_counts["ready"] -= 1
        self.connection_counts["closed"] += 1
        self._notify_subscribers("connection_detached", was_ready=was_ready)

    def detach_connection_for_transfer(self, *, was_ready=False):
        if self.connection_counts["active"] > 0:
            self.connection_counts["active"] -= 1
        if was_ready and self.connection_counts["ready"] > 0:
            self.connection_counts["ready"] -= 1
        self._notify_subscribers("connection_context_transferred")

    def attach_listener(self):
        self.listener_counts["active"] += 1
        self._notify_subscribers("listener_attached")

    def mark_listener_listening(self):
        self.listener_counts["listening"] += 1
        self._notify_subscribers("listener_listening")

    def detach_listener(self, *, was_listening=False):
        if self.listener_counts["active"] > 0:
            self.listener_counts["active"] -= 1
        if was_listening and self.listener_counts["listening"] > 0:
            self.listener_counts["listening"] -= 1
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
        self._notify_subscribers("interface_policy_updated", interface=interface_id)

    def set_pvd_policy(self, pvd_id, *, available=True, preference_adjustment=0):
        self.pvd_policy[pvd_id] = {
            "available": available,
            "preferenceAdjustment": preference_adjustment,
            "lastUpdated": time.time(),
        }
        self._notify_subscribers("pvd_policy_updated", pvd=pvd_id)

    def set_address_family_policy(self, family, preference_adjustment=0):
        normalized = family.lower()
        if normalized not in {"ipv4", "ipv6"}:
            raise KeyError(f"Unsupported address family policy: {family}")
        self.address_family_policy[normalized] = preference_adjustment
        self._notify_subscribers("address_family_policy_updated", family=normalized)

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

    def get_path_score(self, local_path, remote_path, protocol=None):
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
        if not candidates:
            return 0

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
        return (best_score or 0) - advisory_penalty

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
        cloned.subscribers = list(self.subscribers)
        return cloned
