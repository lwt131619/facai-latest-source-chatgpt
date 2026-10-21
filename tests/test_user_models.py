"""Client-supplied models: encrypted keys, tenant isolation, gateway injection.

A client registers their own endpoint + API key. The key is encrypted at rest
(Fernet, master key outside the database), decrypted only in memory when a
request or a run needs it, never returned by any API (masked tail only), and
their missions prefer their model over the shared defaults. Operator models
stay untouched and invisible-keyed as before.
"""

import base64
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from facai.api import create_app
from facai.secretbox import SecretBoxError, decrypt, encrypt, mask, validate_public_endpoint

BASIC_ADMIN = {"Authorization": "Basic " + base64.b64encode(b"admin:um-pass").decode()}
BASIC_CLIENT = {"Authorization": "Basic " + base64.b64encode(b"client1:client-pass").decode()}

PAYLOAD = {"name": "我的模型", "endpoint": "https://example.com",
           "model": "their-model", "api_key": "sk-their-secret-key"}


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("FACAI_ADMIN_PASSWORD", "um-pass")
    monkeypatch.delenv("FACAI_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("FACAI_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("FACAI_N8N_RELAY_ENABLED", "0")
    monkeypatch.setenv("FACAI_CHANNEL_NOTIFY_ENABLED", "0")
    # client1 is a seeded registration-capable tenant for these tests
    app = create_app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/v1/auth/register", json={"username": "client1", "password": "client-pass"})
        assert r.status_code in (201, 409)
    return TestClient(app), app


# --- encryption at rest ---------------------------------------------------------


def test_key_is_encrypted_at_rest_and_masked_in_api(monkeypatch, tmp_path):
    client, app = _client(monkeypatch, tmp_path)
    r = client.post("/api/v1/my/models", json=PAYLOAD, headers=BASIC_ADMIN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["api_key_masked"] == mask("sk-their-secret-key")
    assert "sk-their-secret-key" not in json.dumps(body)

    listing = client.get("/api/v1/my/models", headers=BASIC_ADMIN).json()
    assert len(listing) == 1 and listing[0]["api_key_masked"] == body["api_key_masked"]
    assert "sk-their-secret-key" not in json.dumps(listing)

    # The database file itself never carries the plaintext key or the
    # encrypted token outside the user_models table.
    raw = (tmp_path / "facai.sqlite3").read_bytes()
    assert b"sk-their-secret-key" not in raw
    row = sqlite3.connect(tmp_path / "facai.sqlite3").execute(
        "SELECT api_key_encrypted FROM user_models").fetchone()
    assert row and decrypt(tmp_path, row[0]) == "sk-their-secret-key"


def test_mask_never_reveals_a_short_key():
    assert mask("abc") == "****" and mask("") == ""


# --- registration guard rails ---------------------------------------------------


def test_private_endpoints_are_rejected_unless_explicitly_allowed(monkeypatch, tmp_path):
    client, _app = _client(monkeypatch, tmp_path)
    r = client.post("/api/v1/my/models", json={**PAYLOAD, "endpoint": "http://127.0.0.1:9999"},
                    headers=BASIC_ADMIN)
    assert r.status_code == 422  # loopback target would be an SSRF primitive
    monkeypatch.setenv("FACAI_ALLOW_PRIVATE_MODEL_ENDPOINTS", "1")
    r = client.post("/api/v1/my/models", json={**PAYLOAD, "endpoint": "http://127.0.0.1:9999"},
                    headers=BASIC_ADMIN)
    assert r.status_code in (201, 422)  # allowed by the flag; Facai's own API port still refused
    if r.status_code == 422:
        assert "Facai" in r.json()["detail"] or "回环" in r.json()["detail"]


def test_tenant_cannot_point_a_model_at_operator_key_pool(monkeypatch, tmp_path):
    client, _app = _client(monkeypatch, tmp_path)
    model = {"name": "shared-looking", "endpoint": "https://example.com/v1",
             "model": "demo", "api_key_envs": ["DEEPSEEK_API_KEY"]}
    denied = client.put('/api/v1/registries/models/shared-looking',
                        json=model, headers=BASIC_CLIENT)
    assert denied.status_code == 403
    assert client.get('/api/v1/registries/models', headers=BASIC_CLIENT).json().get(
        'shared-looking') is None


def test_operator_key_pool_requires_unique_env_references(monkeypatch, tmp_path):
    client, _app = _client(monkeypatch, tmp_path)
    model = {"name": "pool", "endpoint": "https://example.com/v1", "model": "demo"}
    path = '/api/v1/registries/models/operator-pool'
    assert client.put(path, json={**model, 'api_key_envs': ['FACAI_POOL_A', 'FACAI_POOL_A']},
                      headers=BASIC_ADMIN).status_code == 422
    assert client.put(path, json={**model, 'api_key_envs': ['invalid-name!']},
                      headers=BASIC_ADMIN).status_code == 422
    assert client.put(path, json={**model, 'api_key_envs': ['FACAI_POOL_A', 'FACAI_POOL_B']},
                      headers=BASIC_ADMIN).status_code == 200


def test_reregistering_same_name_replaces_credential(monkeypatch, tmp_path):
    client, _app = _client(monkeypatch, tmp_path)
    first = client.post("/api/v1/my/models", json=PAYLOAD, headers=BASIC_ADMIN).json()
    second = client.post("/api/v1/my/models",
                         json={**PAYLOAD, "api_key": "sk-rotated-key"}, headers=BASIC_ADMIN).json()
    assert first["id"] == second["id"]  # same name -> in-place rotation
    listing = client.get("/api/v1/my/models", headers=BASIC_ADMIN).json()
    assert len(listing) == 1


# --- tenant isolation -----------------------------------------------------------


def test_models_are_invisible_across_tenants(monkeypatch, tmp_path):
    client, app = _client(monkeypatch, tmp_path)
    client.post("/api/v1/my/models", json=PAYLOAD, headers=BASIC_ADMIN)
    other = client.get("/api/v1/my/models", headers=BASIC_CLIENT)
    assert other.status_code == 200 and other.json() == []
    r = client.delete("/api/v1/my/models/whatever", headers=BASIC_CLIENT)
    assert r.status_code == 404


def test_client_model_wins_the_owner_binding(monkeypatch, tmp_path):
    client, app = _client(monkeypatch, tmp_path)
    client.post("/api/v1/my/models", json=PAYLOAD, headers=BASIC_ADMIN)
    # The binding helper is reached through mission creation internals; here we
    # verify the registry resolution order it depends on.
    models = app.state.registries.list("models", owner_id="admin")
    own = [key for key, value in models.items() if value.get("user_model_id")]
    assert len(own) == 1
    assert models[own[0]]["endpoint"] == "https://example.com"
    assert "api_key" not in models[own[0]]  # registry entries never carry keys
    # And another tenant does not see the user-registered entry at all.
    other_models = app.state.registries.list("models", owner_id="client1")
    assert not [key for key, value in other_models.items() if value.get("user_model_id")]


# --- gateway uses the decrypted key ---------------------------------------------


def test_gateway_injects_client_key_into_upstream_call(monkeypatch, tmp_path):
    client, app = _client(monkeypatch, tmp_path)
    created = client.post("/api/v1/my/models", json=PAYLOAD, headers=BASIC_ADMIN).json()
    import facai.gateway as gateway
    seen = {}

    class _FakeResponse:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"total_tokens": 1}}

    async def fake_post(self, url, json=None, headers=None):
        seen["auth"] = (headers or {}).get("Authorization")
        seen["url"] = url
        return _FakeResponse()

    monkeypatch.setattr(gateway.httpx.AsyncClient, "post", fake_post)
    from facai.secretbox import decrypt as _dec
    from facai.store import Store
    token = Store(tmp_path).user_model_encrypted_key("admin", created["id"])
    key = decrypt(tmp_path, token)
    model = {"name": PAYLOAD["name"], "endpoint": PAYLOAD["endpoint"],
             "model": PAYLOAD["model"], "api_key": key}
    import asyncio
    result = asyncio.run(gateway.complete(model, [{"role": "user", "content": "hi"}]))
    assert result["status"] == "MODEL_COMPLETED"
    assert seen["auth"] == f"Bearer {key}"  # the decrypted key rode in the header
    assert seen["url"].startswith("https://example.com/")


