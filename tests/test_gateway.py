import asyncio
import json

import httpx
import pytest

from facai.gateway import complete


def invoke(handler, model, rotation=None):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await complete(model, [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Hello"},
            ], client, rotation=rotation)
    return asyncio.run(go())


def anthropic_model(endpoint="https://api.anthropic.test/v1"):
    return {"provider": "Anthropic", "endpoint": endpoint, "model": "claude-test",
            "api_key_env": "TEST_ANTHROPIC_KEY"}


def test_anthropic_messages_request_maps_system_and_auth(monkeypatch):
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "mock-secret")
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["json"] = request.read()
        return httpx.Response(200, json={
            "type": "message", "content": [{"type": "text", "text": "Hi"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 4, "output_tokens": 2}})

    result = invoke(handler, anthropic_model())
    payload = json.loads(seen["json"])
    assert seen["url"] == "https://api.anthropic.test/v1/messages"
    assert seen["headers"]["x-api-key"] == "mock-secret"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert "authorization" not in seen["headers"]
    assert payload["system"] == "Be concise."
    assert payload["messages"] == [{"role": "user", "content": "Hello"}]
    assert payload["max_tokens"] == 8192
    assert result["status"] == "MODEL_COMPLETED"
    assert result["content"] == "Hi"
    assert result["usage"] == {"input_tokens": 4, "output_tokens": 2}


def test_anthropic_non_v1_endpoint_uses_messages_path():
    def handler(request):
        assert str(request.url) == "https://api.anthropic.test/v1/messages"
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    assert invoke(handler, anthropic_model("https://api.anthropic.test"))["content"] == "ok"


@pytest.mark.parametrize(("configured", "expected"), [(50000, 32768), (0, 1), ("bad", 8192)])
def test_anthropic_output_limit_is_sane_and_configurable(configured, expected):
    model = anthropic_model()
    model["max_tokens"] = configured

    def handler(request):
        assert json.loads(request.content)["max_tokens"] == expected
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    assert invoke(handler, model)["status"] == "MODEL_COMPLETED"


def test_anthropic_refusal_and_error_are_attributed():
    refusal = invoke(lambda request: httpx.Response(200, json={
        "type": "message", "content": [{"type": "text", "text": "Can't help."},
                                           {"type": "refusal", "text": "policy"}],
        "stop_reason": "refusal"}), anthropic_model())
    assert refusal["status"] == "MODEL_REFUSED"
    assert refusal["upstream"]["content"][1]["type"] == "refusal"

    failed = invoke(lambda request: httpx.Response(400, json={
        "type": "error", "error": {"type": "invalid_request_error", "message": "bad input"}}),
        anthropic_model())
    assert failed["status"] == "MODEL_FAILED"
    assert failed["reason"] == "invalid_request_error bad input"
    assert failed["upstream"]["type"] == "error"


@pytest.mark.parametrize(("status", "expected"), [(401, "MODEL_AUTH_FAILED"),
                                                    (429, "MODEL_RATE_LIMITED")])
def test_anthropic_http_failures_keep_existing_attribution(status, expected):
    outcome = invoke(lambda request: httpx.Response(status, json={
        "type": "error", "error": {"type": "api_error", "message": "unavailable"}}),
        anthropic_model())
    assert outcome["status"] == expected
    assert outcome["http_status"] == status


def test_openai_model_object_shape_reports_content_and_usage():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "hi"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2}})

    result = invoke(handler, {"provider": "OpenAI-compatible",
                              "endpoint": "https://api.example.test/v1",
                              "model": "demo"})
    assert result["status"] == "MODEL_COMPLETED"
    assert result["content"] == "hi"
    assert result["usage"]["prompt_tokens"] == 3
    assert result["usage"]["completion_tokens"] == 2


