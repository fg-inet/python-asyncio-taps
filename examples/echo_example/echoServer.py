import asyncio
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytaps as taps  # noqa: E402

logger = taps.setup_logger("Echo Server", "yellow")


class TestServer:
    """A simple echo server. Listens using TCP by default,
       but can also use UDP.

    """

    def __init__(self, reliable=True):
        self.preconnection = None
        self.loop = None
        self.connection = None
        self.reliable = reliable

    async def handle_connection_received(self, connection):
        logger.info("Received new Connection.")
        self.connection = connection
        self.connection.on_received_partial(self.handle_received_partial)
        self.connection.on_received(self.handle_received)
        self.connection.on_sent(self.handle_sent)
        await self.connection.receive(min_incomplete_length=1, max_length=-1)
        # await self.connection.receive(min_incomplete_length=4, max_length=3)
        # self.connection.on_sent(handle_sent)

    async def handle_received_partial(self, data, context, end_of_message,
                                      connection):
        logger.info(
            "Received partial message %s (end_of_message=%s).",
            data,
            context.end_of_message,
        )
        self.loop.create_task(
            self.connection.receive(min_incomplete_length=1, max_length=5)
        )
        reply_context = None
        if self.connection.protocol == "udp":
            reply_context = self.connection.new_message_context(
                safelyReplayable=True,
                final=False,
            )
        await self.connection.send(data, reply_context)

    async def handle_received(self, data, context, connection):
        logger.info("Received message %s from %s.", data, context.remote_address)
        self.loop.create_task(
            self.connection.receive(min_incomplete_length=1, max_length=5)
        )
        reply_context = None
        if self.connection.protocol == "udp":
            reply_context = self.connection.new_message_context(
                safelyReplayable=True,
                final=False,
            )
        await self.connection.send(data, reply_context)

    async def handle_listen_error(self):
        logger.warning("Listen Error occured.")
        self.loop.stop()

    async def handle_sent(self, message_ref, connection):
        logger.info("Sent cb received, message " + str(message_ref) +
                    " has been sent.")
        # self.connection.close()

    async def handle_stopped(self):
        logger.info("Listener has been stopped")

    async def main(self, args):
        # Create endpoint object
        lp = taps.LocalEndpoint()
        if args.interface:
            lp.with_interface(args.interface)
        if args.local_address:
            lp.with_address(args.local_address)
        if args.local_host:
            lp.with_hostname(args.local_host)
        # If nothing to listen on has been specified, listen on localhost
        if not args.interface and not args.local_address \
                and not args.local_host:
            lp.with_hostname("localhost")
        if args.local_port:
            lp.with_port(args.local_port)

        logger.info("Created endpoint objects.")

        sp = None
        if args.secure or args.trust_ca or args.local_identity:
            # Use TLS
            sp = taps.SecurityParameters()
            if args.trust_ca:
                sp.add_trust_ca(args.trust_ca)
            if args.local_identity:
                sp.add_identity(args.local_identity)
            logger.info("Created SecurityParameters.")

        if self.reliable == "False":
            tp = taps.TransportProperties().unreliable_datagram()
        elif self.reliable == "Both":
            tp = taps.TransportProperties()
            tp.ignore("congestionControl")
            tp.ignore("preserveOrder")
            tp.ignore("reliability")
        else:
            tp = taps.TransportProperties().reliable_inorder_stream()

        self.preconnection = taps.Preconnection(
            local_endpoints=[lp],
            transport_properties=tp,
            security_parameters=sp,
        )
        self.preconnection.on_connection_received(
            self.handle_connection_received)
        self.preconnection.on_listen_error(self.handle_listen_error)
        self.preconnection.on_stopped(self.handle_stopped)
        # self.preconnection.frame_with(taps.TlvFramer())
        await self.preconnection.listen()


if __name__ == "__main__":
    # Parse arguments
    ap = argparse.ArgumentParser(description='PyTAPS test server.')
    ap.add_argument('--interface', '-i', default=None)
    ap.add_argument('--local-address', '--address', '-a', nargs='?',
                    default=None)
    ap.add_argument('--local-host', '--host', '-H', nargs='?',
                    default=None)
    ap.add_argument('--local-port', '--port', '-l', type=int, nargs='?',
                    default=6666)
    ap.add_argument('--local-identity', type=str, default=None)
    ap.add_argument('--trust-ca', type=str, default=None)
    ap.add_argument('--secure', '-s', nargs='?', const=True, type=bool,
                    default=False)
    ap.add_argument('--reliable', type=str, default="yes")
    args = ap.parse_args()
    print(args)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # Start testserver
    if args.reliable in ["yes", "true"]:
        server_tcp = TestServer(reliable="True")
        server_tcp.loop = loop
        loop.create_task(server_tcp.main(args))
    if args.reliable in ["no", "false"]:
        server_udp = TestServer(reliable="False")
        server_udp.loop = loop
        loop.create_task(server_udp.main(args))
    if args.reliable in ["both"]:
        server_both = TestServer(reliable="Both")
        server_both.loop = loop
        loop.create_task(server_both.main(args))

    loop.run_forever()
