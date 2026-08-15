import asyncio

import pytest

import pytaps as taps


P = taps.PreferenceLevel


def test_section_6_2_selection_property_defaults():
    """RFC 9622 Sections 6.2.1-6.2.18 define these Initiate defaults."""
    properties = taps.TransportProperties()

    expected = {
        "reliability": P.REQUIRE,
        "preserveMsgBoundaries": P.IGNORE,
        "perMsgReliability": P.IGNORE,
        "preserveOrder": P.REQUIRE,
        "zeroRttMsg": P.IGNORE,
        "multistreaming": P.PREFER,
        "fullChecksumSend": P.REQUIRE,
        "fullChecksumRecv": P.REQUIRE,
        "congestionControl": P.REQUIRE,
        "keepAlive": P.IGNORE,
        "interface": set(),
        "pvd": set(),
        "useTemporaryLocalAddress": P.PREFER,
        "multipath": "Disabled",
        "advertisesAltaddr": False,
        "direction": "Bidirectional",
        "softErrorNotify": P.IGNORE,
        "activeReadBeforeSend": P.IGNORE,
    }

    for name, value in expected.items():
        assert properties.get(name) == value


def test_sections_6_2_13_and_6_2_14_action_specific_defaults():
    """Listener and Rendezvous defaults differ from Initiate defaults."""
    properties = taps.TransportProperties()

    listener = properties.for_action("listen")
    rendezvous = properties.for_action("rendezvous")

    assert listener.get("useTemporaryLocalAddress") is P.AVOID
    assert listener.get("multipath") == "Passive"
    assert rendezvous.get("useTemporaryLocalAddress") is P.AVOID
    assert rendezvous.get("multipath") == "Disabled"
    assert properties.get("useTemporaryLocalAddress") is P.PREFER
    assert properties.get("multipath") == "Disabled"


def test_action_defaults_do_not_override_explicit_values():
    properties = taps.TransportProperties()
    properties.require("useTemporaryLocalAddress")
    properties.set_property("multipath", "Active")

    listener = properties.for_action("listen")

    assert listener.get("useTemporaryLocalAddress") is P.REQUIRE
    assert listener.get("multipath") == "Active"


def test_section_6_2_rejects_unknown_names_and_invalid_types():
    properties = taps.TransportProperties()

    with pytest.raises(KeyError):
        properties.set_property("preserveMessageBoundaries", P.REQUIRE)
    with pytest.raises(ValueError):
        properties.set_property("reliability", "sometimes")
    with pytest.raises(ValueError):
        properties.set_property("multipath", "Simultaneous")
    with pytest.raises(ValueError):
        properties.set_property("advertisesAltaddr", 1)
    with pytest.raises(ValueError):
        properties.set_property("direction", "Simplex")


def test_sections_8_1_and_8_2_connection_property_defaults():
    properties = taps.TransportProperties()

    assert properties.get_connection_properties() == {
        "recvChecksumLen": "Full Coverage",
        "connPriority": 100,
        "connTimeout": "Disabled",
        "keepAliveTimeout": "Disabled",
        "connScheduler": "Weighted Fair Queueing",
        "connCapacityProfile": "Default",
        "multipathPolicy": "Handover",
        "minSendRate": "Unlimited",
        "minRecvRate": "Unlimited",
        "maxSendRate": "Unlimited",
        "maxRecvRate": "Unlimited",
        "groupConnLimit": "Unlimited",
        "isolateSession": False,
        "tcp.userTimeoutValue": None,
        "tcp.userTimeoutEnabled": False,
        "tcp.userTimeoutChangeable": True,
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("recvChecksumLen", -1),
        ("connPriority", -1),
        ("connTimeout", 0),
        ("keepAliveTimeout", -1),
        ("connScheduler", ""),
        ("connCapacityProfile", "Bulk"),
        ("multipathPolicy", "Redundant"),
        ("minSendRate", 0),
        ("maxRecvRate", -1),
        ("groupConnLimit", 0),
        ("isolateSession", 1),
        ("tcp.userTimeoutValue", 0),
        ("tcp.userTimeoutValue", 1.5),
        ("tcp.userTimeoutEnabled", "yes"),
    ],
)
def test_sections_8_1_and_8_2_validate_connection_properties(name, value):
    with pytest.raises(ValueError):
        taps.TransportProperties().set_property(name, value)


def test_section_9_1_3_message_property_defaults():
    context = taps.MessageContext()

    assert {
        "msgLifetime": context.get("msgLifetime"),
        "msgPriority": context.get("msgPriority"),
        "msgOrdered": context.get("msgOrdered"),
        "safelyReplayable": context.get("safelyReplayable"),
        "final": context.get("final"),
        "msgChecksumLen": context.get("msgChecksumLen"),
        "msgReliable": context.get("msgReliable"),
        "msgCapacityProfile": context.get("msgCapacityProfile"),
        "noFragmentation": context.get("noFragmentation"),
        "noSegmentation": context.get("noSegmentation"),
    } == {
        "msgLifetime": "Infinite",
        "msgPriority": 100,
        "msgOrdered": None,
        "safelyReplayable": False,
        "final": False,
        "msgChecksumLen": "Full Coverage",
        "msgReliable": None,
        "msgCapacityProfile": None,
        "noFragmentation": False,
        "noSegmentation": False,
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("msgLifetime", 0),
        ("msgPriority", -1),
        ("msgOrdered", 1),
        ("safelyReplayable", "yes"),
        ("final", None),
        ("msgChecksumLen", -1),
        ("msgReliable", "yes"),
        ("msgCapacityProfile", "Bulk"),
        ("noFragmentation", 1),
        ("noSegmentation", 1),
    ],
)
def test_section_9_1_3_validates_message_properties(name, value):
    with pytest.raises(ValueError):
        taps.MessageContext().set_property(name, value)


