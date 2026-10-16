import asyncio
import base64

import httpx
import sqlite3
from fastapi.testclient import TestClient

from facai.api import create_app
from facai.gateway import complete


def test_missions_are_isolated_and_progress_is_real(tmp_path):
    client = TestClient(create_app(tmp_path))
    def create(name):
        response = client.post('/api/v1/missions', json={
            'title': name, 'original_input': f'原话 {name}', 'steps': ['Plan', 'Build']})
        assert response.status_code == 201
        return response.json()

    first, second = create('A'), create('B')
    first_step = first['steps'][0]['id']
    assert client.patch(f"/api/v1/missions/{first['id']}/steps/{first_step}", json={'status': 'RUNNING'}).status_code == 200
    assert client.patch(f"/api/v1/missions/{first['id']}/steps/{first_step}", json={'status': 'COMPLETED'}).status_code == 200
    assert client.get(f"/api/v1/missions/{first['id']}").json()['progress'] == 50
    assert client.get(f"/api/v1/missions/{second['id']}").json()['progress'] == 0
    assert client.patch(f"/api/v1/missions/{second['id']}/steps/{first_step}", json={'status': 'RUNNING'}).status_code == 404
    assert client.post(f"/api/v1/missions/{second['id']}/events", json={'type': 'OUTPUT', 'step_id': first_step}).status_code == 409
    first_events = client.get(f"/api/v1/missions/{first['id']}/events").json()
    second_events = client.get(f"/api/v1/missions/{second['id']}/events").json()
    # TRUST auto-collection may append TRUST_EVIDENCE_COLLECTED after
    # STEP_COMPLETED; count only the mission-lifecycle events it guards.
    first_types = [event['type'] for event in first_events
                   if event['type'] != 'TRUST_EVIDENCE_COLLECTED']
    assert len(first_types) == 3
    assert len(second_events) == 1
    assert all(event['mission_id'] == first['id'] for event in first_events)
    assert all(event['mission_id'] == second['id'] for event in second_events)


def test_registry_binding_and_opencode_session_are_mission_specific(tmp_path):
    client = TestClient(create_app(tmp_path))
    a = client.post('/api/v1/missions', json={'title': 'A', 'original_input': 'A', 'steps': ['Build']}).json()
    b = client.post('/api/v1/missions', json={'title': 'B', 'original_input': 'B', 'steps': ['Build']}).json()
    assert client.put('/api/v1/registries/models/one', json={
        'provider': 'local', 'endpoint': 'http://127.0.0.1:11434/v1', 'model': 'example', 'api_key_env': 'MODEL_KEY'}).status_code == 200
    assert 'api_key' not in client.get('/api/v1/registries/models').json()['one']
    assert client.put(f"/api/v1/missions/{a['id']}/binding", json={
        'agent_id': 'opencode', 'model_id': 'one'}).status_code == 200
    prepared = client.post(f"/api/v1/missions/{a['id']}/opencode/prepare").json()
    assert prepared['mission_id'] == a['id']
    assert a['id'] in prepared['workspace']
    assert client.get(f"/api/v1/missions/{b['id']}").json()['model_id'] is None
    assert client.get(f"/api/v1/missions/{b['id']}").json()['sessions'] == []


