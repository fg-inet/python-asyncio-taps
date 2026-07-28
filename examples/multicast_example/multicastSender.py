import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytaps as taps  # noqa: E402


logger = taps.setup_logger("Multicast Sender", "yellow")


class MulticastSender:
    def __init__(self):
        self.connection = None

    async def handle_ready(self, connection):
        logger.info(
            "Multicast sender ready for %s:%s.",
            connection.remote_endpoint.multicast_group,
            connection.remote_endpoint.port,
        )

    async def main(self, args):
        local = None
        if args.source:
            local = taps.LocalEndpoint()
            local.with_address(args.source)
            if args.source_port is not None:
                local.with_port(args.source_port)

        remote = taps.RemoteEndpoint()
        remote.with_multicast_group_ip(args.group)
        remote.with_port(args.port)
        remote.with_hop_limit(args.ttl)

        props = taps.TransportProperties().unreliable_datagram()
        props.prohibit("reliability")
        props.set_property("direction", "Unidirectional Send")

        preconnection = taps.Preconnection(
            local_endpoints=[local] if local is not None else [],
            remote_endpoints=[remote],
            transport_properties=props,
        )
        preconnection.multicast_interface_address = args.interface_address
        preconnection.multicast_disable_loopback = args.disable_loopback
        preconnection.on_ready(self.handle_ready)

        self.connection = await preconnection.initiate()
        await self.connection.wait_ready()

        payload = args.payload.encode("utf-8")
        for idx in range(args.count):
            context = self.connection.new_message_context(
                safelyReplayable=True,
                final=False,
            )
            await self.connection.send(payload, context)
            logger.info(
                "Sent multicast packet %s/%s to %s:%s.",
                idx + 1,
                args.count,
                args.group,
                args.port,
            )
            if idx + 1 < args.count:
                await asyncio.sleep(args.interval_ms / 1000.0)

        self.connection.close()
        await self.connection.wait_closed()


def parse_args():
    parser = argparse.ArgumentParser(description="PyTAPS multicast sender example.")
    parser.add_argument(
        "--group",
        default="232.1.1.1",
        help="Multicast group address to send to.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5001,
        help="UDP destination port for the multicast publication.",
    )
    parser.add_argument(
        "--payload",
        default="hello multicast",
        help="UTF-8 payload to send.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="Number of packets to send.",
    )
    parser.add_argument(
        "--interval-ms",
        type=int,
        default=1000,
        help="Delay between sends in milliseconds.",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Source IP address to bind for the publication.",
    )
    parser.add_argument(
        "--source-port",
        type=int,
        default=None,
        help="Optional source UDP port for the publication.",
    )
    parser.add_argument(
        "--interface-address",
        default=None,
        help="Outgoing interface address to use for the publication.",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=1,
        help="TTL / hop limit for the multicast publication.",
    )
    parser.add_argument(
        "--disable-loopback",
        action="store_true",
        help="Disable multicast loopback on the sender socket.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(MulticastSender().main(parse_args()))
