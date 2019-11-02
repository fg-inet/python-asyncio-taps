
#include <mcrx/libmcrx.h>
#include <stdlib.h>
#include <stdio.h>
#include <inttypes.h>

/*
 * To test (linux), using docker to open a tunnel to a traffic source:
 *
 * ./build_dependencies.sh
 * export LD_LIBRARY_PATH=$DEPS/install/lib
 * sudo ip route add 23.212.185.0/24 dev docker0
 * AMTIP=$(python dependencies/build/libmcrx/driad.py 23.212.185.5)
 * docker run -d --rm --name amtgw --privileged grumpyoldtroll/amtgw $AMTIP
 * 
 * python examples/yang_example/yangClient.py -f examples/yang_example/test-mcast-receive.json
 */

extern "C" {
struct PyObject;
void* initialize(struct PyObject* loop,
    void (*add_sock_cb)(struct PyObject*, void* handle, int fd,
      int (*do_read)(intptr_t handle, int fd)),
    void (*remove_sock_cb)(struct PyObject*, int fd));
int join(void* init_obj, struct PyObject* conn, char* source, char* group, int port,
    int (*got_data)(struct PyObject* conn, int len, uint8_t* data));
void cleanup(void* init_obj);
}

struct ctx_info {
  struct PyObject* py_obj;
  void (*add_sock_cb)(struct PyObject*, void* handle, int fd,
      int (*do_receive)(intptr_t handle, int fd));
  void (*remove_sock_cb)(struct PyObject*, int fd);
  int log_callbacks;
};
static struct ctx_info g_ctx_info = { 0 };

struct sub_info {
  int packets;
  int (*got_data)(struct PyObject* conn, int len, uint8_t* data);
  struct PyObject* conn;
};

static int receive_cb(struct mcrx_packet* pkt) {
  mcrx_subscription* sub = mcrx_packet_get_subscription(pkt);
  struct sub_info* info = (struct sub_info*)mcrx_subscription_get_userdata(sub);
  info->packets++;
  uint8_t* data = 0;
  int len = mcrx_packet_get_contents(pkt, &data);
  info->got_data(info->conn, len, data);
  mcrx_packet_unref(pkt);
  return MCRX_RECEIVE_CONTINUE;
}

struct receiver_info {
  intptr_t handle;
  int fd;
  int (*do_receive)(intptr_t handle, int fd);
};

static int stub_receive(intptr_t handle, int fd) {
  struct receiver_info* rx_info = (struct receiver_info*)handle;
  return rx_info->do_receive(rx_info->handle, rx_info->fd);
}

static int add_socket_cb(
    struct mcrx_ctx* ctx,
    intptr_t handle,
    int fd,
    int (*do_receive)(intptr_t handle, int fd)) {
  struct ctx_info* info = (struct ctx_info*)mcrx_ctx_get_userdata(ctx);
  struct PyObject* py_obj = info->py_obj;

  struct receiver_info* rx_info = (struct receiver_info*)malloc(
      sizeof(struct receiver_info));
  rx_info->handle = handle;
  rx_info->fd = fd;
  rx_info->do_receive = do_receive;

  do_receive(handle, fd);

  info->add_sock_cb(py_obj, (void*)rx_info, fd, stub_receive);
  return 0;
}

static int remove_socket_cb(
    struct mcrx_ctx* ctx,
    int fd) {
  struct ctx_info* info = (struct ctx_info*)mcrx_ctx_get_userdata(ctx);
  struct PyObject* py_obj = info->py_obj;
  info->remove_sock_cb(py_obj, fd);
  return 0;
}

static void string_log_fn(
    struct mcrx_ctx *ctx,
    int priority,
    const char *file,
    int line,
    const char *fn,
    const char *str) {
  (void)priority;
  struct ctx_info *info = (struct ctx_info*)mcrx_ctx_get_userdata(ctx);
  info->log_callbacks += 1;
  // TBD: callback to python
  fprintf(stderr, "string_cb(%s:%d(%s)): %s", file, line, fn, str);
}

/*
 * args:
 * loop: the callback object passed with add_socket and remove_socket
 *       callbacks.
 *
 * returns: a ctx object, which later should be passed to
 * call_join
 */
struct PyObject;
void* initialize(struct PyObject* loop,
    void (*add_sock_cb)(struct PyObject*, void* handle, int fd,
      int (*do_read)(intptr_t handle, int fd)),
    void (*remove_sock_cb)(struct PyObject*, int fd)) {
  int err;
  struct mcrx_ctx* ctx = NULL;
  err = mcrx_ctx_new(&ctx);
  if (!ctx) {
    return NULL;
  }
  struct ctx_info* info = (struct ctx_info*)malloc(sizeof(struct ctx_info));
  if (!info) {
    ctx = mcrx_ctx_unref(ctx);
    return NULL;
  }
  info->py_obj = loop;
  info->add_sock_cb = add_sock_cb;
  info->remove_sock_cb = remove_sock_cb;
  mcrx_ctx_set_userdata(ctx, (intptr_t)info);
  mcrx_ctx_set_log_priority(ctx, MCRX_LOGLEVEL_WARNING);
  err = mcrx_ctx_set_receive_socket_handlers(ctx,
      add_socket_cb, remove_socket_cb);
  if (err != 0) {
    ctx = mcrx_ctx_unref(ctx);
    return NULL;
  }

  return ctx;
}

void cleanup(void* init_obj) {
  struct mcrx_ctx* ctx = (struct mcrx_ctx*)init_obj;
  if (!ctx) {
    return;
  }
  struct ctx_info* info = (struct ctx_info*)mcrx_ctx_get_userdata(ctx);
  if (info) {
    mcrx_ctx_set_userdata(ctx, 0);
    free(info);
  }
  mcrx_ctx_unref(ctx);
}

int join(void* init_obj, struct PyObject* conn, char* source, char* group, int port,
    int (*got_data)(struct PyObject* conn, int len, uint8_t* data)) {
  int err = 0;
  struct mcrx_ctx* ctx = (struct mcrx_ctx*)init_obj;
  if (!ctx) {
    return MCRX_ERR_NULLARG;
  }

  struct mcrx_subscription_config cfg = MCRX_SUBSCRIPTION_CONFIG_INIT;
  err = mcrx_subscription_config_pton(&cfg, source, group);
  if (err != 0) {
    return err;
  }
  cfg.port = port;

  struct mcrx_subscription* sub = 0;
  err = mcrx_subscription_new(ctx, &cfg, &sub);
  if (err != 0) {
    return err;
  }

  struct sub_info* subinfo = (struct sub_info*)malloc(sizeof(struct sub_info));
  if (!subinfo) {
    mcrx_subscription_unref(sub);
    return MCRX_ERR_NOMEM;
  }

  subinfo->conn = conn;
  subinfo->got_data = got_data;

  mcrx_subscription_set_receive_cb(sub, receive_cb);
  mcrx_subscription_set_userdata(sub, (intptr_t)subinfo);

  err = mcrx_subscription_join(sub);
  if (err != 0) {
    mcrx_subscription_unref(sub);
    free(subinfo);
    return err;
  }
  return 0;
}

