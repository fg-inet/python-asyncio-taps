import asyncio
import sys
import argparse
sys.path.append(sys.path[0] + "/../..")
import pytaps as taps  # noqa: E402

color = "blue"


class TestServer():
    def __init__(self):
        self.preconnection = None
        self.loop = asyncio.get_event_loop()
        self.connection = None

    async def handle_connection_received(self, connection):
        taps.print_time("Received new Connection.", color)
        self.connection = connection
        self.connection.on_received_partial(self.handle_received_partial)
        self.connection.on_received(self.handle_received)
        self.connection.on_sent(self.handle_sent)
        await self.connection.receive()
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

    async def main(self, fname):
        self.preconnection = taps.Preconnection.from_yangfile(fname)
        taps.print_time("Loaded YANG file: %s." % fname, color)
        self.preconnection.on_connection_received(
                                            self.handle_connection_received)
        self.preconnection.on_listen_error(self.handle_listen_error)
        self.preconnection.on_stopped(self.handle_stopped)
        await self.preconnection.listen()

if __name__ == "__main__":
    # Parse arguments
    ap = argparse.ArgumentParser(description='PyTAPS test server.')
    ap.add_argument('--file', '-f', nargs=1, default=None)
    args = ap.parse_args()
    print(args)
    if not args.file:
        print("\tExiting -- Please specify a YANG file using -f FILE")
        exit()
    # Start testserver
    server = TestServer()
    server.loop.create_task(server.main(args.file[0]))
    server.loop.run_forever()
