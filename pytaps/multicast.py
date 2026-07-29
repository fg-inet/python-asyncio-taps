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
    listener.preconnection.got_mc(
        listener,
        payload,
        packet.source_port,
        source_address=getattr(packet, "source_address", None),
    )


def join_subscription(
    listener,
    *,
    local_endpoint=None,
    interface=None,
):
    _require_mcrx_core()
    if listener.loop is None:
        raise Exception("joining with no asyncio loop attached to connection")

    local_endpoint = local_endpoint or listener.local_endpoint
    local = local_endpoint.multicast_group
    if local is None:
        raise ValueError(
            "A multicast Listener requires a Local Endpoint configured "
            "with a multicast group"
        )
    remote = local_endpoint.multicast_source
    if interface is None:
        interface = getattr(
            listener.preconnection,
            "multicast_interface_address",
            None,
        )
    if interface is None:
        interface = local_endpoint.address or local_endpoint.interface

    ctx = mcrx_core.Context()
    sub = ctx.add_subscription(
        local,
        int(local_endpoint.port),
        source=remote,
        interface=interface,
    )
    joined = False
    try:
        sub.join()
        joined = True
        handle = mcrx_core.add_reader(
            sub,
            lambda packet: _got_packet(listener, packet),
            loop=listener.loop,
        )
    except BaseException:
        if joined:
            sub.leave()
        remove = getattr(sub, "remove", None)
        if callable(remove):
            remove()
        raise
    return {
        "context": ctx,
        "subscription": sub,
        "reader_handle": handle,
        "local_endpoint": local_endpoint.clone(),
        "interface": interface,
        "closed": False,
    }


def leave_subscription(join_ctx):
    if join_ctx is None or join_ctx.get("closed"):
        return False
    join_ctx["closed"] = True
    subscription = join_ctx["subscription"]
    try:
        join_ctx["reader_handle"].close()
    finally:
        try:
            subscription.leave()
        finally:
            remove = getattr(subscription, "remove", None)
            if callable(remove):
                remove()
    return True


def do_join(listener):
    if getattr(listener, "_join_ctx", None) is not None:
        raise RuntimeError("Multicast Listener is already joined")
    listener._join_ctx = join_subscription(listener)
    return True


def do_leave(listener):
    _require_mcrx_core()
    if not hasattr(listener, "_join_ctx") or listener._join_ctx is None:
        return False
    join_ctx = listener._join_ctx
    listener._join_ctx = None
    return leave_subscription(join_ctx)
