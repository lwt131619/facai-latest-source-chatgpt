"""Owner-facing 今日开发进度: evidence-based, integer, authenticated.

The panel must tell the owner what each AI provider actually changed today and
whether that work is verified locally vs. deployed to production. These tests
lock in the honest-data contract: percentages are whole integers derived only
from completed milestones, a provider whose call failed is never shown as
active, and the endpoint sits behind the dashboard's authentication.
"""

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from facai.api import (DAILY_PROGRESS_FILE, _daily_progress_percent, create_app,
                       daily_progress_payload, is_public_path)

STATUSES = {"verified_local", "in_progress", "blocked", "deployed", "stopped",
            "not_executing"}


def by_provider(payload):
    return {entry["provider"]: entry for entry in payload["providers"]}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FACAI_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("FACAI_ADMIN_PASSWORD", "daily-pass")
    monkeypatch.delenv("FACAI_COOKIE_SECURE", raising=False)
    return TestClient(create_app(tmp_path))


def login(client):
    response = client.post("/api/v1/auth/login",
                           json={"username": "admin", "password": "daily-pass"})
    assert response.status_code == 200
    return response.json()


# --- seeded facts ----------------------------------------------------------

def test_data_file_is_versioned_and_has_a_date():
    assert DAILY_PROGRESS_FILE.is_file(), DAILY_PROGRESS_FILE
    raw = json.loads(DAILY_PROGRESS_FILE.read_text(encoding="utf-8"))
    assert raw["date"] == "2026-10-02"
    assert raw["providers"]


def test_deepseek_completed_and_deployed():
    entry = by_provider(daily_progress_payload())["DeepSeek"]
    assert entry["status"] == "deployed"
    assert entry["local_verified"] is True
    assert entry["production_deployed"] is True
    assert entry["percent"] == 100
    assert "188" in " ".join(entry["evidence"])
    assert "488" in " ".join(entry["evidence"])


def test_gemini_tenant_isolation_deployed_after_regression_fix():
    entry = by_provider(daily_progress_payload())["Gemini"]
    assert entry["status"] == "deployed"
    assert entry["local_verified"] is True
    assert entry["production_deployed"] is True
    assert entry["percent"] == 100
    assert not entry["blockers"]


def test_grok_wechat_fix_deployed():
    entry = by_provider(daily_progress_payload())["Grok"]
    assert entry["status"] == "deployed"
    assert entry["local_verified"] is True
    assert entry["production_deployed"] is True
    assert entry["percent"] == 100
    evidence = " ".join(entry["evidence"])
    assert "20" in evidence and "deselect" in evidence.lower()


def test_glm_blocked_and_never_counted_as_active():
    entry = by_provider(daily_progress_payload())["GLM"]
    assert entry["status"] == "blocked"
    assert entry["local_verified"] is False
    assert entry["percent"] == 0
    text = " ".join(entry["evidence"]) + " ".join(entry["blockers"])
    assert "余额" in text or "资源" in text


def test_claude_stopped_is_not_a_failure():
    entry = by_provider(daily_progress_payload())["Claude"]
    assert entry["status"] == "stopped"
    assert entry["percent"] == 0
    assert entry["status"] != "blocked"


def test_other_providers_are_listed_as_not_executing():
    entries = by_provider(daily_progress_payload())
    others = [e for e in entries.values() if e["status"] == "not_executing"]
    assert others, "other providers must be listed"
    for entry in others:
        assert entry["blockers"] == []
        assert entry["percent"] == 0


def test_only_shipped_providers_claim_production_deployment():
    for entry in daily_progress_payload()["providers"]:
        expected = entry["provider"] in {"DeepSeek", "Gemini", "Grok", "Codex / OpenAI"}
        assert entry["production_deployed"] is expected
        assert (entry["status"] == "deployed") is expected


def test_every_status_is_a_known_explicit_value():
    for entry in daily_progress_payload()["providers"]:
        assert entry["status"] in STATUSES
        assert entry["status_label"]


# --- integer percentages from completed milestones -------------------------

def test_percent_helper_is_whole_and_boundary_safe():
    assert _daily_progress_percent([]) == 0
    assert _daily_progress_percent([{"done": False}]) == 0
    assert _daily_progress_percent([{"done": True}]) == 100
    assert _daily_progress_percent([{"done": True}, {"done": False},
                                    {"done": False}]) == 33
    assert _daily_progress_percent([{"done": True}, {"done": True},
                                    {"done": False}]) == 66
    for n in range(0, 8):
        value = _daily_progress_percent([{"done": True}] * n + [{"done": False}] * n)
        assert isinstance(value, int) and not isinstance(value, bool)
        assert 0 <= value <= 100


