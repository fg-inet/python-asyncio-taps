import os.path
import sys

try:
    import yang_glue
except ImportError:
    yang_glue = None

YANG_FMT_XML = 1  # MUST match LYD_XML from enum LYD_FORMAT in libyang
YANG_FMT_JSON = 2  # MUST match LYD_JSON from enum LYD_FORMAT in libyang


def _require_yang_glue():
    if yang_glue is None:
        raise ImportError(
            "YANG support requires the optional 'yang_glue' extension. "
            "Build the native extensions or install the package with YANG support enabled."
        )


def _pull_error_msgs():
    _require_yang_glue()
    errcount = yang_glue.errmsg_count()
    msgs = []
    for i in range(errcount):
        msgs.append(str(yang_glue.errmsg(i)))
    done_errcount = yang_glue.errmsg_count()
    if errcount != done_errcount:
        msgs.append('error: libyangcheck error count changed (%d to %d)' % (errcount, done_errcount) +
                    'while enumerating')
        if done_errcount > errcount:
            for i in range(errcount, done_errcount):
                msgs.append(str(yang_glue.errmsg(i)))
    yang_glue.clear_errors()
    return msgs


class YangException(Exception):
    def __init__(self, base_msg=None):
        msgs = _pull_error_msgs()
        if base_msg:
            msgs = [base_msg] + msgs
        msg_text = '\n'.join(msgs)
        super().__init__(msg_text)


def validate(frmat, text):
    _require_yang_glue()
    start_errs = yang_glue.errmsg_count()
    err_msg = None
    if start_errs:
        errs = ['%d prior yang errors' % start_errs] + _pull_error_msgs()
        err_msg = '\n'.join(errs)
        start_errs = 0

    retval = yang_glue.validate(frmat, text)
    done_errs = yang_glue.errmsg_count()
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

    if done_errs != start_errs:
        print('warning: yang errors with passed validation:\n%s' %
              ('\n'.join(_pull_error_msgs())), file=sys.stderr)

    return True


def convert(from_frmat, text, to_frmat):
    _require_yang_glue()
    start_errs = yang_glue.errmsg_count()
    err_msg = None
    if start_errs:
        errs = ['%d prior yang errors' % start_errs] + _pull_error_msgs()
        err_msg = '\n'.join(errs)
        start_errs = 0

    retval = yang_glue.convert(from_frmat, text, to_frmat)
    done_errs = yang_glue.errmsg_count()
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

    if done_errs != start_errs:
        print('warning: yang errors with passed validation:\n%s' %
              ('\n'.join(_pull_error_msgs())), file=sys.stderr)

    return retval


def _on_load():
    _require_yang_glue()
    tapspath = os.path.abspath(os.path.dirname(__file__))
    modulepath = os.path.join(tapspath, 'modules')
    try:
        yang_glue.initialize(modulepath)
    except Exception as e:
        msgs = _pull_error_msgs()
        for msg in msgs:
            print(msg)
        raise e


if yang_glue is not None:
    _on_load()
