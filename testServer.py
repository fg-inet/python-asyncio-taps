import PyTAPS as taps
import asyncio
import sys
color = "blue"


class TestServer():
    def __init__(self):
        self.preconnection = None
        self.loop = asyncio.get_event_loop()

    def handle_connection_received(self, connection):
        taps.print_time("Received new Connection.", color)
        connection.on_received_partial(self.handle_received_partial)
        connection.on_received(self.handle_received)
        connection.receive(min_incomplete_length=1)

    def handle_received_partial(self, data, context, end_of_message):
        taps.print_time("Received message " + str(data) + ".", color)
        self.loop.stop()

    def handle_received(self, data, context):
        taps.print_time("Received message " + str(data) + ".", color)
        self.loop.stop()

    def handle_listen_error(self):
        taps.print_time("Listen Error occured.", color)
        self.loop.stop()

    def handle_stopped(self):
        taps.print_time("Listener has been stopped")

    async def main(self):
        lp = taps.LocalEndpoint()
        taps.print_time("Created endpoint objects.", color)

        if len(sys.argv) == 3:
            lp.with_interface(str(sys.argv[1]))
            lp.with_port(int(sys.argv[2]))
        else:
            exit("Please call with localAddress, localPort, ")
        # tp = taps.transportProperties()
        # tp.add("Reliable_Data_Transfer", taps.preferenceLevel.REQUIRE)
        # taps.print_time("Created transportProperties object.", color)
        self.preconnection = taps.Preconnection(local_endpoint=lp)
        self.preconnection.on_connection_received(self.handle_connection_received)
        self.preconnection.on_listen_error(self.handle_listen_error)
        self.preconnection.on_stopped(self.handle_stopped)

        self.preconnection.listen()


if __name__ == "__main__":
    server = TestServer()
    server.loop.create_task(server.main())
    server.loop.run_forever()
