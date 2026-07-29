import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytaps as taps  # noqa: E402

from lengthPrefixFramer import (  # noqa: E402
    LengthPrefixFramer,
    configure_example_logging,
)


def tcp_properties():
    properties = taps.TransportProperties().reliable_inorder_stream()
    properties.prohibit("confidentiality")
    properties.prohibit("multistreaming")
    return properties


async def handle_connection(connection, framer):
    peer = connection.remote_endpoint
    print(f"accepted peer={peer.address}:{peer.port} protocol={connection.protocol}")
    try:
        while True:
            message = await connection.receive()
            text = message.data.decode(errors="replace")
            print(
                f"received length={message.get(framer, 'payloadLength')} "
                f"kind={message.get(framer, 'kind')} message={text!r}"
            )

            context = taps.MessageContext()
            context.add(framer, "kind", "echo")
            sequence = message.get(framer, "sequence")
            if sequence is not None:
                context.add(framer, "sequence", sequence)
            await connection.send(b"echo: " + message.data, context)
    except (ConnectionError, EOFError):
        pass
    finally:
        connection.close()
        await connection.wait_closed()


async def main(args):
    configure_example_logging(args.verbose)

    framer = LengthPrefixFramer()
    local = (
        taps.LocalEndpoint()
        .with_address(args.local_address)
        .with_port(args.port)
    )
    preconnection = taps.Preconnection(
        local_endpoint=local,
        transport_properties=tcp_properties(),
    ).add_framer(framer)
    listener = await preconnection.listen(timeout=args.timeout)
    await listener.wait_listening(timeout=args.timeout)
    print(f"listening address={args.local_address} port={args.port}")

    tasks = set()
    try:
        while True:
            connection = await listener.accept()
            task = asyncio.create_task(handle_connection(connection, framer))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    finally:
        await listener.stop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Echo length-prefixed Messages through the PyTAPS Framer API."
    )
    parser.add_argument("--local-address", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7778)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(main(parse_args()))
    except KeyboardInterrupt:
        pass
