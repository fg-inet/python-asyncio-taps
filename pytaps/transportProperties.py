from enum import Enum


class PreferenceLevel(Enum):
    REQUIRE = 2
    PREFER = 1
    IGNORE = 0
    AVOID = -1
    PROHIBIT = -2


PROPERTY_ALIASES = {
    "preserve-msg-boundaries": "preserveMsgBoundaries",
    "per-msg-reliability": "perMessageReliability",
    "preserve-order": "preserveOrder",
    "zero-rtt-msg": "zeroRttMsg",
    "per-msg-checksum-len-send": "fullChecksumSend",
    "per-msg-checksum-len-recv": "fullChecksumRecv",
    "congestion-control": "congestionControl",
    "soft-error-notify": "softErrorNotify",
    "retransmit-notify": "softErrorNotify",
    "unidirection-send": "Unidirectional Send",
    "unidirection-receive": "Unidirectional Receive",
    "bidirectional": "Bidirectional",
}


SELECTION_PROPERTY_DEFAULTS = {
    "reliability": PreferenceLevel.REQUIRE,
    "confidentiality": PreferenceLevel.IGNORE,
    "integrity": PreferenceLevel.IGNORE,
    "peerAuthentication": PreferenceLevel.IGNORE,
    "secureKeyExchange": PreferenceLevel.IGNORE,
    "preserveMsgBoundaries": PreferenceLevel.PREFER,
    "perMessageReliability": PreferenceLevel.IGNORE,
    "preserveOrder": PreferenceLevel.REQUIRE,
    "zeroRttMsg": PreferenceLevel.PREFER,
    "multistreaming": PreferenceLevel.PREFER,
    "fullChecksumSend": PreferenceLevel.REQUIRE,
    "fullChecksumRecv": PreferenceLevel.REQUIRE,
    "congestionControl": PreferenceLevel.REQUIRE,
    "keepAlive": PreferenceLevel.IGNORE,
    "interface": set(),
    "pvd": set(),
    "useTemporaryLocalAddress": PreferenceLevel.PREFER,
    "multipath": "Disabled",
    "advertisesAltaddr": False,
    "direction": "Bidirectional",
    "softErrorNotify": PreferenceLevel.IGNORE,
    "activeReadBeforeSend": PreferenceLevel.IGNORE,
}


