import pytest

pytestmark = pytest.mark.raw_budget

"""Per-user monthly spending budgets with hard enforcement at dispatch.

The contract under test: a limit of 0 never blocks anyone; once a user's
month-to-date recorded USD spend reaches their limit, every NEW-dispatch path
refuses (402 for plain API calls, an audited DEFERRED for channel paths); only
USD costs count toward the limit while other currencies are reported, never
converted at an invented rate; and budget rows are self-service (or set by
the operator).
"""

import pytest
from fastapi.testclient import TestClient

from facai.api import create_app
from facai.billing import budget_gate

PASSWORD = "budget-pass-123"


def build(tmp_path, monkeypatch, default_limit=None):
    monkeypatch.setenv("FACAI_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.delenv("FACAI_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("FACAI_SCHEDULER_ENABLED", "0")
    if default_limit is None:
        monkeypatch.delenv("FACAI_DEFAULT_MONTHLY_LIMIT_USD", raising=False)
    else:
        monkeypatch.setenv("FACAI_DEFAULT_MONTHLY_LIMIT_USD", default_limit)
    app = create_app(tmp_path)
    client = TestClient(app)
    session = client.post("/api/v1/auth/login",
                          json={"username": "admin", "password": PASSWORD}).json()
    client.headers.update({"X-CSRF-Token": session["csrf_token"]})
    return app, client


def seed_spend(app, amount=0.6, currency="USD", owner="admin", tokens=1200):
    """A mission owned by ``owner`` with one cost row in the current month."""
    mission = app.state.store.create("已花费任务", "原话", ["执行"], owner_id=owner)
    app.state.store.add_cost(mission["id"], "deepseek", "deepseek-flash",
                             amount, tokens, currency=currency)
    return mission


def record_inbound(app, external_id, text="帮我做一个任务面板"):
    return app.state.store.record_channel_message(
        "telegram", "INBOUND", "RECEIVED", external_id=external_id,
        sender="42", recipient="9", text=text, payload={"update_id": external_id})


# --- store layer ---------------------------------------------------------------

def test_budget_upsert_rejects_negative_and_reads_back(tmp_path, monkeypatch):
    app, _client = build(tmp_path, monkeypatch)
    store = app.state.store
    assert store.get_user_budget("admin") is None
    row = store.set_user_budget("admin", 0.5)
    assert row["monthly_limit_usd"] == 0.5
    row = store.set_user_budget("admin", 2.0)
    assert row["monthly_limit_usd"] == 2.0
    assert store.get_user_budget("admin")["monthly_limit_usd"] == 2.0
    with pytest.raises(ValueError):
        store.set_user_budget("admin", -1)


def test_user_spend_month_aggregates_usd_tokens_and_missions(tmp_path, monkeypatch):
    app, _client = build(tmp_path, monkeypatch)
    store = app.state.store
    seed_spend(app, 0.6)
    spend = store.user_spend_month("admin")
    assert spend["cost_usd"] == pytest.approx(0.6)
    assert spend["total_tokens"] == 1200
    assert spend["missions"] == 1
    assert spend["other_currencies"] == []
    # Another owner spends nothing.
    assert store.user_spend_month("nobody")["cost_usd"] == 0.0


def test_user_spend_month_excludes_non_usd_but_lists_codes(tmp_path, monkeypatch):
    app, _client = build(tmp_path, monkeypatch)
    store = app.state.store
    store.set_user_budget("admin", 0.5)
    seed_spend(app, 0.2, currency="CNY")
    spend = store.user_spend_month("admin")
    assert spend["cost_usd"] == 0.0
    assert spend["other_currencies"] == ["CNY"]
    # Non-USD spend never reaches the limit.
    assert budget_gate("admin", store)["allowed"] is True


# --- API gates -----------------------------------------------------------------

def test_exhausted_budget_blocks_auto_mission_with_402(tmp_path, monkeypatch):
    app, client = build(tmp_path, monkeypatch)
    store = app.state.store
    store.set_user_budget("admin", 0.5)
    seed_spend(app, 0.6)
    response = client.post("/api/v1/missions/auto", json={"original_input": "再做一件事"})
    assert response.status_code == 402
    assert "预算已用尽" in response.json()["detail"]
    assert "0.60" in response.json()["detail"] and "0.50" in response.json()["detail"]
    # No NEW mission was created; only the seeded spend mission exists.
    assert len(store.list()) == 1


def test_exhausted_budget_blocks_manual_mission_create(tmp_path, monkeypatch):
    app, client = build(tmp_path, monkeypatch)
    app.state.store.set_user_budget("admin", 0.5)
    seed_spend(app, 0.6)
    response = client.post("/api/v1/missions",
                           json={"title": "新任务", "original_input": "原话", "steps": ["开发"]})
    assert response.status_code == 402
    assert "预算已用尽" in response.json()["detail"]


def test_zero_balance_blocks_until_credited(tmp_path, monkeypatch):
    """Balance-pool semantics (operator directive 2026-10-06): 0 = cannot start.

    No row and no env default reads as balance 0; an explicit 0 row reads as a
    frozen account. Credit the pool (admin tops up user_budgets) to start.
    """
    app, client = build(tmp_path, monkeypatch)
    seed_spend(app, 0)
    assert client.post("/api/v1/missions/auto",
                       json={"original_input": "余额为 0"}).status_code == 402
    app.state.store.set_user_budget("admin", 0)
    assert client.post("/api/v1/missions/auto",
                       json={"original_input": "冻结也是 0"}).status_code == 402
    # Crediting the pool unlocks dispatch.
    app.state.store.set_user_budget("admin", 5)
    assert client.post("/api/v1/missions/auto",
                       json={"original_input": "充值后开工"}).status_code == 201


def test_env_default_limit_applies_without_explicit_row(tmp_path, monkeypatch):
    app, client = build(tmp_path, monkeypatch, default_limit="0.5")
    seed_spend(app, 0.6)
    assert client.post("/api/v1/missions/auto",
                       json={"original_input": "环境默认上限"}).status_code == 402
    # An explicit row overrides the env default.
    app.state.store.set_user_budget("admin", 10)
    assert client.post("/api/v1/missions/auto",
                       json={"original_input": "显式行优先"}).status_code == 201


# --- billing endpoints ---------------------------------------------------------

def test_put_own_budget_and_me_reports_spend(tmp_path, monkeypatch):
    app, client = build(tmp_path, monkeypatch)
    put = client.put("/api/v1/billing/users/admin/budget",
                     json={"monthly_limit_usd": 0.5})
    assert put.status_code == 200, put.text
    assert put.json()["monthly_limit_usd"] == 0.5
    row = client.get("/api/v1/billing/users/admin/budget").json()
    assert row["explicit"] is True and row["monthly_limit_usd"] == 0.5

    seed_spend(app, 0.6)
    me = client.get("/api/v1/billing/me").json()
    assert me["monthly_limit_usd"] == 0.5
    assert me["spend"]["cost_usd"] >= me["monthly_limit_usd"]
    assert me["allowed"] is False
    assert "预算已用尽" in me["reason"]
    assert me["spend"]["missions"] == 1


def test_billing_check_is_dry_run_and_has_no_side_effects(tmp_path, monkeypatch):
    app, client = build(tmp_path, monkeypatch)
    app.state.store.set_user_budget("admin", 0.5)
    seed_spend(app, 0.6)
    check = client.post("/api/v1/billing/check").json()
    assert check["allowed"] is False
    assert check["limit"] == 0.5
    assert "预算已用尽" in check["reason"]
    assert check["spend"]["cost_usd"] == pytest.approx(0.6)
    assert len(app.state.store.list()) == 1  # the gate itself creates nothing


def test_budget_negative_value_rejected(tmp_path, monkeypatch):
    _app, client = build(tmp_path, monkeypatch)
    response = client.put("/api/v1/billing/users/admin/budget",
                          json={"monthly_limit_usd": -0.5})
    assert response.status_code == 422


def test_budget_read_and_write_require_self_or_operator(tmp_path, monkeypatch):
    app, client = build(tmp_path, monkeypatch)
    registered = client.post("/api/v1/auth/register",
                             json={"username": "bob", "password": "bob-pass-123"})
    assert registered.status_code == 201
    bob = registered.json()

    # A registered tenant cannot touch the operator's budget row...
    other = {"X-CSRF-Token": bob["csrf_token"]}
    assert client.put("/api/v1/billing/users/admin/budget", headers=other,
                      json={"monthly_limit_usd": 1}).status_code == 403
    assert client.get("/api/v1/billing/users/admin/budget", headers=other).status_code == 403
    # ...but may cap their own spending.
    own = client.put("/api/v1/billing/users/bob/budget", headers=other,
                     json={"monthly_limit_usd": 0.25})
    assert own.status_code == 200
    assert own.json()["monthly_limit_usd"] == 0.25

    # The operator (env-configured account) manages any tenant's row. The
    # registration response left bob's session cookie on this client, so log
    # the operator back in for a matching session + CSRF pair.
    relogin = client.post("/api/v1/auth/login",
                          json={"username": "admin", "password": PASSWORD}).json()
    client.headers.update({"X-CSRF-Token": relogin["csrf_token"]})
    operator = client.put("/api/v1/billing/users/bob/budget",
                          json={"monthly_limit_usd": 0.75})
    assert operator.status_code == 200
    assert operator.json()["monthly_limit_usd"] == 0.75


def test_pricing_reports_monthly_limit_for_current_owner(tmp_path, monkeypatch):
    _app, client = build(tmp_path, monkeypatch, default_limit="2.5")
    assert client.get("/api/v1/pricing").json()["monthly_limit_usd"] == 2.5
    client.put("/api/v1/billing/users/admin/budget", json={"monthly_limit_usd": 7})
    assert client.get("/api/v1/pricing").json()["monthly_limit_usd"] == 7


# --- channel paths -------------------------------------------------------------

def test_channel_dispatch_defers_with_budget_exceeded_event(tmp_path, monkeypatch):
    app, client = build(tmp_path, monkeypatch)
    store = app.state.store
    store.set_user_budget("admin", 0.5)
    seed_spend(app, 0.6)
    message_id = record_inbound(app, "update:budget")

    response = client.post(f"/api/v1/channels/telegram/messages/{message_id}/dispatch")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "DEFERRED"
    assert body["reason"] == "BUDGET_EXCEEDED"
    assert "预算已用尽" in body["detail"]

    events = store.channel_events(message_id=message_id)
    assert [event["type"] for event in events] == ["BUDGET_EXCEEDED"]
    assert "预算已用尽" in events[0]["payload"]["reason"]
    # The message stays verbatim and unconverted; no mission, no jobs.
    recorded = store.channel_message(message_id)
    assert recorded["mission_id"] is None
    assert recorded["text"] == "帮我做一个任务面板"
    assert len(store.list()) == 1


def test_channel_budget_reply_sent_via_configured_provider(tmp_path, monkeypatch):
    app, client = build(tmp_path, monkeypatch)
    store = app.state.store
    store.set_user_budget("admin", 0.5)
    seed_spend(app, 0.6)

    class StubProvider:
        name, implemented = "telegram", True

        def __init__(self):
            self.replies = []

        def configured(self):
            return True

        async def send(self, reply):
            self.replies.append(reply)
            return {"status": "SENT", "external_id": "stub-1"}

    stub = StubProvider()
    app.state.channels.providers["telegram"] = stub
    message_id = record_inbound(app, "update:budget-reply")
    response = client.post(f"/api/v1/channels/telegram/messages/{message_id}/dispatch")
    assert response.json()["status"] == "DEFERRED"
    assert len(stub.replies) == 1
    assert stub.replies[0].recipient == "42"
    assert stub.replies[0].text.startswith("预算已用尽")


def test_ensure_channel_dispatch_defers_existing_mission_before_enqueue(tmp_path, monkeypatch):
    app, client = build(tmp_path, monkeypatch)
    store = app.state.store
    store.set_user_budget("admin", 0.5)
    seed_spend(app, 0.6)
    mission = store.create("预算外任务", "原话", ["执行"], owner_id="admin")
    message_id = record_inbound(app, "update:budget-linked")
    store.attach_channel_mission(message_id, mission["id"])

    response = client.post(f"/api/v1/channels/telegram/messages/{message_id}/dispatch")
    assert response.status_code == 201  # the linked mission itself is returned
    deferred = [event for event in store.events(mission["id"])
                if event["type"] == "DISPATCH_DEFERRED"]
    assert deferred and deferred[0]["payload"]["reason"] == "BUDGET_EXCEEDED"
    assert store.jobs(mission["id"]) == []


# --- regression pins: budget closure (token blindness, resume/retry, webhook) ---

def test_model_complete_tokens_spend_the_budget(tmp_path, monkeypatch):
    """A /model/complete month records amount 0 in the ledger, yet the reported
    token split is real money: the gate must close on the PRICING estimate."""
    app, client = build(tmp_path, monkeypatch)
    store = app.state.store
    store.set_user_budget("admin", 100)
    mission = store.create("零元账单", "原话", ["执行"], owner_id="admin")
    client.put(f"/api/v1/missions/{mission['id']}/binding",
               json={"agent_id": "opencode", "model_id": "deepseek-flash"})

    async def fake_complete(model, messages, client=None, timeout=30, **kwargs):
        return {"status": "MODEL_COMPLETED", "label": "上游模型已完成", "http_status": 200,
                "content": "hi", "upstream": None, "reason": None,
                "usage": {"prompt_tokens": 2_000_000, "completion_tokens": 1_000_000,
                          "total_tokens": 3_000_000}}
    monkeypatch.setattr("facai.api.complete", fake_complete)
    response = client.post(f"/api/v1/missions/{mission['id']}/model/complete",
                           json={"messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200, response.text

    gate = budget_gate("admin", store)
    assert gate["allowed"] is False  # 2M×$95 + 1M×$218 = $408 while the ledger reads $0
    assert "$408.00" in gate["reason"]
    assert store.user_spend_month("admin")["cost_usd"] == 0.0
    assert client.post("/api/v1/missions",
                       json={"title": "续单", "original_input": "原话", "steps": ["开发"]}
                       ).status_code == 402


def test_retry_and_resume_refuse_a_spent_owner(tmp_path, monkeypatch):
    """resume/retry re-enqueue work, so they are NEW dispatches: a spent owner
    gets 402 and the job keeps its status. pause/cancel stay available — a
    spent owner must still be able to stop in-flight work."""
    app, client = build(tmp_path, monkeypatch)
    store = app.state.store
    store.set_user_budget("admin", 0.5)
    seed_spend(app, 0.6)
    mission = store.create("复活任务", "原话", ["执行"], owner_id="admin")
    failed = store.enqueue_job(mission["id"], "会失败的一步")
    store.finish_job(failed["id"], "FAILED", error="boom")
    paused = store.enqueue_job(mission["id"], "被暂停的一步")
    store.pause_job(paused["id"])

    retry = client.post(f"/api/v1/jobs/{failed['id']}/retry")
    assert retry.status_code == 402 and "预算已用尽" in retry.json()["detail"]
    assert store.get_job(failed["id"])["status"] == "FAILED"

    resume = client.post(f"/api/v1/jobs/{paused['id']}/resume")
    assert resume.status_code == 402 and "预算已用尽" in resume.json()["detail"]
    assert store.get_job(paused["id"])["status"] == "PAUSED"

    cancelled = client.post(f"/api/v1/jobs/{paused['id']}/cancel")
    assert cancelled.status_code == 200
    assert store.get_job(paused["id"])["status"] == "CANCELLED"


def test_budget_deferred_event_is_written_once_not_per_round(tmp_path, monkeypatch):
    """The monitor replays jobless missions every poll round: one stuck mission
    must carry a single DISPATCH_DEFERRED audit event, not one per round."""
    app, client = build(tmp_path, monkeypatch)
    store = app.state.store
    store.set_user_budget("admin", 0.5)
    seed_spend(app, 0.6)
    mission = store.create("卡住任务", "原话", ["执行"], owner_id="admin")
    message_id = record_inbound(app, "update:flood")
    store.attach_channel_mission(message_id, mission["id"])

    for _ in range(5):
        response = client.post(f"/api/v1/channels/telegram/messages/{message_id}/dispatch")
        assert response.status_code == 201  # the linked mission itself is returned
    deferred = [event for event in store.events(mission["id"])
                if event["type"] == "DISPATCH_DEFERRED"
                and event["payload"]["reason"] == "BUDGET_EXCEEDED"]
    assert len(deferred) == 1


def test_relay_webhook_over_budget_gates_before_the_conversion_model(tmp_path, monkeypatch):
    """The webhook budget gate runs right after the conversion claim: a spent
    sender's message creates no mission and burns no operator conversion call,
    the message parks outside the replay pool, and the bridge receives an
    accepted 202 instead of a retry-inviting 503."""
    import hashlib
    import hmac as hmac_mod
    import json as json_mod
    import time

    monkeypatch.setenv("FACAI_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.delenv("FACAI_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("FACAI_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("RELAY_SHARED_SECRET", "budget-relay-secret")
    app = create_app(tmp_path)
    client = TestClient(app)
    session = client.post("/api/v1/auth/login",
                          json={"username": "admin", "password": PASSWORD}).json()
    client.headers.update({"X-CSRF-Token": session["csrf_token"]})
    store = app.state.store
    store.set_user_budget("admin", 0.5)
    seed_spend(app, 0.6)

    calls = []

    async def spy_complete(model, messages, client=None, timeout=30, **kwargs):
        calls.append(model)
        return {"status": "MODEL_COMPLETED", "label": "不应到达", "http_status": 200,
                "content": "hi", "upstream": None, "reason": None, "usage": None}
    monkeypatch.setattr("facai.api.complete", spy_complete)

    body = json_mod.dumps({"external_id": "wa-budget", "sender": "8613800138000",
                           "text": "帮我在超预算后再做一件事"}, ensure_ascii=False).encode("utf-8")
    ts = str(int(time.time()))
    digest = hmac_mod.new(b"budget-relay-secret", ts.encode("ascii") + b"." + body,
                          hashlib.sha256).hexdigest()
    response = client.post("/api/v1/channels/relay/webhook", content=body, headers={
        "Content-Type": "application/json",
        "X-Facai-Signature": f"sha256={digest}", "X-Facai-Timestamp": ts})
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["dispatch"] == "DEFERRED"
    assert payload["results"][0]["status"] == "DEFERRED"
    assert payload["results"][0]["reason"] == "BUDGET_EXCEEDED"
    assert calls == []  # the paid conversion model was never invoked

    assert len(store.list("admin")) == 1  # no mission for the over-budget message
    recorded = store.channel_message_by_external_id("relay", "wa-budget")
    assert recorded["mission_id"] is None
    assert recorded["text"] == "帮我在超预算后再做一件事"  # verbatim, unconverted
    assert recorded["status"] == "DEFERRED"  # outside the RECEIVED/RETRY replay pool
    assert store.pending_inbound_messages("relay") == []
    events = store.channel_events(message_id=recorded["id"])
    assert [event["type"] for event in events] == ["BUDGET_EXCEEDED"]
