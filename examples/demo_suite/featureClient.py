import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytaps as taps  # noqa: E402


logger = taps.setup_logger("TAPS Feature Client", "yellow")


def payload_preview(data, limit=80):
    if isinstance(data, str):
        data = data.encode()
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


def build_remote_endpoint(args):
    remote = taps.RemoteEndpoint()
    if args.remote_address:
        remote.with_address(args.remote_address)
    else:
        remote.with_hostname(args.remote_host)
    remote.with_port(args.port)
    if args.transport in {"tcp", "udp"}:
        remote.with_protocol(args.transport)
    return remote


def build_local_endpoint(args):
    if not args.local_address and not args.interface and args.local_port is None:
        return None
    local = taps.LocalEndpoint()
    if args.local_address:
        local.with_address(args.local_address)
    if args.interface:
        local.with_interface(args.interface)
    if args.local_port is not None:
        local.with_port(args.local_port)
    return local


def build_transport_properties(args):
    if args.transport == "udp":
        properties = taps.TransportProperties().unreliable_datagram()
    else:
        properties = taps.TransportProperties().reliable_inorder_stream()
        properties.prefer("multistreaming")
        properties.prefer("zeroRttMsg")
    properties.set_property("connPriority", args.priority)
    return properties


class FeatureClient:
    def __init__(self, *, verbose_monitoring=False, dump_properties=False):
        self.connection = None
        self.received = []
        self.verbose_monitoring = verbose_monitoring
        self.dump_properties = dump_properties

    def is_connection_open(self):
        return self.connection and self.connection.state is taps.ConnectionState.ESTABLISHED

    async def handle_monitoring_update(self, update):
        if not self.verbose_monitoring:
            return
        health = update["snapshot"]["healthSummary"]
        logger.info(
            "monitor trigger=%s severity=%s guidance=%s",
            update["trigger"],
            health["severity"],
            update["snapshot"]["operationalGuidance"],
        )

    async def handle_sent(self, context, connection):
        if self.dump_properties:
            logger.info("sent message_id=%s props=%s", context.message_id, context.get_properties())
        else:
            logger.info("sent message_id=%s", context.message_id)

    async def handle_send_error(self, context, reason, connection):
        logger.warning("send error message_id=%s reason=%s", context.message_id, reason)

    async def handle_expired(self, context, connection):
        logger.info("expired message_id=%s lifetime=%s", context.message_id, context.lifetime)

    async def handle_received(self, data, context, connection):
        if self.dump_properties:
            logger.info(
                "received echo bytes=%s seq=%s payload=%r props=%s",
                len(data),
                context.receive_sequence,
                payload_preview(data),
                context.get_properties(),
            )
        else:
            logger.info(
                "received echo bytes=%s seq=%s payload=%r",
                len(data),
                context.receive_sequence,
                payload_preview(data),
            )
        self.received.append(data)

    async def handle_received_partial(self, data, context, end_of_message, connection):
        if self.dump_properties:
            logger.info(
                "received partial echo bytes=%s seq=%s eom=%s payload=%r props=%s",
                len(data),
                context.receive_sequence,
                end_of_message,
                payload_preview(data),
                context.get_properties(),
            )
        else:
            logger.info(
                "received partial echo bytes=%s seq=%s eom=%s payload=%r",
                len(data),
                context.receive_sequence,
                end_of_message,
                payload_preview(data),
            )
        self.received.append(data)

    async def handle_connection_error(self, reason, connection):
        logger.warning("connection error protocol=%s reason=%s", connection.protocol, reason)

    async def handle_reestablishment_suggested(self, advice, candidates, connection):
        recommended = advice.get("recommendedCandidate") if advice else None
        logger.info(
            "reestablishment trigger=%s recommended=%s candidate_count=%s",
            advice.get("trigger") if advice else None,
            recommended,
            len(candidates),
        )

    async def main(self, args):
        configure_logging(args)
        remote = build_remote_endpoint(args)
        local = build_local_endpoint(args)
        properties = build_transport_properties(args)
        preconnection = taps.Preconnection(
            local_endpoints=[local] if local is not None else [],
            remote_endpoints=[remote],
            transport_properties=properties,
        )
        preconnection.subscribe_monitoring(self.handle_monitoring_update)
        preconnection.set_address_family_policy(args.prefer_family, preference_adjustment=2)
        if args.avoid_protocol:
            preconnection.set_protocol_policy(
                args.avoid_protocol,
                available=True,
                preference_adjustment=-4,
                racing_cooldown=10,
            )
        if args.alternate_remote:
            preconnection.note_alternate_remote(
                args.remote_address or args.remote_host,
                args.alternate_remote,
                protocol="quic",
            )

        first_context = taps.MessageContext(
            priority=10,
            safely_replayable=True,
            lifetime=args.lifetime,
            final=False,
        )
        self.connection = await preconnection.initiate_with_send(
            args.payload,
            first_context,
        )
        self.connection.on_sent(self.handle_sent)
        self.connection.on_send_error(self.handle_send_error)
        self.connection.on_expired(self.handle_expired)
        self.connection.on_received(self.handle_received)
        self.connection.on_received_partial(self.handle_received_partial)
        self.connection.on_connection_error(self.handle_connection_error)
        self.connection.on_reestablishment_suggested(self.handle_reestablishment_suggested)
        self.connection.subscribe_monitoring(self.handle_monitoring_update)
        try:
            await self.connection.wait_ready(timeout=args.timeout)
        except TimeoutError:
            logger.warning(
                "timed out establishing a connection to %s:%s; "
                "check that featureServer.py is running on the Linode, "
                "that TCP port %s is open, and that the Linode has the updated demo files",
                args.remote_address or args.remote_host,
                args.port,
                args.port,
            )
            if self.dump_properties:
                logger.info("monitoring snapshot=%s", self.connection.get_monitoring_snapshot())
            return

        if self.dump_properties:
            logger.info(
                "ready protocol=%s read_only=%s",
                self.connection.protocol,
                self.connection.get_properties()["readOnly"],
            )
        else:
            logger.info("ready protocol=%s", self.connection.protocol)

        try:
            await self.connection.receive(
                min_incomplete_length=1,
                max_length=4096,
                timeout=args.timeout,
            )
        except TimeoutError:
            logger.warning(
                "timed out waiting for echo on protocol=%s; check server logs and firewall rules",
                self.connection.protocol,
            )
        if not self.is_connection_open():
            logger.warning("connection closed before follow-up demo sends")
            if self.dump_properties:
                logger.info("monitoring snapshot=%s", self.connection.get_monitoring_snapshot())
            return

        batch = []
        for idx in range(args.batch_size):
            payload = f"{args.payload}-{idx}".encode()
            context = self.connection.new_message_context(
                msgPriority=idx,
                safelyReplayable=self.connection.protocol == "udp",
                final=False,
            )
            logger.info("queue batch idx=%s payload=%r", idx, payload_preview(payload))
            batch.append((payload, context, True))
        await self.connection.send_batch(batch)

        expired = self.connection.new_message_context(
            msgLifetime=0.001,
            safelyReplayable=self.connection.protocol == "udp",
            final=False,
        )
        await asyncio.sleep(0.002)
        await self.connection.send(b"this message should expire", expired)

        if args.degrade_path:
            self.connection.note_soft_error(
                "demo path degradation",
                penalty=4,
                lifetime=120,
            )

        await asyncio.sleep(args.settle_time)
        snapshot = self.connection.get_monitoring_snapshot()
        if self.dump_properties:
            logger.info("monitoring snapshot=%s", snapshot)
        else:
            logger.info(
                "summary health=%s events=%s",
                snapshot["connectionContext"]["healthSummary"]["severity"],
                snapshot["connectionContext"]["eventCounters"],
            )
        if self.connection.state is not taps.ConnectionState.CLOSED:
            self.connection.close()
            await self.connection.wait_closed(timeout=args.timeout)