CONNECTION_PROPERTY_DEFAULTS = {
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


PROTOCOLS = [
    {
        "name": "tcp",
        "reliability": True,
        "confidentiality": False,
        "integrity": False,
        "peerAuthentication": False,
        "secureKeyExchange": False,
        "preserveMsgBoundaries": False,
        "perMessageReliability": False,
        "preserveOrder": True,
        "zeroRttMsg": "optional",
        "multistreaming": False,
        "fullChecksumSend": True,
        "fullChecksumRecv": True,
        "congestionControl": True,
        "keepAlive": True,
        "multipath": "optional",
        "advertisesAltaddr": False,
        "softErrorNotify": True,
        "activeReadBeforeSend": True,
    },
    {
        "name": "udp",
        "reliability": False,
        "confidentiality": False,
        "integrity": False,
        "peerAuthentication": False,
        "secureKeyExchange": False,
        "preserveMsgBoundaries": True,
        "perMessageReliability": False,
        "preserveOrder": False,
        "zeroRttMsg": True,
        "multistreaming": False,
        "fullChecksumSend": True,
        "fullChecksumRecv": True,
        "congestionControl": False,
        "keepAlive": False,
        "multipath": False,
        "advertisesAltaddr": False,
        "softErrorNotify": True,
        "activeReadBeforeSend": True,
    },
    {
        "name": "tls-tcp",
        "reliability": True,
        "confidentiality": True,
        "integrity": True,
        "peerAuthentication": True,
        "secureKeyExchange": True,
        "preserveMsgBoundaries": False,
        "perMessageReliability": False,
        "preserveOrder": True,
        "zeroRttMsg": True,
        "multistreaming": False,
        "fullChecksumSend": True,
        "fullChecksumRecv": True,
        "congestionControl": True,
        "keepAlive": True,
        "multipath": False,
        "advertisesAltaddr": False,
        "softErrorNotify": False,
        "activeReadBeforeSend": True,
    },
    {
        "name": "quic",
        "reliability": True,
        "confidentiality": True,
        "integrity": True,
        "peerAuthentication": True,
        "secureKeyExchange": True,
        "preserveMsgBoundaries": False,
        "perMessageReliability": False,
        "preserveOrder": True,
        "zeroRttMsg": True,
        "multistreaming": True,
        "fullChecksumSend": True,
        "fullChecksumRecv": True,
        "congestionControl": True,
        "keepAlive": True,
        "multipath": False,
        "advertisesAltaddr": True,
        "softErrorNotify": True,
        "activeReadBeforeSend": True,
    },
]


def canonicalize_property_name(prop):
    return PROPERTY_ALIASES.get(prop, prop)


def normalize_direction(value):
    if value is None:
        return SELECTION_PROPERTY_DEFAULTS["direction"]
    return PROPERTY_ALIASES.get(value, value)


def get_protocols():
    return [protocol.copy() for protocol in PROTOCOLS]


class TransportProperties:
    """Handle TAPS Selection and Connection Properties."""

    def __init__(self, selection_properties=None, connection_properties=None):
        self.selection_properties = {
            key: (value.copy() if isinstance(value, set) else value)
            for key, value in SELECTION_PROPERTY_DEFAULTS.items()
        }
        self.connection_properties = CONNECTION_PROPERTY_DEFAULTS.copy()
        self.explicit_selection_properties = set()
        self.explicit_connection_properties = set()

        if selection_properties:
            for prop, value in selection_properties.items():
                self.set_property(prop, value)
        if connection_properties:
            for prop, value in connection_properties.items():
                self.set_property(prop, value)

    @property
    def properties(self):
        return self.selection_properties

    def set_property(self, prop, value):
        canonical = canonicalize_property_name(prop)
        if canonical == "direction":
            value = normalize_direction(value)
        if canonical in self.connection_properties:
            self.connection_properties[canonical] = value
            self.explicit_connection_properties.add(canonical)
        else:
            self.selection_properties[canonical] = value
            self.explicit_selection_properties.add(canonical)

    def add(self, prop, value):
        self.set_property(prop, value)

    def get_property(self, prop, default=None):
        return self.get(prop, default)

    def default_property(self, prop):
        self.default(prop)
        return self

    def require(self, prop):
        canonical = canonicalize_property_name(prop)
        self.selection_properties[canonical] = PreferenceLevel.REQUIRE
        self.explicit_selection_properties.add(canonical)

    def prefer(self, prop):
        canonical = canonicalize_property_name(prop)
        self.selection_properties[canonical] = PreferenceLevel.PREFER
        self.explicit_selection_properties.add(canonical)

    def ignore(self, prop):
        canonical = canonicalize_property_name(prop)
        self.selection_properties[canonical] = PreferenceLevel.IGNORE
        self.explicit_selection_properties.add(canonical)

    def avoid(self, prop):
        canonical = canonicalize_property_name(prop)
        self.selection_properties[canonical] = PreferenceLevel.AVOID
        self.explicit_selection_properties.add(canonical)

    def prohibit(self, prop):
        canonical = canonicalize_property_name(prop)
        self.selection_properties[canonical] = PreferenceLevel.PROHIBIT
        self.explicit_selection_properties.add(canonical)

    def default(self, prop):
        canonical = canonicalize_property_name(prop)
        if canonical in SELECTION_PROPERTY_DEFAULTS:
            default_value = SELECTION_PROPERTY_DEFAULTS[canonical]
            self.selection_properties[canonical] = (
                default_value.copy() if isinstance(default_value, set) else default_value
            )
            self.explicit_selection_properties.discard(canonical)
            return
        if canonical in CONNECTION_PROPERTY_DEFAULTS:
            self.connection_properties[canonical] = CONNECTION_PROPERTY_DEFAULTS[canonical]
            self.explicit_connection_properties.discard(canonical)
            return
        raise KeyError(f"Unknown transport property: {prop}")

    def get(self, prop, default=None):
        canonical = canonicalize_property_name(prop)
        if canonical in self.selection_properties:
            return self.selection_properties.get(canonical, default)
        return self.connection_properties.get(canonical, default)

    def get_selection_properties(self):
        return self.selection_properties.copy()

    def get_connection_properties(self):
        return self.connection_properties.copy()

    def get_explicit_selection_properties(self):
        return set(self.explicit_selection_properties)

    def get_explicit_connection_properties(self):
        return set(self.explicit_connection_properties)

    def get_properties(self):
        return {
            "selection": self.get_selection_properties(),
            "connection": self.get_connection_properties(),
            "explicitSelection": self.get_explicit_selection_properties(),
            "explicitConnection": self.get_explicit_connection_properties(),
        }

    def add_interface_preference(self, interface_id, preference):
        self.selection_properties["interface"].add((preference, interface_id))

    def add_pvd_preference(self, pvd_id, preference):
        self.selection_properties["pvd"].add((preference, pvd_id))

    def apply_profile(self, profile_name):
        normalized = profile_name.replace("_", "-").lower()
        if normalized == "reliable-inorder-stream":
            self.require("reliability")
            self.require("preserveOrder")
            self.prohibit("preserveMsgBoundaries")
            self.require("congestionControl")
            return self
        if normalized == "reliable-message":
            self.require("reliability")
            self.require("preserveMsgBoundaries")
            self.require("preserveOrder")
            self.require("congestionControl")
            return self
        if normalized == "unreliable-datagram":
            self.prohibit("reliability")
            self.require("preserveMsgBoundaries")
            self.ignore("preserveOrder")
            self.ignore("congestionControl")
            return self
        raise KeyError(f"Unknown transport property profile: {profile_name}")

    def reliable_inorder_stream(self):
        return self.apply_profile("reliable-inorder-stream")

    def reliable_message(self):
        return self.apply_profile("reliable-message")

    def unreliable_datagram(self):
        return self.apply_profile("unreliable-datagram")
