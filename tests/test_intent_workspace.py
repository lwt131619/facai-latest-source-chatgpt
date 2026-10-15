"""Real SQLite/API tests for understanding, confirmation and authorization boundaries."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from facai.api import create_app
from facai.intent_workspace import IntentWorkspace, IntentError
from facai.store import Store


def test_raw_revisions_missing_context_and_restart(tmp_path):
    ws=IntentWorkspace(Store(tmp_path)); raw='  全面分析这个项目，为了评估状态\n'
    a=ws.compile('alice',raw)
    assert a['raw_request']==raw and a['status']=='NEED_CONTEXT' and a['confidence'] is None
    b=ws.revise('alice',a['id'],1,{'action':'analyze','target':'/owner/project','scope':'comprehensive'})
    assert b['revision']==2 and b['status']=='READY' and len(b['revisions'])==2
    assert IntentWorkspace(Store(tmp_path)).get('alice',a['id'])['raw_request']==raw
    with pytest.raises(IntentError):ws.get('bob',a['id'])
    with pytest.raises(IntentError):ws.revise('alice',a['id'],1,{'action':'delete'})
    with pytest.raises(IntentError):ws.revise('alice',a['id'],2,{'raw_request':'replaced'})


def test_concurrent_confirmation_is_one_mission_without_grant_or_jobs(tmp_path,monkeypatch):
    import facai.store
    calls=[];monkeypatch.setattr(facai.store,'ensure_mission_grant',lambda id:calls.append(id))
    store=Store(tmp_path);ws=IntentWorkspace(store)
    a=ws.compile('alice','分析项目',{'action':'analyze','target':'/owner/repo'})
    with ThreadPoolExecutor(max_workers=6) as pool:
        values=list(pool.map(lambda _:ws.confirm('alice',a['id'],1),range(6)))
    assert len({v['mission_id'] for v in values})==1
    assert sum(not v['duplicate'] for v in values)==1
    mission=store.get(values[0]['mission_id'])
    assert mission['original_input']=='分析项目' and mission['status']=='WAITING'
    assert store.jobs(mission['id'])==[] and calls==[]
    assert any(e['type']=='INTENT_CONFIRMED' for e in store.events(mission['id']))
    assert len(store.list(owner_id='alice'))==1
    with pytest.raises(IntentError):ws.revise('alice',a['id'],1,{'action':'deploy'})


def test_not_ready_cancel_and_external_grant_guard(tmp_path):
    store=Store(tmp_path);ws=IntentWorkspace(store);a=ws.compile('alice','帮我搞一下')
    with pytest.raises(IntentError):ws.confirm('alice',a['id'],1)
    assert ws.cancel('alice',a['id'],1)['state']=='CANCELLED'
    with pytest.raises(IntentError):ws.confirm('alice',a['id'],1)
    with store.connect() as db:
        with pytest.raises(ValueError,match='cannot issue grants'):
            store.create('title','raw',['step'],connection=db)


def test_api_confirmation_auth_csrf_and_owner_isolation(tmp_path,monkeypatch):
    monkeypatch.setenv('FACAI_USERS_JSON','{"admin":"admin-intent-pass","alice":"alice-intent-pass"}')
    monkeypatch.setenv('FACAI_SCHEDULER_ENABLED','0')
    monkeypatch.setenv('FACAI_CHANNEL_NOTIFY_ENABLED','0')
    client=TestClient(create_app(tmp_path))
    assert client.post('/api/v1/intents',json={'raw_request':'分析项目'}).status_code==401
    login=client.post('/api/v1/auth/login',json={'username':'admin','password':'admin-intent-pass'}).json()
    assert client.post('/api/v1/intents',json={'raw_request':'分析项目'}).status_code==403
    client.headers['X-CSRF-Token']=login['csrf_token']
    r=client.post('/api/v1/intents',json={'raw_request':'分析项目','interpretation':{'action':'analyze','target':'/project'}})
    assert r.status_code==201,r.text
    id=r.json()['id'];r=client.post(f'/api/v1/intents/{id}/confirm',json={'expected_revision':1})
    assert r.status_code==200,r.text
    mission_id=r.json()['mission_id']
    with client.app.state.store.connect() as db:
        assert db.execute('SELECT count(*) FROM mission_grants').fetchone()[0]==0
    r=client.post(f'/api/v1/intents/{id}/authorize',json={'expected_revision':1})
    assert r.status_code==200,r.text
    assert r.json()['execution']=='NOT_DISPATCHED'
    assert client.app.state.store.jobs(mission_id)==[]
    other=TestClient(client.app)
    other.post('/api/v1/auth/login',json={'username':'alice','password':'alice-intent-pass'})
    assert other.get(f'/api/v1/intents/{id}').status_code==404
    assert client.get('/workspace').status_code==200


@pytest.mark.raw_budget
def test_confirmation_preserves_actual_zero_budget_gate(tmp_path,monkeypatch):
    monkeypatch.setenv('FACAI_ADMIN_PASSWORD','budget-intent-pass')
    monkeypatch.delenv('FACAI_USERS_JSON',raising=False)
    monkeypatch.delenv('FACAI_DEFAULT_MONTHLY_LIMIT_USD',raising=False)
    client=TestClient(create_app(tmp_path))
    session=client.post('/api/v1/auth/login',json={'username':'admin','password':'budget-intent-pass'}).json()
    client.headers['X-CSRF-Token']=session['csrf_token']
    intent=client.post('/api/v1/intents',json={'raw_request':'分析项目','interpretation':{'action':'analyze','target':'/repo'}}).json()
    r=client.post('/api/v1/intents/'+intent['id']+'/confirm',json={'expected_revision':1})
    assert r.status_code==402
    assert client.app.state.store.list()==[]
    assert client.get('/api/v1/intents/'+intent['id']).json()['state']=='DRAFT'


def test_confirmation_failure_rolls_back_mission_and_workspace(tmp_path,monkeypatch):
    store=Store(tmp_path);ws=IntentWorkspace(store)
    intent=ws.compile('alice','分析目标',{'action':'analyze','target':'/repo'})
    original=store._event
    def fail(db,mission_id,step_id,session_id,agent_id,type,payload):
        if type=='INTENT_CONFIRMED':raise RuntimeError('injected transaction failure')
        return original(db,mission_id,step_id,session_id,agent_id,type,payload)
    monkeypatch.setattr(store,'_event',fail)
    with pytest.raises(RuntimeError):ws.confirm('alice',intent['id'],1)
    assert store.list()==[]
    assert ws.get('alice',intent['id'])['state']=='DRAFT'
    assert not list((tmp_path/'workspaces').iterdir())
