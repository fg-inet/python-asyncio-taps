import time
from dataclasses import dataclass, field

from .transportProperties import CONNECTION_ENUM_VALUES


MESSAGE_PROPERTY_ALIASES = {
    "msgLifetime": "lifetime",
    "msgPriority": "priority",
    "msgOrdered": "ordered",
    "msgReliable": "reliable",
    "safelyReplayable": "safely_replayable",
    "final": "final",
    "msgChecksumLen": "checksum_length",
    "msgCapacityProfile": "capacity_profile",
    "noFragmentation": "no_fragmentation",
    "noSegmentation": "no_segmentation",
    "endOfMessage": "end_of_message",
    "ecn": "ecn",
    "isEarlyData": "is_early_data",
}


MESSAGE_PROPERTY_DEFAULTS = {
    "message_id": None,
    "batch_id": None,
    "priority": 100,
    "ordered": None,
    "reliable": None,
    "final": False,
    "safely_replayable": False,
    "checksum_length": "Full Coverage",
    "capacity_profile": None,
    "no_fragmentation": False,
    "no_segmentation": False,
    "end_of_message": True,
    "lifetime": None,
    "received_at": None,
    "receive_sequence": None,
    "ecn": None,
    "is_early_data": False,
    "framer_context": None,
}

SETTABLE_MESSAGE_PROPERTIES = frozenset(
    {
        "lifetime",
        "priority",
        "ordered",
        "safely_replayable",
        "final",
        "checksum_length",
        "reliable",
        "capacity_profile",
        "no_fragmentation",
        "no_segmentation",
    }
)

BOOLEAN_MESSAGE_PROPERTIES = frozenset(
    {
        "ordered",
        "safely_replayable",
        "final",
        "reliable",
        "no_fragmentation",
        "no_segmentation",
    }
)


def canonicalize_message_property_name(name):
    if not isinstance(name, str):
        raise TypeError("Message Property names must be strings")
    return MESSAGE_PROPERTY_ALIASES.get(name, name)


def is_message_property(name):
    canonical = canonicalize_message_property_name(name)
    return canonical in SETTABLE_MESSAGE_PROPERTIES


