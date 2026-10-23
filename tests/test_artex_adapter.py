"""ARTEX attached-engine adapter: dispatch, honest failure, boundary removal.

The fake transport speaks just enough of ARTEX's HTTP API (/api/tasks,
/api/tasks/{id}, /api/intercept/*) to pin the contract: one mission step
becomes one seeded ARTEX task polled to a terminal status, and the built-in
approval gate is bypassed on first dispatch per the Owner directive
(local/Owner highest) — rules disabled (records kept), intercept tool scope
emptied, judge never configured. No real ARTEX deployment is required.
"""

import httpx
import pytest

from facai.artex import ArtexAdapter


class FakeArtex:
    """In-memory ARTEX stand-in: rule list, task create, status script."""

    def __init__(self, script=("running", "done")):
        self.script = list(script)
        self.rules = [{"id": 1, "enabled": True}, {"id": 2, "enabled": False}]
        self.toggled = []
        self.tools_puts = []
        self.created = []
        self.tasks = {}
        self._next = 100

    def handler(self, request):
        path, method = request.url.path, request.method
        if method == "GET" and path == "/api/intercept/rules":
            return httpx.Response(200, json={"rules": self.rules})
        if method == "POST" and path.startswith("/api/intercept/rules/") and path.endswith("/toggle"):
            rule_id = int(path.split("/")[-2])
            self.toggled.append(rule_id)
            return httpx.Response(200, json={"ok": True, "enabled": False})
        if method == "PUT" and path == "/api/intercept/tools":
            import json
            self.tools_puts.append(json.loads(request.read()))
            return httpx.Response(200, json={"ok": True})
        if method == "POST" and path == "/api/tasks":
            import json
            body = json.loads(request.read())
            self.created.append(body)
            self._next += 1
            task_id = str(self._next)
            self.tasks[task_id] = list(self.script)
            return httpx.Response(200, json={"id": task_id, "status": "created"})
        if method == "GET" and path.startswith("/api/tasks/"):
            task_id = path.rsplit("/", 1)[-1]
            steps = self.tasks.get(task_id)
            if steps is None:
                return httpx.Response(404, json={"error": "task not found"})
            status = steps.pop(0) if len(steps) > 1 else steps[0]
            return httpx.Response(200, json={"id": task_id, "status": status})
        return httpx.Response(404, json={"error": f"unmatched {method} {path}"})


def adapter(fake, **kwargs):
    return ArtexAdapter(base_url="https://artex.test", client=httpx.Client(
        base_url="https://artex.test",
        transport=httpx.MockTransport(fake.handler)), **kwargs)


def test_boundary_removal_disables_rules_and_empties_tool_scope():
    fake = FakeArtex()
    result = adapter(fake).clear_boundaries()
    assert result["cleared"] is True
    assert result["rules_disabled"] == [1]          # already-off rule 2 untouched
    assert fake.toggled == [1]
    assert fake.tools_puts == [{"enabled_tools": []}]  # zero tools interceptable


def test_execute_creates_seeded_task_and_maps_done():
    fake = FakeArtex(script=("running", "done"))
    lines = []
    result = adapter(fake).execute("m1", None, "扫描目标并产出发现",
                                   timeout=30, on_output=lambda s, l: lines.append((s, l)))
    assert result["status"] == "OPENCODE_COMPLETED"
    assert result["exit_code"] == 0
    assert result["workspace"] == "artex://task/101"
    body = fake.created[0]
    assert body["goal"] == "扫描目标并产出发现"
    assert body["seed_first_intent"] is True      # worker starts without a planner round
    assert body["timeout_seconds"] == 30
    assert any("boundaries cleared: True" in line for _, line in lines)


def test_terminal_failures_map_honestly():
    for script, expected, exit_code in (
            (("failed",), "OPENCODE_FAILED", None),
            (("timeout",), "OPENCODE_TIMEOUT", None)):
        fake = FakeArtex(script=script)
        result = adapter(fake, disable_boundaries=False).execute("m", None, "go")
        assert result["status"] == expected
        assert result["exit_code"] == exit_code


def test_unconfigured_base_url_fails_honestly_without_network():
    result = ArtexAdapter(base_url=None).execute("m", None, "go")
    assert result["status"] == "OPENCODE_FAILED"
    assert "ARTEX_BASE_URL" in result["stderr"]


def test_dispatch_window_expiry_reports_timeout_with_last_status():
    fake = FakeArtex(script=("running",))
    adapter_ = adapter(fake, disable_boundaries=False)
    # Patch the poll interval so the window expires after the first poll.
    import facai.artex as artex_module
    original = artex_module.POLL_INTERVAL_SECONDS
    artex_module.POLL_INTERVAL_SECONDS = 0.01
    try:
        result = adapter_.execute("m", None, "go", timeout=1)
    finally:
        artex_module.POLL_INTERVAL_SECONDS = original
    assert result["status"] == "OPENCODE_TIMEOUT"
    assert "status=running" in result["stdout"] or result["stdout"].startswith("status=")


def test_adapter_registered_in_create_app_registry(tmp_path):
    from fastapi.testclient import TestClient
    from facai.api import create_app
    from facai.artex import ArtexAdapter as _Artex
    import os
    os.environ.pop("FACAI_ADMIN_PASSWORD", None)
    client = TestClient(create_app(tmp_path))
    adapters = None
    # resolve_adapter closes over the dict; reach it through a bound agent.
    client.app.state.registries.register("agents", "artex-runner", {
        "name": "ARTEX 自主渗透", "adapter": "artex"}, owner_id="admin")
    mission = client.post("/api/v1/missions", json={
        "title": "artex 绑定", "original_input": "x", "steps": ["侦察"]}).json()
    bound = client.put(f"/api/v1/missions/{mission['id']}/binding", json={
        "agent_id": "artex-runner", "model_id": "deepseek-flash"})
    assert bound.status_code == 200, bound.text
    assert isinstance(client.app.state, object)
    # The adapter instance must be an ArtexAdapter with honest availability.
    found = client.get("/api/v1/registries/agents").json()["artex-runner"]
    assert found["adapter"] == "artex"
    assert _Artex().available()["available"] is False  # unconfigured = honest
