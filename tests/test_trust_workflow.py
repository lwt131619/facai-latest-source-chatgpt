"""Real SQLite/API tests; synthetic execution fixtures are not live provider proof."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import zipfile

from fastapi.testclient import TestClient
import pytest

from facai.api import create_app
from facai.intent_workspace import IntentWorkspace
from facai.mission_spec import MissionSpecifications, digest
from facai.store import Store, now
from facai.trust import TrustError, TrustStore, classify_conflict


def mission(tmp_path, owner='alice'):
    store = Store(tmp_path)
    intents = IntentWorkspace(store)
    intent = intents.compile(owner, '  分析这个项目，为了评估状态\n', {
        'action': 'analyze', 'target': '/owner/project', 'purpose': '评估状态',
        'scope': 'comprehensive', 'constraints': ['只读，不修改文件']})
    id = intents.confirm(owner, intent['id'], 1)['mission_id']
    return store, TrustStore(store), id


def claim(service, id, revision, evidence_ids=(), **changes):
    fields = {'statement': '版本为 1', 'subject': 'project', 'predicate': 'version', 'value': '1',
              'context': {'time': '2026-10-16', 'scope': 'core', 'environment': 'local', 'definition': 'release'},
              'evidence_ids': evidence_ids}
    fields.update(changes)
    return service.add_claim('alice', id, revision, **fields)


def reviewed(service, id):
    e = service.add_evidence('alice', id, 0, content='Observed output version 1', source='Owner observation')
    c = claim(service, id, 1, [e['evidence']['id']])
    service.review('alice', id, 2, c['claim']['id'], verdict='FACT', reason='Compared actual version output; local scope only')
    return c['claim']['id']


def complete_fixture(store, service, id):
    """Synthetic completed execution for acceptance gate tests only."""
    for step in store.get(id)['steps']:
        store.step_status(id, step['id'], 'RUNNING')
        store.step_status(id, step['id'], 'COMPLETED')
        event_id = store.append(id, 'OPENCODE_COMPLETED', {'outcome_state': 'COMPLETED'}, step_id=step['id'])
        revision = service.get('alice', id)['revision']
        service.add_evidence('alice', id, revision, event_id=event_id)


def test_specification_exact_intent_atomic_persistent_and_no_execution(tmp_path):
    store, service, id = mission(tmp_path)
    result = MissionSpecifications(Store(tmp_path)).get('alice', id)
    spec = result['specification']
    assert spec['raw_request'] == '  分析这个项目，为了评估状态\n'
    assert spec['objective']['target'] == '/owner/project'
    assert spec['objective']['constraints'] == ['只读，不修改文件']
    assert spec['intent_revision'] == 1 and spec['trace_id'] == id
    assert len(spec['execution_steps']) == len(store.get(id)['steps'])
    assert result['sha256'] == digest(spec)
    assert MissionSpecifications(store).get('bob', id) is None
    assert store.jobs(id) == []
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM mission_grants').fetchone()[0] == 0


@pytest.mark.parametrize('dimension,expected', [('time', 'TEMPORAL'), ('scope', 'SCOPE'),
                                              ('environment', 'SCOPE'), ('definition', 'SEMANTIC')])
def test_conflict_context_precedes_disagreement(dimension, expected):
    left = {'subject': 'service', 'predicate': 'version', 'value': '1',
            'context': dict.fromkeys(('time','scope','environment','definition'), 'same')}
    right = {**left, 'value': '2', 'context': {**left['context'], dimension: 'changed'}}
    assert classify_conflict(left, right)['classification'] == expected
    assert classify_conflict(left, {**right, 'context': left['context']})['classification'] == 'DIRECT_CONFLICT'
    assert classify_conflict(left, {**right, 'context': {}})['classification'] == 'REVIEW'
    assert classify_conflict(left, {**right, 'subject': 'another'}) is None
    assert classify_conflict(left, {**right, 'value': '1'}) is None


def test_unknown_review_needs_sources_and_disputed_cannot_be_overruled(tmp_path):
    store, service, id = mission(tmp_path)
    c = claim(service, id, 0)['claim']
    assert service.get('alice', id)['claims'][0]['status'] == 'UNKNOWN'
    with pytest.raises(TrustError, match='linked evidence'):
        service.review('alice', id, 1, c['id'], verdict='FACT', reason='no source')
    assert service.get('alice', id)['revision'] == 1
    evidence = service.add_evidence('alice', id, 1, content='Claim 2 observed', source='manual note')['evidence']
    second = claim(service, id, 2, [evidence['id']], value='2')['claim']
    service.review('alice', id, 3, second['id'], verdict='FACT', reason='Owner review')
    state = service.get('alice', id)
    assert state['conflicts'][0]['classification'] == 'DIRECT_CONFLICT'
    assert {c['status'] for c in state['claims']} == {'DISPUTED'}
    assert state['verification']['confidence'] is None


def test_owner_and_mission_isolation_no_url_fetch_or_server_file_reads(tmp_path):
    store, service, id = mission(tmp_path)
    other = store.create('Other', 'Other', ['Other'], owner_id='bob', issue_grant=False)['id']
    e = service.add_evidence('bob', other, 0, content='private', source='https://example.invalid')['evidence']
    with pytest.raises(TrustError) as error:
        service.get('bob', id)
    assert error.value.status == 404
    with pytest.raises(TrustError, match='does not belong'):
        claim(service, id, 0, [e['id']])
    event = store.append(other, 'OPENCODE_COMPLETED', {'secret': 'private'})
    with pytest.raises(TrustError):
        service.add_evidence('alice', id, 0, event_id=event)
    note = service.add_evidence('alice', id, 0, content='/etc/passwd', source='file:///etc/passwd')
    assert note['evidence']['content'] == '/etc/passwd'
    assert note['evidence']['source_verification'] == 'NOT_INDEPENDENTLY_VERIFIED'


def test_concurrent_revision_lock_and_transaction_rollback(tmp_path, monkeypatch):
    store, service, id = mission(tmp_path)
    def write(_):
        try:
            return service.add_evidence('alice', id, 0, content='test', source='note')
        except TrustError:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(write, range(4)))
    assert sum(x is not None for x in results) == 1
    original = store._event
    def fail(*args, **kwargs):
        raise RuntimeError('injected audit failure')
    monkeypatch.setattr(store, '_event', fail)
    with pytest.raises(RuntimeError):
        service.add_evidence('alice', id, 1, content='must rollback', source='note')
    monkeypatch.setattr(store, '_event', original)
    state = service.get('alice', id)
    assert state['revision'] == 1 and len(state['evidence']) == 1


def test_proof_not_acceptance_unknown_and_unexecuted_stay_blocked(tmp_path):
    store, service, id = mission(tmp_path)
    proof = service.snapshot('alice', id, 0)
    assert 'EXECUTION_NOT_COMPLETED' in proof['verification']['acceptance_blockers']
    with pytest.raises(TrustError, match='Acceptance blocked'):
        service.accept('alice', id, proof['id'], proof['sha256'], 'accept')
    assert store.jobs(id) == []


def test_acceptance_binds_current_snapshot_revision_execution_and_artifacts(tmp_path):
    store, service, id = mission(tmp_path)
    reviewed(service, id)
    complete_fixture(store, service, id)
    state = service.get('alice', id)
    assert state['verification']['acceptance_blockers'] == []
    proof = service.snapshot('alice', id, state['revision'])
    with pytest.raises(TrustError, match='hash mismatch'):
        service.accept('alice', id, proof['id'], '0'*64, 'checked')
    accepted = service.accept('alice', id, proof['id'], proof['sha256'], 'Compared objective, constraints and all step records')
    assert accepted['actor'] == 'alice'
    assert service.accept('alice', id, proof['id'], proof['sha256'], 'retry') == accepted
    assert TrustStore(Store(tmp_path)).get('alice', id)['proofs'][0]['acceptance_current']
    store.append(id, 'NEW_OUTPUT', {'message': 'actual new runtime data'})
    assert not service.get('alice', id)['proofs'][0]['acceptance_current']
    with pytest.raises(TrustError, match='stale'):
        service.accept('alice', id, proof['id'], proof['sha256'], 'retry')


def test_artifact_bytes_change_invalidates_proof_and_outside_paths_are_not_read(tmp_path):
    store, service, id = mission(tmp_path)
    reviewed(service, id)
    complete_fixture(store, service, id)
    file = tmp_path/'workspaces'/id/'result.txt'
    file.write_text('output 1')
    with store.connect() as db:
        db.execute('INSERT INTO artifacts VALUES (?,?,?,?,?)', ('output', id, 'result.txt', str(file), now()))
    state = service.get('alice', id)
    assert state['execution']['artifacts'][0]['sha256'] == hashlib.sha256(file.read_bytes()).hexdigest()
    proof = service.snapshot('alice', id, state['revision'])
    file.write_text('output 2')
    with pytest.raises(TrustError, match='stale'):
        service.accept('alice', id, proof['id'], proof['sha256'], 'checked')
    with store.connect() as db:
        db.execute('UPDATE artifacts SET path=? WHERE id=?', ('/etc/passwd','output'))
    state = service.get('alice', id)
    assert state['execution']['artifacts'][0]['sha256'] is None
    assert 'ARTIFACT_INTEGRITY_UNAVAILABLE' in state['verification']['acceptance_blockers']


def test_export_hashes_and_snapshot_integrity_no_paths_or_event_payloads(tmp_path):
    store, service, id = mission(tmp_path)
    store.append(id, 'OPENCODE_COMPLETED', {'path': '/private/server/path', 'api_key': 'synthetic-private-value'})
    proof = service.snapshot('alice', id, 0)
    raw = service.export('alice', id, proof['id'])
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert {'report.md','evidence.json','verification.json','trace.json','metadata.json','manifest.json'} <= set(archive.namelist())
        manifest = json.loads(archive.read('manifest.json'))
        for name, sha in manifest['files'].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == sha
        assert '/private/server/path' not in archive.read('proof.json').decode()
        assert 'synthetic-private-value' not in archive.read('proof.json').decode()
    with store.connect() as db:
        db.execute('UPDATE trust_proofs SET document=? WHERE id=?', ('{}', proof['id']))
    with pytest.raises(TrustError, match='integrity'):
        service.export('alice', id, proof['id'])


def test_spec_failure_rolls_back_intent_confirmation(tmp_path, monkeypatch):
    store = Store(tmp_path); intents = IntentWorkspace(store)
    intent = intents.compile('alice', '分析 project', {'target': '/repo'})
    def fail(*args):
        raise RuntimeError('spec failure')
    monkeypatch.setattr(intents.specifications, 'create', fail)
    with pytest.raises(RuntimeError):
        intents.confirm('alice', intent['id'], 1)
    assert store.list() == []
    assert not list((tmp_path/'workspaces').iterdir())
    assert intents.get('alice', intent['id'])['state'] == 'DRAFT'


def test_retract_conflicting_claim_preserves_both_sources_and_invalidates_snapshot(tmp_path):
    store, service, id = mission(tmp_path)
    reviewed(service, id)
    source = service.get('alice', id)['evidence'][0]['id']
    other = claim(service, id, 3, [source], value='2')['claim']
    state = service.get('alice', id)
    assert all(c['status']=='DISPUTED' for c in state['claims'])
    proof = service.snapshot('alice', id, state['revision'])
    service.retract('alice', id, state['revision'], other['id'], 'Wrong environment; withdraw applicability, keep original record')
    state = service.get('alice', id)
    assert len(state['claims']) == 2 and len(state['conflicts']) == 1
    assert state['conflicts'][0]['classification']=='DIRECT_CONFLICT' and not state['conflicts'][0]['active']
    assert state['claims'][0]['status']=='FACT'
    assert state['claims'][1]['lifecycle']=='RETRACTED'
    assert state['claims'][1]['retraction']['actor']=='alice'
    assert not state['proofs'][0]['current']
    assert 'CLAIMS_REQUIRE_REVIEW' not in state['verification']['acceptance_blockers']
    with pytest.raises(TrustError, match='Retracted'):
        service.review('alice',id,state['revision'],other['id'],verdict='FACT',reason='cannot resurrect silently')
    with pytest.raises(TrustError):
        service.retract('bob',id,state['revision'],other['id'],'cross owner')
    first=state['claims'][0]['id']
    service.retract('alice',id,state['revision'],first,'withdraw remaining claim')
    assert 'CLAIMS_REQUIRE_REVIEW' in service.get('alice',id)['verification']['acceptance_blockers']


def test_api_auth_csrf_cross_owner_and_machine_cannot_review(tmp_path, monkeypatch):
    monkeypatch.setenv('FACAI_USERS_JSON', '{"admin":"trust-admin-password","alice":"trust-alice-password"}')
    monkeypatch.setenv('FACAI_SCHEDULER_ENABLED','0')
    monkeypatch.setenv('FACAI_CHANNEL_NOTIFY_ENABLED','0')
    client = TestClient(create_app(tmp_path))
    session = client.post('/api/v1/auth/login',json={'username':'alice','password':'trust-alice-password'}).json()
    client.headers['X-CSRF-Token'] = session['csrf_token']
    intent = client.post('/api/v1/intents',json={'raw_request':'分析项目','interpretation':{'action':'analyze','target':'/repo'}}).json()
    id = client.post('/api/v1/intents/'+intent['id']+'/confirm',json={'expected_revision':1}).json()['mission_id']
    base = '/api/v1/missions/'+id+'/trust'
    assert client.get('/api/v1/missions/'+id+'/specification').status_code == 200
    assert client.get('/evidence-center').status_code == 200
    assert TestClient(client.app).get(base).status_code == 401
    no_csrf = TestClient(client.app); no_csrf.cookies.update(client.cookies)
    assert no_csrf.post(base+'/proofs',json={'expected_revision':0}).status_code == 403
    e = client.post(base+'/evidence',json={'expected_revision':0,'source':'Owner note','content':'observed'}).json()['evidence']
    c = client.post(base+'/claims',json={'expected_revision':1,'statement':'v1','subject':'project','predicate':'version','value':'1','evidence_ids':[e['id']]}).json()['claim']
    key_response = client.post('/api/v1/my/keys')
    assert key_response.status_code == 201, key_response.text
    key = key_response.json()
    machine = TestClient(client.app); machine.headers['Authorization']='Bearer '+key['api_key']
    assert machine.get(base).status_code == 200
    assert machine.post(base+'/claims/'+c['id']+'/reviews',json={'expected_revision':2,'verdict':'FACT','reason':'automated'}).status_code == 403
    assert client.post(base+'/claims/'+c['id']+'/reviews',json={'expected_revision':2,'verdict':'FACT','reason':'Owner compared source'}).status_code == 201
    other = TestClient(client.app)
    other.post('/api/v1/auth/login',json={'username':'admin','password':'trust-admin-password'})
    assert other.get(base).status_code == 404
    assert other.get('/api/v1/missions/'+id+'/specification').status_code == 404
