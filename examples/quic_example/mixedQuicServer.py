import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytaps as taps  # noqa: E402


logger = taps.setup_logger("Mixed QUIC Server", "cyan")


def quiet_library_logs():
    for name in (
        "pytaps.connection",
        "pytaps.listener",
        "pytaps.preconnection",
        "pytaps.securityParameters",
        "pytaps.transports",
        "quic",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)


def quic_properties():
    properties = taps.TransportProperties()
    properties.require("multistreaming")
    return properties


def security_parameters(args):
    security = taps.SecurityParameters()
    security.add_identity(args.identity)
    if args.private_key:
        security.add_private_key(args.private_key)
    security.set_alpn_protocols([args.alpn])
    return security


async def receive_complete(connection, timeout):
    chunks = []
    while True:
        message = await connection.receive(
            min_incomplete_length=1,
            max_length=65536,
            timeout=timeout,
        )
        chunks.append(message.data)
        if message.is_complete:
            return b"".join(chunks), message


async def send_and_wait(connection, data, context, timeout):
    loop = asyncio.get_running_loop()
    completion = loop.create_future()
    message_id = None

    async def sent(sent_context, _connection):
        if sent_context.message_id == message_id and not completion.done():
            completion.set_result(sent_context)

    async def send_error(error_context, reason, _connection):
        if error_context.message_id == message_id and not completion.done():
            completion.set_exception(reason)

    connection.on_sent(sent)
    connection.on_send_error(send_error)
    message_id = await connection.send(data, context)
    await asyncio.wait_for(completion, timeout)


class MixedQuicServer:
    def __init__(self, args):
        self.args = args
        self.connections = set()
        self.handlers = set()

    async def handle_connection(self, connection):
        self.connections.add(connection)
        mode = connection.get_property("_pytaps.quicTransportMode")
        direction = connection.get_property("direction")
        stream_id = getattr(connection.transports[0], "stream_id", None)

        async def path_changed(previous, current, _connection):
            logger.info(
                "validated peer path change previous=%s current=%s",
                previous,
                current,
            )

        connection.on_path_change(path_changed)
        logger.info(
            "accepted mode=%s direction=%s stream_id=%s group=%s",
            mode,
            direction,
            stream_id,
            id(connection.connection_group),
        )
        try:
            if mode == "Datagram":
                while True:
                    data, message = await receive_complete(
                        connection,
                        self.args.timeout,
                    )
                    logger.info(
                        "received mode=%s payload=%r reliable=%s "
                        "early_data=%s",
                        mode,
                        data,
                        connection.get_property("reliability"),
                        message.get("isEarlyData"),
                    )
                    await send_and_wait(
                        connection,
                        b"datagram-echo:" + data,
                        connection.new_message_context(),
                        self.args.timeout,
                    )
            else:
                data, message = await receive_complete(
                    connection,
                    self.args.timeout,
                )
                logger.info(
                    "received mode=%s payload=%r reliable=%s "
                    "early_data=%s",
                    mode,
                    data,
                    connection.get_property("reliability"),
                    message.get("isEarlyData"),
                )
                if direction == "Bidirectional":
                    await send_and_wait(
                        connection,
                        b"stream-echo:" + data,
                        connection.new_message_context(final=True),
                        self.args.timeout,
                    )
        except asyncio.CancelledError:
            raise
        except ConnectionError:
            logger.info(
                "connection closed mode=%s direction=%s",
                mode,
                direction,
            )
        except Exception:
            logger.exception(
                "connection handler failed mode=%s direction=%s",
                mode,
                direction,
            )

    def handler_done(self, task):
        self.handlers.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "connection handler stopped: %s",
                task.exception(),
            )

    async def close(self, listener):
        for task in self.handlers:
            task.cancel()
        if self.handlers:
            await asyncio.gather(*self.handlers, return_exceptions=True)
        close_tasks = [
            task
            for connection in self.connections
            for task in [connection.close()]
            if task is not None
        ]
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)
        await listener.stop()

    async def run(self):
        local = (
            taps.LocalEndpoint()
            .with_address(self.args.local_address)
            .with_port(self.args.port)
        )
        preconnection = taps.Preconnection(
            local_endpoint=local,
            transport_properties=quic_properties(),
            security_parameters=security_parameters(self.args),
        )
        listener = await preconnection.listen(timeout=self.args.timeout)
        logger.info(
            "listening for QUIC on %s:%s/udp",
            self.args.local_address,
            listener.quic_association.bound_port(),
        )
        try:
            while True:
                connection = await listener.accept()
                task = asyncio.create_task(
                    self.handle_connection(connection)
                )
                self.handlers.add(task)
                task.add_done_callback(self.handler_done)
        finally:
            await self.close(listener)


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Accept mixed QUIC stream directions and DATAGRAM traffic "
            "through PyTAPS."
        )
    )
    parser.add_argument("--local-address", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4433)
    parser.add_argument(
        "--identity",
        default=str(ROOT / "tests" / "keys" / "localhost.pem"),
        help="Certificate PEM, optionally containing its private key.",
    )
    parser.add_argument(
        "--private-key",
        default=None,
        help="Private-key PEM when it is not included in --identity.",
    )
    parser.add_argument("--alpn", default="pytaps-mixed-quic")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show PyTAPS and aioquic INFO logs.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    if not arguments.verbose:
        quiet_library_logs()
    try:
        asyncio.run(MixedQuicServer(arguments).run())
    except KeyboardInterrupt:
        logger.info("server stopped")
