import asyncio
import contextvars
from dataclasses import dataclass, field

from .message import MessageContext
from .utility import _CURRENT_CANDIDATE_VIEW


class DeframingFailed(Exception):
    """The Framer could not turn the available bytes into a Message."""


class FramerFailed(ConnectionError):
    """A Framer rejected or fatally failed a candidate Protocol Stack."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(f"Framer failed the Connection: {reason}")


_CURRENT_BINDING = contextvars.ContextVar(
    "pytaps_current_framer_binding",
    default=None,
)
_CURRENT_SEND = contextvars.ContextVar(
    "pytaps_current_framer_send",
    default=None,
)


@dataclass
class _ReceiveSpan:
    length: int
    context: MessageContext
    end_of_message: bool


class _ReceiveBuffer:
    def __init__(self):
        self.data = bytearray()
        self.spans = []

    @property
    def available(self):
        return len(self.data)

    @property
    def context(self):
        return self.spans[0].context if self.spans else None

    def append(self, data, context, end_of_message):
        data = bytes(data)
        if not data:
            if end_of_message and self.spans:
                self.spans[-1].end_of_message = True
            return
        self.data.extend(data)
        self.spans.append(
            _ReceiveSpan(
                len(data),
                context,
                bool(end_of_message),
            )
        )

    def mark_end(self):
        if self.spans:
            self.spans[-1].end_of_message = True

    def peek(self, minimum, maximum):
        if not self.spans:
            return None, None, False

        boundary = None
        offset = 0
        for span in self.spans:
            offset += span.length
            if span.end_of_message:
                boundary = offset
                break

        available = boundary if boundary is not None else self.available
        if available < minimum and boundary is None:
            return None, None, False

        length = min(available, maximum)
        end_of_message = boundary is not None and length == boundary
        return bytes(self.data[:length]), self.spans[0].context, end_of_message

    def advance(self, length):
        if length < 0 or length > self.available:
            raise ValueError("Receive cursor advance exceeds available data")
        if length == 0:
            return

        del self.data[:length]
        remaining = length
        while remaining:
            span = self.spans[0]
            if remaining < span.length:
                span.length -= remaining
                remaining = 0
            else:
                remaining -= span.length
                self.spans.pop(0)

    def pop_span(self):
        if not self.spans:
            return None
        span = self.spans[0]
        data = bytes(self.data[:span.length])
        context = span.context
        end_of_message = span.end_of_message
        self.advance(span.length)
        return data, context, end_of_message


@dataclass
class _PendingDelivery:
    context: MessageContext
    remaining: int
    end_of_message: bool
    data: bytearray = field(default_factory=bytearray)


@dataclass
class _FramerBinding:
    framer: object
    stack: object
    position: int
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    event_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_deliveries: list = field(default_factory=list)
    started: bool = False
    stopped: bool = False
    defer_ready: bool = False
    defer_closed: bool = False
    passthrough: bool = False


@dataclass
class _SendInvocation:
    binding: _FramerBinding
    called_send: bool = False
    result: object = None


class Framer:
    """Base class for an RFC 9623-style Message Framer.

    A Framer implementation overrides the event handlers. The helper actions
    are valid while handling an event for a Connection. A single Framer object
    may be added to multiple Preconnections; runtime state is maintained by
    each candidate's private Framer stack.
    """

    def __init__(self, event_loop=None, *, namespace=None):
        if event_loop is not None:
            self.loop = event_loop
        else:
            try:
                self.loop = asyncio.get_running_loop()
            except RuntimeError:
                self.loop = None
        self.namespace = namespace or (
            f"{self.__class__.__module__}.{self.__class__.__qualname__}"
        )

    async def start(self, connection):
        """Handle creation of a candidate Protocol Stack."""

    async def stop(self, connection):
        """Handle teardown before the Connection emits ``Closed``."""

    async def new_sent_message(
        self,
        connection,
        data,
        context,
        end_of_message,
    ):
        """Frame one outbound Message.

        Implementations may either return transformed data or call
        :meth:`send` explicitly.
        """

        return await self.send(
            connection,
            data,
            context,
            end_of_message,
        )

    async def handle_received_data(self, connection):
        """Parse newly available inbound data and deliver Messages."""

        raise NotImplementedError

    def _binding(self, connection):
        binding = _CURRENT_BINDING.get()
        if (
            binding is not None
            and binding.framer is self
            and binding.stack.connection is connection
        ):
            return binding

        stack = getattr(connection, "framer_stack", None)
        if stack is None:
            raise RuntimeError("The Framer is not active on this Connection")
        matches = [
            candidate
            for candidate in stack.bindings
            if candidate.framer is self
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "The Framer action is ambiguous outside a Framer event"
            )
        return matches[0]

    def defer_connection_ready(self, connection):
        """Delay Connection readiness until ``make_connection_ready``."""

        self._binding(connection).defer_ready = True

    def make_connection_ready(self, connection):
        """Mark this Framer's candidate setup as complete."""

        self._binding(connection).ready.set()

    def defer_connection_closed(self, connection):
        """Delay Connection closure until ``make_connection_closed``."""

        self._binding(connection).defer_closed = True

    def make_connection_closed(self, connection):
        """Mark this Framer's teardown as complete."""

        self._binding(connection).closed.set()

    def fail_connection(self, connection, error):
        """Fatally fail the current candidate or established Connection."""

        binding = self._binding(connection)
        binding.stack.fail(binding, error)

    def prepend_framer(self, connection, framer):
        """Add ``framer`` immediately above this Framer before readiness."""

        binding = self._binding(connection)
        return binding.stack.prepend(binding, framer)

    def start_passthrough(self, connection):
        """Stop intercepting data for this Framer."""

        binding = self._binding(connection)
        binding.stack.start_passthrough(binding)

    async def send(
        self,
        connection,
        data,
        context=None,
        end_of_message=True,
    ):
        """Send framed data to the next lower layer."""

        binding = self._binding(connection)
        return await binding.stack.send_from_framer(
            binding,
            data,
            context,
            end_of_message,
        )

    def parse(
        self,
        connection,
        minimum_incomplete_length=0,
        maximum_length=float("inf"),
    ):
        """Inspect bytes at this Framer's receive cursor."""

        binding = self._binding(connection)
        return binding.stack.parse(
            binding,
            minimum_incomplete_length,
            maximum_length,
        )

    def advance_receive_cursor(self, connection, length):
        """Discard ``length`` bytes from this Framer's receive cursor."""

        binding = self._binding(connection)
        binding.stack.advance_receive_cursor(binding, length)

    def deliver(
        self,
        connection,
        context,
        data,
        end_of_message=True,
    ):
        """Deliver allocated data to the next higher layer."""

        binding = self._binding(connection)
        return binding.stack.deliver(
            binding,
            context,
            data,
            end_of_message,
        )

    def deliver_and_advance_receive_cursor(
        self,
        connection,
        context,
        length,
        end_of_message=True,
    ):
        """Earmark and deliver ``length`` bytes from the receive cursor."""

        binding = self._binding(connection)
        return binding.stack.deliver_and_advance_receive_cursor(
            binding,
            context,
            length,
            end_of_message,
        )


