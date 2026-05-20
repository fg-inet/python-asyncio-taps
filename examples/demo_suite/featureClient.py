import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytaps as taps  # noqa: E402


logger = taps.setup_logger("TAPS Feature Client", "yellow")


def build_remote_endpoint(args):
    remote = taps.RemoteEndpoint()
    if args.remote_address:
        remote.with_address(args.remote_address)
    else:
        remote.with_hostname(args.remote_host)
    remote.with_port(args.port)
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
    properties = taps.TransportProperties()
    properties.ignore("congestionControl")
    properties.ignore("preserveOrder")
    properties.prefer("multistreaming")
    properties.prefer("zeroRttMsg")
    properties.set_property("connPriority", args.priority)
    if args.transport == "udp":
        properties.prohibit("reliability")
        properties.require("preserveMsgBoundaries")
    elif args.transport == "tcp":
        properties.require("reliability")
        properties.ignore("preserveMsgBoundaries")
    else:
        properties.ignore("reliability")
    return properties


class FeatureClient:
    def __init__(self):
        self.connection = None
        self.received = []

    async def handle_monitoring_update(self, update):
        health = update["snapshot"]["healthSummary"]
        logger.info(
            "monitor trigger=%s severity=%s guidance=%s",
            update["trigger"],
            health["severity"],
            update["snapshot"]["operationalGuidance"],
        )

    async def handle_sent(self, context, connection):
        logger.info("sent message_id=%s props=%s", context.message_id, context.get_properties())

    async def handle_send_error(self, context, reason, connection):
        logger.warning("send error message_id=%s reason=%s", context.message_id, reason)

    async def handle_expired(self, context, connection):
        logger.info("expired message_id=%s lifetime=%s", context.message_id, context.lifetime)

    async def handle_received(self, data, context, connection):
        logger.info(
            "received echo bytes=%s seq=%s props=%s",
            len(data),
            context.receive_sequence,
            context.get_properties(),
        )
        self.received.append(data)

    async def handle_reestablishment_suggested(self, advice, candidates, connection):
        logger.info("reestablishment advice=%s candidate_count=%s", advice, len(candidates))

    async def main(self, args):
        remote = build_remote_endpoint(args)
        local = build_local_endpoint(args)
        properties = build_transport_properties(args)
        preconnection = taps.Preconnection(
            local_endpoint=local,
            remote_endpoint=remote,
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
        await self.connection.wait_ready(timeout=args.timeout)
        self.connection.on_sent(self.handle_sent)
        self.connection.on_send_error(self.handle_send_error)
        self.connection.on_expired(self.handle_expired)
        self.connection.on_received(self.handle_received)
        self.connection.on_reestablishment_suggested(self.handle_reestablishment_suggested)
        self.connection.subscribe_monitoring(self.handle_monitoring_update)

        logger.info(
            "ready protocol=%s read_only=%s",
            self.connection.protocol,
            self.connection.get_properties()["readOnly"],
        )

        await self.connection.receive(min_incomplete_length=1, max_length=4096, timeout=args.timeout)

        batch = []
        for idx in range(args.batch_size):
            context = self.connection.new_message_context(
                msgPriority=idx,
                safelyReplayable=self.connection.protocol == "udp",
                final=False,
            )
            batch.append((f"{args.payload}-{idx}".encode(), context, True))
        await self.connection.send_batch(batch)

        expired = self.connection.new_message_context(
            msgLifetime=0,
            safelyReplayable=self.connection.protocol == "udp",
            final=False,
        )
        await self.connection.send(b"this message should expire", expired)

        if args.degrade_path:
            self.connection.note_soft_error(
                "demo path degradation",
                penalty=4,
                lifetime=120,
            )

        await asyncio.sleep(args.settle_time)
        snapshot = self.connection.get_monitoring_snapshot()
        logger.info("monitoring snapshot=%s", snapshot)
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
    parser.add_argument(
        "--transport",
        choices=["auto", "tcp", "udp"],
        default="auto",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(FeatureClient().main(parse_args()))
