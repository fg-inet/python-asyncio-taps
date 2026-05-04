import time
from dataclasses import dataclass


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
    "ecn": "ecn",
    "isEarlyData": "is_early_data",
}


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
    remote_address: str | None = None
    remote_port: int | None = None
    local_address: str | None = None
    local_port: int | None = None
    ecn: int | None = None
    is_early_data: bool = False
    framer_context: object | None = None

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

    def is_expired(self):
        if self.lifetime is None:
            return False
        self.ensure_created()
        return (time.monotonic() - self.created_at) >= self.lifetime

    def set_property(self, name, value):
        setattr(self, MESSAGE_PROPERTY_ALIASES.get(name, name), value)
        return self

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
            "msgLifetime": self.lifetime,
            "created_at": self.created_at,
            "remote_address": self.remote_address,
            "remote_port": self.remote_port,
            "local_address": self.local_address,
            "local_port": self.local_port,
            "ecn": self.ecn,
            "isEarlyData": self.is_early_data,
            "framer_context": self.framer_context,
        }


@dataclass
class ReceivedMessage:
    data: object
    context: MessageContext
    connection: object

    @property
    def end_of_message(self):
        return self.context.end_of_message

    def get_properties(self):
        return self.context.get_properties()
