# Mixed QUIC Example

This demo creates one QUIC association and exposes three TAPS Connections on
it:

- a bidirectional QUIC stream;
- a unidirectional QUIC stream;
- the association-wide RFC 9221 QUIC DATAGRAM service.

Optionally, the client validates a new local UDP path, hands the whole
Connection Group over to it, and verifies DATAGRAM delivery after migration.
The client then closes that association and creates a second one from the same
Preconnection. The shared ConnectionContext supplies a session ticket, so a
replay-safe `InitiateWithSend` Message can be transmitted in genuine QUIC
0-RTT.

Raw QUIC DATAGRAM frames do not carry a stream or application-flow ID.
PyTAPS therefore exposes one raw datagram Connection per association. An
application that needs several logical datagram flows should add its own
standardized or application-specific context ID inside each Message.

Install the optional QUIC backend:

```console
python -m pip install -e '.[quic]'
```

For a local run, start the server:

```console
./.venv/bin/python examples/quic_example/mixedQuicServer.py
```

Then run the client in another terminal:

```console
./.venv/bin/python examples/quic_example/mixedQuicClient.py
```

To include validated migration to a fresh local UDP port:

```console
./.venv/bin/python examples/quic_example/mixedQuicClient.py \
  --migrate \
  --migration-address 127.0.0.1
```

The repository's test certificate is valid only for `localhost`. For a
two-host run, use a server certificate whose DNS or IP subject alternative
name matches `--server-name`. On the server:

```console
./.venv/bin/python examples/quic_example/mixedQuicServer.py \
  --local-address 0.0.0.0 \
  --port 4433 \
  --identity /path/to/server-chain.pem \
  --private-key /path/to/server-key.pem
```

Open UDP port 4433 in the host and provider firewalls. On the client:

```console
./.venv/bin/python examples/quic_example/mixedQuicClient.py \
  --remote-address 203.0.113.10 \
  --port 4433 \
  --server-name quic.example.net \
  --trust-ca /path/to/issuing-ca.pem \
  --payload "hello from local" \
  --migrate \
  --migration-address 192.0.2.20
```

Replace `192.0.2.20` with an address configured on the client, or omit
`--migration-address` to keep route-based source-address selection and change
only the local UDP port.

The client logs the bidirectional reply, the unidirectional stream ID, the
datagram reply and reliability metadata, confirmation that all three
Connections share one QUIC association, validated migration state when
requested, measured RTT and establishment history, and the resumed
association's 0-RTT status. A successful run includes output similar to:

```text
measured performance latest_rtt_ms=1.23 smoothed_rtt_ms=1.45 establishment_ms=18.7 success_rate=1.0
validated QUIC handover current={'local': ('127.0.0.1', 50978), 'remote': ('127.0.0.1', 4433)} validations=1 smoothed_rtt_ms=1.39
post-migration datagram reply=b'datagram-echo:post-migration:hello'
```

A successful full run ends with:

```text
0-RTT reply=b'stream-echo:0rtt:hello' resumed=True attempted=True accepted=True rejected=False
```

The server marks a Message received before handshake completion with
`isEarlyData=True`. Only an `InitiateWithSend` Message whose Message Context
sets `safelyReplayable=True` is eligible for this path. Other Messages wait
for the authenticated handshake even when the TLS session resumes. If a peer
rejects early data, PyTAPS discards the speculative QUIC state, establishes a
fresh association without 0-RTT, and sends the queued replay-safe Message
after authentication.

Migration requires the pre-establishment Selection Property
`multipath=Active` and the Connection Property `multipathPolicy=Handover`.
PyTAPS keeps the old socket until aioquic validates the new path with
QUIC `PATH_CHALLENGE`/`PATH_RESPONSE`, rolls back on failure, and emits
`PathChange` on every live Connection in the group only after validation.
Listeners use the RFC default `multipath=Passive` and report a peer's
validated handover. `Interactive` and `Aggregate` concurrent multipath are
not implemented.

RTT values come from the live QUIC recovery state. PyTAPS keeps bounded,
expiring RTT, fresh-association latency, and establishment-success history in
the shared `ConnectionContext`; opening cloned streams does not count as
another association handshake. Equivalent future candidates use this state
for ordering and staggered-racing delays.

Pass `--skip-zero-rtt` to run only the mixed stream and DATAGRAM portion.
