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
g++ -c -fPIC -I $HOME/local-installs/include validate_yang.cxx -o validate_yang.o
g++ validate_yang.o -shared -L $HOME/local-installs/lib -lyang -Wl,-soname,libyangcheck.so -o libyangcheck.so
export LD_LIBRARY_PATH=$HOME/local-installs/lib

relevant environment variables:
  LIBYANG_SEARCH_DIR
*/

#include <stdlib.h>
#include <string.h>
#include <libyang/libyang.h>   // https://github.com/CESNET/libyang
#include <deque>
#include <string>
#include <sstream>

extern "C" {
  int validate(int format, const char* text);
  const char* convert(int from_format, const char* text, int to_format);

  int get_log_level();
  int set_log_level(int err_level);  // returns prior val
  void initialize(const char* search_path);  // NULL=env(LIBYANG_SEARCH_DIR)
  void cleanup();

  int errmsg_count();
  const char* errmsg(int idx);
  void clear_errors();
}

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

int errmsg_count() {
  return static_cast<int>(g_vctx.errors.size());
}

const char* errmsg(int idx) {
  if (idx < 0 || idx >= (int)g_vctx.errors.size()) {
    return NULL;
  }
  return g_vctx.errors[idx].c_str();
}

void clear_errors() {
  g_vctx.errors.clear();
}

int validate(int format, const char* text) {
  if (!g_vctx.loaded_ctx) {
    initialize(NULL);
    if (!g_vctx.loaded_ctx) {
      log_cb(LY_LLERR, "validate: failed to initialize", __FILE__);
      return -1;
    }
  }

  LYD_FORMAT data_fmt;
  switch(format) {
    case LYD_XML:
    case LYD_JSON:
      data_fmt = static_cast<LYD_FORMAT>(format);
      break;
    default:
      log_cb(LY_LLERR, "validate: unknown validation format", __FILE__);
      return -1;
  }

  int options = LYD_OPT_DATA | LYD_OPT_CONFIG;
  struct lyd_node* in_data = lyd_parse_mem(g_vctx.loaded_ctx, text, data_fmt, options);
  if (!in_data) {
    return -1;
  }
  lyd_free(in_data);

  return 0;
}

const char* convert(int from_format, const char* text, int to_format) {
  if (!g_vctx.loaded_ctx) {
    initialize(NULL);
    if (!g_vctx.loaded_ctx) {
      log_cb(LY_LLERR, "validate: failed to initialize", __FILE__);
      return NULL;
    }
  }

  LYD_FORMAT from_fmt;
  switch(from_format) {
    case LYD_XML:
    case LYD_JSON:
      from_fmt = static_cast<LYD_FORMAT>(from_format);
      break;
    default:
      log_cb(LY_LLERR, "convert: unknown conversion format(from)", __FILE__);
      return NULL;
  }

  LYD_FORMAT to_fmt;
  switch(to_format) {
    case LYD_XML:
    case LYD_JSON:
      to_fmt = static_cast<LYD_FORMAT>(to_format);
      break;
    default:
      log_cb(LY_LLERR, "convert: unknown conversion format(from)", __FILE__);
      return NULL;
  }

  int options = LYD_OPT_DATA | LYD_OPT_CONFIG;
  struct lyd_node* in_data = lyd_parse_mem(g_vctx.loaded_ctx, text, from_fmt, options);
  if (!in_data) {
    log_cb(LY_LLERR, "convert: failed lyd_parse_mem", __FILE__);
    return NULL;
  }
  char* output_mem = 0;
  int out_options = LYP_FORMAT | LYP_WD_TRIM;
  int rc = lyd_print_mem(&output_mem, in_data, to_fmt, out_options);
  if (rc) {
    log_cb(LY_LLERR, "convert: failed lyd_print_mem", __FILE__);
  }
  if (!output_mem) {
    g_vctx.last_convert.clear();
    lyd_free(in_data);
    return NULL;
  }
  // jake 2019-04-30: TBD: am i really too cowardly to hold this raw pointer instead of copying an extra time just to use a std::string?
  g_vctx.last_convert = output_mem;
  free(output_mem);
  lyd_free(in_data);
  return g_vctx.last_convert.c_str();
}

int get_log_level() {
  return static_cast<int>(g_vctx.log_level);
}

int set_log_level(int err_level) {
  switch(err_level) {
    case LY_LLERR:
    case LY_LLWRN:
    case LY_LLVRB:
    case LY_LLDBG:
      break;
    default:
      log_cb(LY_LLERR, "set_log_level: bad err_level", __FILE__);
      return LY_LLERR;
  }

  g_vctx.log_level = static_cast<LY_LOG_LEVEL>(err_level);
  int prior_val = static_cast<int>(ly_verb(g_vctx.log_level));
  return prior_val;
}

void initialize(const char* search_path) {
  g_vctx.errors.clear();
  if (g_vctx.loaded_ctx) {
    log_cb(LY_LLERR, "initialize: existing context already loaded", __FILE__);
    return;
  }

  if (!search_path) {
    // jake 2019-04-30: TBD: switch to getenv_s?
    search_path = getenv("LIBYANG_SEARCH_DIR");
  }
  if (!search_path || !search_path[0]) {
    log_cb(LY_LLERR, "initialize: null search path and no LIBYANG_SEARCH_DIR", __FILE__);
    return;
  }

  g_vctx.search_path = search_path;
  ly_set_log_clb(log_cb, 1);

  int options = LY_CTX_NOYANGLIBRARY | LY_CTX_ALLIMPLEMENTED;
  struct ly_ctx* ctx = ly_ctx_new(g_vctx.search_path.c_str(), options);
  if (!ctx) {
    log_cb(LY_LLERR, "initialize: failed ly_ctx_new", __FILE__);
    return;
  }

  const char* taps_version = NULL;
  const struct lys_module *mod = ly_ctx_load_module(ctx, "ietf-taps-api", taps_version);
  if (!mod) {
    ly_ctx_destroy(ctx, 0);
    log_cb(LY_LLERR, "initialize: failed ly_ctx_load_module(ietf-taps-api)", __FILE__);
    return;
  }
  g_vctx.loaded_ctx = ctx;
}

void cleanup() {
  if (!g_vctx.loaded_ctx) {
    log_cb(LY_LLERR, "cleanup: no context loaded", __FILE__);
    return;
  }
  g_vctx.search_path.clear();
  ly_ctx_destroy(g_vctx.loaded_ctx, 0);
  g_vctx.loaded_ctx = NULL;
}

