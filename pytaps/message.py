import time
from dataclasses import asdict, dataclass


@dataclass
class MessageContext:
    message_id: int | None = None
    batch_id: int | None = None
    priority: int = 0
    ordered: bool = True
    final: bool = True
    idempotent: bool = False
    end_of_message: bool = True
    lifetime: float | None = None
    created_at: float | None = None
    remote_address: str | None = None
    remote_port: int | None = None
    local_address: str | None = None
    local_port: int | None = None
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
        setattr(self, name, value)
        return self

    def get_properties(self):
        return asdict(self)


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
