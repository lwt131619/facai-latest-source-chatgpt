import asyncio
import json

import httpx
import pytest

from facai.gateway import complete


def invoke(handler, model):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await complete(model, [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Hello"},
            ], client)
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
