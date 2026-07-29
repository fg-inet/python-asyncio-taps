import logging
import struct

import pytaps as taps


class LengthPrefixFramer(taps.Framer):
    """Encode a fixed metadata header followed by the Message payload."""

    HEADER = struct.Struct("!IBI")
    HEADER_LENGTH = HEADER.size
    MAX_MESSAGE_LENGTH = 16 * 1024 * 1024
    KIND_TO_CODE = {
        None: 0,
        "request": 1,
        "echo": 2,
    }
    CODE_TO_KIND = {
        value: key
        for key, value in KIND_TO_CODE.items()
    }

    def __init__(self):
        super().__init__(namespace="example.length-prefix")

    async def new_sent_message(
        self,
        connection,
        data,
        context,
        end_of_message,
    ):
        if not end_of_message:
            raise ValueError("LengthPrefixFramer requires complete Messages")
        if isinstance(data, str):
            data = data.encode()
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("LengthPrefixFramer payloads must be bytes-like")
        data = bytes(data)
        if len(data) > self.MAX_MESSAGE_LENGTH:
            raise ValueError("Message exceeds the Framer size limit")
        kind = context.get(self, "kind")
        if kind not in self.KIND_TO_CODE:
            raise ValueError(f"Unsupported Message kind: {kind}")
        sequence = context.get(self, "sequence", 0)
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or not 0 <= sequence <= 0xFFFFFFFF
        ):
            raise ValueError("Framer sequence must be a 32-bit unsigned Integer")
        return self.HEADER.pack(
            len(data),
            self.KIND_TO_CODE[kind],
            sequence,
        ) + data

    async def handle_received_data(self, connection):
        header, context, _ = self.parse(
            connection,
            minimum_incomplete_length=self.HEADER_LENGTH,
            maximum_length=self.HEADER_LENGTH,
        )
        if header is None:
            return

        length, kind_code, sequence = self.HEADER.unpack(header)
        if length > self.MAX_MESSAGE_LENGTH:
            self.fail_connection(
                connection,
                ValueError("Received Message exceeds the Framer size limit"),
            )
            return

        self.advance_receive_cursor(connection, self.HEADER_LENGTH)
        context.add(self, "payloadLength", length)
        kind = self.CODE_TO_KIND.get(kind_code)
        if kind is None and kind_code != 0:
            self.fail_connection(
                connection,
                ValueError(f"Received unsupported Message kind: {kind_code}"),
            )
            return
        if kind is not None:
            context.add(self, "kind", kind)
        if sequence:
            context.add(self, "sequence", sequence)
        self.deliver_and_advance_receive_cursor(
            connection,
            context,
            length,
            True,
        )


def configure_example_logging(verbose):
    level = logging.INFO if verbose else logging.WARNING
    for candidate in logging.Logger.manager.loggerDict.values():
        if not isinstance(candidate, logging.Logger):
            continue
        if not candidate.name.startswith("pytaps"):
            continue
        candidate.setLevel(level)
        for handler in candidate.handlers:
            handler.setLevel(level)
