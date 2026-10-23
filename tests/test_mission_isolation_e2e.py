import pytest
"""Acceptance: two missions run independently with their own bindings and attribution.

Every list, event stream, session, artifact, checkpoint, job and cost figure is
checked for cross-mission leakage, and each task carries the provider, model,
runtime and cost that actually produced it.
"""

import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from facai.api import create_app
from facai.opencode import OpenCodeAdapter


class AttributionRunner:
    """Deterministic OpenCode stand-in that reports cost/tokens per task."""

    def __init__(self):
        self.calls = []

    def __call__(self, command, cwd, timeout):
        prompt = command[-1]
        self.calls.append({"prompt": prompt, "cwd": str(cwd)})
        if prompt == "task A":
            cost, tokens, text = 0.001, 10, "A-output"
        else:
            cost, tokens, text = 0.002, 20, "B-output"
        Path(cwd, "result.txt").write_text(text, encoding="utf-8")
        line = json.dumps({"part": {"type": "text", "text": text, "cost": cost,
                                    "tokens": {"total": tokens}}})
        return subprocess.CompletedProcess(command, 0, line + "\n", "")


def build(tmp_path, monkeypatch):
    monkeypatch.setenv("FACAI_SCHEDULER_ENABLED", "0")
    runner = AttributionRunner()
    app = TestClient(create_app(tmp_path, opencode_adapter=OpenCodeAdapter(binary="opencode", runner=runner)))
    app.put('/api/v1/registries/models/local-model', json={
        'provider': 'Local GPU', 'endpoint': 'http://127.0.0.1:11434/v1',
        'model': 'qwen', 'api_key_env': 'LOCAL_KEY'})
    app.put('/api/v1/registries/skills/review', json={'name': 'Review'})
    app.put('/api/v1/registries/skills/ship', json={'name': 'Ship'})
    app.put('/api/v1/registries/runtimes/gpu-eu', json={
        'name': 'GPU host', 'kind': 'gpu', 'provider': 'runpod', 'location': 'eu-west'})
    return app, runner


def make_mission(app, title):
    return app.post('/api/v1/missions', json={
        'title': title, 'original_input': f'原话：{title}', 'steps': ['开发', '测试']}).json()


def run_task(app, mission, prompt):
    job = app.post(f"/api/v1/missions/{mission['id']}/jobs",
                   json={'prompt': prompt, 'step_id': mission['steps'][0]['id']}).json()
    assert job['status'] == 'QUEUED'
    return job


def events(app, mission):
    return app.get(f"/api/v1/missions/{mission['id']}/events").json()


def test_two_missions_stay_isolated_and_fully_attributed(tmp_path, monkeypatch):
    app, runner = build(tmp_path, monkeypatch)
    a, b = make_mission(app, 'A'), make_mission(app, 'B')
    app.put(f"/api/v1/missions/{a['id']}/binding", json={
        'agent_id': 'opencode', 'model_id': 'deepseek-flash',
        'skill_id': 'review', 'runtime_id': 'local'})
    app.put(f"/api/v1/missions/{b['id']}/binding", json={
        'agent_id': 'opencode', 'model_id': 'local-model',
        'skill_id': 'ship', 'runtime_id': 'gpu-eu'})

    run_task(app, a, 'task A')
    run_task(app, b, 'task B')
    result = app.post('/api/v1/scheduler/run').json()
    assert result['executed'] == 2
    jobs = app.get(f"/api/v1/missions/{a['id']}/jobs").json() + \
        app.get(f"/api/v1/missions/{b['id']}/jobs").json()
    assert all(job['status'] == 'COMPLETED' for job in jobs)

    # Each task ran inside its own mission workspace.
    paths_by_prompt = {call['prompt']: call['cwd'] for call in runner.calls}
    assert a['id'] in paths_by_prompt['task A']
    assert b['id'] in paths_by_prompt['task B']

    detail_a = app.get(f"/api/v1/missions/{a['id']}").json()
    detail_b = app.get(f"/api/v1/missions/{b['id']}").json()
    assert detail_a['progress'] == 50 and detail_b['progress'] == 50
    assert round(detail_a['cost_total'], 4) == 0.0022  # FACAI-priced: 10/1M*218
    assert round(detail_b['cost_total'], 4) == 0.0044  # FACAI-priced: 20/1M*218

    events_a, events_b = events(app, a), events(app, b)
    assert all(event['mission_id'] == a['id'] for event in events_a)
    assert all(event['mission_id'] == b['id'] for event in events_b)

    done_a = next(event for event in events_a if event['type'] == 'OPENCODE_COMPLETED')
    done_b = next(event for event in events_b if event['type'] == 'OPENCODE_COMPLETED')
    assert done_a['payload']['provider'] == 'DeepSeek'
    assert done_a['payload']['model_id'] == 'deepseek-flash'
    assert done_a['payload']['runtime_id'] == 'local'
    assert done_a['payload']['skill_id'] == 'review'
    assert done_a['payload']['cost'] == pytest.approx(10 / 1_000_000 * 218)
    assert done_a['payload']['agent_reported_usd'] == pytest.approx(0.001)
    assert done_a['payload']['tokens'] == 10
    assert done_b['payload']['provider'] == 'Local GPU'
    assert done_b['payload']['model_id'] == 'local-model'
    assert done_b['payload']['runtime_id'] == 'gpu-eu'
    assert done_b['payload']['runtime_location'] == 'eu-west'
    assert done_b['payload']['skill_id'] == 'ship'
    assert done_b['payload']['cost'] == pytest.approx(20 / 1_000_000 * 218)
    assert done_b['payload']['agent_reported_usd'] == pytest.approx(0.002)
    assert done_b['payload']['tokens'] == 20

    # No output, prompt or artifact crosses the mission boundary.
    assert not any('B-output' in json.dumps(event) for event in events_a)
    assert not any('A-output' in json.dumps(event) for event in events_b)
    assert detail_a['sessions'][0]['mission_id'] == a['id']
    assert detail_b['sessions'][0]['mission_id'] == b['id']
    assert all(artifact['mission_id'] == a['id'] for artifact in detail_a['artifacts'])
    assert all(artifact['mission_id'] == b['id'] for artifact in detail_b['artifacts'])
    assert all(checkpoint['mission_id'] == a['id'] for checkpoint in detail_a['checkpoints'])
    assert all(checkpoint['mission_id'] == b['id'] for checkpoint in detail_b['checkpoints'])


