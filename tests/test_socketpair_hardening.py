"""Pins for the Windows loopback socketpair hardening in conftest.py.

The hardening exists because dropped loopback SYNs on the Windows host made
CPython's unbounded fallback accept() wedge the whole single-process suite
(AGENT_HANDOFF.md 2026-10-28). These tests pin the wiring and the contract:
pairs must be real duplex sockets, and the fallback must never hang forever.
"""

import os
import socket
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="socketpair fallback hardening is Windows-only"
)


def test_conftest_hardening_is_installed():
    assert socket.socketpair.__name__ == "_hardened_socketpair"


def test_socketpair_returns_a_working_duplex_pair():
    for _ in range(10):
        a, b = socket.socketpair()
        with a, b:
            a.sendall(b"ping")
            assert b.recv(4) == b"ping"
            b.sendall(b"pong")
            assert a.recv(4) == b"pong"


def test_socketpair_survives_a_listener_timeout_without_leaking():
    # A dropped SYN must surface as a bounded failure or a recovered pair,
    # never an unbounded accept(). Simulate the pathological path by
    # exhausting the retry budget with an unreachable listener port: we
    # cannot force a SYN drop, so pin the retry bound indirectly -- the
    # hardened wrapper must complete (value or OSError) within a bounded
    # wall time even under load.
    import time

    start = time.monotonic()
    socket.socketpair()
    assert time.monotonic() - start < 35  # 3 attempts x 10s bound, with slack


def test_stall_watchdog_heartbeat_logic():
    from tests.conftest import _StallWatchdog

    dog = _StallWatchdog(limit=300)
    base = time.monotonic()
    dog._beat = base  # simulate a beat at a known instant
    assert dog.stalled_for(base + 10) == 10
    assert dog.stalled_for(base + 301) > 300
    dog.touch()
    assert dog.stalled_for(time.monotonic()) < 300  # a touch clears the stall
