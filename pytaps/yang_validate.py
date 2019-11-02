import sys
import os.path
from ctypes import cdll, c_int, c_char_p

YANG_FMT_XML = 1   # MUST match LYD_XML from enum LYD_FORMAT in libyang
YANG_FMT_JSON = 2  # MUST match LYD_JSON from enum LYD_FORMAT in libyang

global _lib_load_err, _lib
_lib_load_err = None
_lib = None


def _pull_error_msgs():
    global _lib_load_err, _lib
    if _lib_load_err is not None:
        raise _lib_load_err
    assert(_lib)
    errcount = _lib.errmsg_count()
    msgs = []
    for i in range(errcount):
        msgs.append(str(_lib.errmsg(i)))
    done_errcount = _lib.errmsg_count()
    if errcount != done_errcount:
        msgs.append('error: libyangcheck error count changed (%d to %d)' +
                    'while enumerating' % (errcount, done_errcount))
        if done_errcount > errcount:
            for i in range(errcount, done_errcount):
                msgs.append(str(_lib.errmsg(i)))
    _lib.clear_errors()
    return msgs


class YangException(Exception):
    def __init__(self, base_msg=None):
        msgs = _pull_error_msgs()
        if base_msg:
            msgs = [base_msg] + msgs
        msg_text = '\n'.join(msgs)
        super().__init__(msg_text)


def validate(frmat, text):
    global _lib_load_err, _lib
    if _lib_load_err is not None:
        raise _lib_load_err
    assert(_lib)
    start_errs = _lib.errmsg_count()
    err_msg = None
    if start_errs:
        errs = ['%d prior yang errors' % start_errs] + _pull_error_msgs()
        err_msg = '\n'.join(errs)
        start_errs = 0

    retval = _lib.validate(c_int(frmat),
                           c_char_p(bytes(text, encoding='utf8')))
    done_errs = _lib.errmsg_count()
    if retval != 0:
        if done_errs != start_errs:
            raise YangException(err_msg)
        else:
            if err_msg:
                err_msg += '\n'
            else:
                err_msg = ''
            err_msg += ("error: libyangcheck validation"
                        "failed without error message")
            raise YangException(err_msg)
        return False

    if done_errs != start_errs:
        print('warning: yang errors with passed validation:\n%s' %
              ('\n'.join(_pull_error_msgs())), file=sys.stderr)

    return True


def convert(from_frmat, text, to_frmat):
    global _lib_load_err, _lib
    if _lib_load_err is not None:
        raise _lib_load_err
    assert(_lib)

    start_errs = _lib.errmsg_count()
    err_msg = None
    if start_errs:
        errs = ['%d prior yang errors' % start_errs] + _pull_error_msgs()
        err_msg = '\n'.join(errs)
        start_errs = 0

    retval = _lib.convert(c_int(from_frmat),
                          c_char_p(bytes(text, encoding='utf8')),
                          c_int(to_frmat))
    done_errs = _lib.errmsg_count()
    if not retval:
        if done_errs != start_errs:
            raise YangException(err_msg)
        else:
            if err_msg:
                err_msg += '\n'
            else:
                err_msg = ''
            err_msg += ("error: libyangcheck validation"
                        "failed without error message")
            raise YangException(err_msg)
        return None

    if done_errs != start_errs:
        print('warning: yang errors with passed validation:\n%s' %
              ('\n'.join(_pull_error_msgs())), file=sys.stderr)

    return retval.decode('utf-8')


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

    libpath = os.path.join(depspath, 'libyangcheck.so')
    _lib_load_err = None
    try:
        _lib = cdll.LoadLibrary(libpath)
        _lib.convert.restype = c_char_p
        _lib.errmsg.restype = c_char_p
        _lib.initialize(c_char_p(bytes(modulepath, encoding='utf8')))
        if _lib.errmsg_count() != 0:
            _lib_load_err = YangException()
    except Exception as ex:
        _lib = None
        _lib_load_err = ex

_on_load()
