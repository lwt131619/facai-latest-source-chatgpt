"""Bound-model channel conversion with a deterministic fallback.

The original text stays verbatim. A usable bound model may structure the task
prompt, but a missing model, upstream failure, timeout, refusal or malformed
output must fall back without leaking credentials or raw upstream errors.
Duplicates after mission creation must not call the model again.
"""

import asyncio
import base64
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from facai.api import create_app
from facai.channels import InboundMessage
from facai.registries import DEFAULTS
from facai.prompting import KNOWN_PHASES, convert_message, parse_model_conversion

PASSWORD = "convert-pass"
ORIGINAL = "  帮我整理发布步骤\n不要删空白  "
SECRET = "app-secret-value"


def auth():
    return {"Authorization": "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()}


def model_json(original=ORIGINAL, title="整理发布步骤", prompt=None, phases=None):
    body = prompt or (
        "目标：整理发布步骤。\n范围：仅文档。\n约束：不扩大范围。\n"
        "验收标准：步骤可执行。\n待确认问题：目标环境。\n"
        f"用户原话：{original}"
    )
    return json.dumps({
        "title": title,
        "professional_prompt": body,
        "phases": list(phases or KNOWN_PHASES),
    }, ensure_ascii=False)


def completed(content, usage=None):
    return {"status": "MODEL_COMPLETED", "label": "上游模型已完成", "http_status": 200,
            "content": content, "upstream": {"error": "sk-secret-should-not-leak",
                                            "api_key": "sk-live"},
            "reason": None, "usage": usage}


def failed(status):
    return {"status": status, "label": status, "http_status": 500, "content": None,
            "upstream": {"error": {"message": "Bearer sk-secret raw failure"}},
            "reason": "Bearer sk-secret raw failure", "usage": None}


class Recorder:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def __call__(self, model, messages, client=None, timeout=30):
        self.calls.append({"model_id": model.get("id"), "model": model.get("model"),
                           "messages": messages, "timeout": timeout,
                           "api_key": model.get("api_key"),
                           "api_key_env": model.get("api_key_env")})
        value = self.result
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value()
        return value


def build(tmp_path, monkeypatch, recorder):
    monkeypatch.setenv("FACAI_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.delenv("FACAI_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("FACAI_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("FACAI_N8N_RELAY_ENABLED", "0")
    monkeypatch.setattr("facai.api.complete", recorder)
    app = create_app(tmp_path)
    return app, TestClient(app)


def record(app, text=ORIGINAL, external_id="update:model"):
    return app.state.store.record_channel_message(
        "telegram", "INBOUND", "RECEIVED", external_id=external_id,
        sender="42", text=text, payload={"update_id": external_id})


def dispatch(client, message_id):
    return client.post(f"/api/v1/channels/telegram/messages/{message_id}/dispatch",
                       headers=auth())


def test_bound_model_success_keeps_original_and_sanitized_usage(tmp_path, monkeypatch):
    usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
             "api_key": "sk-secret", "error": "raw"}
    recorder = Recorder(completed(model_json(), usage))
    app, client = build(tmp_path, monkeypatch, recorder)
    message_id = record(app)
    response = dispatch(client, message_id)
    assert response.status_code == 201, response.text
    mission = response.json()
    assert mission["original_input"] == ORIGINAL
    assert ORIGINAL in mission["professional_prompt"]
    assert [step["label"] for step in mission["steps"]] == list(KNOWN_PHASES)
    assert mission["title"] == "整理发布步骤"
    assert len(recorder.calls) == 1
    # The conversion uses the shared default seat: whichever model entry
    # leads the registry (glm-local where GLM_LOCAL_ENDPOINT is set,
    # deepseek-flash otherwise — F4 order, gated on a real local endpoint).
    _default_id, default_entry = next(iter(DEFAULTS["models"].items()))
    assert recorder.calls[0]["model"] == default_entry["model"]
    assert recorder.calls[0]["timeout"] == 20
    assert "claude" not in json.dumps(recorder.calls).lower()
    events = [event for event in app.state.store.events(mission["id"])
              if event["type"] == "PROMPT_CONVERTED"]
    payload = events[0]["payload"]
    assert payload["conversion_source"] == "model"
    assert payload["model_id"] == next(iter(DEFAULTS["models"]))
    assert payload["usage"] == {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}
    assert payload["original_input"] == ORIGINAL
    assert not any(event["type"] == "CONVERSION_FALLBACK"
                   for event in app.state.store.events(mission["id"]))
    blob = json.dumps(payload, ensure_ascii=False)
    assert "sk-secret" not in blob
    assert "raw" not in payload["usage"]


def test_provider_failure_falls_back_without_raw_upstream_error(tmp_path, monkeypatch):
    recorder = Recorder(failed("MODEL_TIMEOUT"))
    app, client = build(tmp_path, monkeypatch, recorder)
    message_id = record(app, external_id="update:timeout")
    mission = dispatch(client, message_id).json()
    fallback = convert_message(ORIGINAL, "telegram")
    assert mission["professional_prompt"] == fallback.professional
    assert mission["original_input"] == ORIGINAL
    assert len(recorder.calls) == 1
    payload = [event for event in app.state.store.events(mission["id"])
               if event["type"] == "PROMPT_CONVERTED"][0]["payload"]
    assert payload["conversion_source"] == "deterministic"
    assert "model_id" not in payload
    assert payload["fallback_from_model"] == next(iter(DEFAULTS["models"]))
    assert payload["fallback_reason_class"] == "ValueError"
    fallbacks = [event for event in app.state.store.events(mission["id"])
                 if event["type"] == "CONVERSION_FALLBACK"]
    assert len(fallbacks) == 1
    assert fallbacks[0]["payload"] == {
        "message_id": message_id, "channel": "telegram",
        "model_id": payload["fallback_from_model"],
        "reason_class": "ValueError", "fallback_source": "deterministic"}
    blob = json.dumps(payload, ensure_ascii=False)
    assert "sk-secret" not in blob
    assert "Bearer" not in blob
    assert "Bearer" not in json.dumps(fallbacks, ensure_ascii=False)


def test_malformed_model_result_falls_back(tmp_path, monkeypatch):
    recorder = Recorder(completed("not-json"))
    app, client = build(tmp_path, monkeypatch, recorder)
    message_id = record(app, external_id="update:bad")
    mission = dispatch(client, message_id).json()
    fallback = convert_message(ORIGINAL, "telegram")
    assert mission["professional_prompt"] == fallback.professional
    assert [step["label"] for step in mission["steps"]] == list(KNOWN_PHASES)
    payload = [event for event in app.state.store.events(mission["id"])
               if event["type"] == "PROMPT_CONVERTED"][0]["payload"]
    assert payload["conversion_source"] == "deterministic"
    assert len([event for event in app.state.store.events(mission["id"])
                if event["type"] == "CONVERSION_FALLBACK"]) == 1
    assert "not-json" not in json.dumps(payload, ensure_ascii=False)


def test_duplicate_dispatch_does_not_call_the_model_again(tmp_path, monkeypatch):
    recorder = Recorder(completed(model_json()))
    app, client = build(tmp_path, monkeypatch, recorder)
    message_id = record(app, external_id="update:once")
    first = dispatch(client, message_id).json()
    second = dispatch(client, message_id).json()
    assert first["id"] == second["id"]
    assert len(recorder.calls) == 1
    assert len(app.state.store.list()) == 1
    assert not any(event["type"] == "CONVERSION_FALLBACK"
                   for event in app.state.store.events(first["id"]))


def test_missing_model_uses_deterministic_conversion_without_a_call(tmp_path, monkeypatch):
    recorder = Recorder(completed(model_json()))
    app, client = build(tmp_path, monkeypatch, recorder)
    app.state.registries.catalogs["models"].clear()
    message_id = record(app, external_id="update:nomodel")
    mission = dispatch(client, message_id).json()
    fallback = convert_message(ORIGINAL, "telegram")
    assert mission["professional_prompt"] == fallback.professional
    assert recorder.calls == []


def test_parser_rejects_unknown_phases_and_missing_original():
    try:
        parse_model_conversion(model_json(phases=["理解需求"]), ORIGINAL, "telegram")
    except ValueError as exc:
        assert "阶段" in str(exc)
    else:
        raise AssertionError("unknown phases must be rejected")
    try:
        parse_model_conversion(model_json(prompt="没有原话"), ORIGINAL, "telegram")
    except ValueError as exc:
        assert "原话" in str(exc)
    else:
        raise AssertionError("missing original must be rejected")


def _whatsapp_body(text):
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "biz", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "messages": [{"from": "8613800000000", "id": "wamid.MODEL", "timestamp": "1",
                          "type": "text", "text": {"body": text}}],
        }}]}],
    }
    body = json.dumps(payload).encode()
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, {"X-Hub-Signature-256": f"sha256={digest}"}


