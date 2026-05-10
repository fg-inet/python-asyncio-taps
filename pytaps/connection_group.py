class ConnectionGroup:
    """Minimal Connection Group support for cloned Connections."""

    ENTANGLED_PROPERTIES = {
        "connTimeout",
        "keepAliveTimeout",
        "connScheduler",
        "connCapacityProfile",
        "multipathPolicy",
        "minSendRate",
        "minRecvRate",
        "maxSendRate",
        "maxRecvRate",
        "groupConnLimit",
        "isolateSession",
        "tcp.userTimeoutValue",
        "tcp.userTimeoutEnabled",
        "tcp.userTimeoutChangeable",
    }

    def __init__(self, initial_connection=None):
        self.connections = []
        self.shared_connection_properties = {}
        if initial_connection is not None:
            self.add_connection(initial_connection)

    def __iter__(self):
        return iter(self.connections)

    def __len__(self):
        return len(self.connections)

    def add_connection(self, connection):
        limit = self.shared_connection_properties.get("groupConnLimit")
        if (
            isinstance(limit, int)
            and limit >= 0
            and connection not in self.connections
            and len(self.connections) >= limit
        ):
            raise RuntimeError("ConnectionGroup limit reached")
        if connection not in self.connections:
            self.connections.append(connection)
        connection.connection_group = self
        self._apply_shared_properties(connection)

    def remove_connection(self, connection):
        self.connections = [candidate for candidate in self.connections
                            if candidate is not connection]
        if getattr(connection, "connection_group", None) is self:
            connection.connection_group = None

    def set_property(self, prop, value):
        if prop not in self.ENTANGLED_PROPERTIES:
            return
        self.shared_connection_properties[prop] = value
        for connection in self.connections:
            connection.transport_properties.connection_properties[prop] = value
        if (
            prop == "groupConnLimit"
            and isinstance(value, int)
            and value >= 0
            and len(self.connections) > value
        ):
            raise RuntimeError("ConnectionGroup already exceeds the new groupConnLimit")

    async def close(self):
        for connection in list(self.connections):
            connection.close()

    async def abort(self):
        for connection in list(self.connections):
            connection.abort(reason="Connection group aborted")

    def get_properties(self):
        return {
            "size": len(self.connections),
            "connections": list(self.connections),
            "sharedConnectionProperties": dict(self.shared_connection_properties),
        }

    def _apply_shared_properties(self, connection):
        for prop, value in self.shared_connection_properties.items():
            connection.transport_properties.connection_properties[prop] = value
