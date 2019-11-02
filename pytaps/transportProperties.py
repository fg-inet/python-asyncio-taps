from enum import Enum
import json


class PreferenceLevel(Enum):
    REQUIRE = 2
    PREFER = 1
    IGNORE = 0
    AVOID = -1
    PROHIBIT = -2


# TODO: Is this accurate? What properties
#       are actually supported by this implementation
def get_protocols():
    protocols = []
    tcp = """{
        "name": "tcp",
        "reliability": true,
        "preserve-msg-boundaries": false,
        "per-msg-reliability": false,
        "preserve-order": true,
        "zero-rtt-msg": "optional",
        "multistreaming": "optional",
        "per-msg-checksum-len-send": false,
        "per-msg-checksum-len-recv": false,
        "congestion-control": true,
        "multipath": "optional",
        "retransmit-notify": true,
        "soft-error-notify": true
    }"""
    udp = """{
        "name": "udp",
        "reliability": false,
        "preserve-msg-boundaries": true,
        "per-msg-reliability": false,
        "preserve-order": false,
        "zero-rtt-msg": true,
        "multistreaming": false,
        "per-msg-checksum-len-send": false,
        "per-msg-checksum-len-recv": false,
        "congestion-control": false,
        "multipath": false,
        "retransmit-notify": false,
        "soft-error-notify": true
    }"""
    tls_tcp = """{
        "name": "tls-tcp",
        "reliability": true,
        "preserve-msg-boundaries": false,
        "per-msg-reliability": false,
        "preserve-order": true,
        "zero-rtt-msg": true,
        "multistreaming": false,
        "per-msg-checksum-len-send": false,
        "per-msg-checksum-len-recv": false,
        "congestion-control": true,
        "multipath": false,
        "retransmit-notify": false,
        "soft-error-notify": false
    }"""
    dtls_udp = """{
        "name": "dtls-udp",
        "reliability": false,
        "preserve-msg-boundaries": true,
        "per-msg-reliability": false,
        "preserve-order": false,
        "zero-rtt-msg": false,
        "multistreaming": false,
        "per-msg-checksum-len-send": false,
        "per-msg-checksum-len-recv": false,
        "congestion-control": false,
        "multipath": false,
        "retransmit-notify": false,
        "soft-error-notify": true
    }"""
    sctp = """{
        "name": "sctp",
        "reliability": "optional",
        "preserve-msg-boundaries": true,
        "per-msg-reliability": true,
        "preserve-order": true,
        "zero-rtt-msg": false,
        "multistreaming": "optional",
        "per-msg-checksum-len-send": false,
        "per-msg-checksum-len-recv": false,
        "congestion-control": true,
        "multipath": "optional",
        "retransmit-notify": true,
        "soft-error-notify": false
    }"""
    quic = """{
        "name": "quic",
        "reliability": true,
        "preserve-msg-boundaries": false,
        "per-msg-reliability": false,
        "preserve-order": true,
        "zero-rtt-msg": "optional",
        "multistreaming": "optional",
        "per-msg-checksum-len-send": false,
        "per-msg-checksum-len-recv": false,
        "congestion-control": true,
        "multipath": false,
        "retransmit-notify": false,
        "soft-error-notify": true
    }"""
    mptcp = """{
        "name": "mptcp",
        "reliability": true,
        "preserve-msg-boundaries": false,
        "per-msg-reliability": false,
        "preserve-order": true,
        "zero-rtt-msg": true,
        "multistreaming": true,
        "per-msg-checksum-len-send": false,
        "per-msg-checksum-len-recv": false,
        "congestion-control": true,
        "multipath": true,
        "retransmit-notify": true,
        "soft-error-notify": true
    }"""
    protocols.append(json.loads(tcp))
    protocols.append(json.loads(udp))
    # protocols.append(json.loads(tls))
    # protocols.append(json.loads(dtls))
    # protocols.append(json.loads(sctp))
    # protocols.append(json.loads(quic))
    # protocols.append(json.loads(mptcp))
    return protocols


class TransportProperties:
    """ Class to handle the TAPS transport properties.

    """
    def __init__(self):
        self.properties = {
            "reliability": PreferenceLevel.REQUIRE,
            "preserve-msg-boundaries": PreferenceLevel.PREFER,
            "per-msg-reliability": PreferenceLevel.IGNORE,
            "preserve-order": PreferenceLevel.REQUIRE,
            "zero-rtt-msg": PreferenceLevel.PREFER,
            "multistreaming": PreferenceLevel.PREFER,
            "per-msg-checksum-len-send": PreferenceLevel.IGNORE,
            "per-msg-checksum-len-recv": PreferenceLevel.IGNORE,
            "congestion-control": PreferenceLevel.REQUIRE,
            "multipath": PreferenceLevel.PREFER,
            "direction": "bidirectional",
            "retransmit-notify": PreferenceLevel.IGNORE,
            "soft-error-notify": PreferenceLevel.IGNORE
        }

    def add(self, prop, value):
        """ Adds the property prop with value to the set of transport properties.
                
        Attributes:
            prop (string, required): Property to be added.
            value (PreferenceLevel, required): Preference for the property.
        """
        self.properties[prop] = value

    def require(self, prop):
        """ Adds the property prop with value "require" to the set of transport properties.
                
        Attributes:
            prop (string, required): Property to be added.
        """
        self.properties[prop] = PreferenceLevel.REQUIRE

    def prefer(self, prop):
        """ Adds the property prop with value "prefer" to the set of transport properties.
                
        Attributes:
            prop (string, required): Property to be added.
        """
        self.properties[prop] = PreferenceLevel.PREFER

    def ignore(self, prop):
        """ Adds the property prop with value "ignore" to the set of transport properties.
                
        Attributes:
            prop (string, required): Property to be added.
        """
        self.properties[prop] = PreferenceLevel.IGNORE

    def avoid(self, prop):
        """ Adds the property prop with value "avoid" to the set of transport properties.
                
        Attributes:
            prop (string, required): Property to be added.
        """
        self.properties[prop] = PreferenceLevel.AVOID

    def prohibit(self, prop):
        """ Adds the property prop with value "prohibit" to the set of transport properties.
                
        Attributes:
            prop (string, required): Property to be added.
        """
        self.properties[prop] = PreferenceLevel.PROHIBIT

    def default(self, prop):
        """ Sets the property prop back to its default value.
                
        Attributes:
            prop (string, required): Property to be set back to its default.
        """
        defaults = {
            "reliability": PreferenceLevel.REQUIRE,
            "preserve-msg-boundaries": PreferenceLevel.PREFER,
            "per-msg-reliability": PreferenceLevel.IGNORE,
            "preserve-order": PreferenceLevel.REQUIRE,
            "zero-rtt-msg": PreferenceLevel.PREFER,
            "multistreaming": PreferenceLevel.PREFER,
            "per-msg-checksum-len-send": PreferenceLevel.IGNORE,
            "per-msg-checksum-len-recv": PreferenceLevel.IGNORE,
            "congestion-control": PreferenceLevel.REQUIRE,
            "multipath": PreferenceLevel.PREFER,
            "direction": "Bidirectional",
            "retransmit-notify": PreferenceLevel.IGNORE,
            "soft-error-notify": PreferenceLevel.IGNORE
        }
        self.properties[prop] = defaults.get(prop)
