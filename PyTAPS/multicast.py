import asyncio
import socket
import sys
import os.path
from ctypes import cdll, c_void_p, c_char_p, c_int, py_object, CFUNCTYPE, POINTER, c_ubyte, cast

global _lib_load_err, _lib, _libhandle, _loop
_lib_load_err = None
_lib = None
_libhandle = c_void_p(0)
_loop = None

CB_DOREAD_TYPE = CFUNCTYPE(c_int, c_void_p, c_int)
CB_ADDED_TYPE = CFUNCTYPE(None, py_object, c_void_p, c_int, CB_DOREAD_TYPE)
CB_REMOVED_TYPE = CFUNCTYPE(None, py_object, c_int)
CB_GOT_DATA_TYPE = CFUNCTYPE(c_int, py_object, c_int, POINTER(c_ubyte))

def added_sock_cb(loop, handle, fd, do_read):
    #sock = socket.socket(fileno=fd)

    c_do_read = CB_DOREAD_TYPE(do_read)
    def read_handler(handle, fd):
        c_do_read(c_void_p(handle), c_int(fd))
    loop.add_reader(fd, read_handler, handle, fd)

def removed_sock_cb(loop, fd):
    #sock = socket.socket(fileno=fd)
    loop.remove_reader(fd)

added_sock_param = CB_ADDED_TYPE(added_sock_cb)
removed_sock_param = CB_REMOVED_TYPE(removed_sock_cb)

class BytesConverter(object):
    def __init__(self, length, data):
        self.current = 0
        self.high = length
        self.data = data

    def __iter__(self):
        return self

    def __next__(self):
        if self.current >= self.high:
            raise StopIteration
        else:
            self.current += 1
            return self.data[self.current - 1]

def got_packet(conn, size, data):
    cb_data = bytes(BytesConverter(size, data))
    addr = conn.remote_endpoint.address
    conn.datagram_received(cb_data, addr)
    return 0

got_packet_param = CB_GOT_DATA_TYPE(got_packet)

def do_join(conn):
    global _loop, _lib, _libhandle
    if _loop is None:
        if conn.loop is None:
            raise Exception("joining with no asyncio loop attached to connection")
            return False
        _on_load()
        if not _lib:
            raise _lib_load_err
        _loop = conn.loop
        print("initializing")
        _libhandle = _lib.initialize(_loop, added_sock_param,
                removed_sock_param)

    if _loop is not conn.loop:
        # if we hit this, we need a separate initialize call per loop
        raise Exception("not yet supported: joining with a different asyncio loop")

    retval = _lib.join(_libhandle, conn, conn.remote_endpoint.address.encode(),
            conn.local_endpoint.address.encode(), c_int(int(conn.local_endpoint.port)),
            got_packet_param)
    return (retval == 0)

def _on_load():
    global _lib_load_err, _lib
    tapspath = os.path.abspath(os.path.dirname(__file__))
    depspath = os.path.join(os.path.abspath(os.path.dirname(tapspath)),
            'dependencies/install/lib')
    modulepath = os.path.join(tapspath, 'modules')

    if depspath not in os.environ.get('LD_LIBRARY_PATH', ''):
        if os.environ.get('LD_LIBRARY_PATH'):
            os.environ['LD_LIBRARY_PATH'] = os.environ['LD_LIBRARY_PATH'] + ':' + depspath
        else:
            os.environ['LD_LIBRARY_PATH'] = depspath

    libpath = os.path.join(depspath, 'libmulticast_glue.so')
    _lib_load_err = None
    try:
        _lib = cdll.LoadLibrary(libpath)
        _lib.initialize.restype = c_void_p
        _lib.initialize.argtypes = [py_object, CB_ADDED_TYPE, CB_REMOVED_TYPE]
        _lib.join.restype = c_int
        _lib.join.argtypes = [c_void_p, py_object, c_char_p, c_char_p, c_int, CB_GOT_DATA_TYPE]
        _lib.cleanup.argtypes = [c_void_p]
    except Exception as ex:
        _lib = None
        _lib_load_err = ex

# _on_load()
