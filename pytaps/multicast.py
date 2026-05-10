try:
    import mcrx_core
except ImportError:
    mcrx_core = None


def _require_mcrx_core():
    if mcrx_core is None:
        raise ImportError(
            "Multicast support requires the optional 'mcrx-core-py' package. "
            "Install the multicast extra to enable multicast listeners."
        )


def _got_packet(listener, packet):
    payload = bytes(packet.payload)
    listener.preconnection.got_mc(listener, payload, packet.source_port)


def do_join(listener):
    _require_mcrx_core()
    if listener.loop is None:
        raise Exception("joining with no asyncio loop attached to connection")

    remote = listener.remote_endpoint.address[0]
    local = listener.local_endpoint.address[0]
    interface = getattr(listener.preconnection, "multicast_interface_address", None)
    if interface is None:
        interface = (
            listener.local_endpoint.interface[0]
            if getattr(listener.local_endpoint, "interface", None)
            else None
        )

    ctx = mcrx_core.Context()
    sub = ctx.add_subscription(
        local,
        int(listener.local_endpoint.port),
        source=remote,
        interface=interface,
    )
    sub.join()
    handle = mcrx_core.add_reader(
        sub,
        lambda packet: _got_packet(listener, packet),
        loop=listener.loop,
    )
    listener._join_ctx = {
        "context": ctx,
        "subscription": sub,
        "reader_handle": handle,
    }
    return True


def do_leave(listener):
    _require_mcrx_core()
    if not hasattr(listener, '_join_ctx') or listener._join_ctx is None:
        raise Exception('leaving a connection not joined')

    join_ctx = listener._join_ctx
    join_ctx["reader_handle"].close()
    join_ctx["subscription"].leave()
    listener._join_ctx = None
