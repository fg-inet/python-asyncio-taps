import asyncio
import sys
import argparse
sys.path.append(sys.path[0] + "/../..")
import PyTAPS as taps  # noqa: E402

color = "blue"


class TestServer():
    """A simple echo server. Listens using TCP by default, but can also use UDP.

    """
    def __init__(self, reliable=True):
        self.preconnection = None
        self.loop = asyncio.get_event_loop()
        self.connection = None
        self.reliable = reliable

    async def handle_connection_received(self, connection):
        taps.print_time("Received new Connection.", color)
        self.connection = connection
        self.connection.on_received_partial(self.handle_received_partial)
        self.connection.on_received(self.handle_received)
        self.connection.on_sent(self.handle_sent)
        await self.connection.receive(min_incomplete_length=1, max_length=-1)
        # await self.connection.receive(min_incomplete_length=4, max_length=3)
        # self.connection.on_sent(handle_sent)

    async def handle_received_partial(self, data, context, end_of_message,
                                      connection):
        taps.print_time("Received partial message " + str(data) + ".", color)
        await self.connection.receive(min_incomplete_length=1, max_length=5)
        msgref = await self.connection.send_message(str(data))

    async def handle_received(self, data, context, connection):
        taps.print_time("Received message " + str(data) + ".", color)
        await self.connection.receive(min_incomplete_length=1, max_length=5)
        await self.connection.send_message(data)

    async def handle_listen_error(self):
        taps.print_time("Listen Error occured.", color)
        self.loop.stop()

    async def handle_sent(self, message_ref):
        taps.print_time("Sent cb received, message " + str(message_ref) +
                        " has been sent.", color)
        # self.connection.close()

    async def handle_stopped(self):
        taps.print_time("Listener has been stopped")

    async def main(self, args):
        # Create endpoint object
        lp = taps.LocalEndpoint()
        if args.interface:
            lp.with_interface(args.interface)
        if args.local_address:
            lp.with_address(args.local_address)
        if args.local_port:
            lp.with_port(args.local_port)

        taps.print_time("Created endpoint objects.", color)

        sp = None
        if args.secure or args.trust_ca or args.local_identity:
            # Use TLS
            sp = taps.SecurityParameters()
            if args.trust_ca:
                sp.addTrustCA(args.trust_ca)
            if args.local_identity:
                sp.addIdentity(args.local_identity)
            taps.print_time("Created SecurityParameters.", color)

        tp = taps.TransportProperties()
        if self.reliable is False:
            tp.ignore("reliability")
            tp.ignore("congestion-control")
            tp.ignore("preserve-order")

        self.preconnection = taps.Preconnection(local_endpoint=lp,
                                                transport_properties=tp,
                                                security_parameters=sp)
        self.preconnection.on_connection_received(
                                            self.handle_connection_received)
        self.preconnection.on_listen_error(self.handle_listen_error)
        self.preconnection.on_stopped(self.handle_stopped)
        # self.preconnection.frame_with(taps.TlvFramer())
        await self.preconnection.listen()


if __name__ == "__main__":
    # Parse arguments
    ap = argparse.ArgumentParser(description='PyTAPS test server.')
    ap.add_argument('--interface', '-i', nargs=1, default=None)
    ap.add_argument('--local-address', '--address', '-a', nargs='?',
                    default='::1')
    ap.add_argument('--local-port', '--port', '-l', type=int, nargs='?',
                    default=6666)
    ap.add_argument('--local-identity', type=str, nargs=1, default=None)
    ap.add_argument('--trust-ca', type=str, default=None)
    ap.add_argument('--secure', '-s', nargs='?', const=True, type=bool,
                    default=False)
    ap.add_argument('--reliable', type=str, default="yes")
    args = ap.parse_args()
    print(args)

    loop = asyncio.get_event_loop()
    # Start testserver
    if args.reliable in ["yes", "true", "both"]:
        server_tcp = TestServer(reliable=True)
        loop.create_task(server_tcp.main(args))
    if args.reliable in ["no", "false", "both"]:
        server_udp = TestServer(reliable=False)
        loop.create_task(server_udp.main(args))
    loop.run_forever()