def _normalize_message_property_value(name, value, *, allow_unset=False):
    if name == "lifetime":
        if value == "Infinite":
            return None
        if allow_unset and value is None:
            return None
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value <= 0
        ):
            raise ValueError("msgLifetime must be positive or Infinite")
        return value
    if name == "priority":
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("msgPriority must be a non-negative Integer")
        return value
    if name in BOOLEAN_MESSAGE_PROPERTIES:
        if allow_unset and name in {"ordered", "reliable"} and value is None:
            return None
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be a Boolean")
        return value
    if name == "checksum_length":
        if value != "Full Coverage" and (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise ValueError(
                "msgChecksumLen must be a non-negative Integer or Full Coverage"
            )
        return value
    if name == "capacity_profile":
        if allow_unset and value is None:
            return None
        if value not in CONNECTION_ENUM_VALUES["connCapacityProfile"]:
            raise ValueError(f"Invalid msgCapacityProfile value: {value}")
        return value
    raise KeyError(f"Unknown Message Property: {name}")


@dataclass
class MessageContext:
    message_id: int | None = None
    batch_id: int | None = None
    priority: int = 100
    ordered: bool | None = None
    reliable: bool | None = None
    final: bool = False
    safely_replayable: bool = False
    checksum_length: int | str = "Full Coverage"
    capacity_profile: str | None = None
    no_fragmentation: bool = False
    no_segmentation: bool = False
    end_of_message: bool = True
    lifetime: float | None = None
    created_at: float | None = None
    received_at: float | None = None
    receive_sequence: int | None = None
    remote_address: str | None = None
    remote_port: int | None = None
    local_address: str | None = None
    local_port: int | None = None
    ecn: int | None = None
    is_early_data: bool = False
    framer_context: object | None = None
    explicit_properties: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self):
        for prop in SETTABLE_MESSAGE_PROPERTIES:
            value = _normalize_message_property_value(
                prop,
                getattr(self, prop),
                allow_unset=True,
            )
            setattr(self, prop, value)
            if value != MESSAGE_PROPERTY_DEFAULTS[prop]:
                self.explicit_properties.add(prop)

    @property
    def addr(self):
        if self.remote_address is None or self.remote_port is None:
            return None
        return (self.remote_address, self.remote_port)

    @addr.setter
    def addr(self, value):
        if value is None:
            self.remote_address = None
            self.remote_port = None
            return
        self.remote_address, self.remote_port = value[:2]

    def ensure_created(self):
        if self.created_at is None:
            self.created_at = time.monotonic()
        return self

    def add(self, name, value):
        return self.set_property(name, value)

    def get(self, name, default=None):
        canonical = canonicalize_message_property_name(name)
        if canonical == "lifetime":
            return self.lifetime if self.lifetime is not None else "Infinite"
        return getattr(self, canonical, default)

    def is_expired(self):
        if self.lifetime is None:
            return False
        self.ensure_created()
        return (time.monotonic() - self.created_at) >= self.lifetime

    def set_property(self, name, value):
        canonical = canonicalize_message_property_name(name)
        if canonical not in SETTABLE_MESSAGE_PROPERTIES:
            raise KeyError(f"Unknown Message Property: {name}")
        value = _normalize_message_property_value(canonical, value)
        setattr(self, canonical, value)
        self.explicit_properties.add(canonical)
        return self

    def get_remote_endpoint(self):
        if self.remote_address is None and self.remote_port is None:
            return None
        from .endpoint import RemoteEndpoint

        endpoint = RemoteEndpoint()
        if self.remote_address is not None:
            endpoint.with_address(self.remote_address)
        if self.remote_port is not None:
            endpoint.with_port(self.remote_port)
        return endpoint

    def get_local_endpoint(self):
        if self.local_address is None and self.local_port is None:
            return None
        from .endpoint import LocalEndpoint

        endpoint = LocalEndpoint()
        if self.local_address is not None:
            endpoint.with_address(self.local_address)
        if self.local_port is not None:
            endpoint.with_port(self.local_port)
        return endpoint

    def get_properties(self):
        return {
            "message_id": self.message_id,
            "batch_id": self.batch_id,
            "msgPriority": self.priority,
            "msgOrdered": self.ordered,
            "msgReliable": self.reliable,
            "final": self.final,
            "safelyReplayable": self.safely_replayable,
            "msgChecksumLen": self.checksum_length,
            "msgCapacityProfile": self.capacity_profile,
            "noFragmentation": self.no_fragmentation,
            "noSegmentation": self.no_segmentation,
            "endOfMessage": self.end_of_message,
            "msgLifetime": self.lifetime if self.lifetime is not None else "Infinite",
            "created_at": self.created_at,
            "receivedAt": self.received_at,
            "receiveSequence": self.receive_sequence,
            "remote_address": self.remote_address,
            "remote_port": self.remote_port,
            "local_address": self.local_address,
            "local_port": self.local_port,
            "remoteEndpoint": self.get_remote_endpoint(),
            "localEndpoint": self.get_local_endpoint(),
            "ecn": self.ecn,
            "isEarlyData": self.is_early_data,
            "framer_context": self.framer_context,
        }


@dataclass
class ReceivedMessage:
    data: object
    context: MessageContext
    connection: object
    _end_of_message: bool = None

    def __post_init__(self):
        if self._end_of_message is None:
            self._end_of_message = self.context.end_of_message

    @property
    def end_of_message(self):
        return self._end_of_message

    @property
    def remote_endpoint(self):
        return self.context.get_remote_endpoint()

    @property
    def local_endpoint(self):
        return self.context.get_local_endpoint()

    @property
    def is_complete(self):
        return self._end_of_message

    def get(self, name, default=None):
        return self.context.get(name, default)

    def get_properties(self):
        properties = self.context.get_properties()
        connection = getattr(self, "connection", None)
        selection_view = getattr(connection, "_selection_properties_view", None)
        if callable(selection_view):
            properties["selection"] = selection_view()
        return properties

    def get_read_only_properties(self):
        return {
            "receivedAt": self.context.received_at,
            "receiveSequence": self.context.receive_sequence,
            "endOfMessage": self._end_of_message,
            "remoteEndpoint": self.remote_endpoint,
            "localEndpoint": self.local_endpoint,
            "ecn": self.context.ecn,
        }
