import asyncio
from .endpoint import localEndpoint, remoteEndpoint
from .transportProperties import transportProperties


class connection:
    def __init__(self, lEndpoint=None, rEndpoint=None,
                 tProperties=None, securityParams=None):
                # Assertions
                assert(isinstance(lEndpoint, localEndpoint) or
                       lEndpoint is None), ("If given, lEndpoint "
                                            "needs to be instance of "
                                            "class localEndpoint.")
                assert(isinstance(rEndpoint, remoteEndpoint) or
                       rEndpoint is None), ("If given, rEndpoint "
                                            "needs to be instance of "
                                            "class remoteEndpoint.")
                assert(not (lEndpoint is None and rEndpoint is None)), "You need to specify at least one endpoint"
                assert(isinstance(tProperties, transportProperties) or
                       tProperties is None), ("If given, tProperties "
                                              "needs to be instance of "
                                              "class transportProperties")
                # Initializations
                self.local = lEndpoint
                self.remote = rEndpoint
                self.transportProperties = tProperties
                self.securityParams = securityParams

    async def connect(self):
                self.reader, self.writer = await asyncio.open_connection(
                                       self.remote.address, self.remote.port)

    def sendMessage(self, data):
        self.writer.write(data.encode())
