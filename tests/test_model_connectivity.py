"""Manual model-provider connectivity test with mocked upstream gateway.

The probe must distinguish "credentials are configured" from "the upstream
accepted the request", stay behind session auth + CSRF, classify unknown models
and upstream failures honestly, and never expose the API key or a raw upstream
body in the response or the logs.
"""

import json

import pytest
from fastapi.testclient import TestClient

from facai.api import create_app

PASSWORD = "connectivity-pass"
PROBE_PATH = "/api/v1/registries/models/deepseek-flash/test"


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("FACAI_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.delenv("FACAI_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("FACAI_SCHEDULER_ENABLED", "0")
    return create_app(tmp_path)


def _login(client):
    response = client.post("/api/v1/auth/login",
                           json={"username": "admin", "password": PASSWORD})
    assert response.status_code == 200
    return response.json()


def _fake_complete(result, captured=None):
    async def fake(model, messages, client=None, **kwargs):
        if captured is not None:
            captured.append({"model": model, "messages": messages})
        return result
    return fake


def _completed(usage=None):
    return {"status": "MODEL_COMPLETED", "label": "上游模型已完成",
            "http_status": 200, "content": "ok", "upstream": None,
            "reason": None, "usage": usage}


def test_success_reports_attribution_latency_and_reported_usage(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    monkeypatch.setattr("facai.api.complete", _fake_complete(
        _completed({"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})))
    client = TestClient(app)
    session = _login(client)

    response = client.post(PROBE_PATH, headers={"X-CSRF-Token": session["csrf_token"]})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["ok"] is True
    assert body["status"] == "MODEL_COMPLETED"
    assert body["label"] == "上游模型已完成"
    assert body["provider"] == "DeepSeek"
    assert body["model_id"] == "deepseek-flash"
    assert body["model"] == "deepseek-flash"
    assert isinstance(body["latency_ms"], (int, float)) and body["latency_ms"] >= 0
    assert body["usage"] == {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}


def test_probe_requires_session_and_csrf(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    monkeypatch.setattr("facai.api.complete", _fake_complete(_completed()))
    client = TestClient(app)

    # No session at all: rejected before any upstream work.
    assert client.post(PROBE_PATH).status_code == 401

    session = _login(client)
    # Session but no CSRF header: unsafe request is refused.
    assert client.post(PROBE_PATH).status_code == 403

    allowed = client.post(PROBE_PATH, headers={"X-CSRF-Token": session["csrf_token"]})
    assert allowed.status_code == 200


def test_unknown_model_is_404_and_never_calls_gateway(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    captured = []
    monkeypatch.setattr("facai.api.complete", _fake_complete(_completed(), captured))
    client = TestClient(app)
    session = _login(client)

    response = client.post("/api/v1/registries/models/does-not-exist/test",
                           headers={"X-CSRF-Token": session["csrf_token"]})
    assert response.status_code == 404
    assert captured == []


@pytest.mark.parametrize("status,label", [
    ("MODEL_TIMEOUT", "上游请求超时"),
    ("MODEL_TRANSPORT_FAILED", "网络传输失败"),
    ("MODEL_AUTH_FAILED", "上游认证失败"),
    ("MODEL_FAILED", "上游返回错误"),
])
def test_upstream_failures_are_classified_not_raised(tmp_path, monkeypatch, status, label):
    app = _app(tmp_path, monkeypatch)
    monkeypatch.setattr("facai.api.complete", _fake_complete({
        "status": status, "label": label, "http_status": None, "content": None,
        "upstream": {"error": "raw upstream internals"}, "reason": "raw upstream internals",
        "usage": None}))
    client = TestClient(app)
    session = _login(client)

    response = client.post(PROBE_PATH, headers={"X-CSRF-Token": session["csrf_token"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["status"] == status
    assert body["label"] == label
    assert body["usage"] is None
    assert "upstream" not in body and "content" not in body


def test_response_and_logs_never_expose_secret_or_raw_upstream(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-connectivity-secret")
    app = _app(tmp_path, monkeypatch)
    raw = "raw upstream body leaking sk-connectivity-secret and provider internals"
    captured = []
    monkeypatch.setattr("facai.api.complete", _fake_complete({
        "status": "MODEL_FAILED", "label": "上游返回错误", "http_status": 500,
        "content": None, "upstream": {"error": {"message": raw}}, "reason": raw,
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}},
        captured))
    client = TestClient(app)
    session = _login(client)

    with caplog.at_level("DEBUG"):
        response = client.post(PROBE_PATH, headers={"X-CSRF-Token": session["csrf_token"]})

    assert response.status_code == 200
    body = response.json()
    payload = json.dumps(body)
    assert "sk-connectivity-secret" not in payload
    assert raw not in payload
    assert "upstream" not in body
    assert "content" not in body
    assert body["usage"] == {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4}

    # The credential is read by the gateway, never logged by the probe.
    assert "sk-connectivity-secret" not in caplog.text
    assert raw not in caplog.text

    # The probe sent a short harmless prompt, not the operator's task content.
    assert len(captured) == 1
    messages = captured[0]["messages"]
    assert len(messages) == 1 and messages[0]["role"] == "user"
    assert len(messages[0]["content"]) <= 100


def test_probe_is_not_run_on_page_load_or_registry_listing(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    captured = []
    monkeypatch.setattr("facai.api.complete", _fake_complete(_completed(), captured))
    client = TestClient(app)
    _login(client)

    assert client.get("/").status_code == 200
    assert client.get("/api/v1/registries/models").status_code == 200
    assert captured == []


@pytest.mark.parametrize("raised", [
    ValueError("sk-upstream-secret rejected at internal-host.provider.example"),
    KeyError("sk-upstream-secret leaked in key lookup"),
])
def test_exception_text_is_sanitized_to_generic_422(tmp_path, monkeypatch, caplog, raised):
    app = _app(tmp_path, monkeypatch)

    async def boom(model, messages, client=None, **kwargs):
        raise raised

    monkeypatch.setattr("facai.api.complete", boom)
    client = TestClient(app)
    session = _login(client)

    with caplog.at_level("DEBUG"):
        response = client.post(PROBE_PATH, headers={"X-CSRF-Token": session["csrf_token"]})

    assert response.status_code == 422
    assert response.json()["detail"] == "Model connectivity check failed"
    assert "sk-upstream-secret" not in response.text
    assert "internal-host.provider.example" not in response.text
    assert "sk-upstream-secret" not in caplog.text
    assert "internal-host.provider.example" not in caplog.text


def test_repeated_probes_are_cooldown_limited(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    captured = []
    monkeypatch.setattr("facai.api.complete", _fake_complete(_completed(), captured))
    client = TestClient(app)
    session = _login(client)
    headers = {"X-CSRF-Token": session["csrf_token"]}

    first = client.post(PROBE_PATH, headers=headers)
    assert first.status_code == 200, first.text

    second = client.post(PROBE_PATH, headers=headers)
    assert second.status_code == 429, second.text
    assert "retry" in second.json()["detail"].lower()
    assert int(second.headers["Retry-After"]) >= 1
    # The throttled call never reached the upstream.
    assert len(captured) == 1


def test_cooldown_is_scoped_per_authenticated_user(tmp_path, monkeypatch):
    monkeypatch.setenv("FACAI_USERS_JSON", '{"alice":"alice-pass","bob":"bob-pass"}')
    monkeypatch.delenv("FACAI_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("FACAI_SCHEDULER_ENABLED", "0")
    app = create_app(tmp_path)
    captured = []
    monkeypatch.setattr("facai.api.complete", _fake_complete(_completed(), captured))

    alice, bob = TestClient(app), TestClient(app)
    alice_session = alice.post("/api/v1/auth/login", json={
        "username": "alice", "password": "alice-pass"}).json()
    bob_session = bob.post("/api/v1/auth/login", json={
        "username": "bob", "password": "bob-pass"}).json()

    alice_headers = {"X-CSRF-Token": alice_session["csrf_token"]}
    bob_headers = {"X-CSRF-Token": bob_session["csrf_token"]}

    assert alice.post(PROBE_PATH, headers=alice_headers).status_code == 200
    # Alice's next probe is throttled, but Bob has his own independent slot.
    assert alice.post(PROBE_PATH, headers=alice_headers).status_code == 429
    assert bob.post(PROBE_PATH, headers=bob_headers).status_code == 200
    assert len(captured) == 2
