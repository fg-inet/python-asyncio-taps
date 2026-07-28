import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytaps as taps  # noqa: E402


logger = taps.setup_logger("TAPS Rendezvous Peer", "magenta")


def configure_logging(args):
    if args.quiet_library_logs:
        logging.getLogger("pytaps.connection").setLevel(logging.ERROR)
        logging.getLogger("pytaps.listener").setLevel(logging.ERROR)
        logging.getLogger("pytaps.preconnection").setLevel(logging.ERROR)
        logging.getLogger("pytaps.transports").setLevel(logging.ERROR)


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
    def __init__(self, *, verbose_monitoring=False, dump_properties=False):
        self.verbose_monitoring = verbose_monitoring
        self.dump_properties = dump_properties

    async def handle_monitoring_update(self, update):
        if not self.verbose_monitoring:
            return
        logger.info(
            "monitor trigger=%s health=%s",
            update["trigger"],
            update["snapshot"]["healthSummary"],
        )

    async def handle_rendezvous_done(self, connection):
        logger.info(
            "rendezvous done protocol=%s local=%s remote=%s",
            connection.protocol,
            connection.local_endpoint,
            connection.remote_endpoint,
        )

    async def main(self, args):
        configure_logging(args)
        local = build_local_endpoint(args)
        remote = build_remote_endpoint(args)
        properties = taps.TransportProperties().reliable_inorder_stream()
        properties.set_property("direction", "Bidirectional")

        preconnection = taps.Preconnection(
            local_endpoints=[local],
            remote_endpoints=[remote],
            transport_properties=properties,
        )
        preconnection.subscribe_monitoring(self.handle_monitoring_update)
        preconnection.on_rendezvous_done(self.handle_rendezvous_done)

        connection = await preconnection.rendezvous(timeout=args.timeout)
        receive_task = asyncio.create_task(
            connection.receive(
                min_incomplete_length=1,
                max_length=4096,
                timeout=args.timeout,
            )
        )
        context = connection.new_message_context(final=False)
        await connection.send(args.payload.encode("utf-8"), context)
        message = await receive_task
        logger.info(
            "received peer payload=%r sequence=%s",
            message.data.decode("utf-8", errors="replace"),
            message.context.receive_sequence,
        )
        await asyncio.sleep(args.settle_time)
        snapshot = connection.get_monitoring_snapshot()
        if self.dump_properties:
            logger.info("connection monitoring=%s", snapshot)
        else:
            context = snapshot["connectionContext"]
            logger.info(
                "summary health=%s events=%s",
                context["healthSummary"]["severity"],
                context["eventCounters"],
            )

        if args.close:
            close_task = connection.close()
            if close_task is not None:
                await close_task
            await connection.wait_closed()
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
    asyncio.run(
        RendezvousPeer(
            verbose_monitoring=parsed_args.verbose_monitoring,
            dump_properties=parsed_args.dump_properties,
        ).main(parsed_args)
    )