def test_model_key_pool_fails_over_after_429_and_cools_that_key(monkeypatch):
    monkeypatch.setenv('FACAI_TEST_POOL_A', 'mock-pool-a')
    monkeypatch.setenv('FACAI_TEST_POOL_B', 'mock-pool-b')
    model = {'provider': 'OpenAI-compatible', 'endpoint': 'https://pool-429.test/v1',
             'model': 'demo', 'api_key_envs': ['FACAI_TEST_POOL_A', 'FACAI_TEST_POOL_B']}
    seen = []

    def handler(request):
        credential = request.headers.get('authorization')
        seen.append(credential)
        if credential == 'Bearer mock-pool-a':
            return httpx.Response(429, json={'error': {'message': 'rate limited'}})
        return httpx.Response(200, json={'choices': [{'message': {'content': 'from B'}}]})

    first = invoke(handler, model)
    assert first['status'] == 'MODEL_COMPLETED' and first['content'] == 'from B'
    assert seen == ['Bearer mock-pool-a', 'Bearer mock-pool-b']
    second = invoke(handler, model)
    assert second['status'] == 'MODEL_COMPLETED'
    assert seen[-1] == 'Bearer mock-pool-b'  # A remains in cooldown


def test_model_key_pool_reports_exhaustion_without_exposing_keys(monkeypatch):
    monkeypatch.setenv('FACAI_TEST_POOL_C', 'mock-pool-c')
    monkeypatch.setenv('FACAI_TEST_POOL_D', 'mock-pool-d')
    model = {'provider': 'Anthropic', 'endpoint': 'https://pool-auth.test/v1',
             'model': 'claude-test', 'api_key_envs': ['FACAI_TEST_POOL_C', 'FACAI_TEST_POOL_D']}
    seen = []

    def handler(request):
        seen.append(request.headers.get('x-api-key'))
        return httpx.Response(401, json={'error': {'message': 'invalid credential'}})

    failed = invoke(handler, model)
    assert failed['status'] == 'MODEL_AUTH_FAILED'
    assert set(seen) == {'mock-pool-c', 'mock-pool-d'}
    cooled = invoke(handler, model)
    assert cooled['status'] == 'MODEL_AUTH_FAILED' and len(seen) == 2
    assert 'mock-pool-' not in str(cooled)


def test_model_key_pool_all_429_keeps_rate_limit_attribution(monkeypatch):
    monkeypatch.setenv('FACAI_TEST_POOL_E', 'mock-pool-e')
    monkeypatch.setenv('FACAI_TEST_POOL_F', 'mock-pool-f')
    model = {'provider': 'OpenAI-compatible', 'endpoint': 'https://pool-all-429.test/v1',
             'model': 'demo', 'api_key_envs': ['FACAI_TEST_POOL_E', 'FACAI_TEST_POOL_F']}
    seen = []

    def handler(request):
        seen.append(request.headers.get('authorization'))
        return httpx.Response(429, json={'error': {'type': 'rate_limit'}})

    assert invoke(handler, model)['status'] == 'MODEL_RATE_LIMITED'
    assert len(seen) == 2
    assert invoke(handler, model)['status'] == 'MODEL_RATE_LIMITED'
    assert len(seen) == 2


def test_array_shaped_error_body_is_classified_not_crashing():
    """Gemini's OpenAI-compatible endpoint answers errors as a JSON array.

    A bare ``[{"error": {...}}]`` body must become an honest upstream-failure
    attribution, never an AttributeError that surfaces as Facai 500.
    """
    def handler(request):
        return httpx.Response(404, json=[{
            "error": {"code": 404, "message": "This model is no longer available",
                      "status": "NOT_FOUND"}}])

    result = invoke(handler, {"provider": "Google",
                              "endpoint": "https://generativelanguage.test/v1beta/openai",
                              "model": "gemini-gone"})
    assert result["status"] == "MODEL_FAILED"
    assert result["http_status"] == 404
    assert "no longer available" in result["reason"]
    assert result["upstream"]["error"]["status"] == "NOT_FOUND"


def test_array_body_without_object_or_scalar_body_becomes_empty_object():
    def handler(request):
        return httpx.Response(200, json=["not", "an", "object"])

    result = invoke(handler, {"provider": "OpenAI-compatible",
                              "endpoint": "https://api.example.test/v1",
                              "model": "demo"})
    assert result["status"] == "MODEL_COMPLETED"
    assert result["content"] is None


# ---------------------------------------------------------------------------
# MODEL-001 每Key计量：按环境引用名的调用/结局/tokens 计数，密钥值永不出现
# ---------------------------------------------------------------------------