def test_api_percentages_equal_milestone_arithmetic():
    for entry in daily_progress_payload()["providers"]:
        milestones = entry["milestones"]
        total = len(milestones)
        done = sum(1 for m in milestones if m["done"] is True)
        expected = (100 * done) // total if total else 0
        assert entry["percent"] == expected, entry["provider"]
        assert isinstance(entry["percent"], int) and not isinstance(entry["percent"], bool)


def test_serialised_response_never_carries_a_float_or_raw_activity(client):
    login(client)
    response = client.get("/api/v1/daily-progress")
    assert response.status_code == 200
    payload = response.json()
    for entry in payload["providers"]:
        assert isinstance(entry["percent"], int) and not isinstance(entry["percent"], bool)
    for needle in (b"33.3333", b"66.6667", b".0%"):
        assert needle not in response.content


# --- authentication like the dashboard -------------------------------------

def test_endpoint_is_not_public():
    assert is_public_path("/api/v1/daily-progress") is False


def test_unauthenticated_request_is_rejected_then_allowed_after_login(client):
    assert client.get("/api/v1/daily-progress").status_code == 401
    login(client)
    response = client.get("/api/v1/daily-progress")
    assert response.status_code == 200
    assert {entry["provider"] for entry in response.json()["providers"]} >= {
        "DeepSeek", "Gemini", "Grok", "GLM", "Claude"}


def test_missing_or_malformed_data_file_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("FACAI_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("FACAI_ADMIN_PASSWORD", "daily-pass")
    test_client = TestClient(create_app(tmp_path))
    login(test_client)

    missing = tmp_path / "nope.json"
    monkeypatch.setenv("FACAI_DAILY_PROGRESS_FILE", str(missing))
    assert test_client.get("/api/v1/daily-progress").status_code == 404

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("FACAI_DAILY_PROGRESS_FILE", str(broken))
    assert test_client.get("/api/v1/daily-progress").status_code == 500


# --- console wiring (static, no browser needed) ----------------------------

def console_html():
    return (Path(DAILY_PROGRESS_FILE).parent.parent / "facai" / "static" / "index.html").read_text(
        encoding="utf-8")


def test_console_has_a_visible_daily_progress_panel():
    html = console_html()
    assert 'id="daily-progress-panel"' in html
    assert "今日开发进度" in html
    assert "loadDailyProgress()" in html
    assert "request('/daily-progress')" in html


def test_console_normalises_provider_percent_and_separates_deployment():
    html = console_html()
    assert "progressPct(p.percent)" in html
    assert "${p.percent}%" not in html, "the console must not interpolate raw provider percent"
    assert "本地已验证 · 未部署" in html
    assert "生产已部署" in html


def test_console_fetches_panel_for_the_dashboard_user(tmp_path, monkeypatch):
    monkeypatch.setenv("FACAI_SCHEDULER_ENABLED", "0")
    monkeypatch.delenv("FACAI_ADMIN_PASSWORD", raising=False)
    page = TestClient(create_app(tmp_path)).get("/")
    assert page.status_code == 200
    assert "daily-progress-panel" in page.text
    # Facts are server-rendered only on load; the static shell has no invented
    # provider percentage baked in.
    assert "100% ·" not in page.text


def _launch(playwright):
    try:
        return playwright.chromium.launch(args=['--no-sandbox'], **({'executable_path': os.environ['FACAI_TEST_CHROMIUM']} if os.getenv('FACAI_TEST_CHROMIUM') else {}))
    except Exception as exc:  # browser binary not installed
        pytest.skip(f"Playwright chromium unavailable: {exc}")


def test_panel_renders_seeded_facts_in_a_real_browser(console_server, playwright_api):
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(console_server + "/", wait_until="load")
        # The daily panel is collapsed by default (it used to wall off the
        # mission list); open it before asserting on its rendered rows.
        page.click("#daily-progress-panel summary")
        page.wait_for_selector("#daily-progress-panel .daily-provider[data-provider=deepseek]")

        text = page.inner_text("#daily-progress-panel")
        assert "今日开发进度" in text
        assert "DeepSeek" in text and "生产已部署" in text
        assert "GLM" in text and "受阻" in text
        assert "Claude" in text and "已停止" in text

        deployed = page.get_attribute(
            "#daily-progress-panel .daily-provider[data-provider=deepseek]", "data-deployed")
        assert deployed == "true"

        values = page.eval_on_selector_all(
            "#daily-progress-panel .daily-provider [role=progressbar]",
            "els => els.map(e => e.getAttribute('aria-valuenow'))")
        assert values and all(v.isdigit() and 0 <= int(v) <= 100 for v in values)
        browser.close()
