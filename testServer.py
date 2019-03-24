import PyTAPS as taps
import asyncio
import sys
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
        await self.connection.receive(min_incomplete_length=1, max_length=5)
        # await self.connection.receive(min_incomplete_length=4, max_length=3)
        # self.connection.on_sent(handle_sent)

    async def handle_received_partial(self, data, context, end_of_message,
                                      connection):
        taps.print_time("Received message " + str(data) + ".", color)
        # msgref = await self.connection.send_message(str(data))

    async def handle_received(self, data, context, connection):
        taps.print_time("Received message " + str(data) + ".", color)
        # self.connection.send_message(data)

    async def handle_listen_error(self, connection):
        taps.print_time("Listen Error occured.", color)
        self.loop.stop()

    async def handle_sent(self, message_ref):
        taps.print_time("Sent cb received, message " + str(message_ref) +
                        " has been sent.", color)
        self.connection.close()
        taps.print_time("Queued closure of connection.", color)

    async def handle_stopped(self):
        taps.print_time("Listener has been stopped")

    async def main(self):
        # Create endpoint object
        lp = taps.LocalEndpoint()
        # Set default interface and port
        lp.with_interface("127.0.0.1")
        lp.with_port(6666)
        taps.print_time("Created endpoint objects.", color)

        if len(sys.argv) == 3:
            lp.with_interface(str(sys.argv[1]))
            lp.with_port(int(sys.argv[2]))

        tp = taps.TransportProperties()
        tp.prohibit("reliability")
        tp.ignore("congestion-control")
        tp.ignore("preserve-order")

        self.preconnection = taps.Preconnection(local_endpoint=lp,
                                                transport_properties=tp)
        self.preconnection.on_connection_received(
            self.handle_connection_received)
        self.preconnection.on_listen_error(self.handle_listen_error)
        self.preconnection.on_stopped(self.handle_stopped)
        await self.preconnection.listen()


if __name__ == "__main__":
    server = TestServer()
    server.loop.create_task(server.main())
    server.loop.run_forever()
