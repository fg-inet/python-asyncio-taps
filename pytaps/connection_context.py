import time


class ConnectionContext:
    """Shared cached state and monitoring for related Connections."""

    def __init__(self):
        self.created_at = time.time()
        self.protocol_cache = {}
        self.path_cache = {}
        self.event_counters = {}
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

    def set_pvd_policy(self, pvd_id, *, available=True, preference_adjustment=0):
        self.pvd_policy[pvd_id] = {
            "available": available,
            "preferenceAdjustment": preference_adjustment,
            "lastUpdated": time.time(),
        }

    def set_address_family_policy(self, family, preference_adjustment=0):
        normalized = family.lower()
        if normalized not in {"ipv4", "ipv6"}:
            raise KeyError(f"Unsupported address family policy: {family}")
        self.address_family_policy[normalized] = preference_adjustment

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

    def clear_path_degradation(self, local_path, remote_path):
        self.path_advisories.pop((local_path, remote_path), None)

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

    def attach_group(self):
        self.connection_groups += 1

    def detach_group(self):
        if self.connection_groups > 0:
            self.connection_groups -= 1

    def record_event(self, name):
        self.event_counters[name] = self.event_counters.get(name, 0) + 1

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

    def get_snapshot(self):
        return {
            "createdAt": self.created_at,
            "connectionGroups": self.connection_groups,
            "eventCounters": dict(self.event_counters),
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
        return cloned
