import asyncio
import sys
import argparse
import ipaddress
sys.path.append(sys.path[0] + "/../..")
import pytaps as taps  # noqa: E402

color = "yellow"


class testFramer(taps.Framer):
    async def start(self, connection):
        taps.print_time("Framer got new connection", color)
        return

    async def new_sent_message(self, data, context, eom):
        taps.print_time("Framing new message " + str(data), color)
        tlv = (data[0] + "/" + str(len(str(data[1]))) + "/" +
               str(data[1]))
        return tlv.encode()

    async def handle_received_data(self, connection):
        byte_stream, context, eom = connection.parse()
        byte_stream = byte_stream.decode()
        taps.print_time("Deframing " + byte_stream, color)
        try:
            tlv = byte_stream.split("/")
        except Exception:
            taps.print_time("Error splitting", color)
            raise taps.DeframingFailed
            return

        if len(tlv) < 3:
            taps.print_time("Deframing error: missing length," +
                            " value or type parameter.", color)
            raise taps.DeframingFailed
            return

        if (len(tlv[2]) < int(tlv[1])):
            taps.print_time("Deframing error: actual length of message" +
                            " shorter than indicated", color)
            raise taps.DeframingFailed
            return
        len_message = len(tlv[0]) + len(tlv[1]) + int(tlv[1]) + 2
        message = (str(tlv[0]), str(tlv[2][0:int(tlv[1])]))
        return(context, message, len_message, eom)
        """
        self.advance_receive_cursor(connection, len_message)
        self.deliver(connection, context, message, eom)
        """


class TestClient():
    def __init__(self):
        self.connection = None
        self.preconnection = None
        self.loop = asyncio.get_event_loop()

    async def handle_received_partial(self, data, context, end_of_message,
                                      connection):
        taps.print_time("Received partial message " + str(data) + ".", color)
        # self.loop.stop()

    async def handle_received(self, data, context, connection):
        taps.print_time("Received message " + str(data) + ".", color)
        # self.loop.stop()

    async def handle_sent(self, message_ref, connection):
        taps.print_time("Sent cb received, message " + str(message_ref) +
                        " has been sent.", color)
        await self.connection.receive(min_incomplete_length=1)

    async def handle_send_error(self, msg, connection):
        taps.print_time("SendError cb received.", color)
        print("Error sending message")

    async def handle_initiate_error(self, connection):
        taps.print_time("InitiateError cb received.", color)
        print("Error init")
        self.loop.stop()

    async def handle_closed(self, connection):
        taps.print_time("Connection closed, stopping event loop.", color)
        # self.loop.stop()

    async def handle_ready(self, connection):
        taps.print_time("Ready cb received from connection to " +
                        connection.remote_endpoint.address + ":" +
                        str(connection.remote_endpoint.port) +
                        " (hostname: " +
                        str(connection.remote_endpoint.host_name) +
                        ")", color)

        # Set connection callbacks
        self.connection.on_sent(self.handle_sent)
        self.connection.on_send_error(self.handle_send_error)
        self.connection.on_closed(self.handle_closed)
        self.connection.on_received_partial(self.handle_received_partial)
        self.connection.on_received(self.handle_received)
        taps.print_time("Connection cbs set.", color)

        msgref = await self.connection.send_message(("STR", "Hello there"))
        msgref = await self.connection.send_message(("STR", "This is a test"))
        msgref = await self.connection.send_message(("INT", 334353))
        msgref = await self.connection.send_message(("STR", "Hope it worked"))
        # Send message
        """
        msgref = await self.connection.send_message("This")
        msgref = await self.connection.send_message("Is")
        msgref = await self.connection.send_message("a")
        msgref = await self.connection.send_message("Test")"""
        taps.print_time("send_message called.", color)

    async def main(self, args):

        # Create endpoint objects
        ep = taps.RemoteEndpoint()
        if args.remote_address:
            ep.with_address(args.remote_address)
        elif args.remote_host:
            ep.with_hostname(args.remote_host)
        if args.remote_port:
            ep.with_port(args.remote_port)
        lp = None
        sp = None
        if args.interface or args.local_address or args.local_port:
            lp = taps.LocalEndpoint()
            if args.interface:
                lp.with_interface(args.interface)
            if args.local_address:
                lp.with_port(args.local_address)
            if args.local_port:
                lp.with_port(args.local_port)

        taps.print_time("Created endpoint objects.", color)

        if args.secure or args.trust_ca or args.local_identity:
            # Use TLS
            sp = taps.SecurityParameters()
            if args.trust_ca:
                sp.addTrustCA(args.trust_ca)
            if args.local_identity:
                sp.addIdentity(args.local_identity)
            taps.print_time("Created SecurityParameters.", color)

        # Create transportProperties Object and set properties
        # Does nothing yet
        tp = taps.TransportProperties()
        # tp.prohibit("reliability")
        tp.ignore("congestion-control")
        tp.ignore("preserve-order")
        # tp.add("Reliable_Data_Transfer", taps.preferenceLevel.REQUIRE)

        # Create the preconnection object with the two prev created EPs
        self.preconnection = taps.Preconnection(remote_endpoint=ep,
                                                local_endpoint=lp,
                                                transport_properties=tp,
                                                security_parameters=sp)
        # Set callbacks
        self.preconnection.on_initiate_error(self.handle_initiate_error)
        self.preconnection.on_ready(self.handle_ready)
        # Set the framer
        framer = testFramer()
        self.preconnection.add_framer(framer)
        taps.print_time("Created preconnection object and set cbs.", color)
        # Initiate the connection
        self.connection = await self.preconnection.initiate()
        # msgref = await self.connection.send_message("Hello\n")
        taps.print_time("Called initiate, connection object created.", color)


if __name__ == "__main__":
    # Parse arguments
    ap = argparse.ArgumentParser(description='PyTAPS test client.')
    ap.add_argument('--remote-host', '--host', nargs='?', default="localhost")
    ap.add_argument('--remote-address', nargs=1)
    ap.add_argument('--remote-port', '--port', type=int, default=6666)
    ap.add_argument('--interface', '-i', nargs=1, default=None)
    ap.add_argument('--local-address', nargs=1, default=None)
    ap.add_argument('--local-port', '-l', type=int, nargs=1, default=None)
    ap.add_argument('--local-identity', type=str, nargs=1, default=None)
    ap.add_argument('--trust-ca', type=str, default=None)
    ap.add_argument('--secure', '-s', nargs='?', const=True,
                    type=bool, default=False)
    args = ap.parse_args()
    print(args)
    # Start testclient
    client = TestClient()
    client.loop.create_task(client.main(args))
    client.loop.run_forever()
