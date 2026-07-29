import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytaps as taps  # noqa: E402


logger = taps.setup_logger("Mixed QUIC Client", "yellow")


def milliseconds(value):
    return round(value * 1000, 2) if value is not None else None


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


def quic_properties(*, enable_migration=False):
    properties = taps.TransportProperties()
    properties.require("multistreaming")
    properties.prefer("zeroRttMsg")
    properties.set_property("_pytaps.quicStreamType", "Bidirectional")
    if enable_migration:
        properties.set_property("multipath", "Active")
        properties.set_property("multipathPolicy", "Handover")
    return properties


def security_parameters(args):
    security = taps.SecurityParameters()
    security.add_trust_ca(args.trust_ca)
    security.with_server_name(args.server_name)
    security.set_alpn_protocols([args.alpn])
    return security


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


async def close_connections(*connections):
    close_tasks = [
        task
        for connection in connections
        if connection is not None
        for task in [connection.close()]
        if task is not None
    ]
    if close_tasks:
        await asyncio.gather(*close_tasks)


async def wait_for_session_ticket(preconnection, timeout):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        session_cache = (
            preconnection.get_connection_context()
            .get_quic_session_cache_snapshot()
        )
        if session_cache["clientTickets"]:
            return True
        await asyncio.sleep(0.01)
    return False


