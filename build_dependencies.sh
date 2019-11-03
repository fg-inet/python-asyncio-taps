#!/bin/bash
set -e
set -x

# INSTALL_PATH=${HOME}/local_install
DEPS=$PWD/dependencies
mkdir -p $DEPS/build

if [ "${INSTALL_PATH}" != "" ]; then
  mkdir -p ${INSTALL_PATH}
fi

if ! which cmake || ! which automake; then
  sudo apt-get update
  # libyang build&run requirements
  sudo apt-get install -y libpcre3-dev cmake

  # libmcrx build requirements
  sudo apt-get install -y autoconf automake libtool
fi

if ! [ -e $DEPS/build/libyang ]; then
  git clone https://github.com/CESNET/libyang $DEPS/build/libyang
fi
mkdir -p $DEPS/build/libyang/build
pushd $DEPS/build/libyang/build
CMAKE_EXTRA=
if [ "${INSTALL_PATH}" != "" ]; then
  CMAKE_EXTRA="-DCMAKE_INSTALL_PREFIX=${INSTALL_PATH}"
fi
cmake ${CMAKE_EXTRA} ..
make
popd

if ! [ -e $DEPS/build/libmcrx/ ]; then
  git clone https://github.com/GrumpyOldTroll/libmcrx $DEPS/build/libmcrx
fi
pushd $DEPS/build/libmcrx
./autogen.sh
CONF_EXTRA=
if [ "${INSTALL_PATH}" != "" ]; then
  CONF_EXTRA="--prefix=${INSTALL_PATH}"
fi
./configure ${CONF_EXTRA}
make
popd

pushd $DEPS/build/libyang/build && make install && popd
pushd $DEPS/build/libmcrx && make install && popd

# works on linux, not mac:
#g++ $DEPS/build/validate_yang.o -shared -L ${INSTALL_PATH}/lib -lyang -Wl,-soname,libyangcheck.so -o ${INSTALL_PATH}/lib/libyangcheck.so

#g++ -c -fPIC -isystem ${INSTALL_PATH}/include pytaps/validate_yang.cxx -o $DEPS/build/validate_yang.o
#g++ $DEPS/build/validate_yang.o -shared -L ${INSTALL_PATH}/lib -lyang -o ${INSTALL_PATH}/lib/libyangcheck.so

#g++ -c -fPIC -isystem ${INSTALL_PATH}/include pytaps/multicast_glue.cxx -o $DEPS/build/multicast_glue.o
#g++ $DEPS/build/multicast_glue.o -shared -L ${INSTALL_PATH}/lib -lmcrx -o ${INSTALL_PATH}/lib/libmulticast_glue.so

#export LD_LIBRARY_PATH=${INSTALL_PATH}/lib

if [ "${INSTALL_PATH}" != "" ]; then
  echo "make sure LD_LIBRARY_PATH has ${INSTALL_PATH}/lib"
fi

