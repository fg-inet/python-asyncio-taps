import PyTAPS as taps
import asyncio


class testClient():
    def __init__(self):
        self.connection = None
        self.preconnection = None
        # Get event loop
        self.loop = asyncio.get_event_loop()

    def handleSent(self):
        self.loop.create_task(self.connection.close())
        exit()

    def handlerSendError(self):
        print("Error sending message")

    def handleInitiateError(self):
        print("Error init")

    def handleReady(self):
        print("Sending")
        self.connection.Sent(self.handleSent)
        self.connection.SendError(self.handlerSendError)
        self.connection.sendMessage("Hello\n")
        exit()

    def main(self):
        # Create local and remote endpoint
        ep = taps.remoteEndpoint()
        ep.withAddress("127.0.0.1")
        ep.withPort(5000)
        lp = taps.localEndpoint()
        lp.withInterface("127.0.0.1")
        # lp.withPort(6000)
        # Create transportProperties Object and set properties
        tp = taps.transportProperties()
        tp.add("Reliable_Data_Transfer", taps.preferenceLevel.REQUIRE)
        # Create the preconnection object with the two prev created EPs
        self.preconnection = taps.preconnection(rEndpoint=ep, lEndpoint=lp)
        self.preconnection.InitiateError(self.handleInitiateError)
        self.preconnection.Ready(self.handleReady)
        # Initiate the connection
        self.connection = self.preconnection.initiate()
        self.loop.run_forever()

if __name__ == "__main__":
    client = testClient()
    client.main()
