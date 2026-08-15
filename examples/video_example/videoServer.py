"""Stream live H.264 over QUIC with PyTAPS.

Every encoded picture becomes one TAPS Message, which lets the Transport
Services API carry the properties that matter for live media:

* connCapacityProfile is Low Latency/Interactive (Section 8.1.6 of RFC 9622),
  which the QUIC and TCP backends map onto the recommended DSCP marking.
* Keyframes are sent at a better msgPriority than delta frames
  (Section 9.1.3.2), and telemetry rides a lower-priority Connection in the
  same Connection Group, so connPriority is ordered over msgPriority
  (Section 9.2.6).
* Delta frames carry a msgLifetime (Section 9.1.3.1). A frame that cannot be
  sent before the next one matters is dropped rather than queued, and the
  application is told through an Expired event.
* An H.264 Message Framer restores Message boundaries on the QUIC byte stream
  (Section 9.1.2).
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

from h264Framer import (  # noqa: E402
    AccessUnitAssembler,
    H264AccessUnitFramer,
)

logger = taps.setup_logger("Video Server", "cyan")


def build_encoder_command(args):
    if args.input:
        source = ["-re", "-stream_loop", "-1", "-i", args.input]
    else:
        # -re paces the synthetic source at wall-clock rate. Without it the
        # encoder runs flat out, the send queue backs up and the frame
        # Lifetimes below expire almost everything.
        source = [
            "-re",
            "-f", "lavfi",
            "-i", f"testsrc2=size={args.size}:rate={args.fps}",
        ]
    return [
        args.ffmpeg,
        "-hide_banner", "-loglevel", "error",
        *source,
        "-an",
        "-c:v", args.encoder,
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        # aud=1 marks every access unit so the Framer can find picture
        # boundaries; sliced-threads=0 keeps one slice per picture.
        "-x264-params", "aud=1:sliced-threads=0",
        "-g", str(args.keyframe_interval),
        "-b:v", args.bitrate,
        "-f", "h264",
        "-",
    ]


class VideoSession:
    """One subscriber: a video Connection plus a telemetry Connection."""

    def __init__(self, connection, args):
        self.video = connection
        self.telemetry = None
        self.args = args
        self.sequence = 0
        self.sent = 0
        self.expired = 0
        self.keyframes = 0
        self.bytes_sent = 0
        self.started_at = time.monotonic()

    async def start(self):
        # On QUIC a Connection is a stream, which only exists once a peer
        # writes, so the subscriber announces itself first. RFC 9622
        # Section 6.2.18 describes the opposite pattern, where the initiating
        # side reads before it writes.
        request = await self.video.receive(timeout=self.args.subscribe_timeout)
        logger.info("Subscribe request: %s", bytes(request.data).decode(errors="replace"))

        self.video.on_sent(self._on_sent)
        self.video.on_expired(self._on_expired)
        self.video.on_send_error(self._on_send_error)
        self.video.on_closed(self._on_closed)

        # Low Latency/Interactive asks the stack to trade capacity for
        # response time; on QUIC and TCP this also sets the recommended DSCP.
        self.video.set_property("connCapacityProfile", "Low Latency/Interactive")
        # Media is useless late, so the video Connection outranks telemetry.
        self.video.set_property("connPriority", 0)

        if self.args.telemetry:
            try:
                self.telemetry = await self.video.clone()
                self.telemetry.set_property("connPriority", 10)
                logger.info(
                    "Opened a telemetry Connection in the same group "
                    "(scheduler=%s)",
                    self.video.get_property("connScheduler"),
                )
            except Exception as error:
                logger.warning("Telemetry Connection unavailable: %s", error)
                self.telemetry = None

        await self._pump()

    async def _on_sent(self, message_reference, connection):
        self.sent += 1

    async def _on_expired(self, message_reference, connection):
        # RFC 9622 Section 9.1.3.1: the frame waited longer than its Lifetime,
        # so the stack dropped it instead of sending it late.
        self.expired += 1

    async def _on_send_error(self, message_reference, error, connection):
        logger.warning("Send error: %s", error)

    async def _on_closed(self, connection):
        logger.info("Subscriber closed the Connection.")

    async def _pump(self):
        command = build_encoder_command(self.args)
        logger.info("Starting encoder: %s", " ".join(command))
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assembler = AccessUnitAssembler()
        report_at = time.monotonic() + self.args.report_interval
        try:
            while True:
                chunk = await process.stdout.read(65536)
                if not chunk:
                    break
                for access_unit, keyframe in assembler.feed(chunk):
                    await self._send_access_unit(access_unit, keyframe)
                if time.monotonic() >= report_at:
                    await self._report()
                    report_at = time.monotonic() + self.args.report_interval
                if self.video.state is not taps.ConnectionState.ESTABLISHED:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Streaming stopped: %s", error)
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
            await self._report(final=True)

    async def _send_access_unit(self, access_unit, keyframe):
        if not access_unit:
            return
        if self.video.state is not taps.ConnectionState.ESTABLISHED:
            return

        self.sequence += 1
        context = self.video.new_message_context()
        # A keyframe every other frame depends on cannot be dropped; a delta
        # frame is worthless once the next one is due.
        context.set_property("msgPriority", 0 if keyframe else 10)
        if not keyframe and self.args.frame_lifetime > 0:
            context.set_property("msgLifetime", self.args.frame_lifetime)
        context.add(self._framer_key(), "keyframe", keyframe)
        context.add(self._framer_key(), "sequence", self.sequence)
        context.add(self._framer_key(), "capturedAt", int(time.time() * 1_000_000))

        await self.video.send(access_unit, context)
        self.bytes_sent += len(access_unit)
        if keyframe:
            self.keyframes += 1

    def _framer_key(self):
        return "example.h264"

    async def _report(self, *, final=False):
        elapsed = max(time.monotonic() - self.started_at, 1e-9)
        line = (
            f"frames={self.sequence} keyframes={self.keyframes} "
            f"sent={self.sent} expired={self.expired} "
            f"{self.bytes_sent * 8 / elapsed / 1e6:.2f} Mbit/s"
        )
        logger.info("%s%s", "final " if final else "", line)
        if self.telemetry is not None and (
            self.telemetry.state is taps.ConnectionState.ESTABLISHED
        ):
            context = self.telemetry.new_message_context()
            context.set_property("msgLifetime", 1.0)
            context.add(self._framer_key(), "sequence", self.sequence)
            try:
                await self.telemetry.send(line.encode(), context)
            except Exception:
                pass


async def main(args):
    properties = taps.TransportProperties()
    properties.require("reliability")
    properties.require("preserveOrder")
    # Multistreaming lets the telemetry Clone share one QUIC association.
    properties.require("multistreaming")
    properties.set_property("connCapacityProfile", "Low Latency/Interactive")

    security = taps.SecurityParameters()
    security.add_identity(args.identity)
    security.set_alpn_protocols([args.alpn])

    local = taps.LocalEndpoint()
    local.with_address(args.local_address)
    local.with_port(args.port)

    preconnection = taps.Preconnection(
        local_endpoints=[local],
        transport_properties=properties,
        security_parameters=security,
    )
    preconnection.add_framer(H264AccessUnitFramer())

    sessions = []

    async def on_connection_received(connection):
        logger.info("Subscriber connected over %s.", connection.protocol)
        session = VideoSession(connection, args)
        sessions.append(session)
        try:
            await session.start()
        except Exception:
            logger.exception("Session failed")
        finally:
            connection.close()

    preconnection.on_connection_received(on_connection_received)
    listener = await preconnection.listen(timeout=10)
    logger.info(
        "Streaming %s on %s:%s (ALPN %s). Press Ctrl-C to stop.",
        args.input or f"testsrc2 {args.size}@{args.fps}",
        args.local_address,
        args.port,
        args.alpn,
    )
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await listener.stop()


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-address", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4460)
    parser.add_argument(
        "--identity",
        default=str(ROOT / "tests" / "keys" / "localhost.pem"),
        help="Certificate and key PEM for the QUIC Listener.",
    )
    parser.add_argument("--alpn", default="pytaps-video")
    parser.add_argument(
        "--input",
        default=None,
        help="Media file to loop. Defaults to an ffmpeg test pattern.",
    )
    parser.add_argument("--size", default="640x480")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--bitrate", default="1M")
    parser.add_argument(
        "--keyframe-interval",
        type=int,
        default=60,
        help="Keyframe interval in frames.",
    )
    parser.add_argument(
        "--frame-lifetime",
        type=float,
        default=0.5,
        help="msgLifetime for delta frames in seconds; 0 disables expiry.",
    )
    parser.add_argument("--encoder", default="libx264")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--report-interval", type=float, default=5.0)
    parser.add_argument("--subscribe-timeout", type=float, default=10.0)
    parser.add_argument(
        "--no-telemetry",
        dest="telemetry",
        action="store_false",
        help="Do not open the second, lower-priority Connection.",
    )
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
