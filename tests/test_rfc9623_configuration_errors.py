"""RFC 9623 Section 3.1: configuration-time errors fail during preestablishment.

Appendix A.2 of RFC 9622 sanctions reporting these synchronously, as "an
exception when attempting to initiate a Connection with inconsistent Transport
Properties".
"""
import asyncio

import pytest

import pytaps as taps
from pytaps.utility import describe_unsatisfiable_properties


def _remote():
    return taps.RemoteEndpoint().with_address("192.0.2.10").with_port(443)


def _local():
    return taps.LocalEndpoint().with_address("127.0.0.1").with_port(0)


def _preconnection(properties, *, local=False, remote=True):
    return taps.Preconnection(
        local_endpoint=_local() if local else None,
        remote_endpoint=_remote() if remote else None,
        transport_properties=properties,
        event_loop=asyncio.new_event_loop(),
    )


# --- the two cases Section 3.1 calls out ---


@pytest.mark.asyncio
async def test_section_3_1_property_no_protocol_offers_is_rejected():
    """"...requires perMsgReliability, but no such feature is available..."""
    properties = taps.TransportProperties()
    properties.require("perMsgReliability")
    preconnection = taps.Preconnection(
        remote_endpoint=_remote(),
        transport_properties=properties,
    )

    with pytest.raises(
        taps.UnsatisfiableTransportProperties,
        match="perMsgReliability",
    ):
        await preconnection.initiate()


@pytest.mark.asyncio
async def test_section_3_1_conflicting_properties_are_rejected():
    """"...prohibits reliability but then requires perMsgReliability..."""
    properties = taps.TransportProperties()
    properties.prohibit("reliability")
    properties.require("perMsgReliability")
    preconnection = taps.Preconnection(
        remote_endpoint=_remote(),
        transport_properties=properties,
    )

    with pytest.raises(taps.UnsatisfiableTransportProperties):
        await preconnection.initiate()


@pytest.mark.asyncio
async def test_section_3_1_unsatisfiable_combination_is_rejected():
    """Each constraint is satisfiable alone; no one protocol offers both."""
    properties = taps.TransportProperties()
    properties.require("reliability")
    properties.require("preserveMsgBoundaries")
    preconnection = taps.Preconnection(
        remote_endpoint=_remote(),
        transport_properties=properties,
    )

    with pytest.raises(
        taps.UnsatisfiableTransportProperties,
        match="require preserveMsgBoundaries, require reliability",
    ):
        await preconnection.initiate()


# --- "fail as early as possible": no Connection or Listener is allocated ---


@pytest.mark.asyncio
async def test_section_3_1_failure_allocates_no_connection():
    properties = taps.TransportProperties()
    properties.require("perMsgReliability")
    preconnection = taps.Preconnection(
        remote_endpoint=_remote(),
        transport_properties=properties,
    )
    context = preconnection.connection_context
    before = context.get_snapshot()["connectionCounts"]

    with pytest.raises(taps.UnsatisfiableTransportProperties):
        await preconnection.initiate()

    after = context.get_snapshot()["connectionCounts"]
    assert after == before, "a doomed attempt must not allocate a Connection"
    assert after["active"] == 0


@pytest.mark.asyncio
async def test_section_3_1_listen_and_rendezvous_fail_early_too():
    properties = taps.TransportProperties()
    properties.require("perMsgReliability")

    listen_pre = taps.Preconnection(
        local_endpoint=_local(),
        transport_properties=properties,
    )
    with pytest.raises(taps.UnsatisfiableTransportProperties):
        await listen_pre.listen()

    rendezvous_pre = taps.Preconnection(
        local_endpoint=_local(),
        remote_endpoint=_remote(),
        transport_properties=properties,
    )
    with pytest.raises(taps.UnsatisfiableTransportProperties):
        await rendezvous_pre.rendezvous()


# --- satisfiable configurations must still be accepted ---


@pytest.mark.parametrize(
    "profile",
    ["reliable-inorder-stream", "unreliable-datagram"],
)
def test_section_3_1_supported_profiles_are_satisfiable(profile):
    properties = taps.TransportProperties().apply_profile(profile)

    assert describe_unsatisfiable_properties(
        properties,
        available_protocols={"tcp", "udp", "tls-tcp", "quic"},
    ) is None


def test_section_3_1_reliable_message_profile_needs_a_message_protocol():
    """reliable-message needs SCTP-like support, which this build lacks.

    Appendix B.2.2 of RFC 9622 lists the profile; Section 3.1 of RFC 9623 wants
    the resulting mismatch surfaced rather than discovered while racing.
    """
    properties = taps.TransportProperties().apply_profile("reliable-message")

    reason = describe_unsatisfiable_properties(
        properties,
        available_protocols={"tcp", "udp", "tls-tcp", "quic"},
    )

    assert reason is not None
    assert "preserveMsgBoundaries" in reason and "reliability" in reason


def test_section_3_1_no_available_protocol_is_reported():
    properties = taps.TransportProperties()

    assert describe_unsatisfiable_properties(
        properties,
        available_protocols=set(),
    ) == "no transport protocol is available"


def test_section_3_1_security_requirement_needs_a_secure_protocol():
    """Without a security context, TLS and QUIC are not available."""
    properties = taps.TransportProperties()
    properties.require("confidentiality")

    assert describe_unsatisfiable_properties(
        properties,
        available_protocols={"tcp", "udp"},
    ) is not None
    assert describe_unsatisfiable_properties(
        properties,
        available_protocols={"tcp", "udp", "tls-tcp"},
    ) is None
