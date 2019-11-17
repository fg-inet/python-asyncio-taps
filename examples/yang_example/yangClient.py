import asyncio
import sys
import argparse
import ipaddress
sys.path.append(sys.path[0] + "/../..")
import pytaps as taps  # noqa: E402

color = "yellow"


class TestClient():
    def __init__(self):
        self.connection = None
        self.preconnection = None
        self.loop = asyncio.get_event_loop()

    async def handle_received_partial(self, data, context, end_of_message,
                                      connection):
        taps.print_time("Received partial message " + str(data) + ".", color)
        # self.loop.stop()

    async def handle_received(self, data, context, connection):
        taps.print_time("Received message " + str(data) + ".", color)
        # self.loop.stop()

    async def handle_sent(self, message_ref, connection):
        taps.print_time("Sent cb received, message " + str(message_ref) +
                        " has been sent.", color)
        await self.connection.receive(min_incomplete_length=1)

    async def handle_send_error(self, msg, connection):
        taps.print_time("SendError cb received.", color)
        print("Error sending message")

    async def handle_initiate_error(self, connection):
        taps.print_time("InitiateError cb received.", color)
        print("Error init")
        self.loop.stop()

    async def handle_closed(self, connection):
        taps.print_time("Connection closed, stopping event loop.", color)
        # self.loop.stop()

    async def handle_ready(self, connection):
        taps.print_time("Ready cb received from connection to " +
                        connection.remote_endpoint.address + ":" +
                        str(connection.remote_endpoint.port) +
                        " (hostname: " +
                        str(connection.remote_endpoint.host_name) +
                        ")", color)

        # Set connection callbacks
        self.connection.on_sent(self.handle_sent)
        self.connection.on_send_error(self.handle_send_error)
        self.connection.on_closed(self.handle_closed)
        self.connection.on_received_partial(self.handle_received_partial)
        self.connection.on_received(self.handle_received)
        taps.print_time("Connection cbs set.", color)

        # Send message
        """
        msgref = await self.connection.send_message("Hello\n")
        msgref = await self.connection.send_message("There")
        msgref = await self.connection.send_message("Friend")
        msgref = await self.connection.send_message("How")
        msgref = await self.connection.send_message("Are")
        msgref = await self.connection.send_message("Youuuuu\n")
        msgref = await self.connection.send_message("Today?\n")
        msgref = await self.connection.send_message("343536")"""

        msgref = await self.connection.send_message("Hello\n")
        taps.print_time("send_message called.", color)

    async def main(self, args):
        fname = args.file[0]
        self.preconnection = taps.Preconnection.from_yangfile(fname)
        taps.print_time("Loaded YANG file: %s." % fname, color)

        self.preconnection.on_initiate_error(self.handle_initiate_error)
        self.preconnection.on_ready(self.handle_ready)
        taps.print_time("Created preconnection object and set cbs.", color)

        # Initiate the connection
        self.connection = await self.preconnection.initiate()
        taps.print_time("Called initiate, connection object created.", color)


if __name__ == "__main__":
    # Parse arguments
    ap = argparse.ArgumentParser(description='PyTAPS test client.')
    ap.add_argument('--file', '-f', nargs=1, default=None)
    args = ap.parse_args()
    print(args)
    # Start testclient
    client = TestClient()
    client.loop.create_task(client.main(args))
    client.loop.run_forever()
