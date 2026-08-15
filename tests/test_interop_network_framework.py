"""Interop with Apple's Network.framework, itself a Transport Services system.

Appendix C of RFC 9623 lists Network.framework as an existing Transport
Services implementation, so this is Section 3.4 of RFC 9621 between two
independent TAPS stacks:

    "A Transport Services System MUST NOT require that a peer on the other
     side of a connection use the same API or implementation."

The peer in `tests/interop/nwpeer.swift` is built on NWListener/NWConnection and
is compiled on demand. Everything here skips off Darwin or without a Swift
toolchain.

Three of the four directions run unattended. The fourth, our TLS client against
an NWListener, needs a PKCS#12 identity in the login keychain and is verified by
hand:

    openssl pkcs12 -export -out /tmp/nwpeer.p12 \
        -in tests/keys/localhost.pem -inkey tests/keys/localhost.pem \
        -passout pass:pytaps
    swiftc -O -o /tmp/nwpeer tests/interop/nwpeer.swift
    NWPEER_IDENTITY=/tmp/nwpeer.p12 NWPEER_P12_PASS=pytaps \
        /tmp/nwpeer listen tls 0
"""
import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import pytaps as taps

KEYS = Path(__file__).parent / "keys"
SOURCE = Path(__file__).parent / "interop" / "nwpeer.swift"
SERVER_CERTIFICATE = KEYS / "localhost.pem"
ROOT_CERTIFICATE = KEYS / "MyRootCA.pem"
P12_PASSPHRASE = "pytaps"

SWIFTC = shutil.which("swiftc")
OPENSSL = shutil.which("openssl")

pytestmark = [
    pytest.mark.skipif(
        not sys.platform.startswith("darwin"),
        reason="Network.framework is only available on Darwin",
    ),
    pytest.mark.skipif(SWIFTC is None, reason="swiftc is not installed"),
]


@pytest.fixture(scope="module")
def nwpeer(tmp_path_factory):
    """Compile the Network.framework peer once per run."""
    if not SOURCE.exists():
        pytest.skip(f"{SOURCE} is missing")
    binary = tmp_path_factory.mktemp("nwpeer") / "nwpeer"
    result = subprocess.run(
        [SWIFTC, "-O", "-o", str(binary), str(SOURCE)],
        capture_output=True,
        timeout=300,
    )
    if result.returncode != 0:
        pytest.skip(
            "could not build the Network.framework peer: "
            f"{result.stderr.decode(errors='replace')[-400:]}"
        )
    return binary


@pytest.fixture(scope="module")
def identity(tmp_path_factory):
    """A PKCS#12 identity for the Swift listener.

    SecPKCS12Import places the private key in the user's login keychain, and
    the key's ACL is bound to the code signature of the binary that imported
    it. This test compiles a fresh, ad-hoc signed peer, which the keychain sees
    as a different application, so using the key needs an authorization that
    cannot be granted in a non-interactive run and the TLS handshake stalls.

    The combination is verified by hand instead; see the module docstring. The
    reverse direction, Apple's TLS client against our TLS Listener, needs no
    identity import and runs below.
    """
    pytest.skip(
        "Network.framework TLS listeners need a keychain identity whose ACL "
        "is bound to the importing binary's code signature; a freshly built "
        "test peer cannot use it non-interactively"
    )


def _properties():
    properties = taps.TransportProperties()
    properties.prohibit("multistreaming")
    properties.apply_profile("reliable-inorder-stream")
    return properties


