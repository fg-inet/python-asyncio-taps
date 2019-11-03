import asyncio
import socket
import sys
import os.path
from .transports import *
import multicast_glue

global _loop, _libhandle
_loop = None
_libhandle = None

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

def got_packet(conn, size, data):
    cb_data = data
    addr = conn.remote_endpoint.address
    conn.transports[0].datagram_received(cb_data, addr)
    return 0

def do_join(conn):
    global _loop, _libhandle
    if _loop is None:
        if conn.loop is None:
            raise Exception("joining with no asyncio loop attached to connection")
            return False
        _libhandle = multicast_glue.initialize(conn.loop, added_sock_cb,
                removed_sock_cb)
        _loop = conn.loop
        assert(_libhandle is not None)

    if _loop is not conn.loop:
        # if we hit this, we need to maintain a dict to keep a separate
        # libhandle per loop
        raise Exception("not yet supported: joining with multiple different asyncio loops")

    join_ctx = multicast_glue.join(_libhandle, conn,
        conn.remote_endpoint.address,
        conn.local_endpoint.address,
        int(conn.local_endpoint.port),
        got_packet)
    conn._join_ctx = join_ctx
    new_udp = UdpTransport(conn, conn.local_endpoint, conn.remote_endpoint)
    return (join_ctx is not None)

def do_leave(conn):
    if not hasattr(conn, '_join_ctx') or conn._join_ctx is None:
        raise Exception('leaving a connection not joined')

    multicast_glue.leave(conn._join_ctx)
    conn._join_ctx = None

