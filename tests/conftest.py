"""Shared fixtures for the real-browser console acceptance tests."""

import os
import socket
import threading
import time
import urllib.request

import pytest

import facai.billing
from facai.api import create_app


# --- Windows loopback socketpair hardening ----------------------------------
#
# Root cause of the single-process full-suite wedges recorded in
# AGENT_HANDOFF.md (2026-10-28): on this Windows host loopback SYNs are
# occasionally dropped in bursts (measured with a dedicated hammer: accept
# delays quantise at 2/4/6s = SYN retransmit backoff, and some never
# complete). CPython's Windows socketpair fallback swallows the failed
# non-blocking connect and then blocks in accept() with NO timeout, so a
# single dropped SYN wedges every later event-loop creation (anyio portals,
# uvicorn loops) forever -- the suite hangs until killed, at a random test.
#
# Replace the fallback with a bounded-accept + retry version: a dropped SYN
# costs seconds (the stack's own retransmit usually recovers inside one
# attempt) instead of the whole run, and a persistent drop raises loudly.
# POSIX keeps the native socketpair syscall untouched.

if os.name == "nt":
    _native_socketpair = socket.socketpair

    def _hardened_socketpair(
        family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0
    ):
        if (family, type, proto) != (socket.AF_INET, socket.SOCK_STREAM, 0):
            return _native_socketpair(family, type, proto)
        last_exc: OSError | None = None
        for _ in range(3):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                client.setblocking(False)
                try:
                    client.connect(("127.0.0.1", listener.getsockname()[1]))
                except OSError:
                    pass  # EWOULDBLOCK while the handshake completes is expected
                client.setblocking(True)
                listener.settimeout(10.0)
                peer, _addr = listener.accept()
                listener.close()
                return peer, client
            except OSError as exc:
                last_exc = exc
                listener.close()
                client.close()
        raise OSError(
            f"loopback socketpair failed after 3 attempts "
            f"(dropped loopback SYNs?): {last_exc}"
        ) from last_exc

    socket.socketpair = _hardened_socketpair  # type: ignore[assignment]


# --- Session stall watchdog ---------------------------------------------------
#
# The same host interference that drops loopback SYNs can also abort an
# ESTABLISHED loopback connection (WinError 10053): when it hits an event
# loop's self-pipe wake channel, the loop sleeps forever with callbacks
# stranded in its queue, and any thread waiting on run_sync_from_thread --
# a TestClient portal exit, an app shutdown -- waits forever. That cannot
# be prevented from user code, so the suite must terminate loudly instead
# of wedging: this watchdog dumps all thread stacks and exits nonzero when
# no test/collection progress is observed for STALL_LIMIT seconds.

STALL_LIMIT_SECONDS = 300

# The real stderr fd, saved before pytest's fd-level capture replaces fd 2.
_real_stderr_fd = os.dup(2)


class _StallWatchdog:
    def __init__(self, limit: float = STALL_LIMIT_SECONDS):
        try:
            limit = float(os.environ.get("FACAI_TEST_STALL_LIMIT", limit))
        except ValueError:
            pass
        self.limit = limit
        self._beat = time.monotonic()
        self._lock = threading.Lock()

    def touch(self) -> None:
        with self._lock:
            self._beat = time.monotonic()

    def stalled_for(self, now: float) -> float:
        with self._lock:
            return now - self._beat

    def _run(self) -> None:
        import faulthandler
        import tempfile

        while True:
            time.sleep(min(10, max(1, self.limit / 3)))
            stalled = self.stalled_for(time.monotonic())
            if stalled <= self.limit:
                continue
            # The full dump MUST survive pytest's capture: fd-level capture
            # and os._exit would discard anything written to sys.stderr, so
            # the dump always goes to a file on disk; the banner is
            # best-effort on the real (dup'ed) stderr fd.
            banner = (
                f"!!! watchdog: no suite progress for {stalled:.0f}s "
                f"(> {self.limit:.0f}s); this host intermittently kills "
                f"loopback connections (AGENT_HANDOFF.md test-isolation "
                f"notes). Full thread dump follows in this file.\n"
            )
            dump_path = os.path.join(
                tempfile.gettempdir(), f"facai-pytest-stall-{int(time.time())}.dump"
            )
            try:
                with open(dump_path, "w", encoding="utf-8") as fh:
                    fh.write(banner)
                    faulthandler.dump_traceback(file=fh)
                try:
                    os.write(
                        _real_stderr_fd,
                        (banner + f"full dump: {dump_path}\n").encode(
                            "utf-8", "replace"
                        ),
                    )
                except OSError:
                    pass
            except OSError:
                pass
            os._exit(70)


