#!/bin/bash
set -e
set -x

DEPS=$PWD/dependencies
mkdir -p $DEPS/install
mkdir -p $DEPS/build

if true; then
  git clone https://github.com/CESNET/libyang $DEPS/build/libyang
  mkdir -p $DEPS/build/libyang/build
  pushd $DEPS/build/libyang/build
  cmake -DCMAKE_INSTALL_PREFIX=$DEPS/install ..
  make
  make install
  popd

  git clone https://github.com/GrumpyOldTroll/libmcrx $DEPS/build/libmcrx
  pushd $DEPS/build/libmcrx
  autoreconf --install --symlink
  ./configure --prefix=$DEPS/install
  make
  make install
  popd
fi

# works on linux, not mac:
#g++ $DEPS/build/validate_yang.o -shared -L $DEPS/install/lib -lyang -Wl,-soname,libyangcheck.so -o $DEPS/install/lib/libyangcheck.so
g++ -c -fPIC -isystem $DEPS/install/include PyTAPS/validate_yang.cxx -o $DEPS/build/validate_yang.o
g++ $DEPS/build/validate_yang.o -shared -L $DEPS/install/lib -lyang -o $DEPS/install/lib/libyangcheck.so

g++ -c -fPIC -isystem $DEPS/install/include PyTAPS/multicast_glue.cxx -o $DEPS/build/multicast_glue.o
g++ $DEPS/build/multicast_glue.o -shared -L $DEPS/install/lib -lmcrx -o $DEPS/install/lib/libmulticast_glue.so

#export LD_LIBRARY_PATH=$DEPS/install/lib