def test_channel_webhook_uses_model_conversion_asynchronously(tmp_path, monkeypatch):
    recorder = Recorder(completed(model_json("帮我写个脚本", title="编写脚本")))
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "access-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
    app, client = build(tmp_path, monkeypatch, recorder)
    body, headers = _whatsapp_body("帮我写个脚本")
    response = client.post("/api/v1/channels/whatsapp/webhook", content=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["dispatch"] == "QUEUED"
    assert len(recorder.calls) == 1
    mission_id = app.state.store.channel_messages("whatsapp")[0]["mission_id"]
    mission = app.state.store.get(mission_id)
    assert mission["title"] == "编写脚本"
    assert mission["original_input"] == "帮我写个脚本"
    replay = client.post("/api/v1/channels/whatsapp/webhook", content=body, headers=headers)
    assert replay.status_code == 200
    assert len(recorder.calls) == 1


def test_wechat_monitor_uses_model_conversion(tmp_path, monkeypatch):
    import facai.api as api

    recorder = Recorder(completed(model_json("帮我整理发布步骤", title="整理发布步骤")))
    monkeypatch.setattr(api, "complete", recorder)

    class FakeWeChat:
        def configured(self):
            return True

        async def fetch_updates(self):
            return {"state": "OK", "messages": [
                InboundMessage("wechat", "model-msg", "friend", "帮我整理发布步骤", "now"),
            ]}

        def acknowledge_updates(self):
            return None

    class FakeRegistry:
        def __init__(self, state_dir=None):
            pass

        def require(self, name):
            return FakeWeChat()

    monkeypatch.setattr(api, "ChannelRegistry", FakeRegistry)
    monkeypatch.setenv("FACAI_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("FACAI_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("FACAI_N8N_RELAY_ENABLED", "0")
    monkeypatch.setenv("FACAI_WECHAT_POLL_INTERVAL", "0.01")
    monkeypatch.setenv("FACAI_WECHAT_ERROR_BACKOFF", "0.01")
    app = api.create_app(tmp_path)

    async def scenario():
        async with app.router.lifespan_context(app):
            for _ in range(200):
                messages = app.state.store.channel_messages("wechat")
                if messages and messages[0]["mission_id"]:
                    break
                await asyncio.sleep(0.02)
            message = app.state.store.channel_messages("wechat")[0]
            mission = app.state.store.get(message["mission_id"])
            assert mission["title"] == "整理发布步骤"
            assert mission["original_input"] == "帮我整理发布步骤"
            assert len(recorder.calls) == 1
            payload = [event for event in app.state.store.events(mission["id"])
                       if event["type"] == "PROMPT_CONVERTED"][0]["payload"]
            assert payload["conversion_source"] == "model"
            assert payload["model_id"] == next(iter(DEFAULTS["models"]))

    asyncio.run(scenario())
