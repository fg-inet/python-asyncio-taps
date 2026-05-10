#!/usr/bin/env python3

import os
from pathlib import Path

from setuptools import Extension, find_packages, setup


ROOT = Path(__file__).parent
README = ROOT / "README.md"


def env_flag(name: str) -> bool:
    value = os.getenv(name, "")
    return value.lower() in {"1", "true", "yes", "on"}


include_dirs = ["/usr/local/include"]
library_dirs = ["/usr/local/lib"]

install_path = os.getenv("INSTALL_PATH")
if install_path:
    include_dirs.append(f"{install_path}/include")
    library_dirs.append(f"{install_path}/lib")

ext_modules = []
if env_flag("PYTAPS_BUILD_EXTENSIONS"):
    ext_modules = [
        Extension(
            "yang_glue",
            define_macros=[("MAJOR_VERSION", "1"), ("MINOR_VERSION", "0")],
            include_dirs=include_dirs,
            libraries=["yang"],
            library_dirs=library_dirs,
            sources=["pytaps/yang_glue.cxx"],
        ),
    ]


setup(
    name="pytaps",
    version="0.1.0",
    description="Asyncio-based Transport Services (TAPS) reference implementation",
    long_description=README.read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    author="Max Franke",
    author_email="mfranke@inet.tu-berlin.de",
    url="https://github.com/fg-inet/python-asyncio-taps",
    project_urls={
        "Working Group": "https://datatracker.ietf.org/wg/taps/about/",
        "Architecture RFC": "https://www.rfc-editor.org/rfc/rfc9621.html",
        "API RFC": "https://www.rfc-editor.org/rfc/rfc9622.html",
        "Implementation RFC": "https://www.rfc-editor.org/rfc/rfc9623.html",
        "Documentation": "https://pytaps.readthedocs.io/en/latest/index.html",
        "Source Code": "https://github.com/fg-inet/python-asyncio-taps",
    },
    packages=find_packages(),
    include_package_data=True,
    package_data={
        "pytaps": ["modules/*.yang"],
    },
    python_requires=">=3.10",
    install_requires=[
        "netifaces>=0.11",
    ],
    extras_require={
        "test": [
            "pytest>=8",
            "pytest-asyncio>=0.23",
            "pytest-timeout>=2.3",
        ],
        "docs": [
            "sphinx>=7",
        ],
        "yang": [],
        "multicast": [
            "mcrx-core-py",
            "mctx-core-py",
        ],
        "dev": [
            "ruff>=0.11",
        ],
    },
    ext_modules=ext_modules,
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: Python Software Foundation License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
        "Topic :: Internet",
    ],
    keywords="taps ietf transport-services asyncio reference-implementation",
)