def test_registries_persist_across_app_restarts_without_storing_api_keys(tmp_path):
    client = TestClient(create_app(tmp_path))
    assert client.get('/api/v1/registries/models').json()['deepseek-flash']['api_key_env'] == 'DEEPSEEK_API_KEY'
    rejected = client.put('/api/v1/registries/models/persisted', json={
        'provider': 'local', 'endpoint': 'http://127.0.0.1:11434/v1',
        'model': 'example', 'api_key': 'must-not-be-stored',
        'api_key_env': 'LOCAL_MODEL_KEY'})
    assert rejected.status_code == 422
    response = client.put('/api/v1/registries/models/persisted', json={
        'provider': 'local', 'endpoint': 'http://127.0.0.1:11434/v1',
        'model': 'example', 'api_key_env': 'LOCAL_MODEL_KEY'})
    assert response.status_code == 200
    legacy_enabled = client.put(
        '/api/v1/registries/skills/reviewer',
        json={'name': 'reviewer', 'enabled': True})
    # The enable ban is gone: legacy enabled:true registers with the
    # approved default (import/registration is an immediate admission).
    # PUT responses echo only {id, registered}; state is read back via GET.
    assert legacy_enabled.status_code == 200
    reviewer = client.get('/api/v1/registries/skills').json()['reviewer']
    assert reviewer['enabled'] is True
    assert reviewer['review_status'] == 'approved'
    approved_skill = client.put('/api/v1/registries/skills/reviewer', json={
        'name': 'reviewer', 'description': 'Reviewed workflow helper',
        'source_url': 'https://example.com/skills/reviewer',
        'license': 'Apache-2.0', 'review_status': 'approved', 'enabled': True})
    assert approved_skill.status_code == 200
    client.put('/api/v1/registries/runtimes/worker', json={'kind': 'container', 'image': 'python:3'})
    client.put('/api/v1/registries/agents/custom', json={'adapter': 'custom', 'name': 'Custom'})

    reopened = TestClient(create_app(tmp_path))
    model = reopened.get('/api/v1/registries/models').json()['persisted']
    assert model == {'provider': 'local', 'endpoint': 'http://127.0.0.1:11434/v1',
                     'model': 'example', 'api_key_env': 'LOCAL_MODEL_KEY'}
    persisted_skill = reopened.get('/api/v1/registries/skills').json()['reviewer']
    assert persisted_skill['enabled'] is True
    assert persisted_skill['review_status'] == 'approved'
    assert reopened.get('/api/v1/registries/runtimes').json()['worker']['image'] == 'python:3'
    assert reopened.get('/api/v1/registries/agents').json()['custom']['adapter'] == 'custom'
    with sqlite3.connect(tmp_path / 'facai.sqlite3') as db:
        stored = db.execute("SELECT value FROM registries WHERE id='persisted'").fetchone()[0]
    assert 'must-not-be-stored' not in stored


def test_nested_raw_api_keys_are_rejected_and_legacy_keys_are_redacted(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)
    for nested in (
            {'settings': {'api_key': 'secret'}},
            {'settings': [{'API_KEY': 'secret'}]},
    ):
        response = client.put('/api/v1/registries/models/nested-secret', json=nested)
        assert response.status_code == 422

    # Older persisted entries may contain nested credential fields. Never expose
    # these through the public registry endpoint, even before they are migrated.
    app.state.registries.catalogs['models']['legacy'] = {
        'provider': 'old', 'settings': [{'API_KEY': 'legacy-secret', 'region': 'x'}]}
    response = client.get('/api/v1/registries/models')
    assert response.status_code == 200
    assert 'legacy-secret' not in response.text
    assert response.json()['legacy'] == {
        'provider': 'old', 'settings': [{'region': 'x'}]}


def test_model_outcome_is_attributed_to_upstream():
    def respond(request):
        return httpx.Response(200, json={'choices': [{'message': {'refusal': 'declined'}, 'finish_reason': 'refusal'}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await complete({'endpoint': 'https://example.test/v1', 'model': 'sample'},
                                  [{'role': 'user', 'content': 'hello'}], client)
    result = asyncio.run(run())
    assert result['status'] == 'MODEL_REFUSED'
    assert result['http_status'] == 200
    assert result['upstream']['choices'][0]['message']['refusal'] == 'declined'


def test_admin_password_protects_console_and_api(tmp_path, monkeypatch):
    monkeypatch.setenv('FACAI_ADMIN_PASSWORD', 'test-password')
    client = TestClient(create_app(tmp_path))
    assert client.get('/').status_code == 401
    assert client.get('/api/v1/missions').status_code == 401
    encoded = base64.b64encode(b'admin:test-password').decode()
    headers = {'Authorization': f'Basic {encoded}'}
    assert client.get('/', headers=headers).status_code == 200
    assert client.get('/api/v1/missions', headers=headers).status_code == 200
