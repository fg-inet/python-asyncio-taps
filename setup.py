#!/usr/bin/env python3

from setuptools import setup, find_packages
from distutils.core import setup, Extension
import os
import os.path

include_dirs = ['/usr/local/include']
library_dirs = ['/usr/local/lib']

install_path = os.getenv('INSTALL_PATH', None)
if install_path:
    include_dirs.append('%s/include' % install_path)
    library_dirs.append('%s/lib' % install_path)

yangcheck_ext = Extension('yang_glue',
                    define_macros = [('MAJOR_VERSION', '1'),
                                     ('MINOR_VERSION', '0')],
                    include_dirs = include_dirs,
                    libraries = ['yang'],
                    library_dirs = library_dirs,
                    sources = ['pytaps/yang_glue.cxx'])

multicast_glue_ext = Extension('multicast_glue',
                    define_macros = [('MAJOR_VERSION', '1'),
                                     ('MINOR_VERSION', '0')],
                    include_dirs = include_dirs,
                    libraries = ['mcrx'],
                    library_dirs = library_dirs,
                    sources = ['pytaps/multicast_glue.cxx'])

setup(
    name="pytaps",
    version="0.1",
    packages=find_packages(),

    package_data={
        # If any package contains *.txt or *.rst files, include them:
        '': ['*.txt', '*.rst'],
    },

    # metadata to display on PyPI
    author="Max Franke",
    author_email="mfranke@inet.tu-berlin.de",
    description="TAPS (Transport Services) API Reference implementation for IETF",
    keywords="taps ietf implementation",
    url="https://github.com/fg-inet/python-asyncio-taps",
    project_urls={
        "Working Group": "https://datatracker.ietf.org/wg/taps/about/",
        "Documentation": "https://pytaps.readthedocs.io/en/latest/index.html",
        "Source Code": "https://github.com/fg-inet/python-asyncio-taps",
    },
    classifiers=[
        'License :: OSI Approved :: Python Software Foundation License'
    ],
    data_files=[('pytaps/modules', [
        'pytaps/modules/ietf-taps-api.yang',
        'pytaps/modules/iana-if-type@2019-02-08.yang',
        'pytaps/modules/ietf-interfaces@2018-02-20.yang',
        'pytaps/modules/ietf-yang-types@2013-07-15.yang',
        'pytaps/modules/ietf-inet-types@2013-07-15.yang',])],
    ext_modules=[yangcheck_ext, multicast_glue_ext],
    long_description='''
This is an implementation of a transport system as described by the TAPS (Transport Services) Working Group in the IETF in https://tools.ietf.org/html/draft-ietf-taps-interface-04. The full documentation can be found on https://pytaps.readthedocs.io/en/latest/index.html.

A transport system is a novel way to offer transport layer services to the application layer.

It provides an interface on top of multiple different transport protocols, such as TCP, SCTP, UDP, or QUIC. Instead of having to choose a transport protocol itself, the application only provides abstract requirements (*Transport Properties*), e.g., *Reliable Data Transfer*. The transport system maps then maps these properties to specific transport protocols, possibly trying out multiple different protocols in parallel. Furthermore, it can select between multiple local interfaces and remote IP addresses.

TAPS is currently being standardized in the [IETF TAPS Working Group](https://datatracker.ietf.org/wg/taps/about/):

- [Architecture](https://datatracker.ietf.org/doc/draft-ietf-taps-arch/)
- [Interface](https://datatracker.ietf.org/doc/draft-ietf-taps-interface/)
- [Implementation considerations](https://datatracker.ietf.org/doc/draft-ietf-taps-impl/)

People interested in participating in TAPS can [join the mailing list](https://www.ietf.org/mailman/listinfo/taps).
'''

    # could also include long_description, download_url, etc.
)
