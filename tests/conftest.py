"""Shared fixtures for the real-browser console acceptance tests."""

import os
import socket
import threading
import time
import urllib.request

import pytest

import facai.billing
from facai.api import create_app

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


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _serve(app, base):
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=int(base.rsplit(":", 1)[1]),
                            log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        try:
            urllib.request.urlopen(base + "/api/v1/health", timeout=0.5)
            break
        except Exception:
            time.sleep(0.05)
    else:
        raise RuntimeError("uvicorn did not start")
    return server, thread


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
    base = f"http://127.0.0.1:{_free_port()}"
    server, thread = _serve(create_app(tmp_path), base)
    yield base
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def protected_server(tmp_path, monkeypatch, playwright_api):
    monkeypatch.setenv("FACAI_ADMIN_PASSWORD", "mobile-pass")
    monkeypatch.setenv("FACAI_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("FACAI_CHANNEL_NOTIFY_ENABLED", "0")
    monkeypatch.delenv("FACAI_COOKIE_SECURE", raising=False)
    base = f"http://127.0.0.1:{_free_port()}"
    server, thread = _serve(create_app(tmp_path), base)
    yield base
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def hermetic_model_registration_dns(monkeypatch, request):
    """Registry/storage tests use fake keys and must not depend on external DNS.

    Keep production SSRF validation active. Only public fixture hostnames in these
    three storage/isolation modules get a stable public answer; localhost and all
    explicit SSRF tests retain their own resolver behavior.
    """
    if request.module.__name__.split('.')[-1] not in {
        'test_customer_surface', 'test_tenant_isolation_registries', 'test_user_models'
    }:
        return
    real = socket.getaddrinfo
    fixture_hosts = {'api.deepseek.com', 'api.openai.com', 'api.anthropic.com', 'example.com'}

    def resolve(host, port, *args, **kwargs):
        if host in fixture_hosts:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port or 0))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
