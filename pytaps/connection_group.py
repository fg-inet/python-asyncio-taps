import asyncio

from .transportProperties import CONNECTION_PROPERTY_DEFAULTS


# Bytes a Round-Robin per Packet round bundles when the Connection does not
# report a smaller non-fragmenting Message size.
DEFAULT_PACKET_BYTES = 1500


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

    def _scheduler(self):
        return self.shared_connection_properties.get(
            "connScheduler",
            CONNECTION_PROPERTY_DEFAULTS["connScheduler"],
        )

    @staticmethod
    def _message_bytes(entry):
        """Length of one queued Message, as the schedulers account for it."""
        data = entry.get("data")
        try:
            length = len(data)
        except TypeError:
            return 1
        # A zero-length Message still consumes a scheduling turn.
        return max(1, length)

    @staticmethod
    def _connection_weight(connection):
        """Weighted Fair Queueing weight derived from connPriority.

        RFC 9622 Section 8.1.2 gives higher priority to numerically lower
        connPriority values, so the weight is its reciprocal: a Connection of
        priority 0 gets twice the capacity of one at priority 1, matching the
        ratio rule of RFC 8260 Section 3.6.
        """
        priority = connection.transport_properties.get("connPriority")
        if not isinstance(priority, (int, float)) or priority < 0:
            priority = CONNECTION_PROPERTY_DEFAULTS["connPriority"]
        return 1.0 / (1.0 + priority)

    @staticmethod
    def _packet_bytes(connection):
        """Bytes a Round-Robin per Packet round bundles for one Connection."""
        limit = None
        send_limits = getattr(connection, "_send_limits", None)
        if callable(send_limits):
            limit = send_limits().get("singularTransmissionMsgMaxLen")
        if isinstance(limit, int) and 0 < limit < DEFAULT_PACKET_BYTES:
            return limit
        return DEFAULT_PACKET_BYTES

    def _fair_queue_order(self, queues, weights):
        """Interleave Connections by virtual finish time.

        This is the shared core of the two capacity-aware schedulers: each
        Message advances its Connection's virtual clock by its length divided
        by the Connection's weight, and Messages are then sent in virtual
        finish order. Equal weights give every Connection an equal share of
        the capacity (RFC 8260 Section 3.5); weights taken from connPriority
        share it in proportion (Section 3.6).
        """
        scheduled = []
        for connection, entries in queues.items():
            clock = 0.0
            weight = weights(connection)
            for entry in entries:
                clock += self._message_bytes(entry) / weight
                scheduled.append((clock, connection, entry))
        scheduled.sort(
            key=lambda item: (
                1 if item[2]["context"].final else 0,
                item[0],
                item[1].transport_properties.get("connPriority"),
                item[2]["enqueueOrder"],
            )
        )
        return [(connection, entry) for _clock, connection, entry in scheduled]

    def _round_robin_order(self, queues, *, per_packet):
        """Cycle around the non-empty Connection queues.

        Plain Round-Robin switches Connection after every Message (RFC 8260
        Section 3.2). The per-packet variant keeps taking Messages from one
        Connection until it has filled a packet, so a lost packet only affects
        a single Connection (Section 3.3).
        """
        ordered_connections = sorted(
            queues,
            key=lambda connection: min(
                entry["enqueueOrder"] for entry in queues[connection]
            ),
        )
        cursors = {connection: 0 for connection in ordered_connections}
        scheduled = []
        while True:
            progressed = False
            for connection in ordered_connections:
                entries = queues[connection]
                index = cursors[connection]
                if index >= len(entries):
                    continue
                progressed = True
                if not per_packet:
                    scheduled.append((connection, entries[index]))
                    cursors[connection] = index + 1
                    continue
                budget = self._packet_bytes(connection)
                filled = 0
                while index < len(entries) and filled < budget:
                    entry = entries[index]
                    scheduled.append((connection, entry))
                    filled += self._message_bytes(entry)
                    index += 1
                cursors[connection] = index
            if not progressed:
                break
        # A Final Message is sorted to the end of the whole group regardless of
        # the scheduler (RFC 9622 Section 9.1.3.5).
        return sorted(
            scheduled,
            key=lambda pair: 1 if pair[1]["context"].final else 0,
        )

    def _scheduled_entries(self, queues, scheduler):
        """Order queued Messages of the whole group for one transmission round.

        ``queues`` maps each Connection onto the Messages it has enqueued, each
        already sorted by the Connection's own send order. RFC 9622
        Section 8.1.5 selects the scheduler from the set of RFC 8260
        Section 3; Section 9.2.6 requires that connPriority is ordered over
        msgPriority for the priority-aware ones.
        """
        if scheduler == "First-Come, First-Served":
            # Pure arrival order across the whole group, priorities ignored.
            return sorted(
                (
                    (connection, entry)
                    for connection, entries in queues.items()
                    for entry in entries
                ),
                key=lambda pair: (
                    1 if pair[1]["context"].final else 0,
                    pair[1]["enqueueOrder"],
                ),
            )

        if scheduler == "Priority-Based":
            # Strict priority: every Message of a higher-priority Connection
            # precedes every Message of a lower-priority one.
            return sorted(
                (
                    (connection, entry)
                    for connection, entries in queues.items()
                    for entry in entries
                ),
                key=lambda pair: (
                    1 if pair[1]["context"].final else 0,
                    pair[0].transport_properties.get("connPriority"),
                    pair[1]["context"].priority,
                    pair[1]["enqueueOrder"],
                ),
            )

        if scheduler == "Weighted Fair Queueing":
            return self._fair_queue_order(queues, self._connection_weight)

        if scheduler == "Fair Capacity":
            # Equal capacity for every Connection, so every weight is equal.
            return self._fair_queue_order(queues, lambda connection: 1.0)

        return self._round_robin_order(
            queues,
            per_packet=scheduler == "Round-Robin per Packet",
        )

    async def flush_messages(self):
        """Send every Message enqueued on any Connection of this group.

        This realizes the Connection Group transmission scheduler of RFC 9622
        Section 8.1.5 and the connPriority-over-msgPriority ordering of
        Section 9.2.6. Per RFC 8260 Section 3, the scheduler is selected at the
        sender and is never signalled to the peer.
        """
        queues = {}
        for connection in self.connections:
            if not connection._queued_messages:
                continue
            queues[connection] = connection._sorted_queued_messages()
            connection._queued_messages = []
        if not queues:
            return []

        scheduled = self._scheduled_entries(queues, self._scheduler())
        message_ids = []
        for connection, entry in scheduled:
            message_ids.append(entry["context"].message_id)
            connection._dispatch_queued_entry(entry)
        return message_ids

    def get_properties(self):
        return {
            "size": len(self.connections),
            "connScheduler": self._scheduler(),
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
