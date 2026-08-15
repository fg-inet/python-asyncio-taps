"""RFC 9622 Section 6.3.8: Connection establishment callbacks."""
import asyncio
import ssl
import subprocess
from pathlib import Path

import pytest

import pytaps as taps


KEYS = Path(__file__).parent / "keys"
SERVER_CERTIFICATE = KEYS / "localhost.pem"
ROOT_CERTIFICATE = KEYS / "MyRootCA.pem"


def _tls_properties():
    properties = taps.TransportProperties()
    properties.prohibit("multistreaming")
    return properties


async def _tls_listener():
    server_security = taps.SecurityParameters()
    server_security.add_identity(str(SERVER_CERTIFICATE))
    server = taps.Preconnection(
        local_endpoints=[
            taps.LocalEndpoint().with_address("127.0.0.1").with_port(0)
        ],
        transport_properties=_tls_properties(),
        security_parameters=server_security,
    )
    listener = await server.listen(timeout=2)
    return listener, listener._servers[0].sockets[0].getsockname()[1]


# --- Configuration surface ---


def test_section_6_3_8_callbacks_are_configurable_and_reported():
    security = taps.SecurityParameters()

    def trust(chain):
        return True

    def challenge():
        return b"secret"

    assert security.set_trust_verification_callback(trust) is security
    assert security.set_identity_challenge_callback(challenge) is security

    configuration = security.get_configuration()
    assert configuration["trustVerificationCallback"] is trust
    assert configuration["identityChallengeCallback"] is challenge


def test_section_6_3_8_callbacks_default_to_unset():
    configuration = taps.SecurityParameters().get_configuration()

    assert configuration["trustVerificationCallback"] is None
    assert configuration["identityChallengeCallback"] is None


@pytest.mark.parametrize("value", [object(), "not-callable", 42])
def test_section_6_3_8_callbacks_must_be_callable(value):
    security = taps.SecurityParameters()

    with pytest.raises(TypeError):
        security.set_trust_verification_callback(value)
    with pytest.raises(TypeError):
        security.set_identity_challenge_callback(value)

    assert security.trust_verification_callback is None
    assert security.identity_challenge_callback is None


def test_section_6_3_8_callbacks_can_be_cleared():
    security = taps.SecurityParameters()
    security.set_trust_verification_callback(lambda chain: True)
    security.set_identity_challenge_callback(lambda: b"x")

    security.set_trust_verification_callback(None)
    security.set_identity_challenge_callback(None)

    assert security.trust_verification_callback is None
    assert security.identity_challenge_callback is None


# --- Trust verification semantics ---


def test_section_6_3_8_trust_verification_without_a_callback_accepts():
    assert taps.SecurityParameters().run_trust_verification([b"cert"]) is True


def test_section_6_3_8_trust_verification_receives_the_peer_chain():
    seen = []
    security = taps.SecurityParameters()
    security.set_trust_verification_callback(
        lambda chain: seen.append(chain) or True
    )

    assert security.run_trust_verification([b"leaf", b"intermediate"]) is True
    assert seen == [[b"leaf", b"intermediate"]]


def test_section_6_3_8_rejecting_trust_fails_establishment():
    security = taps.SecurityParameters()
    security.set_trust_verification_callback(lambda chain: False)

    with pytest.raises(ssl.SSLCertVerificationError, match="rejected"):
        security.run_trust_verification([b"leaf"])


def test_section_6_3_8_raising_in_the_callback_fails_establishment():
    security = taps.SecurityParameters()

    def trust(chain):
        raise ValueError("untrusted issuer")

    security.set_trust_verification_callback(trust)

    with pytest.raises(ssl.SSLCertVerificationError, match="untrusted issuer"):
        security.run_trust_verification([b"leaf"])


# --- Establishment integration over real TLS ---


