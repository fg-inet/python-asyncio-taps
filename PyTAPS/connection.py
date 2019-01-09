import asyncio
from .endpoint import localEndpoint, remoteEndpoint
from .transportProperties import transportProperties


class connection:
    """The TAPS connection class.

    Attributes:
        localEndpoint (:obj:'localEndpoint', optional): LocalEndpoint of the
                       preconnection, required if the connection
                       will be used to listen
        remoteEndpoint (:obj:'remoteEndpoint', optional): RemoteEndpoint of the
                        preconnection, required if a connection
                        will be initiated
        transportProperties (:obj:'transportProperties', optional): object with
                             the transport properties
                             with specified preferenceLevel
        securityParams (tbd): Security Parameters for the preconnection
    """
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
    """ Tries to create a (TCP) connection to a remote endpoint
        If a local endpoint was specified on connection class creation,
        it will be used.
    """
    async def connect(self):
                if(self.local is None):
                    self.reader, self.writer = await asyncio.open_connection(
                                        self.remote.address, self.remote.port)
                else:
                    self.reader, self.writer = await asyncio.open_connection(
                                    self.remote.address, self.remote.port,
                                    local_addr=(self.local.interface,
                                                self.local.port))

    """ Tries to send the (string) stored in data
    """
    def sendMessage(self, data):
        self.writer.write(data.encode())

    """ Tries to close the connection
        TODO: Check why port isnt always freed
    """
    async def close(self):
        self.writer.close()
        await self.writer.wait_closed()
