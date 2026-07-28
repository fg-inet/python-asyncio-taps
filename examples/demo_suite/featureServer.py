import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytaps as taps  # noqa: E402


logger = taps.setup_logger("TAPS Feature Server", "cyan")


def payload_preview(data, limit=80):
    preview = data[:limit].decode("utf-8", errors="replace")
    if len(data) > limit:
        preview = f"{preview}..."
    return preview


def configure_logging(args):
    if args.quiet_library_logs:
        logging.getLogger("pytaps.connection").setLevel(logging.ERROR)
        logging.getLogger("pytaps.listener").setLevel(logging.ERROR)
        logging.getLogger("pytaps.preconnection").setLevel(logging.ERROR)
        logging.getLogger("pytaps.transports").setLevel(logging.ERROR)


def build_local_endpoint(args):
    local = taps.LocalEndpoint()
    if args.interface:
        local.with_interface(args.interface)
    if args.local_address:
        local.with_address(args.local_address)
    if args.local_host:
        local.with_hostname(args.local_host)
    if not args.interface and not args.local_address and not args.local_host:
        local.with_address("0.0.0.0")
    local.with_port(args.port)
    if args.transport in {"tcp", "udp"}:
        local.with_protocol(args.transport)
    return local


def build_transport_properties(args):
    if args.transport == "udp":
        properties = taps.TransportProperties().unreliable_datagram()
    else:
        properties = taps.TransportProperties().reliable_inorder_stream()
        if args.transport == "auto":
            properties.prefer("multistreaming")
    properties.set_property("direction", "Bidirectional")
    return properties


class FeatureServer:
    def __init__(self, *, verbose_monitoring=False, dump_properties=False):
        self.listener = None
        self.connections = set()
        self.verbose_monitoring = verbose_monitoring
        self.dump_properties = dump_properties

    async def handle_monitoring_update(self, update):
        if not self.verbose_monitoring:
            return
        summary = update["snapshot"]["healthSummary"]
        logger.info(
            "monitor trigger=%s severity=%s active_conns=%s active_listeners=%s",
            update["trigger"],
            summary["severity"],
            summary["activeConnectionCount"],
            summary["activeListenerCount"],
        )

    async def handle_connection_received(self, connection):
        self.connections.add(connection)
        logger.info("accepted protocol=%s remote=%s", connection.protocol, connection.remote_endpoint)
        connection.subscribe_monitoring(self.handle_monitoring_update)
        connection.on_received(self.handle_received)
        connection.on_received_partial(self.handle_received_partial)
        connection.on_receive_error(self.handle_receive_error)
        connection.on_connection_error(self.handle_connection_error)
        connection.on_closed(self.handle_closed)
        await self.arm_receive(connection)

    async def arm_receive(self, connection):
        try:
            await connection.receive(min_incomplete_length=1, max_length=4096)
        except ConnectionError:
            if connection.state is taps.ConnectionState.CLOSED:
                return
            logger.exception(
                "receive loop failed for protocol=%s",
                connection.protocol,
            )
        except Exception:
            logger.exception("failed to arm receive loop for protocol=%s", connection.protocol)

    async def handle_received(self, data, context, connection):
        if self.dump_properties:
            logger.info(
                "received message bytes=%s seq=%s payload=%r props=%s",
                len(data),
                context.receive_sequence,
                payload_preview(data),
                context.get_properties(),
            )
        else:
            logger.info(
                "received message bytes=%s seq=%s payload=%r",
                len(data),
                context.receive_sequence,
                payload_preview(data),
            )
        reply = connection.new_message_context(
            safelyReplayable=connection.protocol == "udp",
            final=context.final,
        )
        try:
            await connection.send(data, reply)
            if not context.final and connection.state is taps.ConnectionState.ESTABLISHED:
                await self.arm_receive(connection)
        except Exception:
            logger.exception("failed while echoing full message on %s", connection.protocol)

    async def handle_received_partial(self, data, context, end_of_message, connection):
        logger.info(
            "received partial bytes=%s seq=%s eom=%s payload=%r",
            len(data),
            context.receive_sequence,
            end_of_message,
            payload_preview(data),
        )
        reply = connection.new_message_context(
            safelyReplayable=connection.protocol == "udp",
            final=False,
        )
        try:
            await connection.send(data, reply, end_of_message=end_of_message)
            if connection.state is taps.ConnectionState.ESTABLISHED:
                await self.arm_receive(connection)
        except Exception:
            logger.exception("failed while echoing partial message on %s", connection.protocol)

    async def handle_receive_error(self, context, reason, connection):
        if connection.state is taps.ConnectionState.CLOSED:
            logger.info("receive loop ended protocol=%s", connection.protocol)
            return
        logger.warning("receive error on %s: %s", connection.protocol, reason)

    async def handle_connection_error(self, reason, connection):
        logger.warning("connection error on %s: %s", connection.protocol, reason)
        if self.dump_properties:
            logger.info("connection snapshot=%s", connection.get_monitoring_snapshot())
        self.connections.discard(connection)

    async def handle_closed(self, connection):
        logger.info("connection closed protocol=%s", connection.protocol)
        self.connections.discard(connection)

    async def main(self, args):
        configure_logging(args)
        local = build_local_endpoint(args)
        properties = build_transport_properties(args)
        preconnection = taps.Preconnection(
            local_endpoints=[local],
            transport_properties=properties,
        )
        preconnection.subscribe_monitoring(self.handle_monitoring_update)
        preconnection.on_connection_received(self.handle_connection_received)

        self.listener = await preconnection.listen(timeout=args.timeout)
        await self.listener.wait_listening(timeout=args.timeout)
        if self.dump_properties:
            snapshot = self.listener.get_monitoring_snapshot()["connectionContext"]
            logger.info(
                "listening port=%s transport=%s health=%s",
                args.port,
                args.transport,
                snapshot["healthSummary"],
            )
        else:
            logger.info("listening port=%s transport=%s", args.port, args.transport)
        await asyncio.Event().wait()


def parse_args():
    parser = argparse.ArgumentParser(
        description="PyTAPS feature demo server with monitoring output."
    )
    parser.add_argument("--local-address", default=None)
    parser.add_argument("--local-host", default=None)
    parser.add_argument("--interface", default=None)
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument(
        "--transport",
        choices=["auto", "tcp", "udp"],
        default="auto",
        help="Transport preference profile to expose.",
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--verbose-monitoring", action="store_true")
    parser.add_argument("--dump-properties", action="store_true")
    parser.add_argument(
        "--quiet-library-logs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Hide lower-level pytaps INFO logs by default.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    parsed_args = parse_args()
    try:
        asyncio.run(
            FeatureServer(
                verbose_monitoring=parsed_args.verbose_monitoring,
                dump_properties=parsed_args.dump_properties,
            ).main(parsed_args)
        )
    except KeyboardInterrupt:
        logger.info("server stopped")
