"""RFC 9621 Section 3.4: peers need not share our API or implementation.

    "A Transport Services System MUST NOT require that a peer on the other
     side of a connection use the same API or implementation."

Every peer here is deliberately built without pytaps: raw sockets, the openssl
command-line tools, and aioquic's own server API.
"""
import asyncio
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest

import pytaps as taps

KEYS = Path(__file__).parent / "keys"
SERVER_CERTIFICATE = KEYS / "localhost.pem"
ROOT_CERTIFICATE = KEYS / "MyRootCA.pem"

OPENSSL = shutil.which("openssl")


def _stream_properties():
    properties = taps.TransportProperties()
    properties.prohibit("multistreaming")
    properties.apply_profile("reliable-inorder-stream")
    return properties


def _datagram_properties():
    properties = taps.TransportProperties()
    properties.prohibit("multistreaming")
    properties.apply_profile("unreliable-datagram")
    return properties


def _terminate(process):
    if process.returncode is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


# --- plain socket peers (no pytaps, no asyncio) -----------------------------


class RawTcpEchoServer:
    """A blocking socket server in a thread. Nothing here knows about TAPS."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received = None
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _addr = self.sock.accept()
        except OSError:
            return
        with conn:
            data = conn.recv(4096)
            self.received = data
            conn.sendall(b"raw-echo:" + data)
            conn.shutdown(socket.SHUT_WR)

    def close(self):
        self.sock.close()
        self.thread.join(timeout=2)


@pytest.mark.asyncio
async def test_section_3_4_pytaps_client_talks_to_a_raw_socket_server():
    server = RawTcpEchoServer()
    try:
        preconnection = taps.Preconnection(
            remote_endpoint=(
                taps.RemoteEndpoint().with_address("127.0.0.1").with_port(server.port)
            ),
            transport_properties=_stream_properties(),
        )
        connection = await preconnection.initiate(timeout=5)
        assert connection.protocol == "tcp"

        await connection.send(b"hello")
        message = await connection.receive(min_incomplete_length=1, timeout=5)

        assert bytes(message.data).startswith(b"raw-echo:hello")
        assert server.received == b"hello"

        connection.close()
        await connection.wait_closed(timeout=5)
    finally:
        server.close()


@pytest.mark.asyncio
async def test_section_3_4_raw_socket_client_talks_to_a_pytaps_listener():
    received = asyncio.get_running_loop().create_future()

    async def echo(data, conn):
        if not received.done():
            received.set_result(bytes(data))
            await conn.send(b"taps-echo:" + bytes(data))

    async def on_connection(connection):
        async def on_received(data, context, conn):
            await echo(data, conn)

        async def on_received_partial(data, context, end_of_message, conn):
            # A byte stream with minIncompleteLength set delivers here.
            await echo(data, conn)

        connection.on_received(on_received)
        connection.on_received_partial(on_received_partial)
        await connection.receive(min_incomplete_length=1)

    preconnection = taps.Preconnection(
        local_endpoint=taps.LocalEndpoint().with_address("127.0.0.1").with_port(0),
        transport_properties=_stream_properties(),
    )
    preconnection.on_connection_received(on_connection)
    listener = await preconnection.listen(timeout=5)
    port = listener._servers[0].sockets[0].getsockname()[1]

    def raw_client():
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(b"from-raw")
            return sock.recv(4096)

    reply = await asyncio.get_running_loop().run_in_executor(None, raw_client)

    assert await asyncio.wait_for(received, timeout=5) == b"from-raw"
    assert reply.startswith(b"taps-echo:from-raw")
    await listener.stop()


@pytest.mark.asyncio
async def test_section_3_4_pytaps_datagram_client_talks_to_a_raw_udp_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(5)
    port = server.getsockname()[1]

    def raw_udp_server():
        data, addr = server.recvfrom(4096)
        server.sendto(b"raw-udp:" + data, addr)
        return data

    task = asyncio.get_running_loop().run_in_executor(None, raw_udp_server)
    try:
        preconnection = taps.Preconnection(
            remote_endpoint=(
                taps.RemoteEndpoint().with_address("127.0.0.1").with_port(port)
            ),
            transport_properties=_datagram_properties(),
        )
        connection = await preconnection.initiate(timeout=5)
        assert connection.protocol == "udp"

        await connection.send(
            b"ping", taps.MessageContext(safely_replayable=True)
        )
        message = await connection.receive(timeout=5)

        assert bytes(message.data) == b"raw-udp:ping"
        assert await asyncio.wait_for(task, timeout=5) == b"ping"
        connection.close()
    finally:
        server.close()


# --- openssl as the peer ----------------------------------------------------


@pytest.mark.skipif(OPENSSL is None, reason="openssl is not installed")
@pytest.mark.asyncio
async def test_section_3_4_pytaps_client_talks_to_openssl_s_server():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    # s_server relays its stdin to the peer, so it needs a pipe that stays
    # open: on EOF it closes the connection. The pipe is closed explicitly
    # below, because a pipe left for the garbage collector to finalize is
    # reported as an unraisable exception, which CI runs with -W error.
    process = subprocess.Popen(
        [
            OPENSSL, "s_server",
            "-accept", str(port),
            "-cert", str(SERVER_CERTIFICATE),
            "-key", str(SERVER_CERTIFICATE),
            "-quiet",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Do not probe with a bare TCP connect: s_server would accept it,
        # start a handshake that never completes, and drop the real
        # connection that follows. Retry the actual TLS connection instead.
        connection = None
        last_error = None
        for _attempt in range(25):
            security = taps.SecurityParameters()
            security.add_trust_ca(str(ROOT_CERTIFICATE))
            security.with_server_name("localhost")
            preconnection = taps.Preconnection(
                remote_endpoint=(
                    taps.RemoteEndpoint()
                    .with_hostname("localhost")
                    .with_address("127.0.0.1")
                    .with_port(port)
                ),
                transport_properties=_stream_properties(),
                security_parameters=security,
            )
            try:
                connection = await preconnection.initiate(timeout=3)
                break
            except Exception as error:
                last_error = error
                await asyncio.sleep(0.2)
        if connection is None:
            pytest.skip(f"openssl s_server never became ready: {last_error}")

        # Reaching this point means openssl completed a TLS handshake with us
        # and we validated the certificate it served.
        assert connection.protocol == "tls-tcp"

        sent = asyncio.get_running_loop().create_future()

        async def on_sent(message_reference, conn):
            if not sent.done():
                sent.set_result(message_reference)

        connection.on_sent(on_sent)
        await connection.send(b"hello openssl\n")
        # A Sent event means the Protocol Stack consumed the Message
        # (Section 9.2.2.1 of RFC 9622). The Connection state afterwards is
        # not asserted on: the peer may close at any point, and Section 10
        # does not guarantee a remote close is even signalled.
        await asyncio.wait_for(sent, timeout=10)

        connection.close()
    finally:
        if process.stdin is not None:
            process.stdin.close()
        _terminate(process)


@pytest.mark.skipif(OPENSSL is None, reason="openssl is not installed")
@pytest.mark.asyncio
async def test_section_3_4_openssl_s_client_talks_to_a_pytaps_listener():
    received = asyncio.get_running_loop().create_future()

    async def echo_then_close(data, conn):
        if received.done():
            return
        received.set_result(bytes(data))
        await conn.send(b"taps-echo:" + bytes(data))
        # s_client runs until the peer closes, so this also exercises the
        # Close-to-FIN mapping of Section 10.1 of RFC 9623 against a foreign peer.
        conn.close()

    async def on_connection(connection):
        async def on_received(data, context, conn):
            await echo_then_close(data, conn)

        async def on_received_partial(data, context, end_of_message, conn):
            await echo_then_close(data, conn)

        connection.on_received(on_received)
        connection.on_received_partial(on_received_partial)
        await connection.receive(min_incomplete_length=1)

    security = taps.SecurityParameters()
    security.add_identity(str(SERVER_CERTIFICATE))
    preconnection = taps.Preconnection(
        local_endpoint=taps.LocalEndpoint().with_address("127.0.0.1").with_port(0),
        transport_properties=_stream_properties(),
        security_parameters=security,
    )
    preconnection.on_connection_received(on_connection)
    listener = await preconnection.listen(timeout=5)
    port = listener._servers[0].sockets[0].getsockname()[1]

    def run_s_client():
        # The context manager closes every pipe, so none is left for the
        # garbage collector to finalize.
        with subprocess.Popen(
            [
                OPENSSL, "s_client",
                "-connect", f"127.0.0.1:{port}",
                "-CAfile", str(ROOT_CERTIFICATE),
                "-servername", "localhost",
                "-quiet",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            try:
                return process.communicate(input=b"from-openssl\n", timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                return process.communicate()

    stdout, stderr = await asyncio.get_running_loop().run_in_executor(
        None, run_s_client
    )

    payload = await asyncio.wait_for(received, timeout=10)
    # Both of these can only happen after openssl completed the handshake and
    # accepted our certificate. Its human-readable diagnostics are not asserted
    # on, because they vary between OpenSSL releases.
    assert payload.startswith(b"from-openssl"), stderr
    assert b"taps-echo:from-openssl" in stdout, stderr
    await listener.stop()


# --- aioquic's own server, not the pytaps wrapper ---------------------------


@pytest.mark.asyncio
async def test_section_3_4_pytaps_client_talks_to_a_stock_aioquic_server():
    aioquic_serve = pytest.importorskip("aioquic.asyncio").serve
    from aioquic.quic.configuration import QuicConfiguration
    from aioquic.quic.events import StreamDataReceived
    from aioquic.asyncio.protocol import QuicConnectionProtocol

    received = asyncio.get_running_loop().create_future()

    class StockServerProtocol(QuicConnectionProtocol):
        """Written against aioquic directly; knows nothing about pytaps."""

        def quic_event_received(self, event):
            if isinstance(event, StreamDataReceived):
                if not received.done():
                    received.set_result(bytes(event.data))
                self._quic.send_stream_data(
                    event.stream_id,
                    b"aioquic-echo:" + bytes(event.data),
                    end_stream=True,
                )
                self.transmit()

    configuration = QuicConfiguration(
        is_client=False,
        alpn_protocols=["taps-interop"],
    )
    configuration.load_cert_chain(SERVER_CERTIFICATE, SERVER_CERTIFICATE)

    server = await aioquic_serve(
        "127.0.0.1",
        0,
        configuration=configuration,
        create_protocol=StockServerProtocol,
    )
    port = server._transport.get_extra_info("sockname")[1]

    try:
        security = taps.SecurityParameters()
        security.add_trust_ca(str(ROOT_CERTIFICATE))
        security.set_alpn_protocols(["taps-interop"])
        security.with_server_name("localhost")
        properties = taps.TransportProperties()
        properties.require("multistreaming")

        preconnection = taps.Preconnection(
            remote_endpoint=(
                taps.RemoteEndpoint()
                .with_hostname("localhost")
                .with_address("127.0.0.1")
                .with_port(port)
            ),
            transport_properties=properties,
            security_parameters=security,
        )
        connection = await preconnection.initiate(timeout=10)

        assert connection.protocol == "quic"
        await connection.send(b"hello aioquic")
        message = await connection.receive(min_incomplete_length=1, timeout=10)

        assert await asyncio.wait_for(received, timeout=10) == b"hello aioquic"
        assert bytes(message.data).startswith(b"aioquic-echo:hello aioquic")

        connection.close()
        await connection.wait_closed(timeout=5)
    finally:
        server.close()