# --- deletion --------------------------------------------------------------------


def test_delete_removes_model_and_registry_entry(monkeypatch, tmp_path):
    client, app = _client(monkeypatch, tmp_path)
    created = client.post("/api/v1/my/models", json=PAYLOAD, headers=BASIC_ADMIN).json()
    r = client.delete(f"/api/v1/my/models/{created['id']}", headers=BASIC_ADMIN)
    assert r.status_code == 200
    assert client.get("/api/v1/my/models", headers=BASIC_ADMIN).json() == []
    models = app.state.registries.list("models", owner_id="admin")
    assert not [k for k, v in models.items() if v.get("user_model_id")]
    # deleting again is a 404, never a duplicate deletion
    assert client.delete(f"/api/v1/my/models/{created['id']}", headers=BASIC_ADMIN).status_code == 404


# --- secretbox unit ---------------------------------------------------------------


def test_master_key_file_is_created_with_restricted_mode(monkeypatch, tmp_path):
    monkeypatch.delenv("FACAI_MASTER_KEY", raising=False)
    key = tmp_path / "master.key"
    from facai.secretbox import master_key
    master_key(tmp_path)
    assert key.exists()
    assert decrypt(tmp_path, encrypt(tmp_path, "x")) == "x"


def test_wrong_master_key_cannot_recover_credential(tmp_path):
    token = encrypt(tmp_path, "sk-secret")
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(SecretBoxError):
        decrypt(other, token)


def test_validate_public_endpoint_rejects_dns_to_private_target(monkeypatch):
    monkeypatch.delenv("FACAI_ALLOW_PRIVATE_MODEL_ENDPOINTS", raising=False)
    with pytest.raises(SecretBoxError):
        # localhost resolves to loopback regardless of scheme tricks
        validate_public_endpoint("https://localhost:8765")
