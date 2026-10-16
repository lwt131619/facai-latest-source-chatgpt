import pytest

pytestmark = pytest.mark.raw_budget

"""Cost-ledger token split, the frozen operator prices and the /billing/me estimate.

The money contract under test: the ``costs`` table records an input/output
split only when an upstream actually reported one, so historical and
total-only rows keep NULL and are never read as zero; and the console estimate
applies the operator's frozen ``PRICING`` to reported tokens — a row that only
has a combined count is charged at the OUTPUT rate and flagged conservative
rather than having a split invented for it.
"""

import json
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from facai.api import create_app
from facai.billing import PRICING, estimated_cost, priced_cost
from facai.store import Store

PASSWORD = "billing-pass-123"


def build(tmp_path, monkeypatch):
    monkeypatch.setenv("FACAI_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.delenv("FACAI_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("FACAI_SCHEDULER_ENABLED", "0")
    app = create_app(tmp_path)
    client = TestClient(app)
    session = client.post("/api/v1/auth/login",
                          json={"username": "admin", "password": PASSWORD}).json()
    client.headers.update({"X-CSRF-Token": session["csrf_token"]})
    return app, client


def cost_rows(store):
    with sqlite3.connect(store.database) as db:
        db.row_factory = sqlite3.Row
        return {row["id"]: dict(row) for row in db.execute(
            "SELECT id, amount, tokens, input_tokens, output_tokens FROM costs")}


# --- costs schema and split columns --------------------------------------------

def test_legacy_costs_table_gains_split_columns_with_null_history(tmp_path):
    """A deployed database keeps its rows and gains NULL split columns."""
    database = tmp_path / "facai.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("""CREATE TABLE costs (
            id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, provider TEXT, model_id TEXT,
            amount REAL NOT NULL, tokens INTEGER, currency TEXT NOT NULL,
            created_at TEXT NOT NULL)""")
        db.execute("INSERT INTO costs VALUES ('legacy', 'm1', 'deepseek', 'flash', 1.5,"
                   " 900, 'USD', '2026-10-01T00:00:00+00:00')")

    store = Store(tmp_path)

    with sqlite3.connect(store.database) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(costs)")}
        assert {"input_tokens", "output_tokens"} <= columns
        row = db.execute("SELECT tokens, input_tokens, output_tokens FROM costs"
                         " WHERE id='legacy'").fetchone()
    # The combined count survives; the split it never had stays NULL, not 0.
    assert row[0] == 900 and row[1] is None and row[2] is None


def test_add_cost_stores_the_reported_split_and_the_event_carries_it(tmp_path):
    store = Store(tmp_path)
    mission = store.create("分列任务", "原话", ["执行"])
    split_id = store.add_cost(mission["id"], "deepseek", "deepseek-flash", 0.0, 130,
                              input_tokens=80, output_tokens=50)
    total_id = store.add_cost(mission["id"], "deepseek", "deepseek-flash", 0.003, 500)

    rows = cost_rows(store)
    assert rows[split_id]["input_tokens"] == 80 and rows[split_id]["output_tokens"] == 50
    # A caller holding only a combined count records no split at all.
    assert rows[total_id]["tokens"] == 500
    assert rows[total_id]["input_tokens"] is None
    assert rows[total_id]["output_tokens"] is None

    events = [event for event in store.events(mission["id"])
              if event["type"] == "COST_RECORDED"]
    payload = next(event["payload"] for event in events
                   if event["payload"]["tokens"] == 130)
    assert payload["input_tokens"] == 80 and payload["output_tokens"] == 50


def test_add_cost_keeps_split_only_rows_and_drops_empty_ones(tmp_path):
    store = Store(tmp_path)
    mission = store.create("空计量", "原话", ["执行"])
    # Nothing reported at all: no ledger row is written.
    assert store.add_cost(mission["id"], "p", "m", 0.0) is None
    # A reported split is a real measurement even when the amount is zero and no
    # combined count exists, so the row must be kept.
    kept = store.add_cost(mission["id"], "p", "m", 0.0, None,
                          input_tokens=10, output_tokens=0)
    assert kept is not None
    assert len(cost_rows(store)) == 1


def test_user_spend_month_splits_reported_rows_and_flags_total_only(tmp_path):
    store = Store(tmp_path)
    split_mission = store.create("分列", "原话", ["执行"])
    total_mission = store.create("仅总量", "原话", ["执行"])
    split_only = store.create("只有分列", "原话", ["执行"])
    store.add_cost(split_mission["id"], "deepseek", "flash", 0.25, 130,
                   input_tokens=80, output_tokens=50)
    store.add_cost(total_mission["id"], "deepseek", "flash", 0.5, 900)
    store.add_cost(split_only["id"], "deepseek", "flash", 0.0, None,
                   input_tokens=7, output_tokens=3)

    spend = store.user_spend_month("admin")
    assert spend["input_tokens"] == 87 and spend["output_tokens"] == 53
    # Split-only rows contribute to the total even without a combined count.
    assert spend["total_tokens"] == 130 + 900 + 10
    assert spend["total_only_tokens"] == 900
    assert spend["cost_usd"] == pytest.approx(0.75)
    assert spend["missions"] == 3


# --- frozen pricing and the estimate -------------------------------------------

def test_priced_cost_uses_the_frozen_operator_rates():
    assert PRICING == {"input_per_mtok_usd": 95.0, "output_per_mtok_usd": 218.0}
    assert priced_cost(1_000_000, 0) == pytest.approx(95.0)
    assert priced_cost(0, 1_000_000) == pytest.approx(218.0)
    assert priced_cost(2_000_000, 1_000_000) == pytest.approx(408.0)
    # An unreported side contributes nothing instead of being guessed.
    assert priced_cost(None, None) is None
    assert priced_cost(1_000_000, None) == pytest.approx(95.0)


def test_estimated_cost_prices_split_exactly_and_total_only_conservatively():
    split = estimated_cost({"input_tokens": 2_000_000, "output_tokens": 1_000_000,
                            "total_only_tokens": 0})
    assert split["usd"] == pytest.approx(408.0)
    assert split["conservative"] is False

    total_only = estimated_cost({"input_tokens": 0, "output_tokens": 0,
                                 "total_only_tokens": 1_000_000})
    # The conservative bound: a combined count cannot be split, so it is charged
    # at the output rate and says so.
    assert total_only["usd"] == pytest.approx(218.0)
    assert total_only["conservative"] is True

    mixed = estimated_cost({"input_tokens": 1_000_000, "output_tokens": 0,
                            "total_only_tokens": 1_000_000})
    assert mixed["usd"] == pytest.approx(95.0 + 218.0)
    assert mixed["conservative"] is True

    assert estimated_cost({"input_tokens": 0, "output_tokens": 0,
                           "total_only_tokens": 0}) is None


# --- API surface ---------------------------------------------------------------

def bind_mission(client, title="计费"):
    # Balance-pool world: these are pricing-surface tests, so fund the pool
    # before dispatch (raw-budget file — the funded-world fixture is opted out).
    client.app.state.store.set_user_budget("admin", 10_000)
    mission = client.post("/api/v1/missions", json={
        "title": title, "original_input": "原话", "steps": ["开发"]}).json()
    client.put(f"/api/v1/missions/{mission['id']}/binding",
               json={"agent_id": "opencode", "model_id": "deepseek-flash"})
    return mission


def complete_with(client, monkeypatch, usage):
    async def fake_complete(model, messages, client=None, timeout=30):
        return {"status": "MODEL_COMPLETED", "label": "上游模型已完成", "http_status": 200,
                "content": "hi", "upstream": None, "reason": None, "usage": usage}
    monkeypatch.setattr("facai.api.complete", fake_complete)
    mission = bind_mission(client)
    response = client.post(f"/api/v1/missions/{mission['id']}/model/complete",
                           json={"messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200, response.text
    return response.json()


def test_billing_me_reports_frozen_pricing_and_a_split_estimate(tmp_path, monkeypatch):
    _app, client = build(tmp_path, monkeypatch)
    body = complete_with(client, monkeypatch, {"prompt_tokens": 2_000_000,
                                               "completion_tokens": 1_000_000,
                                               "total_tokens": 3_000_000})
    assert body["tokens"] == 3_000_000

    me = client.get("/api/v1/billing/me").json()
    assert me["pricing"]["input_per_mtok_usd"] == 95.0
    assert me["pricing"]["output_per_mtok_usd"] == 218.0
    # The gateway reported the split, so it lands in the ledger columns and the
    # estimate is exact rather than conservative.
    assert me["spend"]["input_tokens"] == 2_000_000
    assert me["spend"]["output_tokens"] == 1_000_000
    assert me["spend"]["total_only_tokens"] == 0
    assert me["estimated_cost"]["usd"] == pytest.approx(2 * 95.0 + 218.0)
    assert me["estimated_cost"]["conservative"] is False


def test_billing_me_flags_a_total_only_month_as_conservative(tmp_path, monkeypatch):
    app, client = build(tmp_path, monkeypatch)
    body = complete_with(client, monkeypatch, {"total_tokens": 1_000_000})
    # A total-only report still records the count, with the split left NULL.
    assert body["tokens"] == 1_000_000
    rows = cost_rows(app.state.store)
    assert all(row["input_tokens"] is None and row["output_tokens"] is None
               for row in rows.values())

    me = client.get("/api/v1/billing/me").json()
    assert me["spend"]["total_only_tokens"] == 1_000_000
    assert me["estimated_cost"]["usd"] == pytest.approx(218.0)
    assert me["estimated_cost"]["conservative"] is True
    assert "保守" in me["estimated_cost"]["basis"] or "输出" in me["estimated_cost"]["basis"]


def test_billing_me_without_tokens_reports_pricing_and_no_estimate(tmp_path, monkeypatch):
    _app, client = build(tmp_path, monkeypatch)
    me = client.get("/api/v1/billing/me").json()
    assert me["pricing"]["input_per_mtok_usd"] == 95.0
    assert me["estimated_cost"] is None
    assert me["spend"]["total_tokens"] == 0


# --- console panel -------------------------------------------------------------

def test_console_billing_panel_shows_the_frozen_unit_prices(console_server, playwright_api):
    """The 我的账单 panel quotes the frozen rates it received, never a local guess."""
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(args=['--no-sandbox'], **({'executable_path': os.environ['FACAI_TEST_CHROMIUM']} if os.getenv('FACAI_TEST_CHROMIUM') else {}))
        except Exception as exc:  # browser binary not installed
            pytest.skip(f"Playwright chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        # Balance-pool world: fund the console owner before dispatch.
        import urllib.request as _ur
        _req = _ur.Request(f"{console_server}/api/v1/billing/users/admin/budget",
                           data=json.dumps({"monthly_limit_usd": 10_000}).encode(),
                           headers={"Content-Type": "application/json"}, method="PUT")
        _ur.urlopen(_req)
        created = page.request.post(
            f"{console_server}/api/v1/missions",
            data=json.dumps({"title": "账单单价", "original_input": "核验单价行",
                             "steps": ["开发"]}),
            headers={"Content-Type": "application/json"})
        assert created.ok, created.text
        mission = created.json()

        page.goto(f"{console_server}/?mission={mission['id']}", wait_until="load")
        panel = page.locator("#detail .billing-panel.my-billing")
        panel.wait_for()
        text = panel.inner_text()
        assert "输入 $95/1M · 输出 $218/1M" in text
        assert "估算费用" in text
        browser.close()
