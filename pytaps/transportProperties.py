from enum import Enum


class PreferenceLevel(Enum):
    REQUIRE = 2
    PREFER = 1
    IGNORE = 0
    AVOID = -1
    PROHIBIT = -2


PROPERTY_ALIASES = {
    "preserve-msg-boundaries": "preserveMsgBoundaries",
    "per-msg-reliability": "perMsgReliability",
    "perMessageReliability": "perMsgReliability",
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


PREFERENCE_SELECTION_PROPERTIES = frozenset(
    {
        "reliability",
        "preserveMsgBoundaries",
        "perMsgReliability",
        "preserveOrder",
        "zeroRttMsg",
        "multistreaming",
        "fullChecksumSend",
        "fullChecksumRecv",
        "congestionControl",
        "keepAlive",
        "useTemporaryLocalAddress",
        "softErrorNotify",
        "activeReadBeforeSend",
        # Security feature properties are implementation extensions used to
        # prevent an insecure candidate from satisfying Security Parameters.
        "confidentiality",
        "integrity",
        "peerAuthentication",
        "secureKeyExchange",
    }
)

SET_SELECTION_PROPERTIES = frozenset({"interface", "pvd"})

SELECTION_PROPERTY_DEFAULTS = {
    "reliability": PreferenceLevel.REQUIRE,
    "preserveMsgBoundaries": PreferenceLevel.IGNORE,
    "perMsgReliability": PreferenceLevel.IGNORE,
    "preserveOrder": PreferenceLevel.REQUIRE,
    "zeroRttMsg": PreferenceLevel.IGNORE,
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
    "confidentiality": PreferenceLevel.IGNORE,
    "integrity": PreferenceLevel.IGNORE,
    "peerAuthentication": PreferenceLevel.IGNORE,
    "secureKeyExchange": PreferenceLevel.IGNORE,
}

ACTION_SELECTION_DEFAULTS = {
    "initiate": {
        "useTemporaryLocalAddress": PreferenceLevel.PREFER,
        "multipath": "Disabled",
    },
    "listen": {
        "useTemporaryLocalAddress": PreferenceLevel.AVOID,
        "multipath": "Passive",
    },
    "rendezvous": {
        "useTemporaryLocalAddress": PreferenceLevel.AVOID,
        "multipath": "Disabled",
        "activeReadBeforeSend": PreferenceLevel.IGNORE,
    },
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

CONNECTION_ENUM_VALUES = {
    "connCapacityProfile": {
        "Default",
        "Scavenger",
        "Low Latency/Interactive",
        "Low Latency/Non-Interactive",
        "Constant-Rate Streaming",
        "Capacity-Seeking",
    },
    "multipathPolicy": {
        "Handover",
        "Interactive",
        "Aggregate",
    },
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
        "perMsgReliability": False,
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
        "perMsgReliability": False,
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
        "perMsgReliability": False,
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
        "perMsgReliability": False,
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
    if not isinstance(prop, str):
        raise TypeError("Transport Property names must be strings")
    return PROPERTY_ALIASES.get(prop, prop)


def normalize_direction(value):
    if value is None:
        return SELECTION_PROPERTY_DEFAULTS["direction"]
    normalized = PROPERTY_ALIASES.get(value, value)
    if isinstance(normalized, str):
        by_lowercase = {
            "bidirectional": "Bidirectional",
            "unidirectional send": "Unidirectional Send",
            "unidirectional receive": "Unidirectional Receive",
        }
        normalized = by_lowercase.get(normalized.lower(), normalized)
    if normalized not in {
        "Bidirectional",
        "Unidirectional Send",
        "Unidirectional Receive",
    }:
        raise ValueError(f"Invalid direction value: {value}")
    return normalized


def _copy_value(value):
    return value.copy() if isinstance(value, set) else value


def _normalize_preference(value):
    if isinstance(value, PreferenceLevel):
        return value
    if isinstance(value, str):
        normalized = value.replace("_", " ").replace("-", " ").strip().lower()
        preferences = {
            "require": PreferenceLevel.REQUIRE,
            "prefer": PreferenceLevel.PREFER,
            "no preference": PreferenceLevel.IGNORE,
            "ignore": PreferenceLevel.IGNORE,
            "avoid": PreferenceLevel.AVOID,
            "prohibit": PreferenceLevel.PROHIBIT,
        }
        if normalized in preferences:
            return preferences[normalized]
    raise ValueError(f"Invalid Preference value: {value}")


def _normalize_multipath(value):
    if not isinstance(value, str):
        raise ValueError(f"Invalid multipath value: {value}")
    normalized = value.strip().lower()
    values = {
        "disabled": "Disabled",
        "active": "Active",
        "passive": "Passive",
    }
    if normalized not in values:
        raise ValueError(f"Invalid multipath value: {value}")
    return values[normalized]


def get_protocols():
    return [protocol.copy() for protocol in PROTOCOLS]


class TransportProperties:
    """Handle RFC 9622 Selection and Connection Properties."""

    def __init__(
        self,
        selection_properties=None,
        connection_properties=None,
        *,
        action="initiate",
    ):
        if action not in ACTION_SELECTION_DEFAULTS:
            raise ValueError(f"Unknown preestablishment action: {action}")
        self.action = action
        self.selection_properties = {
            key: _copy_value(value)
            for key, value in SELECTION_PROPERTY_DEFAULTS.items()
        }
        self.connection_properties = CONNECTION_PROPERTY_DEFAULTS.copy()
        self.profile_message_properties = {}
        self.explicit_selection_properties = set()
        self.explicit_connection_properties = set()
        self._apply_action_defaults(action)

        if selection_properties:
            for prop, value in selection_properties.items():
                self.set_property(prop, value)
        if connection_properties:
            for prop, value in connection_properties.items():
                self.set_property(prop, value)

    @property
    def properties(self):
        return self.selection_properties

    def _apply_action_defaults(self, action):
        for prop, value in ACTION_SELECTION_DEFAULTS[action].items():
            if prop not in self.explicit_selection_properties:
                self.selection_properties[prop] = _copy_value(value)

    def for_action(self, action):
        if action not in ACTION_SELECTION_DEFAULTS:
            raise ValueError(f"Unknown preestablishment action: {action}")
        cloned = self.clone()
        cloned.action = action
        cloned._apply_action_defaults(action)
        if action == "rendezvous":
            cloned.selection_properties["activeReadBeforeSend"] = PreferenceLevel.IGNORE
        return cloned

    def clone(self):
        cloned = object.__new__(TransportProperties)
        cloned.action = self.action
        cloned.selection_properties = {
            key: _copy_value(value)
            for key, value in self.selection_properties.items()
        }
        cloned.connection_properties = self.connection_properties.copy()
        cloned.profile_message_properties = self.profile_message_properties.copy()
        cloned.explicit_selection_properties = set(self.explicit_selection_properties)
        cloned.explicit_connection_properties = set(self.explicit_connection_properties)
        return cloned

    def _normalize_selection_value(self, prop, value):
        if prop in PREFERENCE_SELECTION_PROPERTIES:
            return _normalize_preference(value)
        if prop in SET_SELECTION_PROPERTIES:
            if not isinstance(value, (set, list, tuple)):
                raise ValueError(f"{prop} must be a set of (Preference, value) pairs")
            normalized = set()
            for preference, identifier in value:
                normalized.add((_normalize_preference(preference), identifier))
            return normalized
        if prop == "multipath":
            return _normalize_multipath(value)
        if prop == "advertisesAltaddr":
            if not isinstance(value, bool):
                raise ValueError("advertisesAltaddr must be a Boolean")
            return value
        if prop == "direction":
            return normalize_direction(value)
        raise KeyError(f"Unknown Selection Property: {prop}")

    def _validate_connection_value(self, prop, value):
        if prop in {"connPriority"}:
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{prop} must be a non-negative Integer")
        elif prop in {"connTimeout", "keepAliveTimeout"}:
            if value != "Disabled" and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value <= 0
            ):
                raise ValueError(f"{prop} must be positive or Disabled")
        elif prop in {
            "minSendRate",
            "minRecvRate",
            "maxSendRate",
            "maxRecvRate",
            "groupConnLimit",
        }:
            if value != "Unlimited" and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value <= 0
            ):
                raise ValueError(f"{prop} must be positive or Unlimited")
        elif prop == "recvChecksumLen":
            if value != "Full Coverage" and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise ValueError(
                    "recvChecksumLen must be a non-negative Integer or Full Coverage"
                )
        elif prop in {
            "isolateSession",
            "tcp.userTimeoutEnabled",
            "tcp.userTimeoutChangeable",
        } and not isinstance(value, bool):
            raise ValueError(f"{prop} must be a Boolean")
        elif prop == "tcp.userTimeoutValue" and value is not None and (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
        ):
            raise ValueError(
                "tcp.userTimeoutValue must be a positive Integer or None"
            )
        elif prop == "connScheduler" and (
            not isinstance(value, str) or not value.strip()
        ):
            raise ValueError("connScheduler must be a non-empty Enumeration")
        elif (
            prop in CONNECTION_ENUM_VALUES
            and value not in CONNECTION_ENUM_VALUES[prop]
        ):
            raise ValueError(f"Invalid {prop} value: {value}")
        return value

    def set_property(self, prop, value):
        canonical = canonicalize_property_name(prop)
        if canonical in SELECTION_PROPERTY_DEFAULTS:
            self.selection_properties[canonical] = self._normalize_selection_value(
                canonical,
                value,
            )
            self.explicit_selection_properties.add(canonical)
            return self
        if canonical in CONNECTION_PROPERTY_DEFAULTS:
            self.connection_properties[canonical] = self._validate_connection_value(
                canonical,
                value,
            )
            self.explicit_connection_properties.add(canonical)
            return self
        raise KeyError(f"Unknown Transport Property: {prop}")

    def add(self, prop, value):
        return self.set_property(prop, value)

    def get_property(self, prop, default=None):
        return self.get(prop, default)

    def default_property(self, prop):
        self.default(prop)
        return self

    def _set_preference(self, prop, preference):
        canonical = canonicalize_property_name(prop)
        if canonical not in PREFERENCE_SELECTION_PROPERTIES:
            if canonical not in SELECTION_PROPERTY_DEFAULTS:
                raise KeyError(f"Unknown Selection Property: {prop}")
            raise TypeError(f"{canonical} is not a Preference Selection Property")
        self.selection_properties[canonical] = preference
        self.explicit_selection_properties.add(canonical)
        return self

    def require(self, prop):
        return self._set_preference(prop, PreferenceLevel.REQUIRE)

    def prefer(self, prop):
        return self._set_preference(prop, PreferenceLevel.PREFER)

    def ignore(self, prop):
        return self._set_preference(prop, PreferenceLevel.IGNORE)

    def avoid(self, prop):
        return self._set_preference(prop, PreferenceLevel.AVOID)

    def prohibit(self, prop):
        return self._set_preference(prop, PreferenceLevel.PROHIBIT)

    def default(self, prop):
        canonical = canonicalize_property_name(prop)
        if canonical in SELECTION_PROPERTY_DEFAULTS:
            default_value = ACTION_SELECTION_DEFAULTS[self.action].get(
                canonical,
                SELECTION_PROPERTY_DEFAULTS[canonical],
            )
            self.selection_properties[canonical] = _copy_value(default_value)
            self.explicit_selection_properties.discard(canonical)
            return self
        if canonical in CONNECTION_PROPERTY_DEFAULTS:
            self.connection_properties[canonical] = CONNECTION_PROPERTY_DEFAULTS[
                canonical
            ]
            self.explicit_connection_properties.discard(canonical)
            return self
        raise KeyError(f"Unknown Transport Property: {prop}")

    def get(self, prop, default=None):
        canonical = canonicalize_property_name(prop)
        if canonical in self.selection_properties:
            return self.selection_properties[canonical]
        if canonical in self.connection_properties:
            return self.connection_properties[canonical]
        return default

    def get_selection_properties(self):
        return {
            key: _copy_value(value)
            for key, value in self.selection_properties.items()
        }

    def get_connection_properties(self):
        return self.connection_properties.copy()

    def get_explicit_selection_properties(self):
        return set(self.explicit_selection_properties)

    def get_explicit_connection_properties(self):
        return set(self.explicit_connection_properties)

    def get_profile_message_properties(self):
        return self.profile_message_properties.copy()

    def get_properties(self):
        return {
            "selection": self.get_selection_properties(),
            "connection": self.get_connection_properties(),
            "profileMessage": self.get_profile_message_properties(),
            "explicitSelection": self.get_explicit_selection_properties(),
            "explicitConnection": self.get_explicit_connection_properties(),
        }

    def add_interface_preference(self, interface_id, preference):
        self.selection_properties["interface"].add(
            (_normalize_preference(preference), interface_id)
        )
        self.explicit_selection_properties.add("interface")
        return self

    def add_pvd_preference(self, pvd_id, preference):
        self.selection_properties["pvd"].add(
            (_normalize_preference(preference), pvd_id)
        )
        self.explicit_selection_properties.add("pvd")
        return self

    def apply_profile(self, profile_name):
        normalized = profile_name.replace("_", "-").lower()
        if normalized == "reliable-inorder-stream":
            self.require("reliability")
            self.require("preserveOrder")
            self.require("congestionControl")
            self.ignore("preserveMsgBoundaries")
            return self
        if normalized == "reliable-message":
            self.require("reliability")
            self.require("preserveMsgBoundaries")
            self.require("preserveOrder")
            self.require("congestionControl")
            return self
        if normalized == "unreliable-datagram":
            self.avoid("reliability")
            self.avoid("preserveOrder")
            self.ignore("congestionControl")
            self.require("preserveMsgBoundaries")
            self.profile_message_properties["safelyReplayable"] = True
            return self
        raise KeyError(f"Unknown Transport Property profile: {profile_name}")

    def reliable_inorder_stream(self):
        return self.apply_profile("reliable-inorder-stream")

    def reliable_message(self):
        return self.apply_profile("reliable-message")

    def unreliable_datagram(self):
        return self.apply_profile("unreliable-datagram")
