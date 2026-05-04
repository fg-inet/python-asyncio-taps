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

    def add_connection(self, connection):
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

    def _apply_shared_properties(self, connection):
        for prop, value in self.shared_connection_properties.items():
            connection.transport_properties.connection_properties[prop] = value
