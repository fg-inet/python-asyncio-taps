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

_MISSING = object()


_MESSAGE_PROPERTY_NAME_LOOKUP = {
    **{
        property_name.casefold(): property_name
        for property_name in SETTABLE_MESSAGE_PROPERTIES
    },
    **{
        alias.casefold(): canonical
        for alias, canonical in MESSAGE_PROPERTY_ALIASES.items()
    },
}


def canonicalize_message_property_name(name):
    if not isinstance(name, str):
        raise TypeError("Message Property names must be strings")
    return _MESSAGE_PROPERTY_NAME_LOOKUP.get(name.casefold(), name)


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


def _framer_namespace(framer):
    if isinstance(framer, str):
        return framer
    namespace = getattr(framer, "namespace", None)
    if isinstance(namespace, str) and namespace:
        return namespace
    framer_type = type(framer)
    return f"{framer_type.__module__}.{framer_type.__qualname__}"


@dataclass(init=False)
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
    framer_metadata: dict[str, dict[str, object]] = field(default_factory=dict)
    explicit_properties: set[str] = field(default_factory=set, repr=False)

    def __init__(
        self,
        message_id=None,
        batch_id=None,
        priority=_MISSING,
        ordered=_MISSING,
        reliable=_MISSING,
        final=_MISSING,
        safely_replayable=_MISSING,
        checksum_length=_MISSING,
        capacity_profile=_MISSING,
        no_fragmentation=_MISSING,
        no_segmentation=_MISSING,
        end_of_message=True,
        lifetime=_MISSING,
        created_at=None,
        received_at=None,
        receive_sequence=None,
        remote_address=None,
        remote_port=None,
        local_address=None,
        local_port=None,
        ecn=None,
        is_early_data=False,
        framer_metadata=None,
        explicit_properties=None,
    ):
        self.message_id = message_id
        self.batch_id = batch_id
        self.end_of_message = end_of_message
        self.created_at = created_at
        self.received_at = received_at
        self.receive_sequence = receive_sequence
        self.remote_address = remote_address
        self.remote_port = remote_port
        self.local_address = local_address
        self.local_port = local_port
        self.ecn = ecn
        self.is_early_data = is_early_data
        self.framer_metadata = (
            {}
            if framer_metadata is None
            else {
                namespace: dict(values)
                for namespace, values in framer_metadata.items()
            }
        )
        self.explicit_properties = set(explicit_properties or ())

        supplied_properties = {
            "priority": priority,
            "ordered": ordered,
            "reliable": reliable,
            "final": final,
            "safely_replayable": safely_replayable,
            "checksum_length": checksum_length,
            "capacity_profile": capacity_profile,
            "no_fragmentation": no_fragmentation,
            "no_segmentation": no_segmentation,
            "lifetime": lifetime,
        }
        for prop, supplied_value in supplied_properties.items():
            was_supplied = supplied_value is not _MISSING
            value = (
                supplied_value
                if was_supplied
                else MESSAGE_PROPERTY_DEFAULTS[prop]
            )
            value = _normalize_message_property_value(
                prop,
                value,
                allow_unset=True,
            )
            setattr(self, prop, value)
            if was_supplied:
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

    def add(self, property_or_framer, key_or_value, value=_MISSING):
        """Add a Message Property or namespaced Framer metadata."""

        if value is _MISSING:
            return self.set_property(property_or_framer, key_or_value)
        return self.add_framer_metadata(
            property_or_framer,
            key_or_value,
            value,
        )

    def get(self, property_or_framer, key_or_default=_MISSING, default=_MISSING):
        """Get a Message Property or namespaced Framer metadata."""

        if not isinstance(property_or_framer, str):
            if key_or_default is _MISSING:
                raise TypeError("Framer metadata lookup requires a key")
            fallback = None if default is _MISSING else default
            return self.get_framer_metadata(
                property_or_framer,
                key_or_default,
                fallback,
            )

        if (
            property_or_framer in self.framer_metadata
            and key_or_default is not _MISSING
        ):
            fallback = None if default is _MISSING else default
            return self.get_framer_metadata(
                property_or_framer,
                key_or_default,
                fallback,
            )

        property_default = (
            None if key_or_default is _MISSING else key_or_default
        )
        name = property_or_framer
        canonical = canonicalize_message_property_name(name)
        if canonical == "lifetime":
            return self.lifetime if self.lifetime is not None else "Infinite"
        return getattr(self, canonical, property_default)

    def add_framer_metadata(self, framer, key, value):
        if not isinstance(key, str) or not key:
            raise TypeError("Framer metadata keys must be non-empty strings")
        namespace = _framer_namespace(framer)
        self.framer_metadata.setdefault(namespace, {})[key] = value
        return self

    def get_framer_metadata(self, framer, key, default=None):
        namespace = _framer_namespace(framer)
        return self.framer_metadata.get(namespace, {}).get(key, default)

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
            "framerMetadata": {
                namespace: dict(values)
                for namespace, values in self.framer_metadata.items()
            },
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

    def get(self, property_or_framer, key_or_default=_MISSING, default=_MISSING):
        return self.context.get(
            property_or_framer,
            key_or_default,
            default,
        )

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
