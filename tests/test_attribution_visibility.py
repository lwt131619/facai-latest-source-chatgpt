import pytest
"""provider/model/runtime/cost must be consistently visible in events and responses."""

import subprocess

from fastapi.testclient import TestClient

from facai.api import create_app
from facai.opencode import OpenCodeAdapter


class JsonRunner:
    def __init__(self, stdout):
        self.stdout = stdout

    def __call__(self, command, cwd, timeout):
        return subprocess.CompletedProcess(command, 0, self.stdout, "")


def setup(tmp_path, adapter=None):
    app = TestClient(create_app(tmp_path, opencode_adapter=adapter))
    mission = app.post('/api/v1/missions', json={
        'title': '归因', 'original_input': '原话', 'steps': ['开发']}).json()
    app.put('/api/v1/registries/runtimes/gpu-eu',
            json={'name': 'GPU host', 'kind': 'gpu', 'provider': 'runpod',
                  'location': 'eu-west', 'status': 'ready'})
    app.put(f"/api/v1/missions/{mission['id']}/binding",
            json={'agent_id': 'opencode', 'model_id': 'deepseek-flash', 'runtime_id': 'gpu-eu'})
    return app, mission


def test_opencode_task_events_carry_provider_model_runtime_and_cost(tmp_path):
    stdout = '{"part":{"type":"text","text":"done","cost":0.0012,"tokens":{"total":123}}}\n'
    app, mission = setup(tmp_path, OpenCodeAdapter(binary='opencode', runner=JsonRunner(stdout)))

    body = app.post(f"/api/v1/missions/{mission['id']}/opencode/run",
                    json={'prompt': 'work', 'step_id': mission['steps'][0]['id']}).json()
    assert body['provider'] == 'DeepSeek'
    assert body['model_id'] == 'deepseek-flash'
    assert body['runtime_id'] == 'gpu-eu'
    assert body['runtime_location'] == 'eu-west'
    # Billing standard: body['cost'] is the FACAI-priced amount
    assert body['cost'] == pytest.approx(123 / 1_000_000 * 218)
    assert body['agent_reported_usd'] == pytest.approx(0.0012)
    assert body['tokens'] == 123

    events = app.get(f"/api/v1/missions/{mission['id']}/events").json()
    terminal = next(event for event in events if event['type'] == 'OPENCODE_COMPLETED')
    for key, value in (('provider', 'DeepSeek'), ('model_id', 'deepseek-flash'),
                       ('runtime_id', 'gpu-eu'), ('runtime_location', 'eu-west'),
                       ('cost', 123 / 1_000_000 * 218),
                       ('agent_reported_usd', 0.0012), ('tokens', 123)):
        assert terminal['payload'][key] == value
    started = next(event for event in events if event['type'] == 'OPENCODE_STARTED')
    assert started['payload']['runtime_id'] == 'gpu-eu'
    assert started['payload']['provider'] == 'DeepSeek'

    detail = app.get(f"/api/v1/missions/{mission['id']}").json()
    assert round(detail['cost_total'], 4) == 0.0268  # FACAI-priced


def test_model_complete_records_attribution_and_tokens(tmp_path, monkeypatch):
    async def fake_complete(model, messages, **kwargs):
        return {'status': 'MODEL_COMPLETED', 'label': '上游模型已完成', 'http_status': 200,
                'content': 'hi', 'upstream': None, 'reason': None,
                'usage': {'total_tokens': 77}}
    monkeypatch.setattr('facai.api.complete', fake_complete)
    app, mission = setup(tmp_path)

    body = app.post(f"/api/v1/missions/{mission['id']}/model/complete",
                    json={'messages': [{'role': 'user', 'content': 'hi'}],
                          'step_id': mission['steps'][0]['id']}).json()
    assert body['provider'] == 'DeepSeek' and body['model_id'] == 'deepseek-flash'
    assert body['runtime_id'] == 'gpu-eu' and body['runtime_location'] == 'eu-west'
    assert body['content'] == 'hi' and body['tokens'] == 77

    events = app.get(f"/api/v1/missions/{mission['id']}/events").json()
    outcome = next(event for event in events if event['type'] == 'MODEL_COMPLETED')
    assert outcome['payload']['provider'] == 'DeepSeek'
    assert outcome['payload']['runtime_id'] == 'gpu-eu'
    assert outcome['payload']['tokens'] == 77
    cost = next(event for event in events if event['type'] == 'COST_RECORDED')
    assert cost['payload']['tokens'] == 77


def test_upstream_failure_keeps_its_own_identity(tmp_path, monkeypatch):
    async def refused(model, messages, **kwargs):
        return {'status': 'MODEL_REFUSED', 'label': '上游模型拒绝回答', 'http_status': 200,
                'content': None, 'upstream': {'choices': [{'message': {'refusal': 'no'}}]},
                'reason': 'refusal', 'usage': None}
    monkeypatch.setattr('facai.api.complete', refused)
    app, mission = setup(tmp_path)
    body = app.post(f"/api/v1/missions/{mission['id']}/model/complete",
                    json={'messages': [{'role': 'user', 'content': 'x'}]}).json()
    assert body['status'] == 'MODEL_REFUSED' and body['label'] == '上游模型拒绝回答'
    events = app.get(f"/api/v1/missions/{mission['id']}/events").json()
    outcome = next(event for event in events if event['type'] == 'MODEL_REFUSED')
    assert outcome['payload']['upstream']['choices'][0]['message']['refusal'] == 'no'
    # A refusal never completes the mission or the step.
    detail = app.get(f"/api/v1/missions/{mission['id']}").json()
    assert detail['steps'][0]['status'] == 'WAITING'
