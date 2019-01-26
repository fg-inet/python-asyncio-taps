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
                if lEndpoint is None and rEndpoint is None:
                    raise Exception("At least one endpoint need "
                                    "to be specified")
                # Initializations
                self.local = lEndpoint
                self.remote = rEndpoint
                self.transportProperties = tProperties
                self.securityParams = securityParams
                self.loop = asyncio.get_event_loop()
                self.MsgRefTop = 0
    """ Tries to create a (TCP) connection to a remote endpoint
        If a local endpoint was specified on connection class creation,
        it will be used.
    """
    async def connect(self):
        try:
                if(self.local is None):
                    self.reader, self.writer = await asyncio.open_connection(
                                        self.remote.address, self.remote.port)
                else:
                    self.reader, self.writer = await asyncio.open_connection(
                                    self.remote.address, self.remote.port,
                                    local_addr=(self.local.interface,
                                                self.local.port))
        except:
            if self.InitiateError:
                self.loop.call_soon(self.InitiateError)
            return
        if self.Ready:
            self.loop.call_soon(self.Ready)
        return

    """ Tries to send the (string) stored in data
    """
    async def sendData(self, data):
        try:
            self.writer.write(data.encode())
            await self.writer.drain()
        except:
            if self.SendError:
                self.loop.call_soon(self.SendError)
            return
        if self.Sent:
            self.loop.call_soon(self.Sent)
        return

    """ Helper function required to make sending
        of messages non blocking
    """
    async def sendDataHelper(self, data):
        asyncio.create_task(self.sendData(data))

    """ Wrapper function that assigns MsgRef
        and then calls async helper function
        to send a message
    """
    def sendMessage(self, data):
        self.loop.run_until_complete(self.sendDataHelper(data))
        self.MsgRefTop += 1
        return MsgRefTop

    """ Queues reception of a message
        TODO: Get this to work properly and as intended 
    """
    async def receive(self, minIncompleteLength=0, maxLength=float("inf")):
        try:
            data = self.reader.read(maxLength)
        except:
            if self.ReceiveError:
                self.loop.call_soon(self.ReceiveError)
            return
        if len(data) < minIncompleteLength:
            if self.ReceivedPartial:
                self.loop.call_soon(self.ReceivedPartial, (data, "Context", False))
        elif self.Received:
            self.loop.call_soon(self.Received, (data, "Context"))

    """ Tries to close the connection
        TODO: Check why port isnt always freed
    """
    async def close(self):
        self.writer.close()
        await self.writer.wait_closed()

    # Events for active open
    def Ready(self, a):
        self.Ready = a

    def InitiateError(self, a):
        self.InitiateError = a

    # Events for sending messages
    def Sent(self, a):
        self.Sent = a

    def SendError(self, a):
        self.SendError = a

    def Expired(self, a):
        self.Expired = a

    # Events for receiving messages
    def Received(self, a):
        self.Received = a

    def ReceivedPartial(self, a):
        self.ReceivedPartial = a

    def ReceiveError(self, a):
        self.ReceiveError = a
