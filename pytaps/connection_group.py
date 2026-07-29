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
        self.current_path = {
            "local": None,
            "remote": None,
        }
        self.previous_path = {
            "local": None,
            "remote": None,
        }
        self.path_change_count = 0
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
        if (
            self.current_path["local"] is not None
            or self.current_path["remote"] is not None
        ):
            connection._current_path = self.current_path.copy()
            connection._previous_path = self.previous_path.copy()
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

    def set_property(self, prop, value, *, explicit=True):
        if prop not in self.ENTANGLED_PROPERTIES:
            return
        proposals = []
        for connection in self.connections:
            proposed = connection._propose_connection_property(
                prop,
                value,
                explicit=explicit,
            )
            normalized = proposed.get(prop)
            connection._validate_connection_property(
                prop,
                normalized,
                transport_properties=proposed,
            )
            proposals.append((connection, proposed))

        previous_shared = self.shared_connection_properties.copy()
        previous_state = [
            (
                connection,
                connection.transport_properties,
                connection._backend_property_effects.copy(),
            )
            for connection, _proposed in proposals
        ]
        normalized = (
            proposals[0][1].get(prop)
            if proposals
            else value
        )
        try:
            self.shared_connection_properties[prop] = normalized
            for connection, proposed in proposals:
                connection.transport_properties = proposed
            for connection, _proposed in proposals:
                connection._apply_connection_properties(
                    strict_property=prop,
                )
        except Exception:
            self.shared_connection_properties = previous_shared
            for connection, properties, effects in previous_state:
                connection.transport_properties = properties
                connection._backend_property_effects = effects
            for connection, _properties, _effects in previous_state:
                try:
                    connection._apply_connection_properties(
                        strict_property=prop,
                    )
                except Exception:
                    pass
            raise

    def set_derived_property(self, prop, value):
        if prop not in self.ENTANGLED_PROPERTIES:
            return
        self.shared_connection_properties[prop] = value
        for connection in self.connections:
            connection.transport_properties.connection_properties[prop] = value

    def note_path_change(
        self,
        previous_path,
        current_path,
        *,
        initial=False,
    ):
        if current_path == self.current_path:
            return self.current_path.copy()
        self.previous_path = previous_path.copy()
        self.current_path = current_path.copy()
        if not initial:
            self.path_change_count += 1
        return self.current_path.copy()

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
        close_tasks = [
            connection._close_task
            for connection in connections
            if connection._close_task is not None
        ]
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)
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
            "currentPath": self.current_path.copy(),
            "previousPath": self.previous_path.copy(),
            "pathChangeCount": self.path_change_count,
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
            connection,
            was_ready=connection._context_ready_recorded,
        )
        self.connection_context.attach_connection(connection)
        if connection._context_ready_recorded:
            self.connection_context.mark_connection_ready()
