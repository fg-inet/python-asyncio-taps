import time


class ConnectionContext:
    """Shared cached state and monitoring for related Connections."""

    def __init__(self):
        self.created_at = time.time()
        self.protocol_cache = {}
        self.path_cache = {}
        self.event_counters = {}
        self.connection_groups = 0

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
                "lastError": None,
                "lastOutcome": None,
            },
        )
        if success:
            entry["successes"] += 1
            entry["lastOutcome"] = "success"
            entry["lastError"] = None
        else:
            entry["failures"] += 1
            entry["lastOutcome"] = "failure"
            entry["lastError"] = str(error) if error is not None else None

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
                "lastProtocol": None,
                "lastOutcome": None,
                "lastError": None,
                "lastUpdated": None,
            },
        )
        if success:
            entry["successes"] += 1
            entry["lastOutcome"] = "success"
            entry["lastError"] = None
        else:
            entry["failures"] += 1
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
        if entry is None:
            return 0
        score = (entry["successes"] * 3) - (entry["failures"] * 2)
        if entry["lastOutcome"] == "success":
            score += 1
        elif entry["lastOutcome"] == "failure":
            score -= 1
        return score

    def get_path_score(self, local_path, remote_path, protocol=None):
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
            if protocol is not None and entry["lastProtocol"] == protocol:
                score += 1
            if best_score is None or score > best_score:
                best_score = score
        return best_score or 0

    def get_snapshot(self):
        return {
            "createdAt": self.created_at,
            "connectionGroups": self.connection_groups,
            "eventCounters": dict(self.event_counters),
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
