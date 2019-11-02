/*
 * Copyright 2019, Akamai Technologies, Inc.
 * Jake Holland <jholland@akamai.com>
 * (MIT-licensed, please see LICENSE file in python-asyncio-taps for details)
 */

/*
best known reference for libyang:
https://github.com/CESNET/libyang/blob/master/src/libyang.h.in

git clone https://github.com/CESNET/libyang
mkdir libyang/build
cd libyang/build
cmake -DCMAKE_INSTALL_PREFIX=$HOME/local-installs ..
make && make install
cd ../..

relevant environment variables:
  LIBYANG_SEARCH_DIR
*/

#include <stdlib.h>
#include <string.h>
#include <libyang/libyang.h>   // https://github.com/CESNET/libyang
#include <deque>
#include <string>
#include <sstream>

#define PY_SSIZE_T_CLEAN  /* Make "s#" use Py_ssize_t rather than int. */
#include <Python.h>

// jake 2019-04-30: TBD: structuring this as a context object even though
//   libyang log level and callback is actually global, so only 1 can be
//   supported right now.
struct validate_ctx {
  ly_ctx* loaded_ctx;
  LY_LOG_LEVEL log_level;
  std::string search_path;
  std::deque<std::string> errors;
  unsigned int max_errs;
  std::string last_convert;

  validate_ctx():
    loaded_ctx(NULL),
    log_level(LY_LLERR),
    max_errs(15)
  {}
};
static validate_ctx g_vctx;
static PyObject* YangGlueError = NULL;

static const char* log_str(LY_LOG_LEVEL level) {
  switch(level) {
    case LY_LLERR:
      return "ERR";
    case LY_LLWRN:
      return "WARN";
    case LY_LLVRB:
      return "VERBOSE";
    case LY_LLDBG:
      return "DEBUG";
    default:
      return "UNKNOWN";
  }
}

static void log_cb(LY_LOG_LEVEL level, const char* msg, const char* path) {
  /*
   * Deliberately suppressing a dumb error here.  Reasoning is as follows:
   *
   * If you pull the yang model from here:
   *   http://www.iana.org/assignments/yang-parameters/iana-if-type@2019-02-08.yang
   * (or the latest model, as referenced from here:)
   *   https://www.iana.org/assignments/iana-if-type/iana-if-type.xhtml
   *
   * You'll see that it contains 2 identically-named revisions:
   *   revision 2018-06-28 {
   *     description
   *       "Registered ifType 294.";
   *   }
   *   revision 2018-06-28 {
   *     description
   *       "Registered ifType 293.";
   *   }
   *
   * This seems as required according to the YANG 1.1 format:
   *  https://tools.ietf.org/html/rfc6020#section-7.1.9
   *
   * However, it would perhaps be better to allow a disambiguation of the
   * revision key, since it also seems sane to provide a warning.
   *
   * As of 2019-05-02, libyang provides a WARN level with a null path
   * for this warning, and this exact text.  Hopefully one day we can
   * get a better upstream removal of this warning (either by extending
   * YANG and disambiguating the actual 2 different revisions in if-type,
   * or by adding it as an expected and ignorable warning in libyang),
   * but for now at least I'm refusing to propagate it.
   * -Jake
   */
  if (level == LY_LLWRN &&
      !strcmp(msg, "Module's revisions are not unique (2018-06-28).") &&
      (!path || !strcmp(path, "iana-if-type@2019-02-08.yang"))) {
    return;
  }
  if (g_vctx.errors.size() > g_vctx.max_errs) {
    g_vctx.errors.pop_front();
  }
  std::ostringstream sm;
  sm << log_str(level) << " in " << (path?path:"null_path") << ": " << msg;
  g_vctx.errors.push_back(sm.str());
}

static PyObject*
yg_errmsg_count(PyObject* dummy, PyObject* args) {
  if (!PyArg_ParseTuple(args, "")) {
    return NULL;
  }
  PyObject* result = Py_BuildValue("i",
      static_cast<int>(g_vctx.errors.size()));
  return result;
}

static PyObject*
yg_errmsg(PyObject* dummy, PyObject* args) {
  int idx = 0;
  if (!PyArg_ParseTuple(args, "i", &idx)) {
    return NULL;
  }
  if (idx < 0 || idx >= (int)g_vctx.errors.size()) {
    PyErr_SetString(YangGlueError, "bad index in errmsg");
    return NULL;
  }

  PyObject* result = Py_BuildValue("s", g_vctx.errors[idx].c_str());
  return result;
}

static PyObject*
yg_clear_errors(PyObject* dummy, PyObject* args) {
  if (!PyArg_ParseTuple(args, "")) {
    return NULL;
  }
  g_vctx.errors.clear();
  Py_INCREF(Py_None);
  return Py_None;
}

static PyObject*
yg_get_log_level(PyObject* dummy, PyObject* args) {
  if (!PyArg_ParseTuple(args, "")) {
    return NULL;
  }
  PyObject* result = Py_BuildValue("i",
      static_cast<int>(g_vctx.log_level));
  return result;
}

