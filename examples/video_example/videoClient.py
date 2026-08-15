"""Play a PyTAPS live video stream.

Receives one TAPS Message per H.264 access unit and feeds them to ffplay, or
writes them to a file with --save. The Framer metadata that arrives with each
Message (keyframe flag, sequence number, capture timestamp) is used to report
loss and one-way latency without decoding anything.
"""
import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytaps as taps  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))

from h264Framer import H264AccessUnitFramer  # noqa: E402

logger = taps.setup_logger("Video Client", "magenta")

FRAMER_NAMESPACE = "example.h264"


class Player:
    """Feed access units to ffplay, or to a file."""

    def __init__(self, args):
        self.args = args
        self.process = None
        self.handle = None

    async def start(self):
        if self.args.save:
            self.handle = open(self.args.save, "wb")
            logger.info("Writing the stream to %s", self.args.save)
            return
        command = [
            self.args.ffplay,
            "-hide_banner", "-loglevel", "error",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-framedrop",
            "-window_title", "PyTAPS live video",
            "-f", "h264",
            "-i", "-",
        ]
        logger.info("Starting player: %s", " ".join(command))
        self.process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    def write(self, access_unit):
        if self.handle is not None:
            self.handle.write(access_unit)
            return True
        if self.process is None or self.process.stdin is None:
            return False
        if self.process.returncode is not None:
            return False
        try:
            self.process.stdin.write(access_unit)
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    async def stop(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None
        if self.process is None:
            return
        if self.process.stdin is not None and not self.process.stdin.is_closing():
            try:
                self.process.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()


class Statistics:
    def __init__(self):
        self.frames = 0
        self.keyframes = 0
        self.bytes_received = 0
        self.missing = 0
        self.expected_sequence = None
        self.latency_total = 0.0
        self.latency_samples = 0
        self.started_at = time.monotonic()

    def observe(self, payload, sequence, keyframe, captured_at):
        self.frames += 1
        self.bytes_received += len(payload)
        if keyframe:
            self.keyframes += 1
        if sequence is not None:
            if (
                self.expected_sequence is not None
                and sequence > self.expected_sequence
            ):
                # The sender expired or lost frames between these two.
                self.missing += sequence - self.expected_sequence
            self.expected_sequence = sequence + 1
        if captured_at:
            latency = time.time() - captured_at / 1_000_000
            if -1.0 < latency < 30.0:
                self.latency_total += latency
                self.latency_samples += 1

    def summary(self):
        elapsed = max(time.monotonic() - self.started_at, 1e-9)
        line = (
            f"frames={self.frames} keyframes={self.keyframes} "
            f"missing={self.missing} "
            f"{self.bytes_received * 8 / elapsed / 1e6:.2f} Mbit/s"
        )
        if self.latency_samples:
            average = self.latency_total / self.latency_samples * 1000
            line += f" latency~{average:.0f} ms"
        return line


async def main(args):
    properties = taps.TransportProperties()
    properties.require("reliability")
    properties.require("preserveOrder")
    properties.require("multistreaming")
    properties.set_property("connCapacityProfile", "Low Latency/Interactive")

    security = taps.SecurityParameters()
    security.add_trust_ca(args.trust_ca)
    security.with_server_name(args.server_name)
    security.set_alpn_protocols([args.alpn])

    remote = taps.RemoteEndpoint()
    remote.with_hostname(args.server_name)
    remote.with_address(args.remote_address)
    remote.with_port(args.port)

    preconnection = taps.Preconnection(
        remote_endpoints=[remote],
        transport_properties=properties,
        security_parameters=security,
    )
    preconnection.add_framer(H264AccessUnitFramer())

    connection = await preconnection.initiate(timeout=args.timeout)
    logger.info(
        "Connected over %s; capacity profile %s.",
        connection.protocol,
        connection.get_property("connCapacityProfile"),
    )

    subscribe = connection.new_message_context()
    subscribe.add(FRAMER_NAMESPACE, "control", True)
    await connection.send(f"SUBSCRIBE {args.track}".encode(), subscribe)
    logger.info("Subscribed to track %r.", args.track)

    player = Player(args)
    await player.start()
    statistics = Statistics()
    finished = asyncio.get_running_loop().create_future()

    def stop(reason):
        if not finished.done():
            logger.info("Stopping: %s", reason)
            finished.set_result(reason)

    async def on_received(data, context, conn):
        payload = bytes(data)
        keyframe = bool(context.get(FRAMER_NAMESPACE, "keyframe", False))
        sequence = context.get(FRAMER_NAMESPACE, "sequence", None)
        captured_at = context.get(FRAMER_NAMESPACE, "capturedAt", 0)
        statistics.observe(payload, sequence, keyframe, captured_at)

        if not player.write(payload):
            stop("the player exited")
            return
        if args.frames and statistics.frames >= args.frames:
            stop(f"received {args.frames} frames")
            return
        await conn.receive()

    async def on_connection_error(error, conn):
        stop(f"connection error: {error}")

    async def on_closed(conn):
        stop("the sender closed the Connection")

    connection.on_received(on_received)
    connection.on_connection_error(on_connection_error)
    connection.on_closed(on_closed)
    await connection.receive()

    reporter = asyncio.create_task(_report(statistics, args.report_interval))
    try:
        if args.duration:
            try:
                await asyncio.wait_for(finished, timeout=args.duration)
            except asyncio.TimeoutError:
                logger.info("Stopping: reached --duration.")
        else:
            await finished
    finally:
        reporter.cancel()
        await asyncio.gather(reporter, return_exceptions=True)
        logger.info("final %s", statistics.summary())
        await player.stop()
        connection.close()
        try:
            await connection.wait_closed(timeout=5)
        except Exception:
            pass


async def _report(statistics, interval):
    while True:
        await asyncio.sleep(interval)
        logger.info(statistics.summary())


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote-address", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4460)
    parser.add_argument("--server-name", default="localhost")
    parser.add_argument(
        "--trust-ca",
        default=str(ROOT / "tests" / "keys" / "MyRootCA.pem"),
    )
    parser.add_argument("--alpn", default="pytaps-video")
    parser.add_argument("--track", default="video")
    parser.add_argument(
        "--save",
        default=None,
        help="Write the H.264 stream to this file instead of playing it.",
    )
    parser.add_argument("--ffplay", default="ffplay")
    parser.add_argument(
        "--frames",
        type=int,
        default=0,
        help="Stop after this many frames; 0 means run until stopped.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0,
        help="Stop after this many seconds; 0 means run until stopped.",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--report-interval", type=float, default=5.0)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_arguments()
    if not arguments.verbose:
        for name in ("pytaps.connection", "pytaps.transports", "pytaps.listener"):
            logging.getLogger(name).setLevel(logging.WARNING)
    try:
        asyncio.run(main(arguments))
    except KeyboardInterrupt:
        logger.info("Stopped.")