async def main(args):
    remote = (
        taps.RemoteEndpoint()
        .with_hostname(args.server_name)
        .with_address(args.remote_address)
        .with_port(args.port)
    )
    preconnection = taps.Preconnection(
        remote_endpoint=remote,
        transport_properties=quic_properties(
            enable_migration=args.migrate,
        ),
        security_parameters=security_parameters(args),
    )

    bidirectional = None
    unidirectional = None
    datagrams = None
    resumed = None
    try:
        bidirectional = await preconnection.initiate(timeout=args.timeout)
        logger.info(
            "association ready protocol=%s group=%s",
            bidirectional.protocol,
            id(bidirectional.connection_group),
        )

        bidi_payload = f"bidi:{args.payload}".encode()
        await send_and_wait(
            bidirectional,
            bidi_payload,
            bidirectional.new_message_context(final=True),
            args.timeout,
        )
        bidi_reply, _ = await receive_complete(
            bidirectional,
            args.timeout,
        )
        logger.info("bidirectional reply=%r", bidi_reply)

        unidirectional = await bidirectional.clone(
            connection_properties={
                "_pytaps.quicStreamType": "Unidirectional",
            }
        )
        uni_payload = f"uni:{args.payload}".encode()
        await send_and_wait(
            unidirectional,
            uni_payload,
            unidirectional.new_message_context(final=True),
            args.timeout,
        )
        logger.info(
            "unidirectional sent stream_id=%s direction=%s",
            unidirectional.transports[0].stream_id,
            unidirectional.get_property("direction"),
        )

        datagrams = await bidirectional.clone(
            connection_properties={
                "_pytaps.quicTransportMode": "Datagram",
            }
        )
        datagram_payload = f"datagram:{args.payload}".encode()
        await send_and_wait(
            datagrams,
            datagram_payload,
            datagrams.new_message_context(),
            args.timeout,
        )
        datagram_reply = await datagrams.receive(timeout=args.timeout)
        logger.info(
            "datagram reply=%r reliable=%s max_len=%s",
            datagram_reply.data,
            datagram_reply.get("msgReliable"),
            datagrams.get_properties()["readOnly"]["sendMsgMaxLen"],
        )

        assert (
            bidirectional.quic_association
            is unidirectional.quic_association
            is datagrams.quic_association
        )
        logger.info(
            "verified one association with %s TAPS Connections",
            bidirectional.get_properties()["readOnly"]["groupSize"],
        )
        association_snapshot = bidirectional.get_properties()["readOnly"][
            "quicAssociation"
        ]
        cached_performance = (
            preconnection.get_connection_context()
            .get_performance_metrics(
                protocol="quic",
            )
        )
        logger.info(
            "measured performance latest_rtt_ms=%s smoothed_rtt_ms=%s "
            "establishment_ms=%s success_rate=%s",
            milliseconds(
                association_snapshot["performance"]["latestRtt"]
            ),
            milliseconds(
                association_snapshot["performance"]["smoothedRtt"]
            ),
            milliseconds(
                cached_performance["establishmentLatency"]
                if cached_performance is not None
                else None
            ),
            (
                cached_performance["successRate"]
                if cached_performance is not None
                else None
            ),
        )

        if args.migrate:
            async def path_changed(previous, current, connection):
                logger.info(
                    "path changed mode=%s previous=%s current=%s",
                    connection.get_property("_pytaps.quicTransportMode"),
                    previous,
                    current,
                )

            for connection in (
                bidirectional,
                unidirectional,
                datagrams,
            ):
                connection.on_path_change(path_changed)

            migration_endpoint = taps.LocalEndpoint().with_port(
                args.migration_port
            )
            if args.migration_address:
                migration_endpoint.with_address(
                    args.migration_address
                )
            migrated_path = await bidirectional.migrate_path(
                migration_endpoint,
                timeout=args.timeout,
            )
            snapshot = bidirectional.get_properties()["readOnly"][
                "quicAssociation"
            ]
            logger.info(
                "validated QUIC handover current=%s validations=%s "
                "smoothed_rtt_ms=%s",
                migrated_path,
                snapshot["pathValidationSuccesses"],
                milliseconds(
                    snapshot["performance"]["smoothedRtt"]
                ),
            )

            await send_and_wait(
                datagrams,
                f"post-migration:{args.payload}".encode(),
                datagrams.new_message_context(),
                args.timeout,
            )
            migrated_reply = await datagrams.receive(
                timeout=args.timeout
            )
            logger.info(
                "post-migration datagram reply=%r",
                migrated_reply.data,
            )

        if not args.skip_zero_rtt:
            ticket_ready = await wait_for_session_ticket(
                preconnection,
                args.timeout,
            )
            if not ticket_ready:
                logger.warning(
                    "no session ticket arrived; the next "
                    "InitiateWithSend will use 1-RTT"
                )
            await close_connections(
                datagrams,
                unidirectional,
                bidirectional,
            )
            datagrams = None
            unidirectional = None
            bidirectional = None

            resumed = await preconnection.initiate_with_send(
                f"0rtt:{args.payload}".encode(),
                taps.MessageContext(
                    safely_replayable=True,
                    final=True,
                ),
                timeout=args.timeout,
            )
            zero_rtt_reply, _ = await receive_complete(
                resumed,
                args.timeout,
            )
            association = resumed.get_properties()["readOnly"][
                "quicAssociation"
            ]
            logger.info(
                "0-RTT reply=%r resumed=%s attempted=%s "
                "accepted=%s rejected=%s",
                zero_rtt_reply,
                association["sessionResumed"],
                association["earlyDataAttempted"],
                association["earlyDataAccepted"],
                association["earlyDataRejected"],
            )
    finally:
        await close_connections(
            resumed,
            datagrams,
            unidirectional,
            bidirectional,
        )


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Send bidirectional stream, unidirectional stream, and QUIC "
            "DATAGRAM traffic over one PyTAPS QUIC association, then "
            "resume a second association with replay-safe 0-RTT."
        )
    )
    parser.add_argument("--remote-address", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4433)
    parser.add_argument("--server-name", default="localhost")
    parser.add_argument(
        "--trust-ca",
        default=str(ROOT / "tests" / "keys" / "MyRootCA.pem"),
    )
    parser.add_argument("--alpn", default="pytaps-mixed-quic")
    parser.add_argument("--payload", default="hello")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument(
        "--skip-zero-rtt",
        action="store_true",
        help="Skip the second, session-resuming InitiateWithSend.",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help=(
            "Validate a new local UDP path and hand over the live "
            "association before the 0-RTT reconnect."
        ),
    )
    parser.add_argument(
        "--migration-address",
        default=None,
        help=(
            "Local IP for --migrate. By default, rebind on the "
            "route-selected address."
        ),
    )
    parser.add_argument(
        "--migration-port",
        type=int,
        default=0,
        help="Local UDP port for --migrate (default: ephemeral).",
    )
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
    asyncio.run(main(arguments))
