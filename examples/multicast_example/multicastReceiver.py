import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytaps as taps  # noqa: E402


logger = taps.setup_logger("Multicast Receiver", "yellow")


class MulticastReceiver:
    def __init__(self):
        self.connection = None
        self.listener = None

    async def handle_connection_received(self, connection):
        logger.info("Multicast flow ready.")
        self.connection = connection
        connection.on_received(self.handle_received)
        await connection.receive(min_incomplete_length=1)

    async def handle_received(self, data, context, connection):
        logger.info(
            "Received multicast packet from %s:%s to local %s:%s: %r",
            context.remote_address,
            context.remote_port,
            context.local_address,
            context.local_port,
            data,
        )
        await connection.receive(min_incomplete_length=1)

    async def main(self, args):
        context = taps.ConnectionContext()
        policy_monitor = None
        local = taps.LocalEndpoint()
        local.with_single_source_multicast_group_ip(args.group, args.source)
        local.with_port(args.port)
        if args.interface:
            local.with_interface(args.interface)

        props = taps.TransportProperties().unreliable_datagram()
        props.set_property("direction", "Unidirectional Receive")
        props.prohibit("reliability")

        preconnection = taps.Preconnection(
            local_endpoints=[local],
            remote_endpoints=[],
            transport_properties=props,
            connection_context=context,
        )
        if args.interface_address:
            preconnection.multicast_interface_address = args.interface_address
        preconnection.on_connection_received(self.handle_connection_received)

        if args.interface:
            policy_monitor = taps.SystemPolicyMonitor(
                context,
                interval=args.policy_interval,
            )
            await policy_monitor.refresh()
            policy_monitor.start()
        try:
            self.listener = await preconnection.listen()
            await self.listener.wait_listening()
            logger.info(
                "Listening for multicast from source %s to group %s:%s.",
                args.source,
                args.group,
                args.port,
            )
            await asyncio.Event().wait()
        finally:
            if self.listener is not None:
                await self.listener.stop()
            if policy_monitor is not None:
                await policy_monitor.stop()


def parse_args():
    parser = argparse.ArgumentParser(description="PyTAPS multicast receiver example.")
    parser.add_argument(
        "--group",
        default="232.1.1.1",
        help="Multicast group address to join.",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Source IP address to accept packets from.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5001,
        help="UDP destination port for the multicast subscription.",
    )
    parser.add_argument(
        "--interface",
        default=None,
        help="Local interface name to constrain the multicast Endpoint.",
    )
    parser.add_argument(
        "--interface-address",
        default=None,
        help="Fixed local unicast address to use for the subscription.",
    )
    parser.add_argument(
        "--policy-interval",
        type=float,
        default=2.0,
        help="System Policy refresh interval when --interface is used.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(MulticastReceiver().main(parse_args()))
    except KeyboardInterrupt:
        logger.info("receiver stopped")
