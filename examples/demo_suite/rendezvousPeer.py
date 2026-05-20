import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytaps as taps  # noqa: E402


logger = taps.setup_logger("TAPS Rendezvous Peer", "magenta")


def build_local_endpoint(args):
    local = taps.LocalEndpoint()
    if args.local_address:
        local.with_address(args.local_address)
    elif args.local_host:
        local.with_hostname(args.local_host)
    else:
        local.with_address("0.0.0.0")
    local.with_port(args.local_port)
    return local


def build_remote_endpoint(args):
    remote = taps.RemoteEndpoint()
    if args.remote_address:
        remote.with_address(args.remote_address)
    else:
        remote.with_hostname(args.remote_host)
    remote.with_port(args.remote_port)
    return remote


class RendezvousPeer:
    def __init__(self):
        self.received_connections = []

    async def handle_monitoring_update(self, update):
        logger.info(
            "monitor trigger=%s health=%s",
            update["trigger"],
            update["snapshot"]["healthSummary"],
        )

    async def handle_rendezvous_done(self, result, connection):
        logger.info("rendezvous done result=%s protocol=%s", result.get_properties(), connection.protocol)

    async def handle_connection_received(self, connection):
        self.received_connections.append(connection)
        logger.info("passive rendezvous connection protocol=%s", connection.protocol)
        connection.on_received(self.handle_received)
        await connection.receive(min_incomplete_length=1, max_length=4096)

    async def handle_received(self, data, context, connection):
        logger.info("received on passive side: %s", data)

    async def main(self, args):
        local = build_local_endpoint(args)
        remote = build_remote_endpoint(args)
        properties = taps.TransportProperties()
        properties.require("reliability")
        properties.ignore("preserveMsgBoundaries")
        properties.ignore("congestionControl")
        properties.ignore("preserveOrder")
        properties.set_property("direction", "Bidirectional")

        preconnection = taps.Preconnection(
            local_endpoint=local,
            remote_endpoint=remote,
            transport_properties=properties,
        )
        preconnection.subscribe_monitoring(self.handle_monitoring_update)
        preconnection.on_rendezvous_done(self.handle_rendezvous_done)
        preconnection.on_connection_received(self.handle_connection_received)

        result = await preconnection.rendezvous(timeout=args.timeout)
        logger.info("rendezvous result=%s", result.get_properties())

        context = result.connection.new_message_context(final=False)
        await result.connection.send(args.payload.encode("utf-8"), context)
        await asyncio.sleep(args.settle_time)
        logger.info("connection monitoring=%s", result.connection.get_monitoring_snapshot())

        if args.close:
            await result.close()
        else:
            await asyncio.Event().wait()


def parse_args():
    parser = argparse.ArgumentParser(
        description="PyTAPS rendezvous demo peer. Run once on each host with swapped addresses."
    )
    parser.add_argument("--local-address", default=None)
    parser.add_argument("--local-host", default=None)
    parser.add_argument("--local-port", type=int, default=7788)
    parser.add_argument("--remote-address", default=None)
    parser.add_argument("--remote-host", default="localhost")
    parser.add_argument("--remote-port", type=int, default=7788)
    parser.add_argument("--payload", default="hello rendezvous")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--settle-time", type=float, default=1.0)
    parser.add_argument("--close", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(RendezvousPeer().main(parse_args()))
