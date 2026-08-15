# Live Video Over QUIC

A live H.264 sender and player built on PyTAPS. Each encoded picture is one
TAPS Message, which is what makes the Transport Services properties that matter
for media directly expressible.

Requires `ffmpeg` and `ffplay`, plus the QUIC extra:

```
python -m pip install -e '.[quic]'
```

## Running

Start the sender. With no `--input` it encodes an ffmpeg test pattern, so it
runs without a camera or media file:

```
python examples/video_example/videoServer.py --port 4460
```

Then play it:

```
python examples/video_example/videoClient.py --port 4460
```

Useful variations:

```
# Loop a file instead of the test pattern
python examples/video_example/videoServer.py --input movie.mp4

# Capture the stream instead of playing it, and stop after 240 pictures
python examples/video_example/videoClient.py --save out.h264 --frames 240

# Watch frames expire under a tight deadline
python examples/video_example/videoServer.py --frame-lifetime 0.02
```

Both sides print periodic statistics. A healthy loopback run looks like:

```
Video Server  final frames=241 keyframes=5 sent=241 expired=0 1.08 Mbit/s
Video Client  final frames=240 keyframes=4 missing=0 1.08 Mbit/s latency~2 ms
```

## What it demonstrates

**Capacity profile** (Section 8.1.6 of RFC 9622). Both ends set
`connCapacityProfile` to `Low Latency/Interactive`, which asks the system to
optimize response time at the expense of efficient capacity use. The QUIC and
TCP backends also apply the DSCP marking RFC 9622 recommends for that profile.

**Message priority and Connection priority** (Sections 9.1.3.2 and 9.2.6).
Keyframes are sent with `msgPriority` 0 and delta frames with 10, so a keyframe
never queues behind the deltas that depend on it. The sender also opens a
second, lower-priority Connection for telemetry by cloning the video one. Both
live in the same Connection Group, so the group's `connScheduler` orders them
and `connPriority` outranks `msgPriority`: telemetry yields to video regardless
of how each Message is marked.

**Message lifetime** (Section 9.1.3.1). Delta frames carry a `msgLifetime`
(`--frame-lifetime`, 0.5 s by default). A frame that cannot be sent before it
stops mattering is dropped rather than delivered late, and the sender learns
about it through an Expired event instead of a silent stall. Keyframes are sent
without a Lifetime, because dropping one breaks everything that references it.
Lower `--frame-lifetime` to watch the `expired` counter climb.

**Message framing** (Section 9.1.2). QUIC streams do not preserve Message
boundaries, so `h264Framer.py` supplies a Framer that length-prefixes each
access unit and carries a keyframe flag, a sequence number and a capture
timestamp as namespaced Framer metadata (Section 9.1.2.2). The player uses that
metadata to report loss and one-way latency without decoding anything, and the
receiving side gets exactly one picture per Received event.

**Who writes first** (Section 6.2.18). A PyTAPS Connection over QUIC is a
stream, and a stream only exists once a peer writes to it. The subscriber
therefore sends a short subscribe request before it starts reading, which is
also how a real media protocol announces which track it wants.

## Notes

The sender asks ffmpeg for access unit delimiters (`aud=1`) and a single slice
per picture (`sliced-threads=0`). Without the delimiters an encoder that splits
a picture across several slices would look like several pictures to the Framer,
and each slice would become its own Message.

The synthetic source is paced with `-re`. An unpaced encoder produces frames far
faster than real time, which fills the send queue and expires nearly everything.