static PyObject*
yg_set_log_level(PyObject* dummy, PyObject* args) {
  int err_level = 0;
  if (!PyArg_ParseTuple(args, "i", &err_level)) {
    return NULL;
  }
  switch(err_level) {
    case LY_LLERR:
    case LY_LLWRN:
    case LY_LLVRB:
    case LY_LLDBG:
      break;
    default:
      PyErr_SetString(YangGlueError, "bad err_level in set_log_level");
      return NULL;
  }
  int prior_val = static_cast<int>(ly_verb(g_vctx.log_level));
  PyObject* result = Py_BuildValue("i", static_cast<int>(prior_val));
  if (!result) {
    return NULL;
  }

  g_vctx.log_level = static_cast<LY_LOG_LEVEL>(err_level);
  return result;
}

static PyObject*
yg_initialize(PyObject* dummy, PyObject* args) {
  const char* search_path = NULL;
  if (!PyArg_ParseTuple(args, "|s", &search_path)) {
    return NULL;
  }

  g_vctx.errors.clear();
  if (g_vctx.loaded_ctx) {
    PyErr_SetString(YangGlueError, "yang_glue: initialize when already initialized");
    return NULL;
  }

  if (!search_path) {
    // jake 2019-04-30: TBD: switch to getenv_s?
    search_path = getenv("LIBYANG_SEARCH_DIR");
  }
  if (!search_path || !search_path[0]) {
    PyErr_SetString(YangGlueError, "yang_glue: initialize null search path and no LIBYANG_SEARCH_DIR");
    return NULL;
  }

  g_vctx.search_path = search_path;
  ly_set_log_clb(log_cb, 1);

  int options = LY_CTX_NOYANGLIBRARY | LY_CTX_ALLIMPLEMENTED;
  struct ly_ctx* ctx = ly_ctx_new(g_vctx.search_path.c_str(), options);
  if (!ctx) {
    PyErr_SetString(YangGlueError, "yang_glue: initialize failed ly_ctx_new");
    return NULL;
  }

  const char* taps_version = NULL;
  const struct lys_module *mod = ly_ctx_load_module(ctx, "ietf-taps-api", taps_version);
  if (!mod) {
    PyErr_SetString(YangGlueError, "yang_glue: initialize failed ly_ctx_load_module(ietf-taps-api)");
    ly_ctx_destroy(ctx, 0);
    return NULL;
  }
  g_vctx.loaded_ctx = ctx;

  Py_INCREF(Py_None);
  return Py_None;
}

static PyObject*
yg_cleanup(PyObject* dummy, PyObject* args) {
  if (!PyArg_ParseTuple(args, "")) {
    return NULL;
  }

  if (!g_vctx.loaded_ctx) {
    PyErr_SetString(YangGlueError, "yang_glue: cleanup with no context loaded");
    return NULL;
  }

  g_vctx.search_path.clear();
  ly_ctx_destroy(g_vctx.loaded_ctx, 0);
  g_vctx.loaded_ctx = NULL;

  Py_INCREF(Py_None);
  return Py_None;
}

static PyObject*
yg_validate(PyObject* dummy, PyObject* args) {
  int format = 0;
  const char* text = NULL;
  if (!PyArg_ParseTuple(args, "is", &format, &text)) {
    return NULL;
  }

  if (!g_vctx.loaded_ctx) {
    PyObject* init_args = Py_BuildValue("");
    PyObject* init_result = yg_initialize(dummy, init_args);
    Py_DECREF(init_args);
    if (!init_result || !g_vctx.loaded_ctx) {
      // PyErr_SetString(YangGlueError, "yang_glue: validate failed to initialize");
      return NULL;
    }
    Py_DECREF(init_result);
  }

  LYD_FORMAT data_fmt;
  switch(format) {
    case LYD_XML:
    case LYD_JSON:
      data_fmt = static_cast<LYD_FORMAT>(format);
      break;
    default:
      PyErr_SetString(YangGlueError, "yang_glue: unknown data format");
      return NULL;
  }

  int retval;
  int options = LYD_OPT_DATA | LYD_OPT_CONFIG;
  struct lyd_node* in_data = lyd_parse_mem(g_vctx.loaded_ctx, text, data_fmt, options);
  if (in_data) {
    lyd_free(in_data);
    retval = 0;
  } else {
    retval = -1;
  }

  PyObject* result = Py_BuildValue("i", static_cast<int>(retval));
  if (!result) {
    return NULL;
  }

  return result;
}

