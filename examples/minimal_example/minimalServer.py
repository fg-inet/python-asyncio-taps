import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytaps as taps  # noqa: E402


class TestServer():
    async def handle_connection_received(self, connection):
        connection.on_received(self.handle_received)
        await connection.receive()

    async def handle_received(self, data, context, connection):
        print(data, context)

    async def main(self):
        lp = taps.LocalEndpoint()
        lp.with_hostname("localhost")
        lp.with_port(6666)
        tp = taps.TransportProperties().unreliable_datagram()

        self.preconnection = taps.Preconnection(
            local_endpoints=[lp],
            transport_properties=tp,
        )
        self.preconnection.on_connection_received(
                                    self.handle_connection_received)
        await self.preconnection.listen()


if __name__ == "__main__":
    server = TestServer()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(server.main())
    loop.run_forever()