from facai.gateway import key_usage


def test_key_usage_meters_failover_per_reference(monkeypatch):
    monkeypatch.setenv('FACAI_TEST_POOL_G', 'mock-pool-g')
    monkeypatch.setenv('FACAI_TEST_POOL_H', 'mock-pool-h')
    endpoint = 'https://pool-meter.test/v1'
    model = {'provider': 'OpenAI-compatible', 'endpoint': endpoint,
             'model': 'demo', 'api_key_envs': ['FACAI_TEST_POOL_G', 'FACAI_TEST_POOL_H']}

    def handler(request):
        if request.headers.get('authorization') == 'Bearer mock-pool-g':
            return httpx.Response(429, json={'error': {'message': 'rate limited'}})
        return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}],
                                         'usage': {'prompt_tokens': 7, 'completion_tokens': 5}})

    result = invoke(handler, model)
    assert result['status'] == 'MODEL_COMPLETED'
    assert result['key_env'] == 'FACAI_TEST_POOL_H'  # 归因随行：这一步用的是 H
    usage = key_usage(endpoint, ['FACAI_TEST_POOL_G', 'FACAI_TEST_POOL_H'])
    assert usage['FACAI_TEST_POOL_G'] == {
        'calls': 1, 'completed': 0, 'rate_limited': 1, 'auth_failed': 0,
        'failed_other': 0, 'input_tokens': 0, 'output_tokens': 0}
    assert usage['FACAI_TEST_POOL_H']['completed'] == 1
    assert usage['FACAI_TEST_POOL_H']['input_tokens'] == 7
    assert usage['FACAI_TEST_POOL_H']['output_tokens'] == 5
    assert 'mock-pool-' not in str(usage)  # 只有变量名，绝无密钥值


def test_key_usage_endpoint_reports_metering_with_explicit_pricing(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from facai.api import create_app
    monkeypatch.setenv('FACAI_TEST_POOL_M', 'mock-pool-m')
    monkeypatch.setenv('FACAI_TEST_POOL_N', 'mock-pool-n')
    monkeypatch.setenv("FACAI_ADMIN_PASSWORD", "gateway-admin-pass")
    monkeypatch.delenv("FACAI_COOKIE_SECURE", raising=False)
    client = TestClient(create_app(tmp_path))
    session = client.post("/api/v1/auth/login",
                          json={"username": "admin", "password": "gateway-admin-pass"}).json()
    client.headers.update({"X-CSRF-Token": session["csrf_token"]})

    model = {'name': 'Metered', 'provider': 'OpenAI-compatible',
             'endpoint': 'https://pool-meter-api.test/v1', 'model': 'demo',
             'api_key_envs': ['FACAI_TEST_POOL_M', 'FACAI_TEST_POOL_N'],
             'price_input_per_1k': 2.0, 'price_output_per_1k': 8.0}
    assert client.put('/api/v1/registries/models/metered', json=model).status_code == 200
    assert client.put('/api/v1/registries/models/metered-bad',
                      json=dict(model, price_input_per_1k=-1)).status_code == 422
    assert client.put('/api/v1/registries/models/unpriced',
                      json={k: v for k, v in model.items()
                            if not k.startswith('price_')}).status_code == 200

    def handler(request):
        if request.headers.get('authorization') == 'Bearer mock-pool-m':
            return httpx.Response(429, json={'error': {'message': 'limited'}})
        return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}],
                                         'usage': {'prompt_tokens': 500, 'completion_tokens': 250}})

    assert invoke(handler, model,
                  rotation=client.app.state.key_rotation)['status'] == 'MODEL_COMPLETED'

    data = client.get('/api/v1/modelmatrix/key-usage').json()
    assert 'mock-pool-' not in json.dumps(data)
    metered = next(m for m in data['models'] if m['model_id'] == 'metered')
    assert metered['keys']['FACAI_TEST_POOL_M']['rate_limited'] == 1
    assert metered['keys']['FACAI_TEST_POOL_M']['estimated_cost'] == 0.0
    n_row = metered['keys']['FACAI_TEST_POOL_N']
    assert n_row['completed'] == 1 and n_row['input_tokens'] == 500
    # 成本只用注册表显式单价计算：500/1000*2 + 250/1000*8 = 3.0
    assert n_row['estimated_cost'] == 3.0
    assert metered['pricing'] == {'input_per_1k': 2.0, 'output_per_1k': 8.0}
    unpriced = next(m for m in data['models'] if m['model_id'] == 'unpriced')
    assert unpriced['keys']['FACAI_TEST_POOL_N']['estimated_cost'] is None
    assert unpriced['pricing'] == {'input_per_1k': None, 'output_per_1k': None}

    # 租户不得读别人的 Key 台账
    tenant = client.post("/api/v1/auth/register", json={
        "username": "meter-tenant", "password": "meter-pass-1234"})
    client.headers.update({"X-CSRF-Token": tenant.json()["csrf_token"]})
    assert client.get('/api/v1/modelmatrix/key-usage').status_code == 403


