import asyncio
from .connection import connection
from .transportProperties import transportProperties
from .endpoint import localEndpoint, remoteEndpoint


class preconnection:
    """The TAPS preconnection class.

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
                self.localEndpoint = lEndpoint
                self.remoteEndpoint = rEndpoint
                self.transportProperties = tProperties
                self.securityParams = securityParams

    async def initiate(self):
        """ Initiates the preconnection, i.e. creates a connection object
            and attempts to connect it to the specified remote endpoint.
        """
        con = connection(self.localEndpoint, self.remoteEndpoint,
                         self.transportProperties, self.securityParams)
        print("Created connection Object. Connecting...")
        await con.connect()
        print("Connected")
        return con