def two_user_clients(tmp_path, monkeypatch):
    """One shared app, two independent sessions, one runner."""
    monkeypatch.setenv("FACAI_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("FACAI_USERS_JSON", '{"alice":"alice-pass","bob":"bob-pass"}')
    monkeypatch.delenv("FACAI_ADMIN_PASSWORD", raising=False)
    runner = AttributionRunner()
    api = create_app(tmp_path, opencode_adapter=OpenCodeAdapter(binary="opencode", runner=runner))
    return api, TestClient(api), TestClient(api), runner


def login(client, username, password):
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return response.json()["csrf_token"]


def test_two_users_cannot_reach_each_others_mission_records(tmp_path, monkeypatch):
    api, alice, bob, _ = two_user_clients(tmp_path, monkeypatch)
    alice_csrf = login(alice, "alice", "alice-pass")
    bob_csrf = login(bob, "bob", "bob-pass")

    a = alice.post("/api/v1/missions", headers={"X-CSRF-Token": alice_csrf}, json={
        "title": "Alice", "original_input": "alice private", "steps": ["Build"]}).json()
    b = bob.post("/api/v1/missions", headers={"X-CSRF-Token": bob_csrf}, json={
        "title": "Bob", "original_input": "bob private", "steps": ["Build"]}).json()
    alice.put(f"/api/v1/missions/{a['id']}/binding", headers={"X-CSRF-Token": alice_csrf},
              json={"agent_id": "opencode", "model_id": "deepseek-flash", "runtime_id": "local"})
    job = alice.post(f"/api/v1/missions/{a['id']}/jobs", headers={"X-CSRF-Token": alice_csrf},
                     json={"prompt": "alice private prompt",
                           "step_id": a["steps"][0]["id"]}).json()
    assert job["status"] == "QUEUED"

    # Every mission-scoped read is rejected for the non-owner.
    for path in (
        f"/api/v1/missions/{a['id']}",
        f"/api/v1/missions/{a['id']}/events",
        f"/api/v1/missions/{a['id']}/jobs",
        f"/api/v1/missions/{a['id']}/diff",
        f"/api/v1/missions/{a['id']}/channel-messages",
        f"/api/v1/jobs/{job['id']}",
        f"/api/v1/missions/{a['id']}/artifacts/guessed-artifact-id",
    ):
        assert bob.get(path).status_code == 404, path

    # Every mission-scoped write is rejected for the non-owner.
    assert bob.post(f"/api/v1/jobs/{job['id']}/cancel",
                    headers={"X-CSRF-Token": bob_csrf}).status_code == 404
    assert bob.post(f"/api/v1/missions/{a['id']}/jobs",
                    headers={"X-CSRF-Token": bob_csrf}, json={"prompt": "hijack"}).status_code == 404
    assert bob.post(f"/api/v1/missions/{a['id']}/events",
                    headers={"X-CSRF-Token": bob_csrf}, json={"type": "HIJACK"}).status_code == 404
    assert bob.patch(f"/api/v1/missions/{a['id']}/steps/{a['steps'][0]['id']}",
                     headers={"X-CSRF-Token": bob_csrf}, json={"status": "RUNNING"}).status_code == 404
    assert bob.put(f"/api/v1/missions/{a['id']}/binding",
                   headers={"X-CSRF-Token": bob_csrf}, json={"agent_id": "opencode"}).status_code == 404
    assert bob.put(f"/api/v1/missions/{a['id']}/prompt",
                   headers={"X-CSRF-Token": bob_csrf}, json={"professional_prompt": "hijack"}).status_code == 404

    # Lists never merge tenants.
    assert [m["id"] for m in alice.get("/api/v1/missions").json()] == [a["id"]]
    assert [m["id"] for m in bob.get("/api/v1/missions").json()] == [b["id"]]

    # sessions / artifacts / checkpoints / costs stay inside the owning mission.
    store = api.state.store
    store.start_session(a["id"], "opencode", "deepseek-flash")
    # 逆向克隆防护：API 响应不再携带服务器 workspace 路径，从 store 推导。
    workspace = Path(store.root) / "workspaces" / a["id"]
    artifact_path = workspace / "out.txt"
    artifact_path.write_text("alice artifact", encoding="utf-8")
    artifact_id = store.add_artifact(a["id"], "out.txt", artifact_path)
    store.add_checkpoint(a["id"], "deadbeef", "Alice checkpoint")
    store.add_cost(a["id"], "DeepSeek", "deepseek-flash", 1.25, tokens=5)

    alice_detail = alice.get(f"/api/v1/missions/{a['id']}").json()
    assert alice_detail["cost_total"] == 1.25
    assert all(session["mission_id"] == a["id"] for session in alice_detail["sessions"])
    assert all(cp["mission_id"] == a["id"] for cp in alice_detail["checkpoints"])
    assert all(art["mission_id"] == a["id"] for art in alice_detail["artifacts"])
    assert alice.get(f"/api/v1/missions/{a['id']}/artifacts/{artifact_id}").json()["content"] == "alice artifact"
    assert bob.get(f"/api/v1/missions/{a['id']}/artifacts/{artifact_id}").status_code == 404

    # The owner's job survives the attempted cross-tenant mutation.
    owner_view = alice.get(f"/api/v1/jobs/{job['id']}").json()
    assert owner_view["prompt"] == "alice private prompt"
    assert owner_view["status"] == "QUEUED"


def test_two_users_cannot_reach_each_others_channel_dispatch(tmp_path, monkeypatch):
    api, alice, bob, _ = two_user_clients(tmp_path, monkeypatch)
    bob_csrf = login(bob, "bob", "bob-pass")
    login(alice, "alice", "alice-pass")

    store = api.state.store
    a_message = store.record_channel_message(
        "telegram", "INBOUND", "RECEIVED", external_id="alice-1", text="alice secret",
        owner_id="alice")
    store.record_channel_message(
        "telegram", "INBOUND", "RECEIVED", external_id="bob-1", text="bob secret",
        owner_id="bob")

    assert bob.get(f"/api/v1/channels/telegram/messages/{a_message}/events").status_code == 404
    assert bob.post(f"/api/v1/channels/telegram/messages/{a_message}/dispatch",
                    headers={"X-CSRF-Token": bob_csrf}).status_code == 404
    assert [m["text"] for m in bob.get("/api/v1/inbox").json()] == ["bob secret"]


def test_model_refusal_is_attributed_only_to_its_own_mission(tmp_path, monkeypatch):
    app, _ = build(tmp_path, monkeypatch)
    a, b = make_mission(app, 'A'), make_mission(app, 'B')
    app.put(f"/api/v1/missions/{a['id']}/binding", json={
        'agent_id': 'opencode', 'model_id': 'deepseek-flash', 'runtime_id': 'local'})
    app.put(f"/api/v1/missions/{b['id']}/binding", json={
        'agent_id': 'opencode', 'model_id': 'local-model', 'runtime_id': 'gpu-eu'})

    async def refused(model, messages, **kwargs):
        return {'status': 'MODEL_REFUSED', 'label': '上游模型拒绝回答', 'http_status': 200,
                'content': None, 'upstream': {'choices': [{'message': {'refusal': 'no'}}]},
                'reason': 'refusal', 'usage': None}
    monkeypatch.setattr('facai.api.complete', refused)

    response = app.post(f"/api/v1/missions/{a['id']}/model/complete",
                        json={'messages': [{'role': 'user', 'content': 'x'}]}).json()
    assert response['status'] == 'MODEL_REFUSED'
    assert response['provider'] == 'DeepSeek' and response['model_id'] == 'deepseek-flash'

    events_a, events_b = events(app, a), events(app, b)
    assert any(event['type'] == 'MODEL_REFUSED' for event in events_a)
    assert not any(event['type'].startswith('MODEL_') for event in events_b)
    refused_event = next(event for event in events_a if event['type'] == 'MODEL_REFUSED')
    assert refused_event['payload']['runtime_id'] == 'local'
    # A refusal did not mark the mission complete.
    assert app.get(f"/api/v1/missions/{a['id']}").json()['status'] != 'COMPLETED'
