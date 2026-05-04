from dataclasses import dataclass


@dataclass
class MessageContext:
    message_id: int | None = None
    end_of_message: bool = True
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