@pytest.mark.asyncio
async def test_section_6_3_8_trust_callback_runs_during_tls_establishment():
    listener, port = await _tls_listener()
    presented = []

    security = taps.SecurityParameters()
    security.add_trust_ca(str(ROOT_CERTIFICATE))
    security.set_trust_verification_callback(
        lambda chain: presented.append(chain) or True
    )
    preconnection = taps.Preconnection(
        remote_endpoints=[
            taps.RemoteEndpoint()
            .with_hostname("localhost")
            .with_address("127.0.0.1")
            .with_port(port)
        ],
        transport_properties=_tls_properties(),
        security_parameters=security,
    )

    connection = await preconnection.initiate(timeout=2)

    assert connection.protocol == "tls-tcp"
    assert presented, "the trust callback must run before establishment finishes"
    assert all(isinstance(cert, bytes) for cert in presented[0])

    connection.close()
    await connection.wait_closed(timeout=2)
    await listener.stop()


@pytest.mark.asyncio
async def test_section_6_3_8_trust_callback_can_veto_an_otherwise_valid_peer():
    listener, port = await _tls_listener()

    security = taps.SecurityParameters()
    security.add_trust_ca(str(ROOT_CERTIFICATE))
    security.set_trust_verification_callback(lambda chain: False)
    preconnection = taps.Preconnection(
        remote_endpoints=[
            taps.RemoteEndpoint()
            .with_hostname("localhost")
            .with_address("127.0.0.1")
            .with_port(port)
        ],
        transport_properties=_tls_properties(),
        security_parameters=security,
    )

    with pytest.raises(ssl.SSLCertVerificationError, match="rejected"):
        await preconnection.initiate(timeout=2)

    await listener.stop()


# --- Identity challenge ---


PASSPHRASE = "pytaps-test"


@pytest.fixture(scope="module")
def protected_identity(tmp_path_factory):
    """A copy of the test identity whose private key needs a passphrase."""
    target = tmp_path_factory.mktemp("identity") / "protected.pem"
    result = subprocess.run(
        [
            "openssl",
            "rsa",
            "-in",
            str(SERVER_CERTIFICATE),
            "-aes256",
            "-passout",
            f"pass:{PASSPHRASE}",
            "-out",
            str(target),
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        pytest.skip("openssl is unavailable to build a passphrase-protected key")
    # Append the certificate so the file is a complete identity.
    target.write_bytes(target.read_bytes() + SERVER_CERTIFICATE.read_bytes())
    return target


def _listener_preconnection(security):
    return taps.Preconnection(
        local_endpoints=[taps.LocalEndpoint().with_address("127.0.0.1").with_port(0)],
        transport_properties=_tls_properties(),
        security_parameters=security,
        event_loop=asyncio.new_event_loop(),
        _security_role="listener",
    )


def test_section_6_3_8_identity_challenge_unlocks_a_protected_key(
    protected_identity,
):
    calls = []

    security = taps.SecurityParameters()
    security.add_identity(str(protected_identity))
    security.set_identity_challenge_callback(
        lambda: calls.append(True) or PASSPHRASE
    )

    preconnection = _listener_preconnection(security)

    assert preconnection.security_context is not None
    assert calls, "the identity challenge callback must be invoked"


def test_section_6_3_8_wrong_identity_challenge_answer_fails(protected_identity):
    security = taps.SecurityParameters()
    security.add_identity(str(protected_identity))
    security.set_identity_challenge_callback(lambda: "wrong-passphrase")

    with pytest.raises(ssl.SSLError):
        _listener_preconnection(security)


def test_section_6_3_8_unprotected_identity_never_challenges():
    calls = []

    security = taps.SecurityParameters()
    security.add_identity(str(SERVER_CERTIFICATE))
    security.set_identity_challenge_callback(lambda: calls.append(True) or "x")

    preconnection = _listener_preconnection(security)

    assert preconnection.security_context is not None
    assert calls == [], "an unprotected key needs no private key operation"
