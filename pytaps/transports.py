import asyncio
import ipaddress
import socket
import ssl
from copy import deepcopy

from .endpoint import LocalEndpoint, RemoteEndpoint
from .framer import FramerFailed, FramerStack
from .message import MessageContext
from .transportProperties import (
    PreferenceLevel,
    get_protocol_capabilities,
)
from .utility import ConnectionState, setup_logger

try:
    import mctx_core
except ImportError:
    mctx_core = None

try:
    from aioquic.buffer import Buffer
    from aioquic.asyncio import serve as aioquic_serve
    from aioquic.asyncio.client import connect as aioquic_connect
    from aioquic.asyncio.protocol import QuicConnectionProtocol
    from aioquic.quic.configuration import QuicConfiguration
    from aioquic.quic.events import (
        ConnectionTerminated,
        DatagramFrameReceived,
        HandshakeCompleted,
        StopSendingReceived,
        StreamReset,
    )
    from aioquic.quic.packet import pull_quic_header
except ImportError:
    Buffer = None
    aioquic_serve = None
    aioquic_connect = None
    QuicConnectionProtocol = None
    QuicConfiguration = None
    ConnectionTerminated = None
    DatagramFrameReceived = None
    HandshakeCompleted = None
    StopSendingReceived = None
    StreamReset = None
    pull_quic_header = None

logger = setup_logger(__name__, "blue")

QUIC_DATAGRAM_FRAME_SIZE = 65536
QUIC_DATAGRAM_PACKET_OVERHEAD = 64
QUIC_DATAGRAM_QUEUE_LIMIT = 64
QUIC_ASSOCIATION_WRITE_BUFFER_HIGH_WATER = 4 * 1024 * 1024
QUIC_STREAM_WRITE_BUFFER_HIGH_WATER = 256 * 1024
QUIC_TAPS_ABORT_ERROR_CODE = 0x100
QUIC_MAX_EARLY_DATA = 0xFFFFFFFF
CAPACITY_PROFILE_DSCP = {
    "Default": 0,
    "Scavenger": 1,
    "Low Latency/Interactive": 34,
    "Low Latency/Non-Interactive": 18,
    "Constant-Rate Streaming": 26,
    "Capacity-Seeking": 10,
}


def _normalize_quic_host(host):
    if host is None:
        return None
    host = str(host)
    address = host.split("%", 1)[0]
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return host
    if parsed.version == 6 and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    return str(parsed)


def _normalize_quic_path_address(address):
    if not address:
        return None
    return (_normalize_quic_host(address[0]), address[1])


def _address_for_socket_family(address, family):
    if address is None or family != socket.AF_INET:
        return address
    host = _normalize_quic_host(address[0])
    return (host, address[1])


def _address_for_aioquic(address, family):
    if address is None or family != socket.AF_INET:
        return address
    return (f"::ffff:{address[0]}", address[1], 0, 0)


class QuicStreamError(ConnectionError):
    def __init__(self, stream_id, error_code, operation):
        self.stream_id = stream_id
        self.error_code = error_code
        self.operation = operation
        super().__init__(
            f"QUIC stream {stream_id} {operation} with error code "
            f"{error_code}"
        )


class QuicAssociationError(ConnectionError):
    def __init__(self, error_code, reason_phrase="", frame_type=None):
        self.error_code = error_code
        self.reason_phrase = reason_phrase
        self.frame_type = frame_type
        detail = f"QUIC association terminated with error code {error_code}"
        if reason_phrase:
            detail = f"{detail}: {reason_phrase}"
        super().__init__(detail)


class _QuicMigrationTransport:
    """Adapt an IPv4 or IPv6 asyncio UDP transport to aioquic."""

    def __init__(self, transport, family):
        self._transport = transport
        self.family = family

    def sendto(self, data, addr=None):
        self._transport.sendto(
            data,
            _address_for_socket_family(addr, self.family),
        )

    def get_extra_info(self, name, default=None):
        return self._transport.get_extra_info(name, default)

    def is_closing(self):
        return self._transport.is_closing()

    def close(self):
        self._transport.close()


class _QuicMigrationProtocol(asyncio.DatagramProtocol):
    def __init__(self, association, token, family):
        self.association = association
        self.token = token
        self.family = family
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.association.migration_datagram_received(
            self.token,
            data,
            _address_for_aioquic(addr, self.family),
        )

    def error_received(self, error):
        self.association.migration_transport_error(self.token, error)

    def connection_lost(self, error):
        if error is not None:
            self.association.migration_transport_error(self.token, error)


def _require_mctx_core():
    if mctx_core is None:
        raise ImportError(
            "Multicast send support requires the optional 'mctx-core-py' package."
        )


def _require_aioquic():
    if aioquic_connect is None or aioquic_serve is None or QuicConfiguration is None:
        raise ImportError("QUIC support requires the optional 'aioquic' package.")


def _load_quic_identity(configuration, certificate, private_key=None):
    configuration.load_cert_chain(
        certificate,
        keyfile=private_key,
    )
    if configuration.private_key is not None:
        return

    # aioquic's convenience loader expects certificates before an inline key.
    # Parse combined PEM files directly so TLS/TCP and QUIC accept either order.
    from cryptography.hazmat.primitives.serialization import (
        load_pem_private_key,
    )
    from cryptography.x509 import load_pem_x509_certificates

    with open(certificate, "rb") as identity_file:
        identity_data = identity_file.read()
    certificates = load_pem_x509_certificates(identity_data)
    if not certificates:
        raise ValueError("The QUIC identity does not contain a certificate")
    configuration.certificate = certificates[0]
    configuration.certificate_chain = certificates[1:]
    configuration.private_key = load_pem_private_key(
        identity_data,
        password=None,
    )


def _build_quic_configuration(owner, *, is_client):
    _require_aioquic()
    security_parameters = getattr(owner, "security_parameters", None)
    configuration = QuicConfiguration(
        is_client=is_client,
        alpn_protocols=(
            list(security_parameters.alpn_protocols)
            if security_parameters and security_parameters.alpn_protocols
            else ["taps"]
        ),
        max_datagram_frame_size=QUIC_DATAGRAM_FRAME_SIZE,
    )

    if security_parameters:
        if security_parameters.identity:
            _load_quic_identity(
                configuration,
                security_parameters.identity,
                security_parameters.private_key,
            )
        elif security_parameters.public_key:
            _load_quic_identity(
                configuration,
                security_parameters.public_key,
                security_parameters.private_key,
            )
        trust_anchors = list(security_parameters.trustedCA)
        if not trust_anchors and security_parameters.pinned_server_certificates:
            trust_anchors = list(security_parameters.pinned_server_certificates)
        if trust_anchors:
            configuration.load_verify_locations(cafile=trust_anchors[0])
        configuration.server_name = (
            security_parameters.server_name
            or getattr(getattr(owner, "remote_endpoint", None), "host_name", None)
        )
        configuration.verify_mode = (
            ssl.CERT_REQUIRED
            if security_parameters.require_peer_authentication
            else ssl.CERT_NONE
        )
        configuration._pytaps_pinned_server_certificates = list(
            security_parameters.pinned_server_certificates
        )
        configuration._pytaps_security_algorithms = list(
            security_parameters.security_algorithms
        )
        configuration._pytaps_allowed_security_protocols = list(
            security_parameters.allowed_security_protocols
        )
        configuration._pytaps_pre_shared_key = security_parameters.pre_shared_key
        configuration._pytaps_private_key_callback_handle = (
            security_parameters.private_key_callback_handle
        )
        configuration._pytaps_cipher_suites = security_parameters.cipher_suites
        configuration._pytaps_session_cache_capacity = (
            security_parameters.session_cache_capacity
        )
        configuration._pytaps_session_cache_lifetime = (
            security_parameters.session_cache_lifetime
        )
    else:
        configuration.verify_mode = ssl.CERT_REQUIRED if is_client else ssl.CERT_NONE

    return configuration


def _presented_tls_certificate_chain(ssl_object):
    get_chain = getattr(ssl_object, "get_unverified_chain", None)
    if callable(get_chain):
        chain = get_chain()
        if chain:
            return chain
    certificate = ssl_object.getpeercert(binary_form=True)
    return [certificate] if certificate else []


class PytapsQuicProtocol(
    QuicConnectionProtocol if QuicConnectionProtocol is not None else object
):
    def __init__(self, quic, *, association):
        super().__init__(quic, stream_handler=self._handle_stream)
        self.association = association
        self._initial_transport = None
        self._initial_transport_token = object()

    def connection_made(self, transport):
        super().connection_made(transport)
        if self._initial_transport is None:
            self._initial_transport = transport
            if self._quic._is_client:
                self.association.client_transport_created(
                    transport,
                    self._initial_transport_token,
                )

    def _handle_stream(self, reader, writer):
        is_early_data = (
            not self._quic._is_client
            and not self.association.handshake_complete
        )
        self.association.create_background_task(
            self.association.accept_inbound_stream(
                reader,
                writer,
                self,
                is_early_data=is_early_data,
            )
        )

    def datagram_received(self, data, addr):
        self._receive_datagram(
            data,
            addr,
            self._initial_transport_token,
        )

    def _receive_datagram(self, data, addr, transport_token):
        super().datagram_received(data, addr)
        self.association.transport_state_changed()
        self.association.observe_protocol_path(
            received_on=transport_token,
        )

    def connection_lost(self, exc):
        super().connection_lost(exc)
        if exc is not None:
            self.association.transport_lost(exc)

    def quic_event_received(self, event):
        super().quic_event_received(event)
        if (
            DatagramFrameReceived is not None
            and isinstance(event, DatagramFrameReceived)
        ):
            self.association.datagram_received(
                event.data,
                is_early_data=(
                    not self._quic._is_client
                    and not self.association.handshake_complete
                ),
            )
        elif (
            HandshakeCompleted is not None
            and isinstance(event, HandshakeCompleted)
        ):
            self.association.handshake_completed(event)
        elif StreamReset is not None and isinstance(event, StreamReset):
            self.association.stream_event_received(
                event.stream_id,
                event.error_code,
                "reset",
            )
        elif (
            StopSendingReceived is not None
            and isinstance(event, StopSendingReceived)
        ):
            self.association.stream_event_received(
                event.stream_id,
                event.error_code,
                "stop-sending",
            )
        elif (
            ConnectionTerminated is not None
            and isinstance(event, ConnectionTerminated)
        ):
            self.association.connection_terminated(event)
        self.association.transport_state_changed()