async def _start_nw_listener(nwpeer, *, mode, identity=None):
    """Start the Swift listener and wait for it to announce its port."""
    environment = None
    if identity is not None:
        import os

        environment = dict(os.environ)
        environment["NWPEER_IDENTITY"] = str(identity)
        environment["NWPEER_P12_PASS"] = P12_PASSPHRASE

    process = await asyncio.create_subprocess_exec(
        str(nwpeer), "listen", mode, "0",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    try:
        line = await asyncio.wait_for(process.stdout.readline(), timeout=30)
    except asyncio.TimeoutError:
        process.kill()
        pytest.skip("the Network.framework listener did not start")
    text = line.decode(errors="replace").strip()
    if not text.startswith("PORT "):
        process.kill()
        pytest.skip(f"unexpected listener output: {text}")
    return process, int(text.split()[1])


async def _stop(process):
    if process.returncode is None:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        process.kill()


# --- pytaps initiating to a Network.framework Listener ----------------------


@pytest.mark.asyncio
async def test_section_3_4_pytaps_client_to_network_framework_listener(nwpeer):
    process, port = await _start_nw_listener(nwpeer, mode="tcp")
    try:
        preconnection = taps.Preconnection(
            remote_endpoint=(
                taps.RemoteEndpoint().with_address("127.0.0.1").with_port(port)
            ),
            transport_properties=_properties(),
        )
        connection = await preconnection.initiate(timeout=15)

        assert connection.protocol == "tcp"
        await connection.send(b"pytaps-to-nw")
        message = await connection.receive(min_incomplete_length=1, timeout=15)

        assert bytes(message.data) == b"nw-echo:pytaps-to-nw"
        connection.close()
        await connection.wait_closed(timeout=10)
    finally:
        await _stop(process)


@pytest.mark.asyncio
async def test_section_3_4_pytaps_tls_client_to_network_framework_listener(
    nwpeer,
    identity,
):
    process, port = await _start_nw_listener(
        nwpeer, mode="tls", identity=identity
    )
    try:
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
            transport_properties=_properties(),
            security_parameters=security,
        )
        connection = await preconnection.initiate(timeout=20)

        # Our TLS client validated a certificate served by Network.framework.
        assert connection.protocol == "tls-tcp"
        await connection.send(b"pytaps-tls-to-nw")
        message = await connection.receive(min_incomplete_length=1, timeout=15)

        assert bytes(message.data) == b"nw-echo:pytaps-tls-to-nw"
        connection.close()
        await connection.wait_closed(timeout=10)
    finally:
        await _stop(process)


# --- Network.framework initiating to a pytaps Listener ----------------------


async def _pytaps_echo_listener(*, security=None):
    received = asyncio.get_running_loop().create_future()

    async def echo(data, connection):
        if received.done():
            return
        received.set_result(bytes(data))
        await connection.send(b"taps-echo:" + bytes(data))
        connection.close()

    async def on_connection(connection):
        async def on_received(data, context, conn):
            await echo(data, conn)

        async def on_received_partial(data, context, end_of_message, conn):
            await echo(data, conn)

        connection.on_received(on_received)
        connection.on_received_partial(on_received_partial)
        await connection.receive(min_incomplete_length=1)

    preconnection = taps.Preconnection(
        local_endpoint=taps.LocalEndpoint().with_address("127.0.0.1").with_port(0),
        transport_properties=_properties(),
        security_parameters=security,
    )
    preconnection.on_connection_received(on_connection)
    listener = await preconnection.listen(timeout=10)
    port = listener._servers[0].sockets[0].getsockname()[1]
    return listener, port, received


@pytest.mark.asyncio
async def test_section_3_4_network_framework_client_to_pytaps_listener(nwpeer):
    listener, port, received = await _pytaps_echo_listener()
    try:
        process = await asyncio.create_subprocess_exec(
            str(nwpeer), "connect", "tcp", "127.0.0.1", str(port), "nw-to-pytaps",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(), timeout=30
        )

        assert await asyncio.wait_for(received, timeout=10) == b"nw-to-pytaps"
        assert b"REPLY taps-echo:nw-to-pytaps" in stdout, stdout
    finally:
        await listener.stop()


@pytest.mark.asyncio
async def test_section_3_4_network_framework_tls_client_to_pytaps_listener(
    nwpeer,
):
    security = taps.SecurityParameters()
    security.add_identity(str(SERVER_CERTIFICATE))
    listener, port, received = await _pytaps_echo_listener(security=security)
    try:
        process = await asyncio.create_subprocess_exec(
            str(nwpeer), "connect", "tls", "127.0.0.1", str(port),
            "nw-tls-to-pytaps",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(), timeout=30
        )

        # Apple's TLS client completed a handshake against our TLS Listener.
        assert await asyncio.wait_for(received, timeout=10) == b"nw-tls-to-pytaps"
        assert b"REPLY taps-echo:nw-tls-to-pytaps" in stdout, stdout
    finally:
        await listener.stop()