static PyObject*
yg_convert(PyObject* dummy, PyObject* args) {
  int from_format;
  const char* text;
  int to_format;

  if (!PyArg_ParseTuple(args, "isi", &from_format, &text, &to_format)) {
    return NULL;
  }

  if (!g_vctx.loaded_ctx) {
    PyObject* init_args = Py_BuildValue("");
    PyObject* init_result = yg_initialize(dummy, init_args);
    Py_DECREF(init_args);
    if (!init_result || !g_vctx.loaded_ctx) {
      // PyErr_SetString(YangGlueError, "yang_glue: convert failed to initialize");
      return NULL;
    }
    Py_DECREF(init_result);
  }

  LYD_FORMAT from_fmt;
  switch(from_format) {
    case LYD_XML:
    case LYD_JSON:
      from_fmt = static_cast<LYD_FORMAT>(from_format);
      break;
    default:
      PyErr_SetString(YangGlueError, "yang_glue: unknown conversion format(from)");
      return NULL;
  }

  LYD_FORMAT to_fmt;
  switch(to_format) {
    case LYD_XML:
    case LYD_JSON:
      to_fmt = static_cast<LYD_FORMAT>(to_format);
      break;
    default:
      PyErr_SetString(YangGlueError, "yang_glue: unknown conversion format(to)");
      return NULL;
  }

  int options = LYD_OPT_DATA | LYD_OPT_CONFIG;
  struct lyd_node* in_data = lyd_parse_mem(g_vctx.loaded_ctx, text, from_fmt, options);
  if (!in_data) {
    PyErr_SetString(YangGlueError, "yang_glue: convert failed lyd_parse_mem");
    return NULL;
  }
  char* output_mem = 0;
  int out_options = LYP_FORMAT | LYP_WD_TRIM;
  int rc = lyd_print_mem(&output_mem, in_data, to_fmt, out_options);
  if (!output_mem) {
    if (rc) {
      PyErr_SetString(YangGlueError, "yang_glue: convert failed lyd_print_mem");
    } else {
      PyErr_SetString(YangGlueError, "yang_glue: unkonwn error in lyd_print_mem (null output)");
    }
    g_vctx.last_convert.clear();
    lyd_free(in_data);
    return NULL;
  } else {
    if (rc) {
      PyErr_SetString(YangGlueError, "yang_glue: convert failed with output from lyd_print_mem");
    }
  }

  PyObject* result = Py_BuildValue("s", output_mem);
  if (!result) {
    free(output_mem);
    lyd_free(in_data);
    return NULL;
  }

  free(output_mem);
  lyd_free(in_data);
  return result;
}

static PyMethodDef YangGlueMethods[] = {
  {"initialize", yg_initialize, METH_VARARGS,
    "initialize(mod_path): setup mod_path as the module search path."},
  {"cleanup", yg_cleanup, METH_VARARGS,
    "cleanup(): tear down yang handling"},
  {"validate", yg_validate, METH_VARARGS,
    "validate(1|2, text): 1=XML, 2=JSON (these directly match LYD_XML/JSON from enum LYD_FORMAT in libyang), returns 0 for success, -1 for error."},
  {"convert", yg_convert, METH_VARARGS,
    "convert(1|2, text, 2|1): 1=XML, 2=JSON (from LYD_XML/JSON in enum LYD_FORMAT in libyang).  Parse text as first format, return string converted to 2nd format."},
  {"errmsg_count", yg_errmsg_count, METH_VARARGS,
    "errmsg_count(): returns integer count of detected uncleared errors"},
  {"clear_errors", yg_clear_errors, METH_VARARGS,
    "clear_errors(): clears errors"},
  {"errmsg", yg_errmsg, METH_VARARGS,
    "errmsg(idx): returns text of errmsg(idx)."},
  {"set_log_level", yg_set_log_level, METH_VARARGS,
    "set_log_level(val): returns prior value, sets to val.  options are 0=error, 1=warning, 2=verbose, 3=debug (default 0)"},
  {"get_log_level", yg_get_log_level, METH_VARARGS,
    "get_log_level(): returns current log level. options are 0=error, 1=warning, 2=verbose, 3=debug (default 0)"},
  {NULL, NULL, 0, NULL}
};

// https://docs.python.org/3/c-api/module.html
static struct PyModuleDef ygmodule = {
    .m_base = PyModuleDef_HEAD_INIT,
    .m_name = "yang_glue",
    .m_doc = "a very stupid wrapper around a few libyang features (https://github.com/CESNET/libyang)",
    .m_size = -1,
    .m_methods = YangGlueMethods
    // .m_slots = NULL,
    // .m_reload = NULL,
    // .m_clear = NULL,
    // .m_free = NULL
};

PyMODINIT_FUNC
PyInit_yang_glue(void)
{
  PyObject *m;

  m = PyModule_Create(&ygmodule);
  if (m == NULL)
      return NULL;

  YangGlueError = PyErr_NewException("yang_glue.error", NULL, NULL);
  Py_XINCREF(YangGlueError);
  if (PyModule_AddObject(m, "error", YangGlueError) < 0) {
      Py_XDECREF(YangGlueError);
      Py_CLEAR(YangGlueError);
      Py_DECREF(m);
      return NULL;
  }

  return m;
}

