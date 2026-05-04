import os
import ssl
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
ECHO_SERVER = REPO_ROOT / "examples" / "echo_example" / "echoServer.py"


def _wait_for_tcp_port(host, port, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            try:
                sock.connect((host, port))
                return
            except OSError:
                time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for TCP server on [{host}]:{port}")


def _wait_for_tls_port(host, port, cafile, server_name="localhost", timeout=5.0):
    deadline = time.time() + timeout
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    while time.time() < deadline:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            try:
                wrapped = context.wrap_socket(
                    sock,
                    server_hostname=server_name,
                )
                wrapped.connect((host, port))
                wrapped.close()
                return
            except OSError:
                time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for TLS server on [{host}]:{port}")


def _reserve_ipv6_port():
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
        sock.bind(("::1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def echo_servers():
    env = os.environ.copy()
    python = sys.executable
    echo_port = _reserve_ipv6_port()
    tls_port = _reserve_ipv6_port()
    processes = [
        subprocess.Popen(
            [
                python,
                str(ECHO_SERVER),
                "--local-address=::1",
                f"--local-port={echo_port}",
                "--reliable",
                "both",
            ],
            cwd=TESTS_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        ),
        subprocess.Popen(
            [
                python,
                str(ECHO_SERVER),
                "--local-address=::1",
                f"--local-port={tls_port}",
                "--local-identity",
                "keys/localhost.pem",
            ],
            cwd=TESTS_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        ),
    ]

    _wait_for_tcp_port("::1", echo_port)
    _wait_for_tls_port("::1", tls_port, str(TESTS_DIR / "keys" / "MyRootCA.pem"))

    try:
        yield {
            "echo_port": echo_port,
            "tls_port": tls_port,
        }
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def pytest_collection_modifyitems(config, items):
    run_external = config.getoption("--run-external")
    skip_external = pytest.mark.skip(
        reason="external network test disabled; pass --run-external to enable"
    )
    for item in items:
        if "external_network" in item.keywords and not run_external:
            item.add_marker(skip_external)


def pytest_addoption(parser):
    parser.addoption(
        "--run-external",
        action="store_true",
        default=False,
        help="run tests that require external network access",
    )