class FramerStack:
    """A synchronized Framer stack bound to one candidate transport."""

    def __init__(self, connection, framers, transport):
        self.connection = connection
        self.transport = transport
        self.bindings = []
        self._layers = []
        self._send_lock = asyncio.Lock()
        self._receive_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._failure = None
        self._started = False
        self._stopped = False
        self._selected = False

        for framer in framers:
            self._append(framer)

    def __bool__(self):
        return bool(self.bindings)

    @property
    def framers(self):
        return tuple(binding.framer for binding in self.bindings)

    def _append(self, framer):
        if not isinstance(framer, Framer):
            raise TypeError("Framers must be Framer objects")
        binding = _FramerBinding(
            framer=framer,
            stack=self,
            position=len(self.bindings),
        )
        self.bindings.append(binding)
        self._layers.append(_ReceiveBuffer())
        return binding

    def _refresh_positions(self):
        for position, binding in enumerate(self.bindings):
            binding.position = position

    async def _invoke(self, binding, callback, *args):
        async with binding.event_lock:
            binding_token = _CURRENT_BINDING.set(binding)
            candidate_token = _CURRENT_CANDIDATE_VIEW.set(
                (self.connection, self._candidate_view())
            )
            try:
                return await callback(*args)
            finally:
                _CURRENT_CANDIDATE_VIEW.reset(candidate_token)
                _CURRENT_BINDING.reset(binding_token)

    def _candidate_view(self):
        local_endpoint = getattr(self.transport, "local_endpoint", None)
        remote_endpoint = getattr(self.transport, "remote_endpoint", None)
        local_endpoint = (
            local_endpoint.clone()
            if local_endpoint is not None
            else None
        )
        remote_endpoint = (
            remote_endpoint.clone()
            if remote_endpoint is not None
            else None
        )
        return {
            "local_endpoint": local_endpoint,
            "local_endpoints": (
                [local_endpoint] if local_endpoint is not None else []
            ),
            "remote_endpoint": remote_endpoint,
            "remote_endpoints": (
                [remote_endpoint] if remote_endpoint is not None else []
            ),
            "protocol": getattr(
                self.transport,
                "protocol_name",
                self.connection.protocol,
            ),
        }

    async def start(self):
        async with self._lifecycle_lock:
            if self._started:
                return

            position = 0
            while position < len(self.bindings):
                binding = self.bindings[position]
                if not binding.started:
                    binding.started = True
                    await self._invoke(
                        binding,
                        binding.framer.start,
                        self.connection,
                    )
                    self._raise_if_failed()
                    if not binding.defer_ready:
                        binding.ready.set()
                await binding.ready.wait()
                self._raise_if_failed()
                position += 1

            self._started = True

        await self._drain_received()

    async def stop(self):
        async with self._lifecycle_lock:
            if self._stopped:
                return

            for binding in reversed(self.bindings):
                if not binding.started or binding.stopped:
                    continue
                binding.stopped = True
                await self._invoke(
                    binding,
                    binding.framer.stop,
                    self.connection,
                )
                if not binding.defer_closed:
                    binding.closed.set()
                await binding.closed.wait()

            self._stopped = True

    def select(self):
        self._selected = True
        self.connection.framer_stack = self

    def fail(self, binding, error):
        if self._failure is not None:
            return
        if not isinstance(error, BaseException):
            error = RuntimeError(str(error))
        self._failure = FramerFailed(error)
        for candidate in self.bindings:
            candidate.ready.set()
            candidate.closed.set()
        if self._selected:
            self.connection.loop.create_task(
                self.transport._fail_from_framer(self._failure)
            )

    def _raise_if_failed(self):
        if self._failure is not None:
            raise self._failure

    def prepend(self, binding, framer):
        if binding.ready.is_set() or self._started:
            raise RuntimeError("A Framer can only be prepended before readiness")
        if not isinstance(framer, Framer):
            raise TypeError("Framers must be Framer objects")

        position = binding.position + 1
        new_binding = _FramerBinding(
            framer=framer,
            stack=self,
            position=position,
        )
        self.bindings.insert(position, new_binding)
        self._layers.insert(position, _ReceiveBuffer())
        self._refresh_positions()
        return framer

    def start_passthrough(self, binding):
        binding.passthrough = True

    async def frame_outbound(self, data, context, end_of_message):
        async with self._send_lock:
            self._raise_if_failed()
            return await self._frame_from(
                len(self.bindings) - 1,
                data,
                context,
                end_of_message,
            )

    async def _frame_from(self, position, data, context, end_of_message):
        while position >= 0 and self.bindings[position].passthrough:
            position -= 1
        if position < 0:
            return data

        binding = self.bindings[position]
        invocation = _SendInvocation(binding)
        send_token = _CURRENT_SEND.set(invocation)
        try:
            result = await self._invoke(
                binding,
                binding.framer.new_sent_message,
                self.connection,
                data,
                context,
                end_of_message,
            )
        finally:
            _CURRENT_SEND.reset(send_token)

        self._raise_if_failed()
        if invocation.called_send:
            return invocation.result
        if result is None:
            raise RuntimeError(
                "new_sent_message() must return data or call Framer.send()"
            )
        return await self._frame_from(
            position - 1,
            result,
            context,
            end_of_message,
        )

    async def send_from_framer(
        self,
        binding,
        data,
        context,
        end_of_message,
    ):
        context = context or MessageContext(end_of_message=end_of_message)
        invocation = _CURRENT_SEND.get()
        if invocation is not None and invocation.binding is binding:
            invocation.result = await self._frame_from(
                binding.position - 1,
                data,
                context,
                end_of_message,
            )
            invocation.called_send = True
            return invocation.result

        async with self._send_lock:
            framed = await self._frame_from(
                binding.position - 1,
                data,
                context,
                end_of_message,
            )
            await self.transport._write_framer_data(framed)
            return framed

    async def feed_received(self, data, context, end_of_message):
        self._raise_if_failed()
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("Framer input must be bytes-like")

        async with self._receive_lock:
            self._layers[0].append(data, context, end_of_message)
            await self._drain_received_locked()

    async def mark_end_of_stream(self):
        if not self.bindings:
            return
        async with self._receive_lock:
            self._layers[0].mark_end()
            await self._drain_received_locked()

    async def _drain_received(self):
        if self._receive_lock.locked():
            return
        async with self._receive_lock:
            await self._drain_received_locked()

    async def _drain_received_locked(self):
        while True:
            consumed_data = False
            for binding in list(self.bindings):
                if not binding.started:
                    continue

                if binding.pending_deliveries:
                    consumed_data |= self._fulfill_pending_delivery(binding)
                    if binding.pending_deliveries:
                        continue

                if binding.passthrough:
                    consumed_data |= self._drain_passthrough(binding)
                    continue

                layer = self._layers[binding.position]
                if layer.available == 0:
                    continue

                before = layer.available
                try:
                    await self._invoke(
                        binding,
                        binding.framer.handle_received_data,
                        self.connection,
                    )
                except DeframingFailed as error:
                    self.connection._report_receive_error(
                        layer.context,
                        error,
                    )
                except FramerFailed:
                    raise
                except Exception as error:
                    self.fail(binding, error)
                    self._raise_if_failed()

                self._raise_if_failed()
                if binding.passthrough:
                    self._drain_passthrough(binding)
                consumed_data |= layer.available < before

            if not consumed_data:
                return

    def _fulfill_pending_delivery(self, binding):
        layer = self._layers[binding.position]
        consumed = False
        while binding.pending_deliveries and layer.available:
            pending = binding.pending_deliveries[0]
            amount = min(pending.remaining, layer.available)
            pending.data.extend(layer.data[:amount])
            layer.advance(amount)
            pending.remaining -= amount
            consumed = True
            if pending.remaining == 0:
                binding.pending_deliveries.pop(0)
                self.deliver(
                    binding,
                    pending.context,
                    bytes(pending.data),
                    pending.end_of_message,
                )
        return consumed

    def _drain_passthrough(self, binding):
        layer = self._layers[binding.position]
        consumed = False
        while layer.spans:
            data, context, end_of_message = layer.pop_span()
            self.deliver(
                binding,
                context,
                data,
                end_of_message,
            )
            consumed = True
        return consumed

    @staticmethod
    def _parse_lengths(minimum, maximum):
        if (
            not isinstance(minimum, int)
            or isinstance(minimum, bool)
            or minimum < 0
        ):
            raise ValueError(
                "minimum_incomplete_length must be a non-negative Integer"
            )
        if (
            not isinstance(maximum, (int, float))
            or isinstance(maximum, bool)
            or maximum <= 0
            or (
                isinstance(maximum, float)
                and maximum != float("inf")
            )
        ):
            raise ValueError(
                "maximum_length must be a positive Integer or infinity"
            )
        if maximum < minimum:
            raise ValueError(
                "maximum_length cannot be less than minimum_incomplete_length"
            )
        return minimum, maximum

    def parse(self, binding, minimum, maximum):
        minimum, maximum = self._parse_lengths(minimum, maximum)
        layer = self._layers[binding.position]
        return layer.peek(minimum, maximum)

    def advance_receive_cursor(self, binding, length):
        if not isinstance(length, int) or isinstance(length, bool):
            raise TypeError("Receive cursor length must be an Integer")
        self._layers[binding.position].advance(length)

    def deliver(self, binding, context, data, end_of_message):
        if context is None:
            context = (
                self._layers[binding.position].context
                or MessageContext()
            )
        if not isinstance(context, MessageContext):
            raise TypeError("Framer delivery context must be a MessageContext")
        context.end_of_message = bool(end_of_message)

        next_position = binding.position + 1
        if next_position < len(self.bindings):
            if not isinstance(data, (bytes, bytearray, memoryview)):
                raise TypeError(
                    "Data passed between Framers must be bytes-like"
                )
            self._layers[next_position].append(
                data,
                context,
                end_of_message,
            )
            return None

        self.transport.framer_buffer.append(
            (data, context, bool(end_of_message))
        )
        self.transport._wake_receive_waiter()
        return data

    def deliver_and_advance_receive_cursor(
        self,
        binding,
        context,
        length,
        end_of_message,
    ):
        if not isinstance(length, int) or isinstance(length, bool) or length < 0:
            raise ValueError("Delivery length must be a non-negative Integer")

        layer = self._layers[binding.position]
        amount = min(length, layer.available)
        parsed_context = layer.context
        data = bytes(layer.data[:amount])
        layer.advance(amount)
        context = context or parsed_context or MessageContext()

        if amount == length:
            return self.deliver(
                binding,
                context,
                data,
                end_of_message,
            )

        binding.pending_deliveries.append(
            _PendingDelivery(
                context=context,
                remaining=length - amount,
                end_of_message=bool(end_of_message),
                data=bytearray(data),
            )
        )
        return None
