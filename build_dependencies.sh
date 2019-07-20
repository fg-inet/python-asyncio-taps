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
fi

g++ -c -fPIC -isystem $DEPS/install/include PyTAPS/validate_yang.cxx -o $DEPS/build/validate_yang.o
#g++ $DEPS/build/validate_yang.o -shared -L $DEPS/install/lib -lyang -Wl,-soname,libyangcheck.so -o $DEPS/install/lib/libyangcheck.so
g++ $DEPS/build/validate_yang.o -shared -L $DEPS/install/lib -lyang -o $DEPS/install/lib/libyangcheck.so

#export LD_LIBRARY_PATH=$DEPS/install/lib

