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
                if lEndpoint is None and rEndpoint is None:
                    raise Exception("At least one endpoint need "
                                    "to be specified")
                # Initializations
                self.localEndpoint = lEndpoint
                self.remoteEndpoint = rEndpoint
                self.transportProperties = tProperties
                self.securityParams = securityParams
                self.loop = asyncio.get_event_loop()

    async def initiate_helper(self, con):
        # Helper function to allow for immediate return of
        # Connection Object
        asyncio.create_task(con.connect())

    """ Initiates the preconnection, i.e. creates a connection object
        and attempts to connect it to the specified remote endpoint.
    """
    def initiate(self):
        con = connection(self.localEndpoint, self.remoteEndpoint,
                         self.transportProperties, self.securityParams)
        con.InitiateError(self.InitiateError)
        con.Ready(self.Ready)
        print("Created connection Object. Connecting...")
        # This is required because initiate isnt async and therefor
        # there isnt necessarily a running eventloop
        self.loop.run_until_complete(self.initiate_helper(con))
        return con

    # Events for active open
    def Ready(self, a):
        self.Ready = a

    def InitiateError(self, a):
        self.InitiateError = a

    # Events for passive open
    def ConnectionReceived(self, a):
        self.ConnectionReceived = a

    def ListenError(self, a):
        self.ListenError = a

    def Stopped(self, a):
        self.Stopped = a
