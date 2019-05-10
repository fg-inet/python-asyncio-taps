import asyncio
import sys
sys.path.append(sys.path[0] + "/../..")
import PyTAPS as taps  # noqa: E402


class TestServer():
    async def handle_connection_received(self, connection):
        connection.on_received(self.handle_received)
        await connection.receive()

    async def handle_received(self, data, context, connection):
        print(data)

    async def main(self):
        lp = taps.LocalEndpoint()
        lp.with_address("localhost")
        lp.with_port(6666)
        tp = taps.TransportProperties()

        tp.prohibit("reliability")
        tp.ignore("congestion-control")
        tp.ignore("preserve-order")

        self.preconnection = taps.Preconnection(local_endpoint=lp,
                                                transport_properties=tp)
        self.preconnection.on_connection_received(
                                    self.handle_connection_received)
        await self.preconnection.listen()

if __name__ == "__main__":
    server = TestServer()
    asyncio.get_event_loop().create_task(server.main())
    asyncio.get_event_loop().run_forever()
