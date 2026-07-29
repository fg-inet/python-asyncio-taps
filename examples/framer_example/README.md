# Framer example

This example adds a fixed header Framer to a TCP-based TAPS Preconnection. The
header contains a four-byte payload length plus a Message kind and sequence
number. It demonstrates complete Message delivery over a byte stream,
Framer-specific Message Context metadata carried on the wire, and Framer
setup/teardown on both active and passive Connections.

Start the server:

```console
python3 examples/framer_example/framerServer.py \
  --local-address 0.0.0.0 \
  --port 7778
```

Run the client locally or from another machine:

```console
python3 examples/framer_example/framerClient.py \
  --remote-address 192.0.2.10 \
  --port 7778 \
  --message "hello over a framed TAPS Connection"
```

Use `--message` more than once to send multiple Messages. Add `--verbose` to
show the underlying candidate and transport logs.
