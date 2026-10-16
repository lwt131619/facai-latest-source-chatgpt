"""ICS 事件总线测试（facai.eventbus + /api/v1/events*）。

2026-10-13 平台化指令：手机、电脑、IM 订阅同一条 owner 级事件流，
状态同步靠事件不靠各端轮询。这里钉死三件事：规范词汇只在写入端校验、
全局流严格按 owner 隔离、遗留事件类型（PROMPT_CONVERTED 等）原样流过
不被总线拦截。
"""

import pytest

from facai.eventbus import CANONICAL_TYPES, EventBusError, publish
from facai.store import Store

TEXT = "帮我写一个爬虫抓取商品价格并存成表格"


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from facai.api import create_app
    monkeypatch.setenv("FACAI_ADMIN_PASSWORD", "bus-pass-1")
    monkeypatch.delenv("FACAI_COOKIE_SECURE", raising=False)
    client = TestClient(create_app(tmp_path))
    session = client.post("/api/v1/auth/login",
                          json={"username": "admin", "password": "bus-pass-1"}).json()
    client.headers.update({"X-CSRF-Token": session["csrf_token"]})
    return client


class TestVocabulary:
    def test_canonical_types_cover_both_lifecycles(self):
        for name in ("MissionCreated", "StepCompleted", "VerificationPassed",
                     "DeploymentFinished", "SkillAdmitted", "EvolutionApproved"):
            assert name in CANONICAL_TYPES

    def test_publish_rejects_unknown_type_at_the_writer(self, tmp_path):
        store = Store(tmp_path)
        mission = store.create("写个脚本", "写个脚本", ["执行"], owner_id="admin")
        with pytest.raises(EventBusError):
            publish(store, mission["id"], "MissionCeated", {})   # typo dies here
        with pytest.raises(EventBusError):
            publish(store, mission["id"], "MissionCreated", "not-a-dict")

    def test_publish_appends_a_canonical_event(self, tmp_path):
        store = Store(tmp_path)
        mission = store.create("写个脚本", "写个脚本", ["执行"], owner_id="admin")
        event_id = publish(store, mission["id"], "MissionStarted",
                           {"stage": "执行", "agent": "opencode"})
        events = store.events_for_owner("admin")
        assert any(e["id"] == event_id and e["type"] == "MissionStarted"
                   for e in events)


class TestGlobalStreamApi:
    def test_events_replay_is_owner_scoped_with_cursor(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        created = client.post("/api/v1/conversion/console",
                              json={"text": TEXT}).json()
        feed = client.get("/api/v1/events").json()["events"]
        assert len(feed) >= 2
        sequences = [e["sequence"] for e in feed]
        assert sequences == sorted(sequences)
        assert any(e["mission_id"] == created["mission"]["id"] for e in feed)
        # The cursor contract: everything after the first event is replayable.
        after = feed[0]["sequence"]
        tail = client.get(f"/api/v1/events?after={after}").json()["events"]
        assert [e["sequence"] for e in tail] == sequences[1:]

    def test_concurrent_appends_replay_idempotently(self, tmp_path, monkeypatch):
        """Fleet writes: schedulers appending concurrently must leave the
        global feed strictly ordered and duplicate-free, and two clients
        replaying from the same cursor get the identical tail — replay is
        idempotent, never double-delivering a sequence."""
        import threading

        client = _client(tmp_path, monkeypatch)
        client.post("/api/v1/conversion/console", json={"text": TEXT})
        store = Store(tmp_path)
        mission_id = store.events_for_owner("admin")[0]["mission_id"]

        barrier = threading.Barrier(8)
        def publish_one(index):
            barrier.wait()
            publish(store, mission_id, "StepCompleted", {"stage": f"s{index}"})

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(publish_one, range(24)))

        feed = client.get("/api/v1/events").json()["events"]
        sequences = [e["sequence"] for e in feed]
        assert sequences == sorted(sequences)
        assert len(sequences) == len(set(sequences))   # never double-delivered
        assert sum(1 for e in feed if e["type"] == "StepCompleted") == 24
        cursor = sequences[10]
        tail_a = client.get(f"/api/v1/events?after={cursor}").json()["events"]
        tail_b = client.get(f"/api/v1/events?after={cursor}").json()["events"]
        assert tail_a == tail_b
        assert [e["sequence"] for e in tail_a] == sequences[11:]

    def test_legacy_event_types_flow_through_untouched(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        client.post("/api/v1/conversion/console", json={"text": TEXT})
        # A canonical event written by new code lands in the same global feed
        # as the legacy types the console already emits.
        store = Store(tmp_path)
        mission_id = store.events_for_owner("admin")[0]["mission_id"]
        publish(store, mission_id, "StepCompleted", {"stage": "执行"})
        feed = client.get("/api/v1/events").json()["events"]
        types = {e["type"] for e in feed}
        assert "StepCompleted" in types
        assert any(not t.isascii() or t.isupper() for t in types)   # legacy names ride along

    def test_other_owner_sees_nothing(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        client.post("/api/v1/conversion/console", json={"text": TEXT})
        tenant = client.post("/api/v1/auth/register",
                             json={"username": "bob", "password": "bob-pass-1234"})
        assert tenant.status_code == 201, tenant.text
        client.headers.update({"X-CSRF-Token": tenant.json()["csrf_token"]})
        assert client.get("/api/v1/events").json()["events"] == []

    def test_event_endpoints_require_authentication(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from facai.api import create_app
        monkeypatch.setenv("FACAI_ADMIN_PASSWORD", "bus-pass-1")
        client = TestClient(create_app(tmp_path))
        assert client.get("/api/v1/events").status_code == 401
        # Reaching the stream without a session is rejected before any frame.
        assert client.get("/api/v1/events/stream").status_code == 401
