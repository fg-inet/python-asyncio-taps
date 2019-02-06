import PyTAPS as taps
import asyncio
import sys
color = "yellow"


class testClient():
    def __init__(self):
        self.connection = None
        self.preconnection = None
        # Get event loop
        self.loop = asyncio.get_event_loop()

    def handleSent(self):
        taps.printTime("Sent cb received.", color)
        self.connection.close()
        taps.printTime("Queued closure of connection.", color)

    def handlerSendError(self):
        taps.printTime("SeSendError cb received.", color)
        print("Error sending message")

    def handleInitiateError(self):
        taps.printTime("InitiateError cb received.", color)
        print("Error init")

    def handleReady(self):
        taps.printTime("Ready cb received.", color)
        self.connection.Sent(self.handleSent)
        self.connection.SendError(self.handlerSendError)
        taps.printTime("Sent cbs set.", color)
        self.connection.sendMessage("Hello\n")
        taps.printTime("sendMessage called.", color)

    def main(self):
        # Create endpoint objects
        ep = taps.remoteEndpoint()
        lp = None
        taps.printTime("Created endpoint objects.", color)
        # See if a remote and/or local address/port has been specified
        if len(sys.argv) >= 3:
            ep.withAddress(str(sys.argv[1]))
            ep.withPort(int(sys.argv[2]))
            if len(sys.argv) >= 4:
                lp = taps.localEndpoint()
                lp.withInterface(str(sys.argv[3]))
                if len(sys.argv) == 5:
                    lp.withPort(int(sys.argv[4]))
                elif len(sys.argv) > 5:
                    exit("Please call with remoteAddress, remotePort, "
                         "(optional) localAddress, (optional) localPort")
        else:
            exit("Please call with remoteAddress, remotePort, "
                 "(optional) localAddress, (optional) localPort")
        # Create transportProperties Object and set properties
        tp = taps.transportProperties()
        tp.add("Reliable_Data_Transfer", taps.preferenceLevel.REQUIRE)
        taps.printTime("Created transportProperties object.", color)
        # Create the preconnection object with the two prev created EPs
        self.preconnection = taps.preconnection(rEndpoint=ep, lEndpoint=lp)
        self.preconnection.InitiateError(self.handleInitiateError)
        self.preconnection.Ready(self.handleReady)
        taps.printTime("Created preconnection object and set cbs.", color)
        # Initiate the connection
        self.connection = self.preconnection.initiate()
        taps.printTime("Called initiate, connection object created.", color)
        self.loop.run_forever()

if __name__ == "__main__":
    client = testClient()
    client.main()
