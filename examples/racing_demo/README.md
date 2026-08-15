# Candidate Racing

Makes two parts of Connection establishment visible that are otherwise
invisible when they are wrong.

```
python examples/racing_demo/racingDemo.py
```

No arguments needed; it binds only loopback sockets.

## Sample output

```
Resolved 3 IPv6 and 3 IPv4 addresses.

  without interleaving:
    IPv6  IPv6  IPv6  IPv4  IPv4  IPv4
  as PyTAPS races them:
    IPv6  IPv4  IPv6  IPv4  IPv6  IPv4

  first IPv6 attempt at +0 ms; first IPv4 attempt at +250 ms
  without interleaving it would be +750 ms

3 dead addresses ahead of a live Listener, stagger 250 ms.

  waiting out each stagger :     752 ms
  released on failure      :       2 ms
  speedup                  :     445x
```

## What it shows

**Interleaved address families.** Section 4.3.2 of RFC 9623 points at the Happy
Eyeballs algorithm of [RFC 8305](https://www.rfc-editor.org/rfc/rfc8305.html)
for racing between IP addresses, and Section 4 of that document interleaves the
two families. The reason is the second line of output: with the families
grouped, a host whose IPv6 is blackholed waits through every IPv6 address
before trying IPv4 at all. Interleaved, the first IPv4 attempt starts one
stagger in, whatever the address count.

**Failure releases the next candidate.** Section 4.3.2 of RFC 9623: "If a child
node fails to establish connectivity ... before the delay time has expired for
the next child, the next child should be started immediately." Without that, a
refused address costs a full Connection Attempt Delay each, which is what the
first timing line measures by disabling the release. Exactly one waiting
candidate is released per failure, so the race stays staggered rather than
collapsing into the simultaneous racing Section 4.3.1 tells implementations to
avoid.

**Bounded delays.** The Connection Attempt Delay is scaled by cached path
history, so Section 5 of RFC 8305 requires bounds: an attempt must never start
within 10 ms of the previous one. PyTAPS defaults to 250 ms, clamped to
100 ms and 2 s, which are that document's recommended minimum and maximum.

## Options

```
--v6 N --v4 N   how many addresses of each family to order
--dead N        unreachable addresses to place before the live Listener
--no-failover   print the ordering only, without opening sockets
```

Raising `--dead` widens the gap, since each dead address costs one full stagger
when the release is disabled.