def test_section_9_1_3_rejects_unknown_and_receive_only_properties():
    context = taps.MessageContext()

    with pytest.raises(KeyError):
        context.set_property("messageUrgency", 1)
    with pytest.raises(KeyError):
        context.set_property("isEarlyData", True)
    with pytest.raises(KeyError):
        context.set_property("ecn", 1)


def test_section_9_1_3_infinite_lifetime_and_capacity_profile_values():
    context = taps.MessageContext()

    context.set_property("msgLifetime", "Infinite")
    context.set_property("msgCapacityProfile", "Low Latency/Interactive")

    assert context.lifetime is None
    assert context.capacity_profile == "Low Latency/Interactive"


def test_appendix_b_1_preference_convenience_actions():
    properties = taps.TransportProperties()

    assert properties.require("keepAlive").get("keepAlive") is P.REQUIRE
    assert properties.prefer("keepAlive").get("keepAlive") is P.PREFER
    assert properties.no_preference("keepAlive").get("keepAlive") is P.IGNORE
    assert properties.avoid("keepAlive").get("keepAlive") is P.AVOID
    assert properties.prohibit("keepAlive").get("keepAlive") is P.PROHIBIT

    # Appendix B.1: NoPreference(x) is equivalent to Set(x, "No Preference").
    equivalent = taps.TransportProperties()
    equivalent.set_property("keepAlive", "No Preference")

    assert taps.TransportProperties().no_preference("keepAlive").get(
        "keepAlive"
    ) == equivalent.get("keepAlive")


def test_appendix_b_2_transport_property_profiles():
    stream = taps.TransportProperties().reliable_inorder_stream()
    message = taps.TransportProperties().reliable_message()
    datagram = taps.TransportProperties().unreliable_datagram()

    assert {
        name: stream.get(name)
        for name in (
            "reliability",
            "preserveOrder",
            "congestionControl",
            "preserveMsgBoundaries",
        )
    } == {
        "reliability": P.REQUIRE,
        "preserveOrder": P.REQUIRE,
        "congestionControl": P.REQUIRE,
        "preserveMsgBoundaries": P.IGNORE,
    }
    assert {
        name: message.get(name)
        for name in (
            "reliability",
            "preserveOrder",
            "congestionControl",
            "preserveMsgBoundaries",
        )
    } == {
        "reliability": P.REQUIRE,
        "preserveOrder": P.REQUIRE,
        "congestionControl": P.REQUIRE,
        "preserveMsgBoundaries": P.REQUIRE,
    }
    assert {
        name: datagram.get(name)
        for name in (
            "reliability",
            "preserveOrder",
            "congestionControl",
            "preserveMsgBoundaries",
        )
    } == {
        "reliability": P.AVOID,
        "preserveOrder": P.AVOID,
        "congestionControl": P.IGNORE,
        "preserveMsgBoundaries": P.REQUIRE,
    }
    assert datagram.get_profile_message_properties() == {
        "safelyReplayable": True
    }


def test_appendix_b_2_datagram_profile_sets_message_default():
    loop = asyncio.new_event_loop()
    properties = taps.TransportProperties().unreliable_datagram()
    preconnection = taps.Preconnection(
        remote_endpoints=[
            taps.RemoteEndpoint().with_address("192.0.2.1").with_port(7)
        ],
        transport_properties=properties,
        event_loop=loop,
    )

    assert preconnection.get_property("safelyReplayable") is True
    loop.close()


def test_section_6_2_selection_properties_are_read_only_on_connections():
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoints=[
            taps.RemoteEndpoint().with_address("192.0.2.1").with_port(7)
        ],
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)

    with pytest.raises(ValueError):
        connection.set_property("reliability", P.AVOID)
    with pytest.raises(ValueError):
        connection.default_property("preserveOrder")

    loop.close()


def test_section_6_2_established_property_query_shape():
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        local_endpoints=[taps.LocalEndpoint().with_interface("en0")],
        remote_endpoints=[
            taps.RemoteEndpoint().with_address("192.0.2.1").with_port(7)
        ],
        event_loop=loop,
    )
    connection = taps.Connection(preconnection)
    connection.protocol = "tcp"
    connection.state = taps.ConnectionState.ESTABLISHED

    selection = connection.get_properties()["selection"]

    assert selection["reliability"] is True
    assert selection["preserveMsgBoundaries"] is False
    assert selection["perMsgReliability"] is False
    assert selection["multipath"] == "Disabled"
    assert selection["advertisesAltaddr"] is False
    assert selection["direction"] == "Bidirectional"
    assert selection["interface"] == set()
    loop.close()
