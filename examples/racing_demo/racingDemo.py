"""Show how PyTAPS races candidates during Connection establishment.

Section 4.3 of RFC 9623 describes staggered racing, and Section 4.3.2 points at
the Happy Eyeballs algorithm of RFC 8305 for racing between IP addresses. Two
consequences are easy to get wrong and invisible when they are:

* Address families are interleaved (Section 4 of RFC 8305). Trying every
  address of one family first means a broken family stalls establishment for
  as long as its addresses last.
* A child that fails releases the next one immediately (Section 4.3.2 of
  RFC 9623) instead of letting it sit out the rest of its stagger.

This demo prints the candidate order for a resolved address set, then measures
establishment against a set of dead addresses in front of a live Listener, with
and without the acceleration.
"""
import argparse
import asyncio
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytaps as taps  # noqa: E402
from pytaps import connection as connection_module  # noqa: E402
from pytaps.utility import order_remote_addresses  # noqa: E402

logger = taps.setup_logger("Racing Demo", "yellow")

V6 = socket.AddressFamily.AF_INET6
V4 = socket.AddressFamily.AF_INET


def _family(entry):
    return "IPv6" if entry[0] is V6 else "IPv4"


def show_address_ordering(args):
    """Print the order a resolved address set is raced in."""
    addresses = [
        (V6, f"2001:db8::{index + 1}") for index in range(args.v6)
    ] + [
        (V4, f"192.0.2.{index + 1}") for index in range(args.v4)
    ]

    print(f"\nResolved {args.v6} IPv6 and {args.v4} IPv4 addresses.")
    print("RFC 8305 Section 4 interleaves the families so a broken family")
    print("cannot stall establishment behind a long run of its addresses.\n")

    grouped = sorted(addresses, key=lambda entry: (entry[0] is not V6,))
    print("  without interleaving:")
    print("    " + "  ".join(_family(entry) for entry in grouped))

    ordered = order_remote_addresses(addresses)
    print("  as PyTAPS races them:")
    print("    " + "  ".join(_family(entry) for entry in ordered))

    # How long before the first address of the other family is tried?
    leading = ordered[0][0]
    for position, entry in enumerate(ordered):
        if entry[0] is not leading:
            break
    else:
        position = len(ordered)
    stagger = connection_module.RACING_DELAY
    grouped_position = sum(1 for entry in grouped if entry[0] is leading)
    print(
        f"\n  first {_family(ordered[0])} attempt at +0 ms; first "
        f"{'IPv4' if leading is V6 else 'IPv6'} attempt at "
        f"+{position * stagger * 1000:.0f} ms"
    )
    print(
        f"  without interleaving it would be "
        f"+{grouped_position * stagger * 1000:.0f} ms"
    )


async def measure_failover(args, *, accelerate):
    """Time establishment when dead addresses precede a live Listener."""
    original = connection_module.Connection._open_next_race_gate
    if not accelerate:
        # Simulate a stack that makes every child wait out its full stagger.
        connection_module.Connection._open_next_race_gate = lambda self: False
    try:
        properties = taps.TransportProperties()
        properties.prohibit("multistreaming")
        properties.apply_profile("reliable-inorder-stream")

        listener_preconnection = taps.Preconnection(
            local_endpoint=(
                taps.LocalEndpoint().with_address("127.0.0.1").with_port(0)
            ),
            transport_properties=properties,
        )
        listener = await listener_preconnection.listen(timeout=5)
        live_port = listener._servers[0].sockets[0].getsockname()[1]

        # Reserve a port nobody listens on, so those attempts fail at once.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = probe.getsockname()[1]

        endpoints = [
            taps.RemoteEndpoint().with_address("127.0.0.1").with_port(dead_port)
            for _ in range(args.dead)
        ]
        endpoints.append(
            taps.RemoteEndpoint().with_address("127.0.0.1").with_port(live_port)
        )

        preconnection = taps.Preconnection(
            remote_endpoints=endpoints,
            transport_properties=properties,
        )
        started = time.monotonic()
        connection = await preconnection.initiate(timeout=60)
        elapsed = time.monotonic() - started

        connection.close()
        try:
            await connection.wait_closed(timeout=5)
        except Exception:
            pass
        await listener.stop()
        return elapsed
    finally:
        connection_module.Connection._open_next_race_gate = original


async def show_failover(args):
    stagger = connection_module.RACING_DELAY
    print(f"\n{args.dead} dead addresses ahead of a live Listener, "
          f"stagger {stagger * 1000:.0f} ms.")
    print("RFC 9623 Section 4.3.2 starts the next child immediately when one")
    print("fails, rather than waiting for its delay to expire.\n")

    gated = await measure_failover(args, accelerate=False)
    print(f"  waiting out each stagger : {gated * 1000:7.0f} ms")
    accelerated = await measure_failover(args, accelerate=True)
    print(f"  released on failure      : {accelerated * 1000:7.0f} ms")
    if accelerated > 0:
        print(f"  speedup                  : {gated / accelerated:7.0f}x")


def show_delay_bounds():
    print("\nConnection Attempt Delay, per Section 5 of RFC 8305.")
    print("The delay is derived from cached path history, so it is bounded:")
    print(f"  default : {connection_module.RACING_DELAY * 1000:.0f} ms")
    print(f"  minimum : {connection_module.MINIMUM_RACING_DELAY * 1000:.0f} ms"
          "   (RFC 8305 forbids a subsequent attempt within 10 ms)")
    print(f"  maximum : {connection_module.MAXIMUM_RACING_DELAY:.0f} s")


async def main(args):
    show_address_ordering(args)
    show_delay_bounds()
    if not args.no_failover:
        await show_failover(args)
    print()


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v6", type=int, default=3, help="IPv6 addresses")
    parser.add_argument("--v4", type=int, default=3, help="IPv4 addresses")
    parser.add_argument(
        "--dead",
        type=int,
        default=3,
        help="Unreachable addresses to place before the live Listener.",
    )
    parser.add_argument(
        "--no-failover",
        action="store_true",
        help="Only print the ordering, without opening sockets.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    import logging

    # The dead addresses below fail on purpose, so keep the library quiet.
    for name in ("pytaps.connection", "pytaps.transports", "pytaps.listener",
                 "pytaps.preconnection"):
        logging.getLogger(name).setLevel(logging.ERROR)
    asyncio.run(main(parse_arguments()))
