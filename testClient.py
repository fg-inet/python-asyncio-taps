import PyTAPS as taps
import asyncio


def main():
    # Create event loop
    loop = asyncio.get_event_loop()
    # Create local and remote endpoint
    ep = taps.remoteEndpoint()
    ep.withAddress("127.0.0.1")
    ep.withPort(5000)
    lp = taps.localEndpoint()
    lp.withInterface("127.0.0.1")
    lp.withPort(6000)
    # Create transportProperties Object and set properties
    tp = taps.transportProperties()
    tp.add("Reliable_Data_Transfer", taps.preferenceLevel.PROHIBIT)
    # Create the preconnection object with the two prev created EPs
    precon = taps.preconnection(rEndpoint=ep, lEndpoint=lp)
    # Initiate the connection
    # con = precon.initiate()
    con = loop.run_until_complete(precon.initiate())
    # Send a message
    con.sendMessage("Hello\n")
    # Add closing of the connection to the event loop
    loop.run_until_complete(con.close())
    # Close the event loop
    loop.close()
if __name__ == "__main__":
    main()
