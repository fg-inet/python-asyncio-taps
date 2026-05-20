import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytaps as taps  # noqa: E402


logger = taps.setup_logger("TAPS Feature Server", "cyan")


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
    return local


def build_transport_properties(args):
    properties = taps.TransportProperties()
    properties.ignore("congestionControl")
    properties.ignore("preserveOrder")
    properties.set_property("direction", "Bidirectional")
    if args.transport == "udp":
        properties.prohibit("reliability")
        properties.require("preserveMsgBoundaries")
    elif args.transport == "tcp":
        properties.require("reliability")
        properties.ignore("preserveMsgBoundaries")
    else:
        properties.require("reliability")
        properties.ignore("preserveMsgBoundaries")
        properties.prefer("multistreaming")
    return properties


class FeatureServer:
    def __init__(self):
        self.listener = None
        self.connections = set()

    async def handle_monitoring_update(self, update):
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
        logger.info(
            "accepted protocol=%s local=%s remote=%s",
            connection.protocol,
            connection.local_endpoint,
            connection.remote_endpoint,
        )
        connection.subscribe_monitoring(self.handle_monitoring_update)
        connection.on_received(self.handle_received)
        connection.on_received_partial(self.handle_received_partial)
        connection.on_receive_error(self.handle_receive_error)
        connection.on_connection_error(self.handle_connection_error)
        connection.on_closed(self.handle_closed)
        try:
            await connection.receive(min_incomplete_length=1, max_length=4096)
        except Exception:
            logger.exception("failed to arm receive loop for protocol=%s", connection.protocol)

    async def handle_received(self, data, context, connection):
        logger.info(
            "received message bytes=%s seq=%s props=%s",
            len(data),
            context.receive_sequence,
            context.get_properties(),
        )
        reply = connection.new_message_context(
            safelyReplayable=connection.protocol == "udp",
            final=context.final,
        )
        try:
            await connection.send(data, reply)
            if not context.final and connection.state is taps.ConnectionState.ESTABLISHED:
                await connection.receive(min_incomplete_length=1, max_length=4096)
        except Exception:
            logger.exception("failed while echoing full message on %s", connection.protocol)

    async def handle_received_partial(self, data, context, end_of_message, connection):
        logger.info(
            "received partial bytes=%s seq=%s eom=%s",
            len(data),
            context.receive_sequence,
            end_of_message,
        )
        reply = connection.new_message_context(
            safelyReplayable=connection.protocol == "udp",
            final=False,
        )
        try:
            await connection.send(data, reply, end_of_message=end_of_message)
            if connection.state is taps.ConnectionState.ESTABLISHED:
                await connection.receive(min_incomplete_length=1, max_length=4096)
        except Exception:
            logger.exception("failed while echoing partial message on %s", connection.protocol)

    async def handle_receive_error(self, context, reason, connection):
        logger.warning("receive error on %s: %s", connection.protocol, reason)

    async def handle_connection_error(self, reason, connection):
        logger.warning("connection error on %s: %s", connection.protocol, reason)
        logger.info("connection snapshot=%s", connection.get_monitoring_snapshot())
        self.connections.discard(connection)

    async def handle_closed(self, connection):
        logger.info("connection closed protocol=%s", connection.protocol)
        self.connections.discard(connection)

    async def main(self, args):
        local = build_local_endpoint(args)
        properties = build_transport_properties(args)
        preconnection = taps.Preconnection(
            local_endpoint=local,
            transport_properties=properties,
        )
        preconnection.subscribe_monitoring(self.handle_monitoring_update)
        preconnection.on_connection_received(self.handle_connection_received)

        self.listener = await preconnection.listen(timeout=args.timeout)
        await self.listener.wait_listening(timeout=args.timeout)
        snapshot = self.listener.get_monitoring_snapshot()["connectionContext"]
        logger.info(
            "listening port=%s transport=%s health=%s",
            args.port,
            args.transport,
            snapshot["healthSummary"],
        )
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
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(FeatureServer().main(parse_args()))