_watchdog = _StallWatchdog()
threading.Thread(target=_watchdog._run, daemon=True).start()


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items):
    _watchdog.touch()


@pytest.hookimpl(trylast=True)
def pytest_runtest_logreport(report):
    _watchdog.touch()



# Balance-pool semantics (operator directive): production customers start at
# balance 0 and cannot dispatch until topped up. Most tests exercise other
# features and need a FUNDED world, so this default applies everywhere except
# files that explicitly opt out with pytestmark RAW_BUDGET (the budget-gate
# semantics tests). Explicit user_budgets rows always win over the default.
TEST_STARTING_BALANCE_USD = 1000.0


@pytest.fixture(autouse=True)
def funded_test_world(monkeypatch, request):
    if request.node.get_closest_marker("raw_budget"):
        return
    real = facai.billing.effective_monthly_limit

    def funded(store, username, environ=None):
        row = store.get_user_budget(username)
        if row is not None:
            return float(row["monthly_limit_usd"])
        return TEST_STARTING_BALANCE_USD

    monkeypatch.setattr(facai.billing, "effective_monthly_limit", funded)


@pytest.fixture(autouse=True)
def hermetic_upstream(monkeypatch):
    """Keep the whole suite offline: no test may reach a real model provider.

    A developer shell often carries live provider credentials. Without this
    fixture the channel-dispatch tests resolve the default ``deepseek-flash``
    registry entry and send real paid requests to its endpoint, so the
    deterministic-fallback assertions fail or pass depending on whose machine
    and whose balance the suite runs on. Every ``*_API_KEY`` variable is
    removed, and the gateway entry point used by the API layer is replaced with
    an honest local auth failure so prompt conversion falls back
    deterministically without any network traffic. Tests that exercise the
    gateway stub ``facai.api.complete`` themselves via ``monkeypatch``, which
    overrides this fixture (fixtures run before test bodies).
    """
    for name in list(os.environ):
        if name.endswith("_API_KEY"):
            monkeypatch.delenv(name, raising=False)

    async def no_upstream(model, messages, client=None, timeout=30):
        return {"status": "MODEL_AUTH_FAILED", "label": "上游认证失败",
                "http_status": None, "content": None, "upstream": None,
                "reason": "测试环境不调用真实上游", "usage": None}

    monkeypatch.setattr("facai.api.complete", no_upstream)


def _serve(app):
    """Keep the listening socket reserved until uvicorn owns it (no TOCTOU)."""
    import uvicorn

    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    base = f'http://127.0.0.1:{sock.getsockname()[1]}'
    config = uvicorn.Config(app, log_level='warning', lifespan='on')
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    for _ in range(200):
        try:
            urllib.request.urlopen(base + '/api/v1/health', timeout=0.5)
            break
        except Exception:
            time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        raise RuntimeError('uvicorn did not start')
    return server, thread, base


@pytest.fixture
def playwright_api():
    return pytest.importorskip("playwright.sync_api")


@pytest.fixture
def console_server(tmp_path, monkeypatch, playwright_api):
    monkeypatch.delenv("FACAI_ADMIN_PASSWORD", raising=False)
    # UI scenarios advance step records explicitly. A real scheduler racing
    # them can fail a queued CLI job between the UI fetch and backend assertion.
    # Actual scheduler/adapter execution is covered by its dedicated tests.
    monkeypatch.setenv("FACAI_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("FACAI_CHANNEL_NOTIFY_ENABLED", "0")
    server, thread, base = _serve(create_app(tmp_path))
    yield base
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def protected_server(tmp_path, monkeypatch, playwright_api):
    monkeypatch.setenv("FACAI_ADMIN_PASSWORD", "mobile-pass")
    monkeypatch.setenv("FACAI_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("FACAI_CHANNEL_NOTIFY_ENABLED", "0")
    monkeypatch.delenv("FACAI_COOKIE_SECURE", raising=False)
    server, thread, base = _serve(create_app(tmp_path))
    yield base
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def hermetic_model_registration_dns(monkeypatch, request):
    """Registry/storage tests use fake keys and must not depend on external DNS.

    Keep production SSRF validation active. Only public fixture hostnames in these
    storage/isolation/control modules get a stable public answer; localhost and all
    explicit SSRF tests retain their own resolver behavior.
    """
    if request.module.__name__.split('.')[-1] not in {
        'test_customer_surface', 'test_tenant_isolation_registries', 'test_user_models',
        'test_mission_control_flow'
    }:
        return
    real = socket.getaddrinfo
    fixture_hosts = {'api.deepseek.com', 'api.openai.com', 'api.anthropic.com', 'example.com'}

    def resolve(host, port, *args, **kwargs):
        if host in fixture_hosts:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port or 0))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
