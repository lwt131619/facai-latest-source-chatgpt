"""Loopback-only bootstrap, hashed Owner restart, recovery and honest diagnostics."""
import importlib.util
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from facai.api import create_app
from facai.auth import verify_password

spec = importlib.util.spec_from_file_location('setup_launcher', Path(__file__).parents[1]/'scripts/start.py')
launcher = importlib.util.module_from_spec(spec); spec.loader.exec_module(launcher)


@pytest.fixture
def appliance(tmp_path, monkeypatch):
    for key in ['FACAI_ADMIN_PASSWORD','FACAI_USERS_JSON','FACAI_BOOTSTRAP_TOKEN','FACAI_LOCAL_OWNER_FILE','FACAI_ADMIN_USER','FACAI_GATES_OFF']:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('FACAI_SCHEDULER_ENABLED','0')
    monkeypatch.setenv('FACAI_CHANNEL_NOTIFY_ENABLED','0')
    config = launcher.credentials(tmp_path)
    for key, value in config.items(): monkeypatch.setenv(key, value)
    app = create_app(tmp_path)
    client = TestClient(app, base_url='http://localhost', client=('127.0.0.1', 50000))
    return client, config, tmp_path


def initialize(client, config, username='owner'):
    return client.post('/api/v1/setup/bootstrap',headers={'Origin':'http://localhost'},json={
        'token':config['FACAI_BOOTSTRAP_TOKEN'],'username':username,'password':'Grove-orbit-5932!'})


def login(client, user='owner', password='Grove-orbit-5932!'):
    result = client.post('/api/v1/auth/login',json={'username':user,'password':password})
    assert result.status_code == 200, result.text
    client.headers['X-CSRF-Token'] = result.json()['csrf_token']


def test_bootstrap_hash_one_time_restart_and_old_credential_revoked(appliance, monkeypatch):
    client, config, root = appliance
    assert client.get('/api/v1/setup/bootstrap').json()['bootstrap_pending']
    old = client.post('/api/v1/auth/login',json={'username':'admin','password':config['FACAI_ADMIN_PASSWORD']})
    assert old.status_code == 200
    result = initialize(client, config)
    assert result.status_code == 200, result.text
    recovery = result.json()['recovery_key']
    stored = json.loads((root/'local-owner.json').read_text())
    assert 'password' not in stored
    assert verify_password('Grove-orbit-5932!', stored['password_hash'])
    assert recovery not in (root/'local-owner.json').read_text()
    assert initialize(client, config).status_code == 409
    assert client.get('/api/v1/setup/status').status_code == 401
    assert client.post('/api/v1/auth/login',json={'username':'admin','password':config['FACAI_ADMIN_PASSWORD']}).status_code == 401
    monkeypatch.delenv('FACAI_ADMIN_PASSWORD'); monkeypatch.delenv('FACAI_ADMIN_USER')
    restarted = launcher.credentials(root)
    assert 'FACAI_BOOTSTRAP_TOKEN' not in restarted
    for key, value in restarted.items(): monkeypatch.setenv(key, value)
    other = TestClient(create_app(root), base_url='http://localhost', client=('127.0.0.1',50001))
    login(other)
    assert other.get('/api/v1/setup/status').status_code == 200
    assert not other.get('/api/v1/setup/bootstrap').json()['bootstrap_pending']


@pytest.mark.parametrize('headers,base,peer',[
    ({},'http://localhost','127.0.0.1'),
    ({'Origin':'https://attacker.invalid'},'http://localhost','127.0.0.1'),
    ({'Origin':'http://localhost','X-Forwarded-For':'127.0.0.1'},'http://localhost','127.0.0.1'),
    ({'Origin':'http://localhost'},'http://localhost','203.0.113.5'),
    ({'Origin':'http://example.invalid'},'http://example.invalid','127.0.0.1'),
])
def test_bootstrap_rejects_remote_forwarded_and_cross_origin(appliance, headers, base, peer):
    client, config, root = appliance
    other = TestClient(client.app, base_url=base, client=(peer,50001))
    result = other.post('/api/v1/setup/bootstrap',headers=headers,json={
        'token':config['FACAI_BOOTSTRAP_TOKEN'],'username':'owner','password':'Grove-orbit-5932!'})
    assert result.status_code == 403
    assert json.loads((root/'local-owner.json').read_text())['setup_complete'] is False