def parse_args():
    parser = argparse.ArgumentParser(
        description="PyTAPS feature demo client for racing, messages, and monitoring."
    )
    parser.add_argument("--remote-host", default="localhost")
    parser.add_argument("--remote-address", default=None)
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument("--local-address", default=None)
    parser.add_argument("--local-port", type=int, default=None)
    parser.add_argument("--interface", default=None)
    parser.add_argument("--payload", default="hello taps")
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--priority", type=int, default=50)
    parser.add_argument("--lifetime", type=float, default=None)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--settle-time", type=float, default=0.25)
    parser.add_argument("--prefer-family", choices=["ipv4", "ipv6"], default="ipv6")
    parser.add_argument("--avoid-protocol", default=None)
    parser.add_argument("--alternate-remote", default=None)
    parser.add_argument("--degrade-path", action="store_true")
    parser.add_argument("--verbose-monitoring", action="store_true")
    parser.add_argument("--dump-properties", action="store_true")
    parser.add_argument(
        "--quiet-library-logs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Hide lower-level pytaps INFO logs by default.",
    )
    parser.add_argument(
        "--transport",
        choices=["auto", "tcp", "udp"],
        default="auto",
    )
    return parser.parse_args()


if __name__ == "__main__":
    parsed_args = parse_args()
    asyncio.run(
        FeatureClient(
            verbose_monitoring=parsed_args.verbose_monitoring,
            dump_properties=parsed_args.dump_properties,
        ).main(parsed_args)
    )
