import asyncio
import sys
sys.path.append(sys.path[0] + "/../..")
import pytaps as taps  # noqa: E402


class TestClient():
    async def handle_ready(self, connection):
        msgref = await self.connection.send_message("Hello\n")

    async def main(self):
        ep = taps.RemoteEndpoint()
        ep.with_hostname("localhost")
        ep.with_port(6666)
        tp = taps.TransportProperties()

        tp.prohibit("reliability")
        tp.ignore("congestion-control")
        tp.ignore("preserve-order")

        self.preconnection = taps.Preconnection(remote_endpoint=ep,
                                                transport_properties=tp)
        self.preconnection.on_ready(self.handle_ready)
        self.connection = await self.preconnection.initiate()


if __name__ == "__main__":
    client = TestClient()
    asyncio.get_event_loop().create_task(client.main())
    asyncio.get_event_loop().run_forever()
