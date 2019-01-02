import PyTAPS as taps
import asyncio


def main():
    loop = asyncio.get_event_loop()
    ep = taps.remoteEndpoint()
    ep.withAddress("127.0.0.1")
    ep.withPort(5000)
    lep = taps.localEndpoint()
    tp = taps.transportProperties()
    tp.add("Reliable_Data_Transfer", taps.preferenceLevel.PROHIBIT)
    print(tp.properties)
    precon = taps.preconnection(rEndpoint=ep)
    con = precon.initiate()
    con.sendMessage("Hello")

if __name__ == "__main__":
    main()
