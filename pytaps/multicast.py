import asyncio
import socket
import sys
import os.path
from .transports import *
import multicast_glue

global _loop, _libhandle, active_ports
_loop = None
_libhandle = None
active_ports = {}

def added_sock_cb(loop, handle, fd, do_read):
    global _libhandle
    assert(_libhandle is not None)
    #sock = socket.socket(fileno=fd)
    def read_handler(do_read, handle, fd):
        global _libhandle
        assert(_libhandle is not None)
        return multicast_glue.receive_packets(_libhandle, do_read, handle, fd)
    loop.add_reader(fd, read_handler, do_read, handle, fd)
    return 0

def removed_sock_cb(loop, fd):
    #sock = socket.socket(fileno=fd)
    loop.remove_reader(fd)
    return 0

def got_packet(listener, size, data, port):
    cb_data = data
    addr = listener.remote_endpoint.address
    if port in active_ports:
        active_ports[port].transports[0].datagram_received(cb_data, (addr,port))
    else:
        precon = Preconnection(listener.local_endpoint, listener.remote_endpoint, listener.transport_properties, listener.security_parameters, listener.loop)
        conn = Connection(precon)
        new_udp = UdpTransport(conn, conn.local_endpoint, conn.remote_endpoint)
        active_ports[port] = conn
        _loop.create_task(new_udp.active_open(None))

    return 0

def do_join(listener):
    global _loop, _libhandle
    if _loop is None:
        if listener.loop is None:
            raise Exception("joining with no asyncio loop attached to connection")
            return False
        _libhandle = multicast_glue.initialize(listener.loop, added_sock_cb,
                removed_sock_cb)
        _loop = listener.loop
        assert(_libhandle is not None)

    if _loop is not listener.loop:
        # if we hit this, we need to maintain a dict to keep a separate
        # libhandle per loop
        raise Exception("not yet supported: joining with multiple different asyncio loops")
    
    join_ctx = multicast_glue.join(_libhandle, listener,
        listener.remote_endpoint.address,
        listener.local_endpoint.address,
        int(listener.local_endpoint.port),
        got_packet)
    listener._join_ctx = join_ctx
    return (join_ctx is not None)

def do_leave(listener):
    if not hasattr(listener, '_join_ctx') or listener._join_ctx is None:
        raise Exception('leaving a connection not joined')

    multicast_glue.leave(listener._join_ctx)
    listener._join_ctx = None