# ---------------------------------------------------------------------------
# MODEL-001 跨进程共享冷却：两个 rotation 实例 + 同一 SQLite 后端 = 两个进程
# ---------------------------------------------------------------------------

import time as _time

from facai.gateway import KeyRotation
from facai.store import Store


def test_shared_cooldown_visible_across_rotation_instances(tmp_path):
    store = Store(tmp_path)
    process_a = KeyRotation(store)
    process_b = KeyRotation(store)
    endpoint, refs = 'https://shared.test/v1', ['K_A', 'K_B']

    process_a.penalize(endpoint, 'K_A', 403)
    # 进程 B 没有内存态，但读同一 SQLite：K_A 不可用，只剩 K_B
    assert process_b.candidates(endpoint, refs) == ['K_B']
    assert process_a.candidates(endpoint, refs) == ['K_B']
    # 认证失败口径跨进程一致：两个 key 都是 auth 失败才报 AUTH（混合 429 报 RATE）
    process_b.penalize(endpoint, 'K_B', 401)
    assert process_a.unavailable_status(endpoint, refs) == 'MODEL_AUTH_FAILED'
    assert process_b.unavailable_status(endpoint, refs) == 'MODEL_AUTH_FAILED'
    assert process_a.candidates(endpoint, refs) == []

    # 过期即恢复：把行改成已过期的 epoch。B 本就无内存态，直接恢复；
    # A 的内存冷却还在（同进程内存盖不过 DB 过期——诚实合并），模拟 A
    # 重启（内存清空）后同样从后端读到已过期、两把都 ready。
    with store.connect() as db:
        db.execute('UPDATE model_key_cooldowns SET until_epoch=?',
                   (_time.time() - 1,))
    assert sorted(process_b.candidates(endpoint, refs)) == ['K_A']  # B 的内存 K_B 还在冷却
    process_c = KeyRotation(store)  # 新进程：无内存态，纯读后端
    assert sorted(process_c.candidates(endpoint, refs)) == ['K_A', 'K_B']
    # 无后端的实例（旧默认路径）不受 SQLite 影响，也不写后端
    loner = KeyRotation()
    loner.penalize(endpoint, 'K_A', 429)
    assert loner.candidates(endpoint, refs) == ['K_B']
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM model_key_cooldowns').fetchone()[0] == 2
    # 过期行在下次写入时被顺手清理：写一把新冷却，表里只剩这一行
    process_c.penalize(endpoint, 'K_C', 429)
    with store.connect() as db:
        rows = db.execute('SELECT ref FROM model_key_cooldowns').fetchall()
    assert [row['ref'] for row in rows] == ['K_C']


def test_app_owns_its_rotation_with_shared_backend(tmp_path):
    from fastapi.testclient import TestClient
    from facai.api import create_app
    client = TestClient(create_app(tmp_path))
    rotation = client.app.state.key_rotation
    assert rotation is not None and rotation._store is client.app.state.store
    # 同一 Store 的第二个实例（= 另一进程）能看到本 app 写下的冷却
    peer = KeyRotation(client.app.state.store)
    rotation.penalize('https://peer.test/v1', 'K_P', 429)
    assert peer.candidates('https://peer.test/v1', ['K_P', 'K_Q']) == ['K_Q']
