import argparse
import asyncio
import sys

sys.path.append(sys.path[0] + "/../..")
import pytaps as taps  # noqa: E402

logger = taps.setup_logger("Framer Client", "yellow")


class TestFramer(taps.Framer):
    async def start(self, connection):
        logger.info("Framer got new connection")
        return

    async def new_sent_message(self, data, context, eom):
        logger.info("Framing new message " + str(data))
        tlv = (data[0] + "/" + str(len(str(data[1]))) + "/" +
               str(data[1]))
        return tlv.encode()

    async def handle_received_data(self, connection):
        byte_stream, context, eom = connection.parse()
        byte_stream = byte_stream.decode()
        logger.info("Deframing " + byte_stream)
        try:
            tlv = byte_stream.split("/")
        except Exception:
            logger.warn("Error splitting")
            raise taps.DeframingFailed

        if len(tlv) < 3:
            logger.warning("Deframing error: missing length," +
                        " value or type parameter.")
            raise taps.DeframingFailed

        if len(tlv[2]) < int(tlv[1]):
            logger.warning("Deframing error: actual length of message" +
                        " shorter than indicated")
            raise taps.DeframingFailed

        len_message = len(tlv[0]) + len(tlv[1]) + int(tlv[1]) + 2
        message = (str(tlv[0]), str(tlv[2][0:int(tlv[1])]))
        return context, message, len_message, eom


"""
        self.advance_receive_cursor(connection, len_message)
        self.deliver(connection, context, message, eom)
        """


class TestClient:
    def __init__(self):
        self.connection = None
        self.preconnection = None
        self.loop = asyncio.get_event_loop()

    async def handle_received_partial(self, data, context, end_of_message,
                                      connection):
        logger.info("Received partial message " + str(data) + ".")
        # self.loop.stop()

    async def handle_received(self, data, context, connection):
        logger.info("Received message " + str(data) + ".")
        # self.loop.stop()

    async def handle_sent(self, message_ref, connection):
        logger.info("Sent cb received, message " + str(message_ref) +
                    " has been sent.")
        await self.connection.receive(min_incomplete_length=1)

    async def handle_send_error(self, msg, connection):
        logger.info("SendError cb received.")
        print("Error sending message")

    async def handle_initiate_error(self, connection):
        logger.info("InitiateError cb received.")
        print("Error init")
        self.loop.stop()

    async def handle_closed(self, connection):
        logger.info("Connection closed, stopping event loop.")
        # self.loop.stop()

    async def handle_ready(self, connection):
        logger.info("Ready cb received from connection to " +
                    connection.remote_endpoint.address + ":" +
                    str(connection.remote_endpoint.port) +
                    " (hostname: " +
                    str(connection.remote_endpoint.host_name) +
                    ")")

        # Set connection callbacks
        self.connection.on_sent(self.handle_sent)
        self.connection.on_send_error(self.handle_send_error)
        self.connection.on_closed(self.handle_closed)
        self.connection.on_received_partial(self.handle_received_partial)
        self.connection.on_received(self.handle_received)
        logger.info("Connection cbs set.")

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
        logger.info("send_message called.")

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

        logger.info("Created endpoint objects.")

        if args.secure or args.trust_ca or args.local_identity:
            # Use TLS
            sp = taps.SecurityParameters()
            if args.trust_ca:
                sp.add_trust_ca(args.trust_ca)
            if args.local_identity:
                sp.add_identity(args.local_identity)
            logger.info("Created SecurityParameters.")

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
        framer = TestFramer()
        self.preconnection.add_framer(framer)
        logger.info("Created preconnection object and set cbs.")
        # Initiate the connection
        self.connection = await self.preconnection.initiate()
        # msgref = await self.connection.send_message("Hello\n")
        logger.info("Called initiate, connection object created.")


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
