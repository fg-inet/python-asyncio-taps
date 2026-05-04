import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytaps as taps  # noqa: E402


class TestClient():
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
        self.connection = await self.preconnection.initiate_with_send("Hello\n")


if __name__ == "__main__":
    client = TestClient()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(client.main())
    loop.run_forever()
