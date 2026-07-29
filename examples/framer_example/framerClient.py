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


async def main(args):
    configure_example_logging(args.verbose)

    framer = LengthPrefixFramer()
    remote = (
        taps.RemoteEndpoint()
        .with_address(args.remote_address)
        .with_port(args.port)
    )
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        transport_properties=tcp_properties(),
    ).add_framer(framer)

    connection = await preconnection.initiate(timeout=args.timeout)
    await connection.wait_ready(timeout=args.timeout)
    print(f"connected protocol={connection.protocol}")

    for sequence, text in enumerate(args.message, start=1):
        context = taps.MessageContext()
        context.add(framer, "kind", "request")
        context.add(framer, "sequence", sequence)
        await connection.send(text.encode(), context)

        reply = await connection.receive(timeout=args.timeout)
        print(
            f"reply sequence={sequence} "
            f"length={reply.get(framer, 'payloadLength')} "
            f"kind={reply.get(framer, 'kind')} "
            f"message={reply.data.decode(errors='replace')!r}"
        )

    connection.close()
    await connection.wait_closed(timeout=args.timeout)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Send length-prefixed Messages through the PyTAPS Framer API."
    )
    parser.add_argument("--remote-address", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7778)
    parser.add_argument(
        "--message",
        action="append",
        default=None,
        help="Message to send; repeat this option to send more than one.",
    )
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if args.message is None:
        args.message = ["hello from the TAPS Framer demo"]
    return args


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