class QuicAssociationManager:
    def __init__(self, *, loop, listener=None, parent=None):
        self.loop = loop
        self.listener = listener
        self.parent = parent
        self.protocol = None
        self.context_manager = None
        self.server = None
        self._listener_accepting = False
        self._listener_draining = False
        self._listener_datagram_received = None
        self.child_associations = set()
        self.stream_transports = set()
        self.streams_by_id = {}
        self.datagram_transport = None
        self.pending_datagrams = []
        self.anchor_connection = None
        self._connect_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._migration_lock = asyncio.Lock()
        self._stream_open_lock = asyncio.Lock()
        self._write_budget_lock = asyncio.Lock()
        self._passive_datagram_task = None
        self._background_tasks = set()
        self._transport_state_waiters = set()
        self._handshake_complete_event = asyncio.Event()
        self._terminated = False
        self._closing = False
        self._termination_error = None
        self.handshake_complete = False
        self.session_resumed = False
        self.early_data_attempted = False
        self._current_early_data_attempted = False
        self.early_data_accepted = False
        self.session_ticket_available = False
        self._session_ticket_key = None
        self._handshake_owner = None
        self._peer_certificate_verified = False
        self._resumption_ticket = None
        self._pending_client_session_tickets = []
        self.early_data_rejected = False
        self.stream_write_buffer_high_water = (
            QUIC_STREAM_WRITE_BUFFER_HIGH_WATER
        )
        self.association_write_buffer_high_water = (
            QUIC_ASSOCIATION_WRITE_BUFFER_HIGH_WATER
        )
        self.max_observed_stream_buffered_bytes = 0
        self.max_observed_association_buffered_bytes = 0
        self.current_path = {
            "local": None,
            "remote": None,
        }
        self.previous_path = {
            "local": None,
            "remote": None,
        }
        self.path_change_count = 0
        self.path_validation_successes = 0
        self.path_validation_failures = 0
        self.migration_in_progress = False
        self._known_network_paths = {}
        self._client_transports = set()
        self._active_client_transport = None
        self._active_client_transport_token = None
        self._migration_transport = None
        self._migration_transport_token = None
        self._migration_waiter = None
        self._last_performance_recorded_at = None
        self._last_performance_path = None
        self._last_performance_rtt = None

    @staticmethod
    def _stream_id(writer):
        get_extra_info = getattr(writer, "get_extra_info", None)
        if callable(get_extra_info):
            stream_id = get_extra_info("stream_id")
            if stream_id is not None:
                return stream_id
        transport = getattr(writer, "transport", None)
        get_extra_info = getattr(transport, "get_extra_info", None)
        if callable(get_extra_info):
            return get_extra_info("stream_id")
        return None

    def create_background_task(self, awaitable):
        task = self.loop.create_task(awaitable)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_task_done)
        return task

    def _background_task_done(self, task):
        self._background_tasks.discard(task)
        if task is self._passive_datagram_task:
            self._passive_datagram_task = None
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error(
                "QUIC association task failed: %s",
                error,
                exc_info=(
                    type(error),
                    error,
                    error.__traceback__,
                ),
            )

    async def _cancel_background_tasks(self):
        current = asyncio.current_task()
        tasks = [
            task
            for task in self._background_tasks
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_idle(self):
        current = asyncio.current_task()
        while True:
            tasks = [
                task
                for task in self._background_tasks
                if task is not current and not task.done()
            ]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    def transport_state_changed(self):
        waiters = tuple(self._transport_state_waiters)
        self._transport_state_waiters.clear()
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)
        self._record_quic_performance()

    def _new_transport_state_waiter(self):
        waiter = self.loop.create_future()
        self._transport_state_waiters.add(waiter)
        return waiter

    async def _wait_for_transport_state_change(self, waiter):
        try:
            await waiter
        finally:
            self._transport_state_waiters.discard(waiter)

    def client_transport_created(self, transport, token):
        self._client_transports.add(transport)
        if self._active_client_transport is None:
            self._active_client_transport = transport
            self._active_client_transport_token = token

    def _association_context(self):
        owner = (
            self.anchor_connection
            or self._handshake_owner
            or self.listener
        )
        return getattr(owner, "connection_context", None)

    def performance_snapshot(self):
        quic = getattr(self.protocol, "_quic", None)
        recovery = getattr(quic, "_loss", None)
        rtt_available = bool(
            recovery is not None
            and getattr(recovery, "_rtt_initialized", False)
        )
        congestion_control = getattr(recovery, "_cc", None)
        return {
            "rttAvailable": rtt_available,
            "latestRtt": (
                getattr(recovery, "_rtt_latest", None)
                if rtt_available
                else None
            ),
            "smoothedRtt": (
                getattr(recovery, "_rtt_smoothed", None)
                if rtt_available
                else None
            ),
            "minimumRtt": (
                getattr(recovery, "_rtt_min", None)
                if rtt_available
                else None
            ),
            "rttVariation": (
                getattr(recovery, "_rtt_variance", None)
                if rtt_available
                else None
            ),
            "congestionWindow": getattr(
                congestion_control,
                "congestion_window",
                None,
            ),
            "bytesInFlight": getattr(
                congestion_control,
                "bytes_in_flight",
                None,
            ),
        }

    def _performance_network_id(self):
        owner = (
            self.anchor_connection
            or self._handshake_owner
            or self.listener
        )
        local_endpoint = getattr(owner, "local_endpoint", None)
        interface_id = getattr(local_endpoint, "interface", None)
        local_address = (
            local_endpoint.effective_address()
            if local_endpoint is not None
            else None
        )
        connection_context = self._association_context()
        if connection_context is None:
            return interface_id or "default"
        return connection_context.get_network_id(
            interface_id,
            local_address=local_address,
        )

    def _record_quic_performance(self, *, force=False):
        metrics = self.performance_snapshot()
        if not metrics["rttAvailable"]:
            return False
        connection_context = self._association_context()
        if connection_context is None:
            return False

        path = self.current_path
        if path["remote"] is None:
            path = self._path_from_protocol()
        if path["remote"] is None:
            return False

        now = self.loop.time()
        latest_rtt = metrics["latestRtt"]
        same_path = path == self._last_performance_path
        if (
            not force
            and same_path
            and self._last_performance_recorded_at is not None
        ):
            age = now - self._last_performance_recorded_at
            if age < 1:
                return False
            change_threshold = max(
                0.001,
                (self._last_performance_rtt or latest_rtt) * 0.125,
            )
            if (
                age < 5
                and self._last_performance_rtt is not None
                and abs(latest_rtt - self._last_performance_rtt)
                < change_threshold
            ):
                return False

        recorded = connection_context.record_performance_observation(
            path["local"],
            path["remote"],
            "quic",
            network_id=self._performance_network_id(),
            rtt=latest_rtt,
            rtt_variation=metrics["rttVariation"],
            source="aioquic-recovery",
        )
        if recorded:
            self._last_performance_recorded_at = now
            self._last_performance_path = path.copy()
            self._last_performance_rtt = latest_rtt
        return recorded

    def _record_association_event(self, name, **details):
        connection_context = self._association_context()
        if connection_context is not None:
            connection_context.record_event(
                name,
                source="quic-association",
                state=(
                    "Established"
                    if self.handshake_complete
                    else "Establishing"
                ),
                details=details,
            )

    def _network_path_snapshots(self):
        quic = getattr(self.protocol, "_quic", None)
        network_paths = getattr(quic, "_network_paths", ())
        snapshots = []
        for index, network_path in enumerate(network_paths):
            snapshots.append(
                {
                    "remote": _normalize_quic_path_address(
                        getattr(network_path, "addr", None)
                    ),
                    "active": index == 0,
                    "validated": bool(
                        getattr(network_path, "is_validated", False)
                    ),
                    "bytesReceived": getattr(
                        network_path,
                        "bytes_received",
                        0,
                    ),
                    "bytesSent": getattr(
                        network_path,
                        "bytes_sent",
                        0,
                    ),
                }
            )
        return snapshots

    def _path_from_protocol(self, transport=None):
        protocol = self.protocol
        if protocol is None:
            return {
                "local": None,
                "remote": None,
            }
        transport = transport or getattr(protocol, "_transport", None)
        get_extra_info = getattr(transport, "get_extra_info", None)
        sockname = (
            get_extra_info("sockname")
            if callable(get_extra_info)
            else None
        )
        quic = getattr(protocol, "_quic", None)
        network_paths = getattr(quic, "_network_paths", ())
        remote = (
            getattr(network_paths[0], "addr", None)
            if network_paths
            else None
        )
        return {
            "local": _normalize_quic_path_address(sockname),
            "remote": _normalize_quic_path_address(remote),
        }

    def _active_network_path_is_validated(self):
        quic = getattr(self.protocol, "_quic", None)
        network_paths = getattr(quic, "_network_paths", ())
        return bool(
            network_paths
            and getattr(network_paths[0], "is_validated", False)
        )

    @staticmethod
    def _path_arguments(path):
        local = path.get("local")
        remote = path.get("remote")
        return {
            "local_address": local[0] if local else None,
            "local_port": local[1] if local else None,
            "remote_address": remote[0] if remote else None,
            "remote_port": remote[1] if remote else None,
        }

    @staticmethod
    def _sync_transport_path(connection):
        for transport in connection.transports:
            transport.local_endpoint = (
                connection.local_endpoint.clone()
                if connection.local_endpoint is not None
                else None
            )
            transport.remote_endpoint = (
                connection.remote_endpoint.clone()
                if connection.remote_endpoint is not None
                else None
            )

    def _initialize_connection_path(self, connection):
        if not self._active_network_path_is_validated():
            return
        path = self._path_from_protocol()
        if path["local"] is None and path["remote"] is None:
            return
        initial_association_path = (
            self.current_path["local"] is None
            and self.current_path["remote"] is None
        )
        if initial_association_path:
            self.current_path = path.copy()
            self.previous_path = {
                "local": None,
                "remote": None,
            }
            if connection.connection_group is not None:
                connection.connection_group.note_path_change(
                    self.previous_path,
                    self.current_path,
                    initial=True,
                )
        connection.note_path_change(
            **self._path_arguments(self.current_path),
            _record_transition=initial_association_path,
            _update_group=False,
        )
        self._sync_transport_path(connection)
        self._record_quic_performance(force=True)

    def _commit_validated_path(
        self,
        path,
        *,
        reason,
        local_endpoint=None,
    ):
        if path == self.current_path:
            return self.current_path.copy()
        previous_path = self.current_path.copy()
        self.previous_path = previous_path
        self.current_path = path.copy()
        self.path_change_count += 1

        group = (
            self.anchor_connection.connection_group
            if self.anchor_connection is not None
            else None
        )
        if group is not None:
            group.note_path_change(
                previous_path,
                self.current_path,
            )
            members = [
                connection
                for connection in group.connections
                if (
                    not connection._is_terminal()
                    and connection.quic_association is self
                )
            ]
        else:
            members = []

        for index, connection in enumerate(members):
            connection.note_path_change(
                **self._path_arguments(self.current_path),
                _record_transition=index == 0,
                _update_group=False,
            )
            if (
                local_endpoint is not None
                and connection.local_endpoint is not None
            ):
                connection.local_endpoint.interface = (
                    local_endpoint.interface
                )
            self._sync_transport_path(connection)

        self._record_association_event(
            "quic_path_changed",
            reason=reason,
            previousPath=previous_path,
            currentPath=self.current_path.copy(),
            validated=True,
        )
        self._record_quic_performance(force=True)
        return self.current_path.copy()

    def _observe_network_path_states(self):
        for snapshot in self._network_path_snapshots():
            remote = snapshot["remote"]
            if remote is None:
                continue
            previous = self._known_network_paths.get(remote)
            current = (
                snapshot["active"],
                snapshot["validated"],
            )
            if previous is None:
                self._record_association_event(
                    "quic_path_discovered",
                    remote=remote,
                    active=snapshot["active"],
                    validated=snapshot["validated"],
                )
            elif not previous[1] and snapshot["validated"]:
                self._record_association_event(
                    "quic_path_validated",
                    remote=remote,
                    active=snapshot["active"],
                )
            self._known_network_paths[remote] = current

    def observe_protocol_path(self, *, received_on=None):
        self._observe_network_path_states()
        if not self._active_network_path_is_validated():
            return
        quic = getattr(self.protocol, "_quic", None)
        if (
            self.migration_in_progress
            and quic is not None
            and quic._is_client
        ):
            if (
                received_on is self._migration_transport_token
                and self._migration_waiter is not None
                and not self._migration_waiter.done()
            ):
                self._migration_waiter.set_result(None)
            return
        if self.anchor_connection is None:
            return
        path = self._path_from_protocol()
        if (
            self.current_path["local"] is None
            and self.current_path["remote"] is None
        ):
            self._initialize_connection_path(self.anchor_connection)
            return
        if path == self.current_path:
            return
        mode = self.anchor_connection.transport_properties.get(
            "multipath"
        )
        policy = self.anchor_connection.transport_properties.get(
            "multipathPolicy"
        )
        if mode != "Disabled" and policy == "Handover":
            self.path_validation_successes += 1
            self._commit_validated_path(
                path,
                reason="peer_migration",
            )

    def migration_datagram_received(self, token, data, addr):
        if token not in {
            self._migration_transport_token,
            self._active_client_transport_token,
        }:
            return
        if self.protocol is not None:
            self.protocol._receive_datagram(data, addr, token)

    def migration_transport_error(self, token, error):
        if (
            token is self._migration_transport_token
            and self._migration_waiter is not None
            and not self._migration_waiter.done()
        ):
            self._migration_waiter.set_exception(error)
        elif token is self._active_client_transport_token:
            self.transport_lost(error)

    async def _await_migration_validation(self, timeout):
        await asyncio.wait_for(
            asyncio.shield(self._migration_waiter),
            timeout,
        )

    @staticmethod
    def _migration_socket(local_endpoint, remote_path):
        requested_address = local_endpoint.socket_address()
        requested_port = local_endpoint.port or 0
        if local_endpoint.interface is not None and requested_address is None:
            raise ValueError(
                "QUIC migration with an interface constraint also requires "
                "a local IP address"
            )

        if requested_address is not None:
            unscoped_address = requested_address.split("%", 1)[0]
            parsed = ipaddress.ip_address(unscoped_address)
            family = (
                socket.AF_INET
                if parsed.version == 4
                else socket.AF_INET6
            )
            bind_address = requested_address
        else:
            remote_host = remote_path[0] if remote_path else None
            try:
                remote_ip = ipaddress.ip_address(remote_host)
            except (TypeError, ValueError):
                remote_ip = None
            family = (
                socket.AF_INET
                if remote_ip is not None and remote_ip.version == 4
                else socket.AF_INET6
            )
            bind_address = "0.0.0.0" if family == socket.AF_INET else "::"

        sock = socket.socket(family, socket.SOCK_DGRAM)
        completed = False
        try:
            if family == socket.AF_INET6:
                sock.setsockopt(
                    socket.IPPROTO_IPV6,
                    socket.IPV6_V6ONLY,
                    1,
                )
            sock.bind((bind_address, requested_port))
            completed = True
            return sock, family
        finally:
            if not completed:
                sock.close()

    async def migrate_local_path(
        self,
        connection,
        local_endpoint,
        *,
        timeout=5,
    ):
        async with self._migration_lock:
            self._ensure_operational()
            if connection.quic_association is not self:
                raise RuntimeError(
                    "The Connection does not belong to this QUIC association"
                )
            if connection.state is not ConnectionState.ESTABLISHED:
                raise RuntimeError(
                    "QUIC path migration requires an established Connection"
                )
            if not isinstance(local_endpoint, LocalEndpoint):
                raise TypeError(
                    "local_endpoint must be a LocalEndpoint"
                )
            if connection.transport_properties.get("multipath") != "Active":
                raise RuntimeError(
                    "QUIC path migration requires multipath=Active"
                )
            if (
                connection.transport_properties.get("multipathPolicy")
                != "Handover"
            ):
                raise NotImplementedError(
                    "The QUIC backend currently supports only the Handover "
                    "multipath policy"
                )

            protocol = self.protocol
            quic = protocol._quic
            if not quic._is_client:
                raise RuntimeError(
                    "Active local migration is available on initiating "
                    "QUIC associations; listeners observe peer migration"
                )
            network_paths = getattr(quic, "_network_paths", ())
            if not network_paths:
                raise RuntimeError("The QUIC association has no active path")

            old_transport = getattr(protocol, "_transport", None)
            old_transport_token = self._active_client_transport_token
            network_path = network_paths[0]
            saved_network_state = {
                "is_validated": network_path.is_validated,
                "local_challenge_sent": (
                    network_path.local_challenge_sent
                ),
                "bytes_received": network_path.bytes_received,
                "bytes_sent": network_path.bytes_sent,
                "local_challenges": dict(quic._local_challenges),
            }
            sock = None
            candidate_transport = None
            token = object()
            try:
                sock, family = self._migration_socket(
                    local_endpoint,
                    self.current_path.get("remote"),
                )
                migration_protocol = _QuicMigrationProtocol(
                    self,
                    token,
                    family,
                )
                raw_transport, _ = await self.loop.create_datagram_endpoint(
                    lambda: migration_protocol,
                    sock=sock,
                )
                sock = None
                candidate_transport = _QuicMigrationTransport(
                    raw_transport,
                    family,
                )
                self._client_transports.add(candidate_transport)
            except BaseException as error:
                if sock is not None:
                    sock.close()
                self.path_validation_failures += 1
                self._record_association_event(
                    "quic_path_validation_failed",
                    reason=str(error),
                    requestedLocal=(
                        local_endpoint.address,
                        local_endpoint.port,
                    ),
                )
                raise

            self.migration_in_progress = True
            self._migration_transport = candidate_transport
            self._migration_transport_token = token
            self._migration_waiter = self.loop.create_future()
            protocol._transport = candidate_transport
            self._record_association_event(
                "quic_path_validation_started",
                previousPath=self.current_path.copy(),
                requestedLocal=(
                    local_endpoint.address,
                    local_endpoint.port,
                ),
            )

            try:
                quic._local_challenges = {
                    challenge: path
                    for challenge, path in quic._local_challenges.items()
                    if path is not network_path
                }
                network_path.is_validated = False
                network_path.local_challenge_sent = False
                migration_receive_floor = max(
                    network_path.bytes_received,
                    (network_path.bytes_sent + 4095) // 3,
                    1200,
                )
                network_path.bytes_received = migration_receive_floor
                network_path.bytes_sent = 0
                quic.change_connection_id()
                quic.send_ping(id(self._migration_waiter))
                protocol.transmit()
                await self._await_migration_validation(timeout)

                migration_bytes_received = max(
                    0,
                    (
                        network_path.bytes_received
                        - migration_receive_floor
                    ),
                )
                migration_bytes_sent = network_path.bytes_sent
                network_path.bytes_received = (
                    saved_network_state["bytes_received"]
                    + migration_bytes_received
                )
                network_path.bytes_sent = (
                    saved_network_state["bytes_sent"]
                    + migration_bytes_sent
                )
                path = self._path_from_protocol(candidate_transport)
                self.path_validation_successes += 1
                self._active_client_transport = candidate_transport
                self._active_client_transport_token = token
                self._migration_transport = None
                self._migration_transport_token = None
                self._commit_validated_path(
                    path,
                    reason="local_migration",
                    local_endpoint=local_endpoint,
                )
                if (
                    old_transport is not None
                    and old_transport is not candidate_transport
                ):
                    old_transport.close()
                    self._client_transports.discard(old_transport)
                return path
            except BaseException as error:
                self.path_validation_failures += 1
                protocol._transport = old_transport
                network_path.is_validated = saved_network_state[
                    "is_validated"
                ]
                network_path.local_challenge_sent = saved_network_state[
                    "local_challenge_sent"
                ]
                network_path.bytes_received = saved_network_state[
                    "bytes_received"
                ]
                network_path.bytes_sent = saved_network_state[
                    "bytes_sent"
                ]
                quic._local_challenges = saved_network_state[
                    "local_challenges"
                ]
                candidate_transport.close()
                self._client_transports.discard(candidate_transport)
                self._migration_transport = None
                self._migration_transport_token = None
                self._active_client_transport = old_transport
                self._active_client_transport_token = old_transport_token
                self._record_association_event(
                    "quic_path_validation_failed",
                    reason=str(error),
                    previousPath=self.current_path.copy(),
                )
                if old_transport is not None:
                    quic.send_ping(id(error))
                    protocol.transmit()
                raise
            finally:
                self.migration_in_progress = False
                if (
                    self._migration_waiter is not None
                    and not self._migration_waiter.done()
                ):
                    self._migration_waiter.cancel()
                self._migration_waiter = None

    def _ensure_operational(self):
        if (
            self.protocol is None
            or self._terminated
            or self._closing
        ):
            raise ConnectionError("The QUIC association is closed")

    @staticmethod
    def _session_cache_settings(owner):
        security_parameters = getattr(owner, "security_parameters", None)
        if security_parameters is None:
            return None, None
        return (
            security_parameters.session_cache_capacity,
            security_parameters.session_cache_lifetime,
        )

    @staticmethod
    def _client_session_ticket_key(
        connection,
        remote_endpoint,
        configuration,
    ):
        security_parameters = connection.security_parameters
        pinned_certificates = ()
        trusted_cas = ()
        require_peer_authentication = True
        security_algorithms = ()
        allowed_security_protocols = ()
        cipher_suites = None
        if security_parameters is not None:
            pinned_certificates = tuple(
                sorted(
                    digest.hex()
                    for digest in (
                        security_parameters
                        .get_pinned_server_certificate_digests()
                    )
                )
            )
            trusted_cas = tuple(
                str(certificate)
                for certificate in security_parameters.trustedCA
            )
            require_peer_authentication = (
                security_parameters.require_peer_authentication
            )
            security_algorithms = tuple(
                str(algorithm)
                for algorithm in security_parameters.security_algorithms
            )
            allowed_security_protocols = tuple(
                str(protocol)
                for protocol in (
                    security_parameters.allowed_security_protocols
                )
            )
            cipher_suites = repr(security_parameters.cipher_suites)
        return (
            getattr(configuration, "server_name", None),
            remote_endpoint.port,
            tuple(getattr(configuration, "alpn_protocols", None) or ()),
            require_peer_authentication,
            pinned_certificates,
            trusted_cas,
            security_algorithms,
            allowed_security_protocols,
            cipher_suites,
        )

    def _cache_client_session_ticket(self, ticket):
        if self._handshake_owner is None or self._session_ticket_key is None:
            return
        if not self._peer_certificate_verified:
            self._pending_client_session_tickets.append(ticket)
            return
        capacity, lifetime = self._session_cache_settings(
            self._handshake_owner
        )
        self._handshake_owner.connection_context.cache_quic_client_session_ticket(
            self._session_ticket_key,
            ticket,
            capacity=capacity,
            lifetime=lifetime,
        )

    def _flush_pending_client_session_tickets(self):
        pending = self._pending_client_session_tickets
        self._pending_client_session_tickets = []
        for ticket in pending:
            self._cache_client_session_ticket(ticket)

    def _verify_peer_certificate(self, connection):
        if self._peer_certificate_verified:
            return
        security_parameters = connection.security_parameters
        if (
            security_parameters is not None
            and security_parameters.pinned_server_certificates
        ):
            # A resumed PSK authenticates the server represented by the
            # previously verified, policy-bound ticket cache entry.
            if not (
                self.session_resumed
                and self._resumption_ticket is not None
            ):
                tls = getattr(
                    getattr(self.protocol, "_quic", None),
                    "tls",
                    None,
                )
                peer_chain = []
                if tls is not None:
                    peer_certificate = getattr(
                        tls,
                        "_peer_certificate",
                        None,
                    )
                    if peer_certificate is not None:
                        peer_chain.append(peer_certificate)
                    peer_chain.extend(
                        getattr(
                            tls,
                            "_peer_certificate_chain",
                            None,
                        )
                        or []
                    )
                security_parameters.verify_pinned_server_certificates(
                    peer_chain
                )
        self._peer_certificate_verified = True
        self._flush_pending_client_session_tickets()

    def handshake_completed(self, event):
        self.handshake_complete = True
        self.session_resumed = bool(event.session_resumed)
        self.early_data_accepted = bool(event.early_data_accepted)
        if (
            self._current_early_data_attempted
            and not self.early_data_accepted
        ):
            self.early_data_rejected = True
        if not self._handshake_complete_event.is_set():
            self._handshake_complete_event.set()
        owner = self._handshake_owner or self.listener
        connection_context = getattr(owner, "connection_context", None)
        if connection_context is not None:
            connection_context.record_event(
                "quic_handshake_completed",
                source="quic-association",
                state="Established",
                details={
                    "sessionResumed": self.session_resumed,
                    "sessionTicketAvailable": (
                        self.session_ticket_available
                    ),
                    "earlyDataAttempted": (
                        self._current_early_data_attempted
                    ),
                    "earlyDataAccepted": self.early_data_accepted,
                    "earlyDataRejected": self.early_data_rejected,
                },
            )
        self._record_quic_performance(force=True)

    async def wait_handshake_complete(self):
        await self._handshake_complete_event.wait()
        if self.handshake_complete:
            return
        if self._termination_error is not None:
            raise self._termination_error
        raise ConnectionError(
            "The QUIC association closed before the handshake completed"
        )

    async def complete_handshake(self, connection):
        self._ensure_operational()
        if not self.handshake_complete:
            self.protocol.transmit()
            await self.protocol.wait_connected()
            if not self._handshake_complete_event.is_set():
                self.handshake_complete = True
                self._handshake_complete_event.set()
            await self.wait_handshake_complete()
        self._verify_peer_certificate(connection)

    def transport_lost(self, error):
        if self._terminated:
            return
        association_error = QuicAssociationError(
            QUIC_TAPS_ABORT_ERROR_CODE,
            str(error),
        )
        self._terminate_members(association_error)
        self.create_background_task(
            self.close_association(local_error=association_error)
        )

    def resource_snapshot(self):
        quic = getattr(self.protocol, "_quic", None)
        pending_outbound_datagrams = len(
            getattr(quic, "_datagrams_pending", ())
        )
        return {
            "streamTransports": len(self.stream_transports),
            "streamIds": sorted(self.streams_by_id),
            "hasDatagramTransport": self.datagram_transport is not None,
            "pendingInboundDatagrams": len(self.pending_datagrams),
            "pendingOutboundDatagrams": pending_outbound_datagrams,
            "backgroundTasks": sum(
                not task.done() for task in self._background_tasks
            ),
            "transportStateWaiters": sum(
                not waiter.done()
                for waiter in self._transport_state_waiters
            ),
            "terminated": self._terminated,
            "closing": self._closing,
            "handshakeComplete": self.handshake_complete,
            "sessionResumed": self.session_resumed,
            "sessionTicketAvailable": self.session_ticket_available,
            "earlyDataAttempted": self.early_data_attempted,
            "earlyDataAccepted": self.early_data_accepted,
            "earlyDataRejected": self.early_data_rejected,
            "performance": self.performance_snapshot(),
            "currentPath": self.current_path.copy(),
            "previousPath": self.previous_path.copy(),
            "pathChangeCount": self.path_change_count,
            "pathValidationSuccesses": (
                self.path_validation_successes
            ),
            "pathValidationFailures": (
                self.path_validation_failures
            ),
            "migrationInProgress": self.migration_in_progress,
            "networkPaths": self._network_path_snapshots(),
            "maxObservedStreamBufferedBytes": (
                self.max_observed_stream_buffered_bytes
            ),
            "maxObservedAssociationBufferedBytes": (
                self.max_observed_association_buffered_bytes
            ),
        }

    def _remote_stream_limit(self, is_unidirectional):
        self._ensure_operational()
        quic = self.protocol._quic
        attribute = (
            "_remote_max_streams_uni"
            if is_unidirectional
            else "_remote_max_streams_bidi"
        )
        return getattr(quic, attribute, 0)

    async def create_stream(
        self,
        *,
        is_unidirectional,
        wait_for_credit=True,
    ):
        async with self._stream_open_lock:
            while True:
                self._ensure_operational()
                quic = self.protocol._quic
                stream_id = quic.get_next_available_stream_id(
                    is_unidirectional=is_unidirectional
                )
                if (
                    stream_id // 4
                    < self._remote_stream_limit(is_unidirectional)
                ):
                    reader, writer = await self.protocol.create_stream(
                        is_unidirectional=is_unidirectional
                    )
                    actual_stream_id = self._stream_id(writer)
                    if actual_stream_id != stream_id:
                        raise RuntimeError(
                            "aioquic returned an unexpected stream ID"
                        )
                    # aioquic reserves an ID only when the stream is first
                    # used. Materialize it now so concurrent Clone calls
                    # cannot receive the same ID.
                    quic.send_stream_data(stream_id, b"")
                    self.protocol.transmit()
                    return reader, writer, stream_id

                if not wait_for_credit:
                    return None
                waiter = self._new_transport_state_waiter()
                await self._wait_for_transport_state_change(waiter)

    def _stream_buffered_bytes(self, stream_id):
        quic = getattr(self.protocol, "_quic", None)
        stream = (
            getattr(quic, "_streams", {}).get(stream_id)
            if quic is not None
            else None
        )
        sender = getattr(stream, "sender", None)
        if sender is None:
            return 0
        return max(
            0,
            getattr(sender, "_buffer_stop", 0)
            - getattr(sender, "_buffer_start", 0),
        )

    def _association_buffered_bytes(self):
        return sum(
            self._stream_buffered_bytes(stream_id)
            for stream_id in self.streams_by_id
        )

    async def write_stream_data(self, transport, data):
        if (
            self.stream_write_buffer_high_water <= 0
            or self.association_write_buffer_high_water <= 0
        ):
            raise ValueError(
                "QUIC write-buffer high-water marks must be positive"
            )
        data = memoryview(bytes(data))
        offset = 0
        while offset < len(data):
            queued = False
            waiter = None
            async with self._write_budget_lock:
                self._ensure_operational()
                if transport.raw_closed:
                    raise ConnectionError("The QUIC stream is closed")
                stream_buffered = self._stream_buffered_bytes(
                    transport.stream_id
                )
                association_buffered = self._association_buffered_bytes()
                stream_budget = max(
                    0,
                    self.stream_write_buffer_high_water - stream_buffered,
                )
                association_budget = max(
                    0,
                    (
                        self.association_write_buffer_high_water
                        - association_buffered
                    ),
                )
                chunk_length = min(
                    len(data) - offset,
                    stream_budget,
                    association_budget,
                )
                if chunk_length:
                    transport.writer.write(
                        data[offset : offset + chunk_length]
                    )
                    self.protocol.transmit()
                    offset += chunk_length
                    queued = True
                    stream_buffered = self._stream_buffered_bytes(
                        transport.stream_id
                    )
                    association_buffered = (
                        self._association_buffered_bytes()
                    )
                    self.max_observed_stream_buffered_bytes = max(
                        self.max_observed_stream_buffered_bytes,
                        stream_buffered,
                    )
                    self.max_observed_association_buffered_bytes = max(
                        self.max_observed_association_buffered_bytes,
                        association_buffered,
                    )
                else:
                    waiter = self._new_transport_state_waiter()
            if not queued:
                await self._wait_for_transport_state_change(waiter)

        if not data:
            self._ensure_operational()
            transport.writer.write(b"")

    def _attach_connection(self, connection, *, from_peer=False):
        if self.anchor_connection is None:
            if from_peer:
                connection.connection_group.add_connection(
                    connection,
                    from_peer=True,
                )
            self.anchor_connection = connection
        elif (
            connection.connection_group
            is not self.anchor_connection.connection_group
        ):
            self.anchor_connection.connection_group.add_connection(
                connection,
                from_peer=from_peer,
            )
        self._update_endpoints_from_protocol(connection)
        self._initialize_connection_path(connection)

    def _resolved_endpoints_from_protocol(
        self,
        local_endpoint,
        remote_endpoint,
    ):
        resolved_local = (
            local_endpoint.clone()
            if local_endpoint is not None
            else None
        )
        resolved_remote = (
            remote_endpoint.clone()
            if remote_endpoint is not None
            else None
        )
        protocol_transport = getattr(self.protocol, "_transport", None)
        get_extra_info = getattr(protocol_transport, "get_extra_info", None)
        if not callable(get_extra_info):
            return resolved_local, resolved_remote
        peername = get_extra_info("peername")
        sockname = get_extra_info("sockname")
        if not peername:
            quic = getattr(self.protocol, "_quic", None)
            network_paths = getattr(quic, "_network_paths", ())
            if network_paths:
                peername = getattr(network_paths[0], "addr", None)
        if peername:
            resolved_remote = (
                RemoteEndpoint()
                .with_address(_normalize_quic_host(peername[0]))
                .with_port(peername[1])
            )
        if sockname:
            if resolved_local is None:
                resolved_local = LocalEndpoint()
            resolved_local.address = _normalize_quic_host(sockname[0])
            resolved_local.port = sockname[1]
        return resolved_local, resolved_remote

    def _update_endpoints_from_protocol(self, connection):
        local_endpoint, remote_endpoint = self._resolved_endpoints_from_protocol(
            connection.local_endpoint,
            connection.remote_endpoint,
        )
        connection.local_endpoint = local_endpoint
        connection.remote_endpoint = remote_endpoint
        connection.local_endpoints = (
            [local_endpoint]
            if local_endpoint is not None
            else []
        )
        connection.remote_endpoints = (
            [remote_endpoint]
            if remote_endpoint is not None
            else []
        )

    async def connect_client(
        self,
        connection,
        *,
        local_endpoint=None,
        remote_endpoint=None,
        allow_early_data=True,
        use_session_ticket=True,
    ):
        async with self._connect_lock:
            if self.protocol is not None:
                self._ensure_operational()
                return False
            if self._terminated or self._closing:
                raise ConnectionError("The QUIC association is closed")
            configuration = _build_quic_configuration(
                connection,
                is_client=True,
            )
            remote_endpoint = remote_endpoint or connection.remote_endpoint
            local_endpoint = local_endpoint or connection.local_endpoint
            remote_host = (
                remote_endpoint.socket_address()
                or remote_endpoint.host_name
            )
            if getattr(configuration, "server_name", None) is None:
                configuration.server_name = remote_host
            self._handshake_owner = connection
            self._session_ticket_key = self._client_session_ticket_key(
                connection,
                remote_endpoint,
                configuration,
            )
            early_entry = (
                connection._replayable_initiate_with_send_entry()
                if allow_early_data
                else None
            )
            session_ticket = None
            if use_session_ticket:
                session_ticket = (
                    connection.connection_context
                    .take_quic_client_session_ticket(
                        self._session_ticket_key
                    )
                )
            early_data_available = bool(
                early_entry is not None
                and session_ticket is not None
                and session_ticket.max_early_data_size
                == QUIC_MAX_EARLY_DATA
            )
            if session_ticket is not None:
                self.session_ticket_available = True
                self._resumption_ticket = session_ticket
                configuration.session_ticket = deepcopy(session_ticket)
                if not early_data_available:
                    # Resume the TLS session without offering application
                    # early data when TAPS has no replay-safe Message.
                    configuration.session_ticket.max_early_data_size = None
            local_port = local_endpoint.port if local_endpoint else 0
            context_manager = aioquic_connect(
                remote_host,
                remote_endpoint.port,
                configuration=configuration,
                create_protocol=(
                    lambda quic, stream_handler=None: PytapsQuicProtocol(
                        quic,
                        association=self,
                    )
                ),
                session_ticket_handler=self._cache_client_session_ticket,
                wait_connected=False,
                local_port=local_port or 0,
            )
            self.context_manager = context_manager
            try:
                self.protocol = await context_manager.__aenter__()
                self._terminated = False
                self._termination_error = None
                self.transport_state_changed()
                if not early_data_available:
                    await self.complete_handshake(connection)
            except BaseException as error:
                self._pending_client_session_tickets.clear()
                if self.protocol is not None:
                    await context_manager.__aexit__(
                        type(error),
                        error,
                        error.__traceback__,
                    )
                self.protocol = None
                self.context_manager = None
                raise
            return early_data_available

    async def start_listener(self, listener, *, local_endpoint=None):
        if self.server is not None:
            return self.server
        local_endpoint = local_endpoint or listener.local_endpoint
        self._listener_accepting = True
        self._listener_draining = False
        configuration = _build_quic_configuration(listener, is_client=False)
        if configuration.certificate is None and configuration.private_key is None:
            raise RuntimeError(
                "QUIC listeners require a certificate identity or public/private key."
            )

        def create_protocol(quic, stream_handler=None):
            association = QuicAssociationManager(
                loop=self.loop,
                listener=self.listener,
                parent=self,
            )
            protocol = PytapsQuicProtocol(
                quic,
                association=association,
            )
            association.protocol = protocol
            association._handshake_owner = listener
            self.child_associations.add(association)
            return protocol

        capacity, lifetime = self._session_cache_settings(listener)

        def store_session_ticket(ticket):
            listener.connection_context.cache_quic_server_session_ticket(
                ticket,
                capacity=capacity,
                lifetime=lifetime,
            )

        self.server = await aioquic_serve(
            local_endpoint.address,
            local_endpoint.port,
            configuration=configuration,
            create_protocol=create_protocol,
            session_ticket_fetcher=(
                listener.connection_context
                .take_quic_server_session_ticket
            ),
            session_ticket_handler=store_session_ticket,
            stream_handler=None,
        )
        self._listener_datagram_received = self.server.datagram_received
        return self.server

    def _draining_listener_datagram_received(self, data, addr):
        server = self.server
        if (
            server is None
            or Buffer is None
            or pull_quic_header is None
            or self._listener_datagram_received is None
        ):
            return
        try:
            header = pull_quic_header(
                Buffer(data=bytes(data)),
                host_cid_length=(
                    server._configuration.connection_id_length
                ),
            )
        except ValueError:
            return
        if header.destination_cid not in server._protocols:
            return
        self._listener_datagram_received(data, addr)

    def bound_port(self):
        transport = getattr(self.server, "_transport", None)
        get_extra_info = getattr(transport, "get_extra_info", None)
        if not callable(get_extra_info):
            return None
        sockname = get_extra_info("sockname")
        return sockname[1] if sockname else None

    def _should_close_if_unused(self):
        if self.stream_transports or self.datagram_transport is not None:
            return False
        return (
            self.context_manager is not None
            or (
                self.parent is not None
                and self.parent._listener_draining
            )
        )

    async def _close_if_unused(self):
        if self.protocol is not None and self._should_close_if_unused():
            await self.close_association()

    async def _send_replayable_early_message(
        self,
        connection,
        transport,
        entry,
    ):
        context = connection._apply_message_defaults(
            deepcopy(entry["context"])
        )
        context = transport._coerce_message_context(
            context,
            end_of_message=entry["end_of_message"],
        )
        validation_error = connection._validate_message_context(
            entry["data"],
            context,
        )
        if validation_error is not None:
            return False
        context.is_early_data = True
        await transport._start_framers()
        self.early_data_attempted = True
        self._current_early_data_attempted = True
        await transport.write_before_ready(
            entry["data"],
            context,
            entry["end_of_message"],
        )
        return True

    def _complete_replayable_early_message(
        self,
        connection,
        transport,
        entry,
    ):
        claimed = connection._claim_pre_ready_send(entry)
        if claimed is None:
            return False
        context = connection._apply_message_defaults(claimed["context"])
        context = transport._coerce_message_context(
            context,
            end_of_message=claimed["end_of_message"],
        )
        connection._queue_send_event(
            "sent",
            context,
            send_call_id=claimed["send_call_id"],
        )
        return True

    async def _discard_early_transport(self, transport):
        connection = transport.connection
        if transport in connection.transports:
            connection.transports.remove(transport)
        try:
            await transport._stop_framers()
        finally:
            if isinstance(transport, QuicTransport):
                await transport._close_raw(
                    abort=True,
                    error_code=QUIC_TAPS_ABORT_ERROR_CODE,
                    reason="QUIC 0-RTT was rejected",
                )
            else:
                await transport._close_raw(
                    abort=True,
                    reason="QUIC 0-RTT was rejected",
                )

    def _reset_client_for_reconnect(self, connection):
        self.protocol = None
        self.context_manager = None
        self.stream_transports.clear()
        self.streams_by_id.clear()
        self.datagram_transport = None
        self.pending_datagrams.clear()
        self.anchor_connection = None
        self._terminated = False
        self._closing = False
        self._termination_error = None
        self._handshake_complete_event = asyncio.Event()
        self.handshake_complete = False
        self.session_resumed = False
        self._current_early_data_attempted = False
        self.early_data_accepted = False
        self._peer_certificate_verified = False
        self._resumption_ticket = None
        self._handshake_owner = connection
        self._transport_state_waiters.clear()
        self.current_path = {
            "local": None,
            "remote": None,
        }
        self.previous_path = {
            "local": None,
            "remote": None,
        }
        self._known_network_paths.clear()
        self._client_transports.clear()
        self._active_client_transport = None
        self._active_client_transport_token = None
        self._last_performance_recorded_at = None
        self._last_performance_path = None
        self._last_performance_rtt = None

    async def _restart_after_early_rejection(
        self,
        connection,
        transport,
        *,
        local_endpoint,
        remote_endpoint,
    ):
        await self._discard_early_transport(transport)
        if connection.state is not ConnectionState.ESTABLISHING:
            return False
        self._reset_client_for_reconnect(connection)
        await self.connect_client(
            connection,
            local_endpoint=local_endpoint,
            remote_endpoint=remote_endpoint,
            allow_early_data=False,
            use_session_ticket=False,
        )
        return True

    async def open_stream_connection(
        self,
        connection,
        *,
        local_endpoint=None,
        remote_endpoint=None,
    ):
        local_endpoint = local_endpoint or connection.local_endpoint
        remote_endpoint = remote_endpoint or connection.remote_endpoint
        early_data_available = await self.connect_client(
            connection,
            local_endpoint=local_endpoint,
            remote_endpoint=remote_endpoint,
        )
        local_endpoint, remote_endpoint = self._resolved_endpoints_from_protocol(
            local_endpoint,
            remote_endpoint,
        )
        requested_stream_type = connection.transport_properties.get(
            "_pytaps.quicStreamType"
        )
        direction = str(
            connection.transport_properties.get("direction") or ""
        ).lower()
        is_unidirectional = (
            requested_stream_type == "Unidirectional"
            or (
                requested_stream_type == "Auto"
                and direction == "unidirectional send"
            )
        )
        if requested_stream_type == "Unidirectional":
            connection.transport_properties.selection_properties[
                "direction"
            ] = "Unidirectional Send"
        elif requested_stream_type == "Bidirectional":
            connection.transport_properties.selection_properties[
                "direction"
            ] = "Bidirectional"

        transport = None
        try:
            stream = await self.create_stream(
                is_unidirectional=is_unidirectional,
                wait_for_credit=not early_data_available,
            )
            if stream is None:
                await self.complete_handshake(connection)
                early_data_available = False
                stream = await self.create_stream(
                    is_unidirectional=is_unidirectional,
                )
            reader, writer, stream_id = stream
            self._attach_connection(connection)
            transport = QuicTransport(
                connection=connection,
                local_endpoint=(
                    local_endpoint.clone() if local_endpoint else None
                ),
                remote_endpoint=(
                    remote_endpoint.clone() if remote_endpoint else None
                ),
                association=self,
                reader=reader,
                writer=writer,
                stream_id=stream_id,
                can_send=True,
                can_receive=not is_unidirectional,
            )
            self.register_stream(transport)
            early_entry = (
                connection._replayable_initiate_with_send_entry()
                if early_data_available
                else None
            )
            early_message_sent = False
            if early_entry is not None:
                early_message_sent = (
                    await self._send_replayable_early_message(
                        connection,
                        transport,
                        early_entry,
                    )
                )
            await self.complete_handshake(connection)
            if early_message_sent and self.early_data_rejected:
                restarted = await self._restart_after_early_rejection(
                    connection,
                    transport,
                    local_endpoint=local_endpoint,
                    remote_endpoint=remote_endpoint,
                )
                if not restarted:
                    return transport
                local_endpoint, remote_endpoint = (
                    self._resolved_endpoints_from_protocol(
                        local_endpoint,
                        remote_endpoint,
                    )
                )
                reader, writer, stream_id = await self.create_stream(
                    is_unidirectional=is_unidirectional,
                )
                self._attach_connection(connection)
                transport = QuicTransport(
                    connection=connection,
                    local_endpoint=(
                        local_endpoint.clone()
                        if local_endpoint
                        else None
                    ),
                    remote_endpoint=(
                        remote_endpoint.clone()
                        if remote_endpoint
                        else None
                    ),
                    association=self,
                    reader=reader,
                    writer=writer,
                    stream_id=stream_id,
                    can_send=True,
                    can_receive=not is_unidirectional,
                )
                self.register_stream(transport)
                early_message_sent = False
            activated = await transport.active_open_stream()
            if (
                activated
                and early_message_sent
                and self.early_data_accepted
            ):
                self._complete_replayable_early_message(
                    connection,
                    transport,
                    early_entry,
                )
            return transport
        except BaseException:
            if transport is not None:
                await transport._close_raw(
                    abort=True,
                    error_code=QUIC_TAPS_ABORT_ERROR_CODE,
                    reason="QUIC stream establishment cancelled",
                )
            else:
                await self._close_if_unused()
            raise

    async def accept_inbound_stream(
        self,
        reader,
        writer,
        protocol,
        *,
        is_early_data=False,
    ):
        self.protocol = protocol
        stream_id = self._stream_id(writer)
        if self.listener is None:
            logger.warning(
                "Rejecting peer-created QUIC stream %s because this "
                "association was not created by a Listener.",
                stream_id,
            )
            quic = protocol._quic
            try:
                quic.stop_stream(
                    stream_id,
                    error_code=QUIC_TAPS_ABORT_ERROR_CODE,
                )
            except (AssertionError, ValueError):
                pass
            if stream_id is not None and not bool(stream_id & 0x02):
                try:
                    quic.reset_stream(
                        stream_id,
                        error_code=QUIC_TAPS_ABORT_ERROR_CODE,
                    )
                except (AssertionError, ValueError):
                    pass
            protocol.transmit()
            return
        await self.wait_handshake_complete()
        group_context = (
            self.anchor_connection.connection_context
            if self.anchor_connection is not None
            else None
        )
        connection = self.listener._new_connection(
            connection_context=group_context,
        )
        connection.protocol = "quic"
        connection.quic_association = self
        self._update_endpoints_from_protocol(connection)
        is_unidirectional = (
            stream_id is not None and bool(stream_id & 0x02)
        )
        connection.transport_properties.protocol_properties[
            "_pytaps.quicStreamType"
        ] = (
            "Unidirectional"
            if is_unidirectional
            else "Bidirectional"
        )
        connection.transport_properties.selection_properties[
            "direction"
        ] = (
            "Unidirectional Receive"
            if is_unidirectional
            else "Bidirectional"
        )
        try:
            self._attach_connection(connection, from_peer=True)
        except RuntimeError as exc:
            connection._report_connection_error(exc)
            writer.close()
            return
        transport = QuicTransport(
            connection=connection,
            local_endpoint=connection.local_endpoint,
            remote_endpoint=connection.remote_endpoint,
            association=self,
            reader=reader,
            writer=writer,
            listener=self.listener,
            stream_id=stream_id,
            can_send=not is_unidirectional,
            can_receive=True,
            initial_data_is_early=is_early_data,
        )
        self.register_stream(transport)
        try:
            await transport.passive_open_stream()
        except BaseException:
            await transport._close_raw(
                abort=True,
                error_code=QUIC_TAPS_ABORT_ERROR_CODE,
                reason="Inbound QUIC stream setup failed",
            )
            raise

    async def open_datagram_connection(
        self,
        connection,
        *,
        local_endpoint=None,
        remote_endpoint=None,
    ):
        local_endpoint = local_endpoint or connection.local_endpoint
        remote_endpoint = remote_endpoint or connection.remote_endpoint
        early_data_available = await self.connect_client(
            connection,
            local_endpoint=local_endpoint,
            remote_endpoint=remote_endpoint,
        )
        if early_data_available and not self.supports_datagrams:
            await self.complete_handshake(connection)
            early_data_available = False
        local_endpoint, remote_endpoint = self._resolved_endpoints_from_protocol(
            local_endpoint,
            remote_endpoint,
        )
        try:
            if not self.supports_datagrams:
                raise RuntimeError(
                    "The QUIC peer did not negotiate RFC 9221 DATAGRAM support"
                )
            if self.datagram_transport is not None:
                raise RuntimeError(
                    "Raw QUIC provides one association-wide datagram channel"
                )
        except BaseException:
            await self._close_if_unused()
            raise
        self._attach_connection(connection)
        self._configure_datagram_connection(connection)
        transport = QuicDatagramTransport(
            connection=connection,
            local_endpoint=local_endpoint.clone() if local_endpoint else None,
            remote_endpoint=(
                remote_endpoint.clone() if remote_endpoint else None
            ),
            association=self,
        )
        self.register_datagram(transport)
        early_entry = (
            connection._replayable_initiate_with_send_entry()
            if early_data_available
            else None
        )
        early_message_sent = False
        if early_entry is not None:
            early_message_sent = await self._send_replayable_early_message(
                connection,
                transport,
                early_entry,
            )
        await self.complete_handshake(connection)
        if early_message_sent and self.early_data_rejected:
            restarted = await self._restart_after_early_rejection(
                connection,
                transport,
                local_endpoint=local_endpoint,
                remote_endpoint=remote_endpoint,
            )
            if not restarted:
                return transport
            local_endpoint, remote_endpoint = (
                self._resolved_endpoints_from_protocol(
                    local_endpoint,
                    remote_endpoint,
                )
            )
            if not self.supports_datagrams:
                await self._close_if_unused()
                raise RuntimeError(
                    "The QUIC peer did not negotiate RFC 9221 DATAGRAM support"
                )
            self._attach_connection(connection)
            self._configure_datagram_connection(connection)
            transport = QuicDatagramTransport(
                connection=connection,
                local_endpoint=(
                    local_endpoint.clone() if local_endpoint else None
                ),
                remote_endpoint=(
                    remote_endpoint.clone() if remote_endpoint else None
                ),
                association=self,
            )
            self.register_datagram(transport)
            early_message_sent = False
        activated = await transport.active_open_datagram()
        if (
            activated
            and early_message_sent
            and self.early_data_accepted
        ):
            self._complete_replayable_early_message(
                connection,
                transport,
                early_entry,
            )
        return transport

    def _configure_datagram_connection(self, connection):
        connection.transport_properties.protocol_properties[
            "_pytaps.quicTransportMode"
        ] = "Datagram"
        connection.transport_properties.selection_properties.update(
            {
                "reliability": PreferenceLevel.PROHIBIT,
                "preserveOrder": PreferenceLevel.PROHIBIT,
                "preserveMsgBoundaries": PreferenceLevel.REQUIRE,
                "perMsgReliability": PreferenceLevel.PROHIBIT,
            }
        )
        connection._protocol_capabilities = {
            "reliability": False,
            "preserveOrder": False,
            "preserveMsgBoundaries": True,
            "perMsgReliability": False,
        }

    @property
    def supports_datagrams(self):
        quic = getattr(self.protocol, "_quic", None)
        return (
            quic is not None
            and getattr(
                quic,
                "_remote_max_datagram_frame_size",
                None,
            )
            is not None
        )

    @property
    def max_datagram_payload(self):
        if not self.supports_datagrams:
            return 0
        quic = self.protocol._quic
        remote_limit = quic._remote_max_datagram_frame_size
        configuration = getattr(quic, "_configuration", None)
        packet_limit = getattr(configuration, "max_datagram_size", 1200)
        return max(
            0,
            min(
                remote_limit - 9,
                packet_limit - QUIC_DATAGRAM_PACKET_OVERHEAD,
            ),
        )

    async def send_datagram(self, data):
        self._ensure_operational()
        if not self.supports_datagrams:
            raise RuntimeError(
                "The QUIC peer did not negotiate RFC 9221 DATAGRAM support"
            )
        data = bytes(data)
        if len(data) > self.max_datagram_payload:
            raise ValueError(
                "Message exceeds the negotiated QUIC DATAGRAM payload limit "
                f"of {self.max_datagram_payload} bytes"
            )
        while True:
            waiter = None
            async with self._write_budget_lock:
                self._ensure_operational()
                pending = self.protocol._quic._datagrams_pending
                if len(pending) < QUIC_DATAGRAM_QUEUE_LIMIT:
                    self.protocol._quic.send_datagram_frame(data)
                    self.protocol.transmit()
                    return
                waiter = self._new_transport_state_waiter()
            await self._wait_for_transport_state_change(waiter)

    def datagram_received(self, data, *, is_early_data=False):
        if self._terminated or self._closing:
            return
        if self.datagram_transport is not None:
            self.datagram_transport.datagram_received(
                data,
                is_early_data=is_early_data,
            )
            return
        self.pending_datagrams.append(
            (bytes(data), bool(is_early_data))
        )
        if len(self.pending_datagrams) > QUIC_DATAGRAM_QUEUE_LIMIT:
            self.pending_datagrams.pop(0)
        if (
            self.listener is not None
            and (
                self._passive_datagram_task is None
                or self._passive_datagram_task.done()
            )
        ):
            self._passive_datagram_task = self.create_background_task(
                self._open_passive_datagram_connection()
            )

    async def _open_passive_datagram_connection(self):
        await self.wait_handshake_complete()
        if self._terminated or self._closing:
            self.pending_datagrams.clear()
            return
        group_context = (
            self.anchor_connection.connection_context
            if self.anchor_connection is not None
            else None
        )
        connection = self.listener._new_connection(
            connection_context=group_context,
        )
        connection.protocol = "quic"
        connection.quic_association = self
        self._update_endpoints_from_protocol(connection)
        self._configure_datagram_connection(connection)
        try:
            self._attach_connection(connection, from_peer=True)
        except RuntimeError as error:
            connection._report_connection_error(error)
            self.pending_datagrams.clear()
            return
        transport = QuicDatagramTransport(
            connection=connection,
            local_endpoint=connection.local_endpoint,
            remote_endpoint=connection.remote_endpoint,
            association=self,
            listener=self.listener,
        )
        self.register_datagram(transport)
        await transport.passive_open_datagram()

    def register_stream(self, transport):
        existing = self.streams_by_id.get(transport.stream_id)
        if existing is not None and existing is not transport:
            raise RuntimeError(
                f"QUIC stream ID {transport.stream_id} is already registered"
            )
        self.stream_transports.add(transport)
        if transport.stream_id is not None:
            self.streams_by_id[transport.stream_id] = transport

    def register_datagram(self, transport):
        self._ensure_operational()
        if (
            self.datagram_transport is not None
            and self.datagram_transport is not transport
        ):
            raise RuntimeError(
                "Raw QUIC provides one association-wide datagram channel"
            )
        self.datagram_transport = transport
        pending = self.pending_datagrams
        self.pending_datagrams = []
        for data, is_early_data in pending:
            transport.datagram_received(
                data,
                is_early_data=is_early_data,
            )

    def remove_stream(self, transport):
        self.stream_transports.discard(transport)
        if (
            transport.stream_id is not None
            and self.streams_by_id.get(transport.stream_id) is transport
        ):
            self.streams_by_id.pop(transport.stream_id, None)
        self._replace_anchor(transport.connection)
        self.transport_state_changed()

    def remove_datagram(self, transport):
        if self.datagram_transport is transport:
            self.datagram_transport = None
        self._replace_anchor(transport.connection)
        self.transport_state_changed()

    def _replace_anchor(self, removed_connection):
        if self.anchor_connection is not removed_connection:
            return
        candidates = [
            transport.connection
            for transport in self.stream_transports
        ]
        if self.datagram_transport is not None:
            candidates.append(self.datagram_transport.connection)
        if candidates:
            self.anchor_connection = candidates[0]

    def stream_event_received(self, stream_id, error_code, operation):
        transport = self.streams_by_id.get(stream_id)
        if transport is not None:
            transport.peer_stream_event(error_code, operation)

    def connection_terminated(self, event):
        if self._terminated:
            return
        error = None
        if event.error_code:
            error = QuicAssociationError(
                event.error_code,
                event.reason_phrase,
                event.frame_type,
            )
        self._terminate_members(error)
        self.create_background_task(self.close_association())

    def _terminate_members(self, error):
        if self._terminated:
            return
        self._terminated = True
        self._termination_error = error
        if not self._handshake_complete_event.is_set():
            self._handshake_complete_event.set()
        self.transport_state_changed()
        current = asyncio.current_task()
        for task in list(self._background_tasks):
            if task is not current and not task.done():
                task.cancel()

        transports = list(self.stream_transports)
        if self.datagram_transport is not None:
            transports.append(self.datagram_transport)
        self.stream_transports.clear()
        self.streams_by_id.clear()
        self.datagram_transport = None
        self.pending_datagrams.clear()
        self._passive_datagram_task = None
        for transport in transports:
            transport.association_terminated(error)
        if self.parent is not None:
            self.parent.child_associations.discard(self)

    async def close_association(
        self,
        *,
        error_code=0,
        reason_phrase="",
        local_error=None,
    ):
        async with self._close_lock:
            if (
                self.protocol is None
                and self.context_manager is None
            ):
                self._closing = True
                if not self._terminated:
                    self._terminate_members(local_error)
                return
            self._closing = True
            self.transport_state_changed()
            protocol = self.protocol
            context_manager = self.context_manager
            if not self._terminated:
                self._terminate_members(local_error)
            try:
                if protocol is not None and error_code:
                    protocol.close(
                        error_code=error_code,
                        reason_phrase=reason_phrase,
                    )
                if context_manager is not None:
                    await context_manager.__aexit__(None, None, None)
                elif protocol is not None:
                    protocol.close(
                        error_code=error_code,
                        reason_phrase=reason_phrase,
                    )
                    wait_closed = getattr(protocol, "wait_closed", None)
                    if callable(wait_closed):
                        await wait_closed()
            finally:
                for client_transport in list(self._client_transports):
                    client_transport.close()
                self._client_transports.clear()
                self._active_client_transport = None
                self._active_client_transport_token = None
                self.protocol = None
                self.context_manager = None
                if self.parent is not None:
                    self.parent.child_associations.discard(self)
                    self.parent._close_listener_server_if_idle()
                await self._cancel_background_tasks()

    async def abort_association(self, reason="QUIC association aborted"):
        error = QuicAssociationError(
            QUIC_TAPS_ABORT_ERROR_CODE,
            str(reason),
        )
        await self.close_association(
            error_code=QUIC_TAPS_ABORT_ERROR_CODE,
            reason_phrase=str(reason),
            local_error=error,
        )

    def _close_listener_server_if_idle(self):
        if (
            self.server is None
            or self.child_associations
            or not self._listener_draining
        ):
            return False
        self.server.close()
        self.server = None
        self._listener_draining = False
        drain_completed = getattr(
            self.listener,
            "_quic_listener_drain_completed",
            None,
        )
        if callable(drain_completed):
            drain_completed(self)
        return True

    async def stop_listener(self, *, close_associations=False):
        self._listener_accepting = False
        self._listener_draining = True
        if self.server is not None:
            self.server.datagram_received = (
                self._draining_listener_datagram_received
            )
        associations_to_close = (
            list(self.child_associations)
            if close_associations
            else [
                association
                for association in self.child_associations
                if association._should_close_if_unused()
            ]
        )
        if associations_to_close:
            await asyncio.gather(
                *(
                    association.close_association()
                    for association in associations_to_close
                ),
                return_exceptions=True,
            )
            if close_associations:
                self.child_associations.clear()
        self._close_listener_server_if_idle()

