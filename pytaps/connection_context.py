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

    def record_path_use(self, local_path, remote_path, protocol=None):
        key = (local_path, remote_path)
        entry = self.path_cache.setdefault(
            key,
            {
                "uses": 0,
                "lastProtocol": None,
                "lastUpdated": None,
            },
        )
        entry["uses"] += 1
        entry["lastProtocol"] = protocol
        entry["lastUpdated"] = time.time()

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
