#!/bin/bash
# Run echo server on ports 6666 (TCP/UDP) and 6667 (TLS)
# Then run tests

PYTHON="python3.7"

$PYTHON ../examples/echo_example/echoServer.py --local-address=::1 --local-port=6666  --reliable both >/dev/null 2>&1 &

$PYTHON ../examples/echo_example/echoServer.py --local-address=::1 --local-port=6667 --local-identity keys/localhost.pem >/dev/null 2>&1 &


pytest

killall $PYTHON
