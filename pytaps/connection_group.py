import asyncio

from .transportProperties import CONNECTION_PROPERTY_DEFAULTS


class ConnectionGroup:
    """Entangled Connection state shared by cloned or multistream Connections."""

    ENTANGLED_PROPERTIES = set(CONNECTION_PROPERTY_DEFAULTS) - {"connPriority"}

    def __init__(self, initial_connection=None):
        self.connections = []
        self.shared_connection_properties = {}
        self.connection_context = None
        self._peer_connections = set()
        if initial_connection is not None:
            self.add_connection(initial_connection)

    def __iter__(self):
        return iter(self.connections)

    def __len__(self):
        return len(self.connections)

    def add_connection(self, connection, *, from_peer=False):
        current_group = getattr(connection, "connection_group", None)
        if current_group is not None and current_group is not self:
            current_group.remove_connection(connection)
        if self.connection_context is None:
            self.connection_context = connection.connection_context
            self.connection_context.attach_group()
            self.shared_connection_properties = {
                prop: connection.transport_properties.get(prop)
                for prop in self.ENTANGLED_PROPERTIES
            }
        limit = self.shared_connection_properties.get("groupConnLimit")
        if (
            from_peer
            and isinstance(limit, int)
            and limit >= 0
            and connection not in self._peer_connections
            and len(self._peer_connections) >= limit
        ):
            raise RuntimeError("ConnectionGroup limit reached")
        if connection not in self.connections:
            self.connections.append(connection)
        if from_peer:
            self._peer_connections.add(connection)
        connection.connection_group = self
        self._transfer_connection_context(connection)
        connection.connection_context = self.connection_context
        self._apply_shared_properties(connection)
        return connection

    def remove_connection(self, connection):
        self.connections = [candidate for candidate in self.connections
                            if candidate is not connection]
        self._peer_connections.discard(connection)
        if getattr(connection, "connection_group", None) is self:
            connection.connection_group = None
        if not self.connections and self.connection_context is not None:
            self.connection_context.detach_group()
            self.connection_context = None

    def set_property(self, prop, value):
        if prop not in self.ENTANGLED_PROPERTIES:
            return
        self.shared_connection_properties[prop] = value
        for connection in self.connections:
            connection.transport_properties.connection_properties[prop] = value

    async def close(self):
        connections = list(self.connections)
        close_tasks = [
            task
            for connection in connections
            if (task := connection.close()) is not None
        ]
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)
        if connections:
            await asyncio.gather(
                *(connection.wait_closed() for connection in connections),
                return_exceptions=True,
            )
        return self

    async def abort(self):
        connections = list(self.connections)
        for connection in connections:
            connection.abort(reason="Connection group aborted")
        if connections:
            await asyncio.gather(
                *(connection.wait_closed() for connection in connections),
                return_exceptions=True,
            )
        return self

    def get_properties(self):
        return {
            "size": len(self.connections),
            "connections": list(self.connections),
            "sharedConnectionProperties": dict(self.shared_connection_properties),
            "connectionContext": (
                self.connection_context.get_snapshot()
                if self.connection_context is not None else None
            ),
        }

    def _apply_shared_properties(self, connection):
        for prop, value in self.shared_connection_properties.items():
            connection.transport_properties.connection_properties[prop] = value

    def _transfer_connection_context(self, connection):
        previous_context = connection.connection_context
        if previous_context is self.connection_context:
            return
        previous_context.detach_connection_for_transfer(
            was_ready=connection._context_ready_recorded,
        )
        self.connection_context.attach_connection()
        if connection._context_ready_recorded:
            self.connection_context.mark_connection_ready()
