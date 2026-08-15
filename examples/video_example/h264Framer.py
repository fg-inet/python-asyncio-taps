import logging
import struct

import pytaps as taps


logger = logging.getLogger("H264 Framer")


class H264AccessUnitFramer(taps.Framer):
    """Delimit H.264 access units on a byte-stream Connection.

    QUIC streams do not preserve Message boundaries, so a Message Framer
    (Section 9.1.2 of RFC 9622) restores them. The header also carries the
    metadata the receiver needs to reason about a frame it has not decoded
    yet, exposed through the namespaced Framer metadata of Section 9.1.2.2.
    """

    # payload length, flags, sequence, capture timestamp in microseconds
    HEADER = struct.Struct("!IBIQ")
    HEADER_LENGTH = HEADER.size
    MAX_ACCESS_UNIT = 8 * 1024 * 1024

    FLAG_KEYFRAME = 0x01
    FLAG_CONTROL = 0x02

    def __init__(self):
        super().__init__(namespace="example.h264")

    async def new_sent_message(self, connection, data, context, end_of_message):
        if not end_of_message:
            raise ValueError("H264AccessUnitFramer requires complete Messages")
        if isinstance(data, str):
            data = data.encode()
        data = bytes(data)
        if len(data) > self.MAX_ACCESS_UNIT:
            raise ValueError("Access unit exceeds the Framer size limit")

        flags = 0
        if context.get(self, "keyframe", False):
            flags |= self.FLAG_KEYFRAME
        if context.get(self, "control", False):
            flags |= self.FLAG_CONTROL
        sequence = int(context.get(self, "sequence", 0)) & 0xFFFFFFFF
        captured_at = int(context.get(self, "capturedAt", 0)) & 0xFFFFFFFFFFFFFFFF
        return self.HEADER.pack(len(data), flags, sequence, captured_at) + data

    async def handle_received_data(self, connection):
        header, context, _at_eof = self.parse(
            connection,
            minimum_incomplete_length=self.HEADER_LENGTH,
            maximum_length=self.HEADER_LENGTH,
        )
        if header is None:
            return

        length, flags, sequence, captured_at = self.HEADER.unpack(header)
        if length > self.MAX_ACCESS_UNIT:
            self.fail_connection(
                connection,
                taps.DeframingFailed("Access unit exceeds the Framer limit"),
            )
            return

        # Drop the header, then earmark the payload so the Connection delivers
        # exactly one access unit as one Message.
        self.advance_receive_cursor(connection, self.HEADER_LENGTH)
        context.add(self, "keyframe", bool(flags & self.FLAG_KEYFRAME))
        context.add(self, "control", bool(flags & self.FLAG_CONTROL))
        context.add(self, "sequence", sequence)
        context.add(self, "capturedAt", captured_at)
        self.deliver_and_advance_receive_cursor(
            connection,
            context,
            length,
            True,
        )


# --- Annex-B access unit assembly -------------------------------------------

_VCL_NAL_TYPES = frozenset({1, 2, 3, 4, 5})
_KEYFRAME_NAL_TYPES = frozenset({5, 7, 8})
# An access unit starts at these when a picture has already been buffered.
_LEADING_NAL_TYPES = frozenset({6, 7, 8, 9})


def split_annex_b(buffer):
    """Yield (nal_unit_with_start_code, nal_type) from an Annex-B buffer.

    Returns the trailing bytes that do not yet form a complete NAL unit, so a
    caller can carry them into the next read.
    """
    units = []
    start = buffer.find(b"\x00\x00\x01")
    if start < 0:
        return units, buffer

    while True:
        # Include a four-byte start code when one is present.
        begin = start - 1 if start > 0 and buffer[start - 1] == 0 else start
        following = buffer.find(b"\x00\x00\x01", start + 3)
        if following < 0:
            return units, buffer[begin:]
        end = following - 1 if buffer[following - 1] == 0 else following
        unit = buffer[begin:end]
        payload_start = start + 3
        if payload_start < len(buffer):
            units.append((unit, buffer[payload_start] & 0x1F))
        start = following


class AccessUnitAssembler:
    """Group Annex-B NAL units into access units, one Message per picture."""

    _ACCESS_UNIT_DELIMITER = 9

    def __init__(self):
        self._pending = bytearray()
        self._units = []
        self._has_picture = False
        self._keyframe = False
        self._delimited = False

    def feed(self, chunk):
        """Feed encoder output and yield (access_unit, is_keyframe) tuples.

        An encoder may split one picture across several slice NAL units, so a
        new VCL unit does not by itself mean a new picture. When the stream
        carries access unit delimiters those are authoritative; otherwise fall
        back to treating each VCL unit as its own picture.
        """
        self._pending.extend(chunk)
        units, remainder = split_annex_b(bytes(self._pending))
        self._pending = bytearray(remainder)

        for unit, nal_type in units:
            if nal_type == self._ACCESS_UNIT_DELIMITER:
                self._delimited = True

            if self._delimited:
                starts_new = self._units and nal_type == self._ACCESS_UNIT_DELIMITER
            else:
                starts_new = self._has_picture and (
                    nal_type in _LEADING_NAL_TYPES or nal_type in _VCL_NAL_TYPES
                )
            if starts_new:
                yield self._flush()

            self._units.append(unit)
            if nal_type in _KEYFRAME_NAL_TYPES:
                self._keyframe = True
            if nal_type in _VCL_NAL_TYPES:
                self._has_picture = True

    def _flush(self):
        access_unit = b"".join(self._units)
        keyframe = self._keyframe
        self._units = []
        self._has_picture = False
        self._keyframe = False
        return access_unit, keyframe

    def drain(self):
        """Return any buffered access unit at end of stream."""
        if not self._units:
            return None
        self._units.append(bytes(self._pending))
        self._pending = bytearray()
        return self._flush()