class TransportLayer(asyncio.Protocol):
    """ One possible underlying transport for a TAPS connection

        Attributes:
        connection (Connection, required):
                Connection object to which this
                Transport will be attached.
        local_endpoint (LocalEndpoint, optional):
                        LocalEndpoint
        remote_endpoint (RemoteEndpoint, optional):
                        RemoteEndpoint
    """

    def __init__(self, connection, local_endpoint=None, remote_endpoint=None):
        self.local_endpoint = local_endpoint
        self.remote_endpoint = remote_endpoint
        self.connection = connection
        self.loop = connection.loop
        self.connection.transports.append(self)
        self.waiters = []
        self._receive_lock = asyncio.Lock()
        self.open_receives = 0
        # Keeping track of how many messages have been sent for msgref
        self.message_count = 0
        # Determines if the protocol is message based or not (needed?)
        self.message_based = True
        # Reception buffer, holding data returned from the OS
        self.recv_buffer = None
        # Boolean to indicate that EOF has been reached
        self.at_eof = False

        self.framer_buffer = []
        self.framer_stack = (
            FramerStack(connection, connection.framers, self)
            if connection.framers
            else None
        )

        self.transport = None
        self._raw_close_waiter = self.loop.create_future()
        self.current_message_context = None
        self._current_message_was_partial = False
        self._open_task = None
        self._send_tasks = set()

    def _new_message_context(self, *, end_of_message=True):
        context = MessageContext(end_of_message=end_of_message)
        if self.remote_endpoint and self.remote_endpoint.address:
            context.remote_address = self.remote_endpoint.address
        if self.remote_endpoint and self.remote_endpoint.port:
            context.remote_port = self.remote_endpoint.port
        if self.local_endpoint and self.local_endpoint.address:
            context.local_address = self.local_endpoint.address
        if self.local_endpoint and self.local_endpoint.port:
            context.local_port = self.local_endpoint.port
        return context

    def _coerce_message_context(self, message_context=None, *, end_of_message=True):
        if message_context is None:
            return self._new_message_context(end_of_message=end_of_message)
        message_context.end_of_message = end_of_message
        if (
            message_context.remote_address is None
            and self.remote_endpoint
            and self.remote_endpoint.address
        ):
            message_context.remote_address = self.remote_endpoint.address
        if (
            message_context.remote_port is None
            and self.remote_endpoint
            and self.remote_endpoint.port
        ):
            message_context.remote_port = self.remote_endpoint.port
        if (
            message_context.local_address is None
            and self.local_endpoint
            and self.local_endpoint.address
        ):
            message_context.local_address = self.local_endpoint.address
        if (
            message_context.local_port is None
            and self.local_endpoint
            and self.local_endpoint.port
        ):
            message_context.local_port = self.local_endpoint.port
        return message_context

    """ Function that blocks until new data has arrived
    """

    async def await_data(self):
        waiter = self.loop.create_future()
        self.waiters.append(waiter)
        try:
            await waiter
        finally:
            if waiter in self.waiters:
                self.waiters.remove(waiter)

    def _wake_receive_waiter(self):
        for waiter in self.waiters:
            if not waiter.done():
                waiter.set_result(None)
                return

    async def _feed_framer(self, data, context, end_of_message):
        try:
            await self.framer_stack.feed_received(
                data,
                context,
                end_of_message,
            )
        except FramerFailed as error:
            await self._fail_from_framer(error)

    async def _start_framers(self):
        if self.framer_stack is not None:
            await self.framer_stack.start()

    async def _activate_candidate(self):
        try:
            await self._start_framers()
        except BaseException:
            await self._close_raw()
            raise
        if (
            self.connection.state is ConnectionState.ESTABLISHED
            and self.connection.transports
            and self.connection.transports[0] is self
        ):
            self._select_transport()
            self._commit_candidate()
            return True
        if self.connection.state is not ConnectionState.ESTABLISHING:
            await self._stop_framers()
            await self._close_raw()
            return False
        self._select_transport()
        self._commit_candidate()
        return True

    async def wait_open(self):
        while self._open_task is None:
            await asyncio.sleep(0)
        return await self._open_task

    def _set_open_error(self, error):
        self._open_task = self.loop.create_future()
        self._open_task.set_exception(error)

    async def _stop_framers(self):
        if self.framer_stack is not None:
            await self.framer_stack.stop()

    async def _wait_for_raw_close(self):
        if (
            self.transport is not None
            and isinstance(self.transport, asyncio.BaseTransport)
            and not self._raw_close_waiter.done()
        ):
            await asyncio.shield(self._raw_close_waiter)

    def _select_transport(self):
        if self in self.connection.transports:
            self.connection.transports.remove(self)
        self.connection.transports.insert(0, self)
        if self.framer_stack is not None:
            self.framer_stack.select()

    def _commit_candidate(self):
        protocol_name = getattr(
            self,
            "protocol_name",
            self.connection.protocol,
        )
        self.connection._validate_connection_configuration(protocol_name)
        self.connection.protocol = protocol_name
        self.connection.local_endpoint = (
            self.local_endpoint.clone()
            if self.local_endpoint is not None
            else None
        )
        self.connection.remote_endpoint = (
            self.remote_endpoint.clone()
            if self.remote_endpoint is not None
            else None
        )
        get_extra_info = getattr(self.transport, "get_extra_info", None)
        sockname = (
            get_extra_info("sockname")
            if callable(get_extra_info)
            else None
        )
        if sockname:
            if self.connection.local_endpoint is None:
                self.connection.local_endpoint = LocalEndpoint()
            self.connection.local_endpoint.address = sockname[0]
            self.connection.local_endpoint.port = sockname[1]
            self.local_endpoint = self.connection.local_endpoint.clone()
        self.connection._protocol_capabilities = get_protocol_capabilities(
            protocol_name,
            self.connection.transport_properties,
        )
        self.connection._apply_connection_properties()

    def _record_property_effect(self, property_name, effect):
        self.connection._backend_property_effects[property_name] = effect

    def _transport_socket(self):
        get_extra_info = getattr(self.transport, "get_extra_info", None)
        if not callable(get_extra_info):
            return None
        return get_extra_info("socket")

    def _set_socket_option(
        self,
        level,
        option,
        value,
        property_name,
        effect,
    ):
        transport_socket = self._transport_socket()
        if transport_socket is None:
            self._record_property_effect(property_name, "unsupported")
            return False
        try:
            transport_socket.setsockopt(level, option, value)
        except (AttributeError, OSError):
            self._record_property_effect(property_name, "unsupported")
            return False
        self._record_property_effect(property_name, effect)
        return True

    def _apply_capacity_profile(self):
        profile = self.connection.transport_properties.get(
            "connCapacityProfile"
        )
        dscp = CAPACITY_PROFILE_DSCP[profile]
        transport_socket = self._transport_socket()
        family = getattr(transport_socket, "family", None)
        if family is None:
            address = (
                self.remote_endpoint.address
                if self.remote_endpoint is not None
                else None
            )
            family = (
                socket.AF_INET6
                if address is not None and ":" in address
                else socket.AF_INET
            )
        if family == socket.AF_INET6:
            level = socket.IPPROTO_IPV6
            option = getattr(socket, "IPV6_TCLASS", None)
            option_name = "IPV6_TCLASS"
        else:
            level = socket.IPPROTO_IP
            option = getattr(socket, "IP_TOS", None)
            option_name = "IP_TOS"
        if option is None:
            self._record_property_effect(
                "connCapacityProfile",
                "unsupported",
            )
            return False
        return self._set_socket_option(
            level,
            option,
            dscp << 2,
            "connCapacityProfile",
            f"applied:{option_name}:DSCP={dscp}",
        )

    def apply_connection_properties(self, *, strict_property=None):
        return None

    async def _frame_outbound(self, data, context, end_of_message):
        if self.framer_stack is None:
            return data
        return await self.framer_stack.frame_outbound(
            data,
            context,
            end_of_message,
        )

    async def _write_framer_data(self, data):
        return await self._write_raw(data)

    async def _write_raw(self, data):
        raise NotImplementedError

    async def _close_raw(self):
        raise NotImplementedError

    async def _fail_from_framer(self, error):
        if self.connection._is_terminal():
            return
        try:
            await self._close_raw()
        finally:
            self.connection._report_connection_error(error)

    async def _read_framed_message(self, min_incomplete_length, max_length):
        if max_length == -1:
            max_length = float("inf")
        while not self.framer_buffer:
            await self.await_data()

        data, context, end_of_message = self.framer_buffer.pop(0)
        if (
            max_length != float("inf")
            and hasattr(data, "__len__")
            and hasattr(data, "__getitem__")
            and len(data) > max_length
        ):
            delivered = data[:max_length]
            remaining = data[max_length:]
            context.end_of_message = False
            self.framer_buffer.insert(
                0,
                (remaining, context, end_of_message),
            )
            self.connection._deliver_received_partial(delivered, context)
            return

        context.end_of_message = end_of_message
        if end_of_message:
            self.connection._deliver_received(data, context)
        else:
            self.connection._deliver_received_partial(data, context)

    def send(self, data, message_context=None, end_of_message=True, send_call_id=None):
        """ Function responsible for sending data.
        """
        self.message_count += 1
        context = self._coerce_message_context(
            message_context,
            end_of_message=end_of_message,
        )
        if send_call_id is None:
            send_call_id = self.connection._allocate_send_call_id()
        setattr(context, "_pytaps_send_call_id", send_call_id)
        if context.message_id is None:
            context.message_id = self.message_count
        if self.connection.state not in {
            ConnectionState.ESTABLISHED,
            ConnectionState.CLOSING,
        }:
            logger.warning("SendError occurred, connection is not established.")
            self.connection._queue_send_event(
                "send_error",
                context,
                RuntimeError("Connection is not established"),
                send_call_id=send_call_id,
            )
            return
        if context.is_expired():
            self.connection._queue_send_event(
                "expired",
                context,
                send_call_id=send_call_id,
            )
            return self.message_count
        task = self.loop.create_task(
            self._write_with_expiration(
                data,
                context,
                end_of_message,
                send_call_id=send_call_id,
            )
        )
        self._send_tasks.add(task)
        task.add_done_callback(self._send_task_done)
        return self.message_count

    def _send_task_done(self, task):
        self._send_tasks.discard(task)
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    async def _cancel_send_tasks(self):
        current = asyncio.current_task()
        tasks = [
            task
            for task in self._send_tasks
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _write_with_expiration(self, data, message_context, end_of_message, send_call_id=None):
        if message_context.is_expired():
            self.connection._queue_send_event(
                "expired",
                message_context,
                send_call_id=send_call_id,
            )
            return
        try:
            await self.write(
                data,
                message_context,
                end_of_message,
                send_call_id=send_call_id,
            )
        except Exception as exc:
            self.connection._queue_send_event(
                "send_error",
                message_context,
                exc,
                send_call_id=send_call_id,
            )

    async def write(self, data, message_context, end_of_message, send_call_id=None):
        pass

    def receive(self, min_incomplete_length, max_length):
        async def _serialized_read():
            async with self._receive_lock:
                await self.read(min_incomplete_length, max_length)

        return self.loop.create_task(_serialized_read())

    async def read(self, min_incomplete_length,
                   max_length):
        pass

    async def _read_stream_buffer(self, min_incomplete_length, max_length):
        if max_length == -1:
            max_length = float("inf")
        while True:
            available = len(self.recv_buffer) if self.recv_buffer is not None else 0
            if self.at_eof:
                if available == 0:
                    if (
                        self.current_message_context is not None
                        and self._current_message_was_partial
                    ):
                        context = self.current_message_context
                        context.end_of_message = True
                        context.final = True
                        self.connection._deliver_received_partial(
                            b"",
                            context,
                        )
                        self.connection._received_final_message = True
                        self.current_message_context = None
                        self._current_message_was_partial = False
                        return
                    raise EOFError("The stream has reached EOF")
                break
            if (
                min_incomplete_length != float("inf")
                and available >= min_incomplete_length
            ):
                break
            if max_length != float("inf") and available >= max_length:
                break
            await self.await_data()

        if max_length == float("inf") or available <= max_length:
            data = self.recv_buffer
            self.recv_buffer = None
        else:
            data = self.recv_buffer[:max_length]
            self.recv_buffer = self.recv_buffer[max_length:]

        if self.current_message_context is None:
            self.current_message_context = self._new_message_context(
                end_of_message=False
            )
        context = self.current_message_context
        end_of_message = self.at_eof and not self.recv_buffer
        context.end_of_message = end_of_message

        if end_of_message:
            context.final = True
            if self._current_message_was_partial:
                self.connection._deliver_received_partial(data, context)
            else:
                self.connection._deliver_received(data, context)
            self.connection._received_final_message = True
            self.current_message_context = None
            self._current_message_was_partial = False
            return

        self._current_message_was_partial = True
        self.connection._deliver_received_partial(data, context)

    async def close(self):
        pass

    """ ASYNCIO function that gets called when EOF is received
    """

    def eof_received(self):
        logger.info("EOF received")
        self.at_eof = True
        if self.current_message_context is None:
            self.current_message_context = self._new_message_context(
                end_of_message=True
            )
        else:
            self.current_message_context.end_of_message = True
        if self.framer_stack is not None:
            self.loop.create_task(self.framer_stack.mark_end_of_stream())
        for waiter in self.waiters:
            if not waiter.done():
                waiter.set_result(None)

    """ ASYNCIO function that gets called when the connection has
        an error.
        TODO: proper error handling
    """

    def error_received(self, err):
        if type(err) is ConnectionRefusedError:
            logger.warning("Connection Error occurred.")
            self.connection._report_connection_error(err)
            return

    """ ASYNCIO function that gets called when the connection
        is lost
    """

    def connection_lost(self, exc):
        if not self._raw_close_waiter.done():
            self._raw_close_waiter.set_result(None)
        if (
            self.connection.active
            and self.connection.state is ConnectionState.ESTABLISHING
        ):
            return
        receive_reason = exc or ConnectionError(
            "Receive terminated before the current message completed"
        )
        if exc is not None:
            for waiter in list(self.waiters):
                if not waiter.done():
                    waiter.set_exception(receive_reason)
        if (
            not self.waiters
            and self.recv_buffer
            and self.current_message_context is not None
            and not self.at_eof
        ):
            self.connection._report_receive_error(
                self.current_message_context,
                receive_reason,
            )
        if self.framer_stack is not None and not self.connection._is_terminal():
            self.loop.create_task(self._finish_framed_connection_lost(exc))
            return
        self._report_connection_lost(exc)

    def _report_connection_lost(self, exc):
        if exc is None:
            logger.info("Connection closed by the peer.")
            self.connection._report_closed()
        else:
            logger.warning("Connection lost with error.")
            self.connection._report_connection_error(exc)

    async def _finish_framed_connection_lost(self, exc):
        try:
            await self._stop_framers()
        except Exception as error:
            self.connection._report_connection_error(error)
            return
        self._report_connection_lost(exc)

    async def passive_open(self, transport):
        self.transport = transport
        get_extra_info = getattr(transport, "get_extra_info", None)
        peername = (
            get_extra_info("peername")
            if callable(get_extra_info)
            else None
        )
        if peername:
            new_remote_endpoint = RemoteEndpoint()
            logger.info("Received new connection.")
            new_remote_endpoint.with_address(peername[0])
            new_remote_endpoint.with_port(peername[1])
            self.remote_endpoint = new_remote_endpoint
        sockname = (
            get_extra_info("sockname")
            if callable(get_extra_info)
            else None
        )
        if sockname:
            self.connection.note_path_change(
                local_address=sockname[0],
                local_port=sockname[1],
                remote_address=self.remote_endpoint.address,
                remote_port=self.remote_endpoint.port,
            )
        await self._start_framers()
        self._select_transport()
        self._commit_candidate()
        if hasattr(self.connection._originating_preconnection, "_deliver_connection"):
            self.connection._originating_preconnection._deliver_connection(self.connection)
        else:
            self.connection._mark_passive_ready()
        return


class UdpTransport(TransportLayer):
    def __init__(
        self,
        connection,
        local_endpoint=None,
        remote_endpoint=None,
        *,
        protocol_name="udp",
    ):
        super().__init__(connection, local_endpoint, remote_endpoint)
        self.protocol_name = protocol_name

    def apply_connection_properties(self, *, strict_property=None):
        self._apply_capacity_profile()

    async def active_open(self, transport):
        self.transport = transport
        logger.info("Connected successfully UDP to " +
                    str(self.remote_endpoint.address) +
                    ":" + str(self.remote_endpoint.port) +
                    ".")
        if not await self._activate_candidate():
            return
        sockname = transport.get_extra_info("sockname") if transport else None
        if sockname:
            self.connection.note_path_change(
                local_address=sockname[0],
                local_port=sockname[1],
                remote_address=self.remote_endpoint.address,
                remote_port=self.remote_endpoint.port,
            )
        self.connection._mark_ready()
        return

    async def write(self, data, message_context, end_of_message, send_call_id=None):
        """ Sends udp data
        """
        logger.info("Writing UDP data to " +
                    str(self.remote_endpoint.address) +
                    ":" + str(self.remote_endpoint.port) +
                    ".")
        if isinstance(data, str):
            data = data.encode()
        try:
            data = await self._frame_outbound(
                data,
                message_context,
                end_of_message,
            )
            await self._write_raw(data)
        except InterruptedError:
            logger.warning("SendError occurred.")
            self.connection._queue_send_event(
                "send_error",
                message_context,
                InterruptedError("UDP send interrupted"),
                send_call_id=send_call_id,
            )
            return
        logger.info("Data written successfully.")
        self.connection._queue_send_event(
            "sent",
            message_context,
            send_call_id=send_call_id,
        )
        return

    async def _write_raw(self, data):
        if isinstance(data, str):
            data = data.encode()
        if self.connection.active:
            self.transport.sendto(data)
        else:
            self.transport.sendto(
                data,
                (self.remote_endpoint.address, self.remote_endpoint.port),
            )

    async def _close_raw(self):
        if self.transport is not None:
            self.transport.close()

    async def close(self):
        logger.info("Closing connection.")
        await self._stop_framers()
        await self._close_raw()
        await self._wait_for_raw_close()
        self.connection._report_closed()

    async def read(self, min_incomplete_length, max_length):
        if max_length == -1:
            max_length = float("inf")
        if self.framer_stack is not None:
            await self._read_framed_message(
                min_incomplete_length,
                max_length,
            )
            return
        if self.recv_buffer is None:
            await self.await_data()
        data, context = self.recv_buffer[0]
        was_partial = getattr(context, "_pytaps_partial_delivery", False)
        if max_length != float("inf") and len(data) > max_length:
            delivered = data[:max_length]
            remaining = data[max_length:]
            self.recv_buffer[0] = (remaining, context)
            context.end_of_message = False
            setattr(context, "_pytaps_partial_delivery", True)
            self.connection._deliver_received_partial(delivered, context)
            return
        if len(self.recv_buffer) == 1:
            self.recv_buffer = None
        else:
            self.recv_buffer.pop(0)
        context.end_of_message = True
        if was_partial:
            self.connection._deliver_received_partial(data, context)
        else:
            self.connection._deliver_received(data, context)

    # Asyncio Callbacks

    """ ASYNCIO function that gets called when a new
        connection has been made, similar to TAPS ready callback.
    """

    def connection_made(self, transport):
        if self.connection.state == ConnectionState.ESTABLISHED:
            transport.close()
            self._open_task = self.loop.create_task(asyncio.sleep(0))
            return

        # Check if its an incoming or outgoing connection
        if self.connection.active:
            self._open_task = self.loop.create_task(self.active_open(transport))
        else:
            self._open_task = self.loop.create_task(self.passive_open(transport))
            # Stub code for forcefully killing connection tasks
            # Before establishment to the peer has been completed
            """
            for t in self.connection.transports:
                if t != self:
                    self.connection.transports.remove(t)

            for t in self.connection.pending.keys():
                if self.connection.pending[t] != self:
                    print(self.connection.pending[t])
                    print(t)
                    t.cancel()"""

    """ ASYNCIO function that gets called when a new datagram
        is received. It stores the datagram in the recv_buffer
    """

    def datagram_received(self, data, addr):
        self.connection._mark_first_message()
        context = self._new_message_context(end_of_message=True)
        context.addr = addr
        self.current_message_context = context
        if self.framer_stack is not None:
            self.loop.create_task(
                self._feed_framer(data, context, True)
            )
            return
        if self.recv_buffer is None:
            self.recv_buffer = list()
        self.recv_buffer.append((data, context))
        self._wake_receive_waiter()


class MulticastSendTransport(TransportLayer):
    def __init__(self, connection, local_endpoint=None, remote_endpoint=None):
        super().__init__(connection, local_endpoint, remote_endpoint)
        self.protocol_name = "udp"
        self.message_based = True
        self.mctx_context = None
        self.publication = None
        self.async_publication = None

    async def active_open(self, transport):
        _require_mctx_core()

        source = None
        source_port = None
        if self.local_endpoint and self.local_endpoint.address:
            source = self.local_endpoint.address
        if self.local_endpoint and self.local_endpoint.port:
            source_port = self.local_endpoint.port

        interface = self.local_endpoint.interface if self.local_endpoint else None
        interface = getattr(
            self.connection._originating_preconnection,
            "multicast_interface_address",
            interface,
        )
        ttl = self.remote_endpoint.hop_limit
        if ttl is None:
            ttl = getattr(
                self.connection._originating_preconnection,
                "multicast_ttl",
                1,
            )
        loopback = not getattr(
            self.connection._originating_preconnection,
            "multicast_disable_loopback",
            False,
        )

        self.mctx_context = mctx_core.Context()
        self.publication = self.mctx_context.add_publication(
            self.remote_endpoint.multicast_group,
            self.remote_endpoint.port,
            source=source,
            source_port=source_port,
            interface=interface,
            ttl=ttl,
            loopback=loopback,
        )
        self.async_publication = mctx_core.AsyncPublication(
            self.publication,
            loop=self.loop,
        )

        try:
            local_addr = self.publication.local_addr()
        except Exception:
            local_addr = None
        logger.info(
            "Connected multicast sender to %s:%s.",
            self.remote_endpoint.multicast_group,
            self.remote_endpoint.port,
        )
        if not await self._activate_candidate():
            return
        self.connection.multicast_open = True
        if local_addr:
            self.connection.note_path_change(
                local_address=local_addr[0],
                local_port=local_addr[1],
                remote_address=self.remote_endpoint.multicast_group,
                remote_port=self.remote_endpoint.port,
            )
        self.connection._mark_ready()

    async def write(self, data, message_context, end_of_message, send_call_id=None):
        if isinstance(data, str):
            data = data.encode()
        try:
            data = await self._frame_outbound(
                data,
                message_context,
                end_of_message,
            )
            report = await self._write_raw(data)
        except InterruptedError:
            logger.warning("SendError occurred.")
            self.connection._queue_send_event(
                "send_error",
                message_context,
                InterruptedError("Multicast send interrupted"),
                send_call_id=send_call_id,
            )
            return

        if report.local_addr:
            message_context.local_address = report.local_addr[0]
            message_context.local_port = report.local_addr[1]
        if report.source_addr:
            message_context.local_address = report.source_addr
        logger.info("Multicast packet written successfully.")
        self.connection._queue_send_event(
            "sent",
            message_context,
            send_call_id=send_call_id,
        )

    async def _write_raw(self, data):
        if isinstance(data, str):
            data = data.encode()
        return await self.async_publication.send(data)

    async def _close_raw(self):
        if self.publication is not None:
            self.publication.remove()
        self.publication = None
        self.async_publication = None
        self.mctx_context = None

    async def close(self):
        logger.info("Closing multicast sender.")
        await self._stop_framers()
        await self._close_raw()
        self.connection._report_closed()

    async def read(self, min_incomplete_length, max_length):
        raise RuntimeError("Multicast sender transports do not support receive().")


class QuicTransport(TransportLayer):
    def __init__(
        self,
        connection,
        local_endpoint=None,
        remote_endpoint=None,
        *,
        association,
        reader,
        writer,
        listener=None,
        stream_id=None,
        can_send=True,
        can_receive=True,
        initial_data_is_early=False,
    ):
        super().__init__(connection, local_endpoint, remote_endpoint)
        self.protocol_name = "quic"
        self.message_based = False
        self.association = association
        self.reader = reader
        self.writer = writer
        self.listener = listener
        self.stream_id = stream_id
        self.can_send = can_send
        self.can_receive = can_receive
        self._initial_data_is_early = initial_data_is_early
        self._reader_task = None
        self._write_lock = asyncio.Lock()
        self._raw_close_lock = asyncio.Lock()
        self._raw_closed = False
        self._peer_event_task = None

    @property
    def raw_closed(self):
        return self._raw_closed

    def apply_connection_properties(self, *, strict_property=None):
        policy = self.connection.transport_properties.get(
            "multipathPolicy"
        )
        multipath = self.connection.transport_properties.get("multipath")
        if multipath == "Disabled":
            effect = "inactive:multipath-disabled"
        elif policy == "Handover":
            effect = "enforced-handover"
        else:
            effect = "unsupported-concurrent-policy"
        self.connection._backend_property_effects["multipathPolicy"] = effect

    def _buffer_received(self, data):
        if self.current_message_context is None:
            self.current_message_context = self._new_message_context(
                end_of_message=False
            )
            self.current_message_context.is_early_data = (
                self._initial_data_is_early
            )
            self._initial_data_is_early = False
        else:
            self.current_message_context.end_of_message = False
        if self.framer_stack is not None:
            self.loop.create_task(
                self._feed_framer(
                    data,
                    self.current_message_context,
                    False,
                )
            )
            return
        if self.recv_buffer is None:
            self.recv_buffer = data
        else:
            self.recv_buffer = self.recv_buffer + data
        self._wake_receive_waiter()

    async def _pump_reader(self):
        try:
            while True:
                data = await self.reader.read(65536)
                if data == b"":
                    self.eof_received()
                    return
                self._buffer_received(data)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            await self._terminate_from_stream_error(exc)

    async def active_open_stream(self):
        if not await self._activate_candidate():
            return False
        self.connection.quic_association = self.association
        if self.can_receive:
            self._reader_task = self.loop.create_task(self._pump_reader())
        if self.local_endpoint and self.remote_endpoint and self.remote_endpoint.address:
            self.connection.note_path_change(
                local_address=self.local_endpoint.address,
                local_port=self.local_endpoint.port,
                remote_address=self.remote_endpoint.address,
                remote_port=self.remote_endpoint.port,
            )
        self.connection._mark_ready()
        return True

    async def passive_open_stream(self):
        self.connection.protocol = self.protocol_name
        self.connection.quic_association = self.association
        self.connection.local_endpoint = self.local_endpoint
        self.connection.remote_endpoint = self.remote_endpoint
        if self.can_receive:
            self._reader_task = self.loop.create_task(self._pump_reader())
        await self._start_framers()
        self._select_transport()
        self._commit_candidate()
        if self.listener is not None:
            if not self.listener._deliver_connection(self.connection):
                await self.close()

    async def write(self, data, message_context, end_of_message, send_call_id=None):
        logger.info("Writing QUIC stream data.")
        try:
            await self.write_before_ready(
                data,
                message_context,
                end_of_message,
            )
        except InterruptedError:
            logger.warning("SendError occurred.")
            self.connection._queue_send_event(
                "send_error",
                message_context,
                InterruptedError("QUIC send interrupted"),
                send_call_id=send_call_id,
            )
            return
        logger.info("QUIC stream data written successfully.")
        self.connection._queue_send_event(
            "sent",
            message_context,
            send_call_id=send_call_id,
        )

    async def write_before_ready(
        self,
        data,
        message_context,
        end_of_message,
    ):
        async with self._write_lock:
            if self._raw_closed:
                raise ConnectionError("The QUIC stream is closed")
            if not self.can_send:
                raise RuntimeError("The QUIC stream is receive-only")
            if isinstance(data, str):
                data = data.encode()
            data = await self._frame_outbound(
                data,
                message_context,
                end_of_message,
            )
            await self._write_raw(data)
            if message_context.final:
                self.writer.write_eof()
                if self.association.protocol is not None:
                    self.association.protocol.transmit()

    async def read(self, min_incomplete_length, max_length):
        if self.framer_stack is not None:
            await self._read_framed_message(
                min_incomplete_length,
                max_length,
            )
            return

        await self._read_stream_buffer(min_incomplete_length, max_length)

    async def _write_raw(self, data):
        if isinstance(data, str):
            data = data.encode()
        await self.association.write_stream_data(self, data)

    def _mark_writer_adapter_closed(self):
        stream_adapter = getattr(
            self.writer,
            "transport",
            getattr(self.writer, "_transport", None),
        )
        if hasattr(stream_adapter, "_closing"):
            stream_adapter._closing = True

    def _signal_stream_shutdown(
        self,
        *,
        abort,
        error_code,
        peer_operation=None,
    ):
        protocol = self.association.protocol
        if (
            protocol is None
            or self.association._terminated
            or self.stream_id is None
        ):
            return
        quic = protocol._quic
        if peer_operation == "reset":
            if self.can_send:
                try:
                    quic.reset_stream(self.stream_id, error_code)
                except (AssertionError, ValueError):
                    pass
        elif peer_operation == "stop-sending":
            if self.can_receive:
                try:
                    quic.stop_stream(self.stream_id, error_code)
                except (AssertionError, ValueError):
                    pass
        else:
            if self.can_send:
                try:
                    if abort:
                        self._mark_writer_adapter_closed()
                        quic.reset_stream(self.stream_id, error_code)
                    elif self.writer is not None:
                        self.writer.close()
                except (AssertionError, ValueError):
                    pass
            else:
                self._mark_writer_adapter_closed()
            if self.can_receive:
                try:
                    quic.stop_stream(self.stream_id, error_code)
                except (AssertionError, ValueError):
                    pass
        protocol.transmit()

    async def _close_raw(
        self,
        *,
        abort=False,
        error_code=0,
        reason="",
        peer_operation=None,
        abort_association_if_unused=False,
    ):
        async with self._raw_close_lock:
            if self._raw_closed:
                return
            self._raw_closed = True
            self._signal_stream_shutdown(
                abort=abort,
                error_code=error_code,
                peer_operation=peer_operation,
            )
            self._mark_writer_adapter_closed()
            await self._cancel_send_tasks()
            if self._reader_task is not None:
                self._reader_task.cancel()
                if self._reader_task is not asyncio.current_task():
                    await asyncio.gather(
                        self._reader_task,
                        return_exceptions=True,
                    )
            self.association.remove_stream(self)
            if (
                self.association._should_close_if_unused()
                and not self.association._terminated
            ):
                if abort_association_if_unused:
                    await self.association.abort_association(
                        reason or "Last QUIC stream aborted"
                    )
                else:
                    await self.association._close_if_unused()

    async def close(self):
        logger.info("Closing QUIC stream.")
        await self._stop_framers()
        await self._close_raw()
        self.connection._report_closed()

    async def abort(self, reason="QUIC stream aborted"):
        await self._close_raw(
            abort=True,
            error_code=QUIC_TAPS_ABORT_ERROR_CODE,
            reason=str(reason),
            abort_association_if_unused=True,
        )

    def peer_stream_event(self, error_code, operation):
        if self._raw_closed or self._peer_event_task is not None:
            return
        self._peer_event_task = self.association.create_background_task(
            self._handle_peer_stream_event(error_code, operation)
        )

    async def _handle_peer_stream_event(self, error_code, operation):
        try:
            await self._stop_framers()
        except Exception:
            logger.exception("Failed to stop Framer after a QUIC stream event")
        await self._close_raw(
            abort=bool(error_code),
            error_code=error_code,
            peer_operation=operation,
        )
        if error_code == 0:
            self.connection._report_closed()
        else:
            self.connection._report_connection_error(
                QuicStreamError(
                    self.stream_id,
                    error_code,
                    operation,
                ),
                suggest_reestablishment=False,
            )

    async def _terminate_from_stream_error(self, error):
        await self._close_raw(
            abort=True,
            error_code=QUIC_TAPS_ABORT_ERROR_CODE,
        )
        self.connection._report_connection_error(
            error,
            suggest_reestablishment=False,
        )

    def association_terminated(self, error):
        if self._raw_closed:
            return
        self._raw_closed = True
        self._mark_writer_adapter_closed()
        if self._reader_task is not None:
            self._reader_task.cancel()
        for task in list(self._send_tasks):
            task.cancel()
        if error is None:
            self.connection._report_closed()
        else:
            self.connection._report_connection_error(
                error,
                suggest_reestablishment=False,
            )


class QuicDatagramTransport(TransportLayer):
    def __init__(
        self,
        connection,
        local_endpoint=None,
        remote_endpoint=None,
        *,
        association,
        listener=None,
    ):
        super().__init__(connection, local_endpoint, remote_endpoint)
        self.protocol_name = "quic"
        self.message_based = True
        self.association = association
        self.listener = listener
        self._raw_close_lock = asyncio.Lock()
        self._raw_closed = False

    @property
    def raw_closed(self):
        return self._raw_closed

    def apply_connection_properties(self, *, strict_property=None):
        policy = self.connection.transport_properties.get(
            "multipathPolicy"
        )
        multipath = self.connection.transport_properties.get("multipath")
        if multipath == "Disabled":
            effect = "inactive:multipath-disabled"
        elif policy == "Handover":
            effect = "enforced-handover"
        else:
            effect = "unsupported-concurrent-policy"
        self.connection._backend_property_effects["multipathPolicy"] = effect

    def message_size_limits(self):
        maximum = self.association.max_datagram_payload
        return {
            "singularTransmissionMsgMaxLen": maximum,
            "sendMsgMaxLen": maximum,
            "recvMsgMaxLen": maximum,
        }

    async def active_open_datagram(self):
        if not await self._activate_candidate():
            return False
        self.connection.quic_association = self.association
        self.connection._mark_ready()
        return True

    async def passive_open_datagram(self):
        self.connection.protocol = self.protocol_name
        self.connection.quic_association = self.association
        self.connection.local_endpoint = self.local_endpoint
        self.connection.remote_endpoint = self.remote_endpoint
        await self._start_framers()
        self._select_transport()
        self._commit_candidate()
        if self.listener is not None:
            if not self.listener._deliver_connection(self.connection):
                await self.close()

    async def write(
        self,
        data,
        message_context,
        end_of_message,
        send_call_id=None,
    ):
        await self.write_before_ready(
            data,
            message_context,
            end_of_message,
        )
        self.connection._queue_send_event(
            "sent",
            message_context,
            send_call_id=send_call_id,
        )

    async def write_before_ready(
        self,
        data,
        message_context,
        end_of_message,
    ):
        if self._raw_closed:
            raise ConnectionError("The QUIC datagram channel is closed")
        if not end_of_message:
            raise ValueError(
                "QUIC DATAGRAM does not support partial Message sends"
            )
        if isinstance(data, str):
            data = data.encode()
        data = await self._frame_outbound(
            data,
            message_context,
            end_of_message,
        )
        await self._write_raw(data)

    async def _write_raw(self, data):
        await self.association.send_datagram(data)

    def datagram_received(self, data, *, is_early_data=False):
        if self._raw_closed:
            return
        self.connection._mark_first_message()
        context = self._new_message_context(end_of_message=True)
        context.reliable = False
        context.ordered = False
        context.is_early_data = is_early_data
        if self.framer_stack is not None:
            self.loop.create_task(
                self._feed_framer(data, context, True)
            )
            return
        if self.recv_buffer is None:
            self.recv_buffer = []
        self.recv_buffer.append((bytes(data), context))
        self._wake_receive_waiter()

    async def read(self, min_incomplete_length, max_length):
        if max_length == -1:
            max_length = float("inf")
        if self.framer_stack is not None:
            await self._read_framed_message(
                min_incomplete_length,
                max_length,
            )
            return
        if self.recv_buffer is None:
            await self.await_data()
        data, context = self.recv_buffer[0]
        was_partial = getattr(
            context,
            "_pytaps_partial_delivery",
            False,
        )
        if max_length != float("inf") and len(data) > max_length:
            delivered = data[:max_length]
            remaining = data[max_length:]
            self.recv_buffer[0] = (remaining, context)
            context.end_of_message = False
            setattr(context, "_pytaps_partial_delivery", True)
            self.connection._deliver_received_partial(delivered, context)
            return
        if len(self.recv_buffer) == 1:
            self.recv_buffer = None
        else:
            self.recv_buffer.pop(0)
        context.end_of_message = True
        if was_partial:
            self.connection._deliver_received_partial(data, context)
        else:
            self.connection._deliver_received(data, context)

    async def _close_raw(
        self,
        *,
        abort=False,
        reason="",
        abort_association_if_unused=False,
    ):
        async with self._raw_close_lock:
            if self._raw_closed:
                return
            self._raw_closed = True
            await self._cancel_send_tasks()
            self.association.remove_datagram(self)
            if (
                self.association._should_close_if_unused()
                and not self.association._terminated
            ):
                if abort and abort_association_if_unused:
                    await self.association.abort_association(
                        reason or "Last QUIC datagram channel aborted"
                    )
                else:
                    await self.association._close_if_unused()

    async def close(self):
        logger.info("Closing QUIC datagram channel.")
        await self._stop_framers()
        await self._close_raw()
        self.connection._report_closed()

    async def abort(self, reason="QUIC datagram channel aborted"):
        await self._close_raw(
            abort=True,
            reason=str(reason),
            abort_association_if_unused=True,
        )

    def association_terminated(self, error):
        if self._raw_closed:
            return
        self._raw_closed = True
        for task in list(self._send_tasks):
            task.cancel()
        if error is None:
            self.connection._report_closed()
        else:
            self.connection._report_connection_error(
                error,
                suggest_reestablishment=False,
            )


class TcpTransport(TransportLayer):
    def __init__(
        self,
        connection,
        local_endpoint=None,
        remote_endpoint=None,
        *,
        protocol_name="tcp",
    ):
        super().__init__(connection, local_endpoint, remote_endpoint)
        self.protocol_name = protocol_name

    async def active_open(self, transport):
        self.transport = transport
        logger.info("Connected successfully on TCP.")
        if not await self._activate_candidate():
            return
        sockname = transport.get_extra_info("sockname")
        if sockname:
            self.connection.note_path_change(
                local_address=sockname[0],
                local_port=sockname[1],
                remote_address=self.remote_endpoint.address,
                remote_port=self.remote_endpoint.port,
            )
        self.connection._mark_ready()
        return

    async def write(self, data, message_context, end_of_message, send_call_id=None):
        """ Send tcp data
        """
        logger.info("Writing TCP data.")
        if isinstance(data, str):
            data = data.encode()
        try:
            data = await self._frame_outbound(
                data,
                message_context,
                end_of_message,
            )
            await self._write_raw(data)
            if message_context.final:
                can_write_eof = getattr(
                    self.transport,
                    "can_write_eof",
                    None,
                )
                if callable(can_write_eof) and can_write_eof():
                    self.transport.write_eof()
        except InterruptedError:
            logger.warning("SendError occurred.")
            self.connection._queue_send_event(
                "send_error",
                message_context,
                InterruptedError("TCP send interrupted"),
                send_call_id=send_call_id,
            )
            return
        logger.info("Data written successfully.")
        self.connection._queue_send_event(
            "sent",
            message_context,
            send_call_id=send_call_id,
        )
        return

    async def read(self, min_incomplete_length, max_length):
        if self.framer_stack is not None:
            await self._read_framed_message(
                min_incomplete_length,
                max_length,
            )
            return

        await self._read_stream_buffer(min_incomplete_length, max_length)

    async def _write_raw(self, data):
        if isinstance(data, str):
            data = data.encode()
        self.transport.write(data)

    async def _close_raw(self):
        if self.transport is not None:
            self.transport.close()

    def apply_connection_properties(self, *, strict_property=None):
        if self.transport is None:
            return
        properties = self.connection.transport_properties
        self._apply_capacity_profile()
        keep_alive = properties.get("keepAlive")
        keep_alive_timeout = properties.get("keepAliveTimeout")
        keep_alive_requested = (
            keep_alive in {PreferenceLevel.REQUIRE, PreferenceLevel.PREFER}
            or keep_alive_timeout != "Disabled"
        )
        if keep_alive_requested:
            keep_alive_applied = self._set_socket_option(
                socket.SOL_SOCKET,
                socket.SO_KEEPALIVE,
                1,
                "keepAlive",
                "applied:SO_KEEPALIVE",
            )
            if (
                strict_property == "keepAliveTimeout"
                and not keep_alive_applied
            ):
                raise NotImplementedError(
                    "keepAliveTimeout is not supported by this TCP socket"
                )
        if keep_alive_timeout != "Disabled":
            keep_idle_option = getattr(
                socket,
                "TCP_KEEPIDLE",
                getattr(socket, "TCP_KEEPALIVE", None),
            )
            if keep_idle_option is None:
                self._record_property_effect(
                    "keepAliveTimeout",
                    "unsupported",
                )
                if strict_property == "keepAliveTimeout":
                    raise NotImplementedError(
                        "keepAliveTimeout is not supported on this platform"
                    )
            else:
                timeout_applied = self._set_socket_option(
                    socket.IPPROTO_TCP,
                    keep_idle_option,
                    max(1, int(keep_alive_timeout)),
                    "keepAliveTimeout",
                    "applied:TCP_KEEPIDLE",
                )
                if (
                    strict_property == "keepAliveTimeout"
                    and not timeout_applied
                ):
                    raise NotImplementedError(
                        "keepAliveTimeout is not supported by this TCP socket"
                    )

        user_timeout = None
        user_timeout_source = None
        conn_timeout = properties.get("connTimeout")
        if conn_timeout != "Disabled":
            user_timeout = conn_timeout
            user_timeout_source = "connTimeout"
        if user_timeout is not None or strict_property == "connTimeout":
            user_timeout_option = getattr(socket, "TCP_USER_TIMEOUT", None)
            if user_timeout_option is None:
                self._record_property_effect(
                    user_timeout_source or "connTimeout",
                    "unsupported",
                )
                if strict_property == "connTimeout":
                    raise NotImplementedError(
                        "connTimeout is not supported on this platform"
                    )
            else:
                timeout_value = (
                    max(1, int(float(user_timeout) * 1000))
                    if user_timeout is not None
                    else 0
                )
                timeout_applied = self._set_socket_option(
                    socket.IPPROTO_TCP,
                    user_timeout_option,
                    timeout_value,
                    user_timeout_source or "connTimeout",
                    (
                        "applied:TCP_USER_TIMEOUT"
                        if user_timeout is not None
                        else "disabled:TCP_USER_TIMEOUT"
                    ),
                )
                if (
                    strict_property == "connTimeout"
                    and not timeout_applied
                ):
                    raise NotImplementedError(
                        "connTimeout is not supported by this TCP socket"
                    )
        for property_name in {
            "tcp.userTimeoutValue",
            "tcp.userTimeoutEnabled",
            "tcp.userTimeoutChangeable",
        }:
            self._record_property_effect(
                property_name,
                "unsupported:RFC5482",
            )

    async def close(self):
        logger.info("Closing connection.")
        await self._stop_framers()
        await self._close_raw()
        await self._wait_for_raw_close()
        self.connection._report_closed()

    # Asyncio Callbacks

    """ ASYNCIO function that gets called when a new
        connection has been made, similar to TAPS ready callback.
    """

    def connection_made(self, transport):
        if self.connection.state == ConnectionState.ESTABLISHED:
            transport.close()
            self._open_task = self.loop.create_task(asyncio.sleep(0))
            return

        # Check if its an incoming or outgoing connection
        if self.connection.active:
            if (
                self.protocol_name == "tls-tcp"
                and self.connection.security_parameters is not None
                and self.connection.security_parameters.pinned_server_certificates
            ):
                try:
                    ssl_object = transport.get_extra_info("ssl_object")
                    if ssl_object is None:
                        raise ssl.SSLCertVerificationError(
                            "TLS peer certificate is unavailable"
                        )
                    peer_chain = _presented_tls_certificate_chain(ssl_object)
                    self.connection.security_parameters. \
                        verify_pinned_server_certificates(peer_chain)
                except (TypeError, ValueError, ssl.SSLError) as exc:
                    transport.close()
                    self._set_open_error(exc)
                    return
            self._open_task = self.loop.create_task(self.active_open(transport))
        else:
            self._open_task = self.loop.create_task(self.passive_open(transport))

            # Stub code for forcefully killing connection tasks
            # Before establishment to the peer has been completed
            """
            for t in self.connection.transports:
                if t != self:
                    self.connection.transports.remove(t)

            for t in self.connection.pending:
                if t != asyncio.current_task() and not t.done():
                    print(t)
                    print(asyncio.current_task())
                    t.cancel() """

    """ ASYNCIO function that gets called when new data is made available
        by the OS. Stores new data in buffer and triggers the receive waiter
    """

    def data_received(self, data):
        logger.info("Received %d bytes" % len(data))
        if self.current_message_context is None:
            self.current_message_context = self._new_message_context(
                end_of_message=False
            )
        else:
            self.current_message_context.end_of_message = False

        if self.framer_stack is not None:
            self.loop.create_task(
                self._feed_framer(
                    data,
                    self.current_message_context,
                    False,
                )
            )
            return
        # See if we already have so data buffered
        if self.recv_buffer is None:
            self.recv_buffer = data
        else:
            self.recv_buffer = self.recv_buffer + data
        self._wake_receive_waiter()