def test_invalid_token_weak_password_and_existing_mission_do_not_initialize(appliance):
    client, config, root = appliance
    body={'token':'incorrect','username':'owner','password':'Grove-orbit-5932!'}
    assert client.post('/api/v1/setup/bootstrap',headers={'Origin':'http://localhost'},json=body).status_code == 403
    body.update(token=config['FACAI_BOOTSTRAP_TOKEN'],password='password')
    assert client.post('/api/v1/setup/bootstrap',headers={'Origin':'http://localhost'},json=body).status_code == 422
    client.app.state.store.create('Existing','Existing',['Existing'],issue_grant=False)
    assert initialize(client, config).status_code == 409
    assert not json.loads((root/'local-owner.json').read_text())['setup_complete']


def test_recovery_single_use_rotates_key_and_revokes_sessions(appliance):
    client, config, root = appliance
    response = initialize(client, config); key=response.json()['recovery_key']
    login(client)
    assert client.get('/api/v1/setup/status').status_code == 200
    result = client.post('/api/v1/setup/recover',headers={'Origin':'http://localhost'},json={
        'recovery_key':key,'password':'Forest-cobalt-9275!'})
    assert result.status_code == 200, result.text
    assert result.json()['recovery_key'] != key
    assert client.get('/api/v1/setup/status').status_code == 401
    assert client.post('/api/v1/setup/recover',headers={'Origin':'http://localhost'},json={
        'recovery_key':key,'password':'Another-orbit-5275!'}).status_code == 403
    assert client.post('/api/v1/auth/login',json={'username':'owner','password':'Grove-orbit-5932!'}).status_code == 401
    login(client,password='Forest-cobalt-9275!')


def test_external_credentials_cannot_be_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv('FACAI_ADMIN_PASSWORD','externally-managed-pass')
    monkeypatch.delenv('FACAI_LOCAL_OWNER_FILE',raising=False)
    client=TestClient(create_app(tmp_path),base_url='http://localhost',client=('127.0.0.1',50000))
    assert client.get('/api/v1/setup/bootstrap').json()['local_appliance'] is False
    assert initialize(client,{'FACAI_BOOTSTRAP_TOKEN':'arbitrary'}).status_code == 409
    assert not (tmp_path/'local-owner.json').exists()


def test_diagnostics_are_protected_truthful_and_progress_persistent(appliance, monkeypatch):
    client, config, root = appliance
    initialize(client,config); login(client)
    monkeypatch.setattr('facai.setup_routes.shutil.which',lambda command:None)
    state=client.get('/status').json()
    assert state['checks']['database']['status']=='AVAILABLE'
    assert state['checks']['storage']['status']=='READ_WRITE'
    assert state['checks']['docker']['status']=='NOT_INSTALLED'
    assert state['checks']['rust']['required'] is False
    assert 'MODEL_NOT_VERIFIED' in state['blocking_checks']
    assert 'EXECUTION_CLI_NOT_INSTALLED' in state['blocking_checks']
    assert client.post('/api/v1/setup/finish',json={}).status_code==409
    result=client.post('/api/v1/setup/finish',json={'acknowledge_unready':True})
    assert result.status_code==200 and result.json()['production_ready'] is False
    assert client.get('/api/v1/setup/status').json()['setup']['actor']=='owner'
    assert client.get('/metrics').status_code==200
    assert 'facai_missions 0' in client.get('/metrics').text
    anonymous=TestClient(client.app)
    assert anonymous.get('/status').status_code==401
    assert anonymous.get('/metrics').status_code==401
    assert anonymous.get('/health').status_code==200


def test_model_probe_is_explicit_persistent_and_invalidated_by_config(appliance, monkeypatch):
    client, config, root = appliance
    initialize(client,config); login(client)
    calls=[]
    async def complete(model, messages, **kwargs):
        calls.append(model)
        return {'status':'MODEL_COMPLETED','http_status':200,'usage':{'input_tokens':1,'output_tokens':1}}
    monkeypatch.setattr('facai.api.complete',complete)
    body={'name':'Local fixture','provider':'Ollama','endpoint':'http://127.0.0.1:11434/v1','model':'fixture'}
    assert client.put('/api/v1/registries/models/setup-local',json=body).status_code==200
    before=client.get('/api/v1/setup/status').json()
    assert calls==[] and next(m for m in before['models'] if m['id']=='setup-local')['status']=='NOT_TESTED'
    result=client.post('/api/v1/setup/models/setup-local/probe')
    assert result.status_code==200 and result.json()['status']=='MODEL_COMPLETED'
    assert len(calls)==1
    state=client.get('/api/v1/setup/status').json()
    assert next(m for m in state['models'] if m['id']=='setup-local')['status']=='LAST_PROBE_SUCCEEDED'
    assert client.put('/api/v1/registries/models/setup-local',json={**body,'model':'different'}).status_code==200
    state=client.get('/api/v1/setup/status').json()
    assert next(m for m in state['models'] if m['id']=='setup-local')['status']=='NOT_TESTED'
    assert client.post('/api/v1/setup/models/missing/probe').status_code==404
