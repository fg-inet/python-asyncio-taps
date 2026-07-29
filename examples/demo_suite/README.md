# PyTAPS Feature Demos

These demos exercise the newer RFC-facing PyTAPS features: candidate selection,
message contexts, batching, expiration, monitoring subscriptions, adaptive
policy snapshots, re-establishment guidance, rendezvous, and multicast.

Run all commands from the repository root with the virtualenv active:

```bash
source .venv/bin/activate
```

## 1. Feature Echo Between Local And Linode

`auto` intentionally prefers reliable transports for cross-machine testing.
That keeps the first demo on TCP/QUIC-style paths instead of getting fooled by
UDP sends that succeed locally while replies are blocked by a firewall. On the
Linode, open TCP port `7777` in the firewall/security group, then run:

```bash
./.venv/bin/python examples/demo_suite/featureServer.py \
  --local-address 0.0.0.0 \
  --port 7777 \
  --transport auto
```

On the local machine, replace `LINODE_IP` with the Linode's public address:

```bash
./.venv/bin/python examples/demo_suite/featureClient.py \
  --remote-address LINODE_IP \
  --port 7777 \
  --transport auto \
  --payload "hello from local" \
  --batch-size 3 \
  --degrade-path
```

What to look for:

- the selected protocol in the client `ready` log
- `payload='hello from local'` in the receive/echo logs
- `sent`, `expired`, and compact summary logs
- `reestablishment advice` after `--degrade-path`

By default, the feature demos hide lower-level `pytaps.*` chatter and avoid
dumping the full property dictionaries. Add `--verbose-monitoring` for monitor
callbacks, `--dump-properties` for full property snapshots, or
`--no-quiet-library-logs` for transport/library internals.

## 2. Force UDP Message Semantics

For this one, open UDP port `7778` on the Linode. On the Linode:

```bash
./.venv/bin/python examples/demo_suite/featureServer.py \
  --local-address 0.0.0.0 \
  --port 7778 \
  --transport udp
```

On the local machine:

```bash
./.venv/bin/python examples/demo_suite/featureClient.py \
  --remote-address LINODE_IP \
  --port 7778 \
  --transport udp \
  --payload "udp message demo" \
  --batch-size 2
```

This path exercises `safelyReplayable`, datagram message boundaries, and the
UDP send/receive path.

## 3. Rendezvous

Rendezvous needs both hosts to be able to connect to each other on the selected
port. This is easiest with public IPv6 or with explicit firewall/NAT forwarding
on both sides.

On the Linode:

```bash
./.venv/bin/python examples/demo_suite/rendezvousPeer.py \
  --local-address LINODE_IP \
  --local-port 7788 \
  --remote-address LOCAL_REACHABLE_IP \
  --remote-port 7788 \
  --payload "hello from linode"
```

On the local machine:

```bash
./.venv/bin/python examples/demo_suite/rendezvousPeer.py \
  --local-address LOCAL_REACHABLE_IP \
  --local-port 7788 \
  --remote-address LINODE_IP \
  --remote-port 7788 \
  --payload "hello from local"
```

What to look for:

- `rendezvous done` logs
- one RFC `RendezvousDone` Connection on each peer
- `received peer payload=...` with the other peer's message
- shared monitoring updates

The Rendezvous demo also hides library-level logs by default. Use
`--verbose-monitoring`, `--dump-properties`, or `--no-quiet-library-logs` when
debugging establishment and candidate selection.

## 4. Multicast Send/Receive

The multicast demos live in `examples/multicast_example` and use PyTAPS on both
the sender and receiver paths.

Receiver:

```bash
./.venv/bin/python examples/multicast_example/multicastReceiver.py \
  --group ff3e::8000:1234 \
  --source SOURCE_IP \
  --port 5001 \
  --interface-address RECEIVER_INTERFACE_IP
```

Use `--interface INTERFACE_NAME` instead when the receiver should follow a
named interface through dynamic System Policy updates. PyTAPS joins a
replacement membership before leaving the old one when that interface's
address or network identity changes; the example starts the policy monitor
automatically. `--interface-address` is an explicit, fixed local-address
constraint.

Sender:

```bash
./.venv/bin/python examples/multicast_example/multicastSender.py \
  --group ff3e::8000:1234 \
  --port 5001 \
  --payload hello-v6 \
  --count 5 \
  --interval-ms 100 \
  --source SOURCE_IP \
  --interface-address SENDER_INTERFACE_IP
```

Install the optional multicast bindings first if needed:

```bash
python -m pip install -e /Users/mfranke/Devtools/Multicast/mcrx-core/mcrx-core-py
python -m pip install -e /Users/mfranke/Devtools/Multicast/mctx-core/mctx-core-py
```

## 5. Optional QUIC

If `aioquic` is installed, the `auto` demos include QUIC stream candidates.
Without `aioquic`, QUIC candidates are skipped cleanly and the demos continue
with the available transports.

The dedicated [`quic_example`](../quic_example/README.md) demonstrates
bidirectional streams, unidirectional streams, and RFC 9221 datagrams on one
association. It then resumes a second association and exercises replay-safe
`InitiateWithSend` over genuine QUIC 0-RTT, including the received
`isEarlyData` metadata.
