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
from facai.trust import TrustError, TrustStore, auto_collect_runtime_event, classify_conflict


def mission(tmp_path, owner='alice'):
    store = Store(tmp_path)
    intents = IntentWorkspace(store)
    intent = intents.compile(owner, '  分析这个项目，为了评估状态\n', {
        'action': 'analyze', 'target': '/owner/project', 'purpose': '评估状态',
        'scope': 'comprehensive', 'constraints': ['只读，不修改文件']})
    id = intents.confirm(owner, intent['id'], 1)['mission_id']
    return store, TrustStore(store), id


def claim(service, id, revision, evidence_ids=(), owner='alice', **changes):
    fields = {'statement': '版本为 1', 'subject': 'project', 'predicate': 'version', 'value': '1',
              'context': {'time': '2026-10-16', 'scope': 'core', 'environment': 'local', 'definition': 'release'},
              'evidence_ids': evidence_ids}
    fields.update(changes)
    return service.add_claim(owner, id, revision, **fields)


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
        try:
            service.add_evidence('alice', id, revision, event_id=event_id)
        except TrustError as error:
            # 事件侧自动采集已把该事件导入为 RUNTIME_EVENT evidence，幂等闸返回 409。
            assert error.status == 409 and 'already collected' in str(error)


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
    revision = service.get('alice', id)['revision']
    proof = service.snapshot('alice', id, revision)
    raw = service.export('alice', id, proof['id'])
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert {'report.md','evidence.json','verification.json','trace.json','metadata.json','manifest.json'} <= set(archive.namelist())
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['files']['proof.json'] == proof['sha256']
        for name, sha in manifest['files'].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == sha
        assert '/private/server/path' not in archive.read('proof.json').decode()
        assert 'synthetic-private-value' not in archive.read('proof.json').decode()
    with store.connect() as db:
        db.execute('UPDATE trust_proofs SET document=? WHERE id=?', ('{}', proof['id']))
    with pytest.raises(TrustError, match='integrity'):
        service.export('alice', id, proof['id'])


def test_export_is_byte_deterministic_across_time(tmp_path):
    """The immutable-proof contract: one proof, byte-identical export forever.

    zipfile pins entry mtimes to the wall clock by default, so two downloads
    straddling a second boundary used to produce different bytes for the same
    proof (observed as a rare closed-loop restart flake).
    """
    import time
    from datetime import datetime

    store, service, id = mission(tmp_path)
    revision = service.get('alice', id)['revision']
    proof = service.snapshot('alice', id, revision)
    first = service.export('alice', id, proof['id'])
    time.sleep(1.1)  # cross a second boundary deterministically
    second = service.export('alice', id, proof['id'])
    assert first == second
    with store.connect() as db:
        created_at = db.execute('SELECT created_at FROM trust_proofs WHERE id=?',
                                (proof['id'],)).fetchone()[0]
    stamped = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
    utc = stamped.utctimetuple()
    # DOS ZIP timestamps carry seconds in 2s steps: the odd bit is lost on
    # the round trip, so compare at the format's own granularity.
    expected = utc[:5] + (utc[5] // 2 * 2,)
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        for info in archive.infolist():
            assert info.date_time == expected


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


def _forbid_network(monkeypatch):
    """Any network access attempt during verification must fail loudly."""
    import socket
    import urllib.request
    real_connect = socket.socket.connect
    def deny(sock, address, *args, **kwargs):
        # Loopback stays open (asyncio/TestClient self-pipes on Windows);
        # any other destination is a fetch attempt and must fail loudly.
        if not (isinstance(address, tuple) and address and address[0] in ('127.0.0.1', '::1', 'localhost')):
            raise AssertionError('network access attempted to ' + repr(address))
        return real_connect(sock, address, *args, **kwargs)
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket, 'create_connection', deny)
    monkeypatch.setattr(urllib.request, 'urlopen', deny)


def test_source_verification_records_tier_and_never_fetches(tmp_path, monkeypatch):
    _forbid_network(monkeypatch)
    store, service, id = mission(tmp_path)
    e = service.add_evidence('alice', id, 0, content='Upstream changelog says 1.2',
                             source='https://example.invalid/changelog')['evidence']
    c = claim(service, id, 1, [e['id']])['claim']
    state = service.get('alice', id)
    assert state['claims'][0]['status'] == 'UNKNOWN'
    assert state['claims'][0]['source_trust']['distribution'] == {'EXTERNALLY_VERIFIED': 0, 'OWNER_ATTESTED': 0, 'UNVERIFIED': 1}
    v = service.verify_evidence('alice', id, state['revision'], e['id'], method='EXTERNAL_DOC_REFERENCE',
                                result='CORROBORATED', note='Owner manually compared the referenced changelog text')
    assert v['verification']['verified_by'] == 'alice'
    assert v['verification']['verified_at'] and v['verification']['method'] == 'EXTERNAL_DOC_REFERENCE'
    state = service.get('alice', id)
    assert state['evidence'][0]['source_trust_tier'] == 'EXTERNALLY_VERIFIED'
    assert state['evidence'][0]['verifications'][0]['id'] == v['verification']['id']
    assert state['source_trust']['tiers'] == {'EXTERNALLY_VERIFIED': 1, 'OWNER_ATTESTED': 0, 'UNVERIFIED': 0}
    assert state['source_trust']['registry'] == [
        {'source_key': 'https://example.invalid/changelog', 'tier': 'EXTERNALLY_VERIFIED',
         'verification_id': v['verification']['id'], 'mission_id': id,
         'updated_at': state['source_trust']['registry'][0]['updated_at']}]
    assert state['claims'][0]['source_trust']['distribution']['EXTERNALLY_VERIFIED'] == 1
    assert state['claims'][0]['status'] == 'UNKNOWN'  # annotation only, never a verdict
    with store.connect() as db:
        kinds = [row['type'] for row in db.execute('SELECT type FROM events WHERE mission_id=?', (id,))]
    assert 'TRUST_EVIDENCE_VERIFIED' in kinds
    # A later OWNER_RECHECK becomes the latest record and re-annotates the tier.
    service.verify_evidence('alice', id, state['revision'], e['id'], method='OWNER_RECHECK',
                            result='CORROBORATED', note='Owner re-read the same text')
    state = service.get('alice', id)
    assert state['evidence'][0]['source_trust_tier'] == 'OWNER_ATTESTED'
    assert state['source_trust']['registry'][0]['tier'] == 'OWNER_ATTESTED'
    assert state['claims'][0]['status'] == 'UNKNOWN'


def test_verification_conflict_409_and_result_never_changes_verdict_or_blockers(tmp_path):
    store, service, id = mission(tmp_path)
    reviewed(service, id)
    state = service.get('alice', id)
    evidence_id = state['evidence'][0]['id']
    assert state['claims'][0]['status'] == 'FACT'
    blockers = state['verification']['acceptance_blockers']
    with pytest.raises(TrustError, match='refresh before writing'):
        service.verify_evidence('alice', id, state['revision'] - 1, evidence_id,
                                method='OWNER_RECHECK', result='CORROBORATED', note='stale revision')
    service.verify_evidence('alice', id, state['revision'], evidence_id,
                            method='OPERATOR_REPLY', result='CONTRADICTED', note='operator reports otherwise')
    state = service.get('alice', id)
    assert state['claims'][0]['status'] == 'FACT'  # verdict still only from human review
    assert state['verification']['confidence'] is None
    assert state['verification']['acceptance_blockers'] == blockers
    assert state['evidence'][0]['source_trust_tier'] == 'UNVERIFIED'
    with pytest.raises(TrustError, match='method'):
        service.verify_evidence('alice', id, state['revision'], evidence_id,
                                method='AUTO_FETCH', result='CORROBORATED', note='no such method')
    with pytest.raises(TrustError, match='result'):
        service.verify_evidence('alice', id, state['revision'], evidence_id,
                                method='OWNER_RECHECK', result='TRUE', note='no such result')
    with pytest.raises(TrustError, match='Evidence not found'):
        service.verify_evidence('alice', id, state['revision'], 'missing', method='OWNER_RECHECK',
                                result='CORROBORATED', note='x')


def test_api_machine_session_cannot_write_source_verification(tmp_path, monkeypatch):
    _forbid_network(monkeypatch)
    monkeypatch.setenv('FACAI_USERS_JSON', '{"alice":"trust-verify-password"}')
    monkeypatch.setenv('FACAI_SCHEDULER_ENABLED','0')
    monkeypatch.setenv('FACAI_CHANNEL_NOTIFY_ENABLED','0')
    client = TestClient(create_app(tmp_path))
    session = client.post('/api/v1/auth/login',json={'username':'alice','password':'trust-verify-password'}).json()
    client.headers['X-CSRF-Token'] = session['csrf_token']
    intent = client.post('/api/v1/intents',json={'raw_request':'分析项目','interpretation':{'action':'analyze','target':'/repo'}}).json()
    id = client.post('/api/v1/intents/'+intent['id']+'/confirm',json={'expected_revision':1}).json()['mission_id']
    base = '/api/v1/missions/'+id+'/trust'
    e = client.post(base+'/evidence',json={'expected_revision':0,'source':'https://example.invalid/doc','content':'note'}).json()['evidence']
    key = client.post('/api/v1/my/keys').json()
    machine = TestClient(client.app); machine.headers['Authorization']='Bearer '+key['api_key']
    verify_url = base+'/evidence/'+e['id']+'/verification'
    assert machine.post(verify_url,json={'expected_revision':1,'method':'OWNER_RECHECK','result':'CORROBORATED'}).status_code == 403
    machine.headers['X-CSRF-Token'] = session['csrf_token']
    assert machine.post(verify_url,json={'expected_revision':1,'method':'OWNER_RECHECK','result':'CORROBORATED'}).status_code == 403
    assert client.post(verify_url,json={'expected_revision':1,'method':'OPERATOR_REPLY','result':'CORROBORATED',
                                        'note':'Operator confirmed by reply'}).status_code == 201
    state = client.get(base).json()
    assert state['evidence'][0]['source_trust_tier'] == 'EXTERNALLY_VERIFIED'
    assert state['source_trust']['tiers']['EXTERNALLY_VERIFIED'] == 1
    assert state['claims'] == []  # tier annotation never created or judged a claim


def _auto_evidence(state):
    return [e for e in state['evidence'] if e['collected_by'] == 'system:auto-collect']


def test_runtime_events_auto_collected_idempotent_and_honest(tmp_path):
    store, service, id = mission(tmp_path)
    step = store.get(id)['steps'][0]
    store.step_status(id, step['id'], 'RUNNING')
    event_id = store.append(id, 'STEP_COMPLETED', {'status': 'COMPLETED'}, step_id=step['id'])
    state = service.get('alice', id)
    auto = _auto_evidence(state)
    assert len(auto) == 1
    item = auto[0]
    assert item['kind'] == 'RUNTIME_EVENT' and item['event_id'] == event_id
    assert item['collected_by'] == 'system:auto-collect'
    assert item['source_verification'] == 'PERSISTED_EVENT_REFERENCE'
    assert item['source'] == 'mission_event:' + event_id
    # 幂等：同一事件重复采集不重复入库（静默跳过，而非 409 中断派发）。
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        assert auto_collect_runtime_event(store, db, id, event_id) is None
        db.execute('ROLLBACK')
    # Owner 手动导入同一事件撞上幂等闸，仍是显式 409。
    with pytest.raises(TrustError, match='already collected'):
        service.add_evidence('alice', id, service.get('alice', id)['revision'], event_id=event_id)
    assert len(_auto_evidence(service.get('alice', id))) == 1
    with store.connect() as db:
        kinds = [row['type'] for row in db.execute('SELECT type FROM events WHERE mission_id=?', (id,))]
    assert kinds.count('TRUST_EVIDENCE_COLLECTED') == 1  # 自动采集自身也留痕一次


def test_auto_collect_failure_never_blocks_dispatch(tmp_path, monkeypatch):
    store, service, id = mission(tmp_path)
    import facai.trust as trust_module
    def boom(*args, **kwargs):
        raise RuntimeError('injected auto-collect failure')
    monkeypatch.setattr(trust_module, 'auto_collect_runtime_event', boom)
    event_id = store.append(id, 'OPENCODE_COMPLETED', {'outcome_state': 'COMPLETED'})
    assert event_id  # 采集失败未阻断事件发布
    state = service.get('alice', id)
    assert _auto_evidence(state) == []  # 失败的采集没有留下半条记录
    monkeypatch.undo()
    revision = state['revision']
    service.add_evidence('alice', id, revision, event_id=event_id)  # Owner 仍可手动补采


def test_auto_collect_switch_off_skips_collection(tmp_path, monkeypatch):
    store, service, id = mission(tmp_path)
    monkeypatch.setenv('FACAI_TRUST_AUTO_COLLECT', '0')
    store.append(id, 'OPENCODE_COMPLETED', {'outcome_state': 'COMPLETED'})
    assert _auto_evidence(service.get('alice', id)) == []
    monkeypatch.setenv('FACAI_TRUST_AUTO_COLLECT', '1')
    second = store.append(id, 'OPENCODE_FAILED', {'outcome_state': 'FAILED'})
    state = service.get('alice', id)
    auto = _auto_evidence(state)
    assert len(auto) == 1 and auto[0]['event_id'] == second


def test_artifact_drift_annotated_recorded_and_deduplicated(tmp_path):
    store, service, id = mission(tmp_path)
    reviewed(service, id)
    complete_fixture(store, service, id)
    file = tmp_path/'workspaces'/id/'result.txt'
    file.write_text('output 1')
    with store.connect() as db:
        db.execute('INSERT INTO artifacts VALUES (?,?,?,?,?)', ('output', id, 'result.txt', str(file), now()))
    state = service.get('alice', id)
    assert state['proofs'] == []  # 尚无快照：不标注、不留痕
    revision = state['revision']
    proof = service.snapshot('alice', id, revision)
    file.write_text('output 2')  # 验收之外的读路径上发现字节漂移
    state = service.get('alice', id)
    assert state['proofs'][0]['artifact_drift_detected'] is True
    drift = state['proofs'][0]['artifact_drift'][0]
    assert drift['artifact_id'] == 'output' and drift['name'] == 'result.txt'
    assert drift['snapshot_sha256'] == hashlib.sha256(b'output 1').hexdigest()
    assert drift['observed_sha256'] == hashlib.sha256(b'output 2').hexdigest()
    # 快照状态只标注：current/acceptance_current 语义不变，blockers 不新增。
    assert state['proofs'][0]['current'] is False  # 既有语义：快照已过期
    assert 'ARTIFACT_INTEGRITY_UNAVAILABLE' not in state['verification']['acceptance_blockers']
    with store.connect() as db:
        rows = db.execute("SELECT payload FROM events WHERE mission_id=? AND type='TRUST_ARTIFACT_DRIFT'",
                          (id,)).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]['payload'])
    assert payload['marker'] == digest(drift)
    # 漂移未消除时重复读路径不重复留痕（幂等留痕）。
    service.get('alice', id)
    with store.connect() as db:
        count = db.execute("SELECT count(*) FROM events WHERE mission_id=? AND type='TRUST_ARTIFACT_DRIFT'",
                           (id,)).fetchone()[0]
    assert count == 1


def test_auto_and_manual_collection_coexist(tmp_path):
    store, service, id = mission(tmp_path)
    auto_id = store.append(id, 'EXECUTION_STARTED', {'outcome_state': 'STARTED'})
    manual = service.add_evidence('alice', id, service.get('alice', id)['revision'],
                                  content='Owner note about the run', source='Owner observation')['evidence']
    state = service.get('alice', id)
    assert [e['kind'] for e in state['evidence']] == ['RUNTIME_EVENT', 'OWNER_NOTE']
    assert state['evidence'][0]['collected_by'] == 'system:auto-collect'
    assert state['evidence'][1]['collected_by'] == 'alice'
    assert state['evidence'][0]['id'] != manual['id']
    # 自动采集不会吃掉 Owner 手记，也不会把自动记录算成人工来源。
    assert state['evidence'][0]['source_trust_tier'] == 'UNVERIFIED'
    assert state['evidence'][1]['source_trust_tier'] == 'UNVERIFIED'
    # Owner 手动导入已被自动采集的事件：显式 409，互不覆盖。
    with pytest.raises(TrustError, match='already collected'):
        service.add_evidence('alice', id, state['revision'], event_id=auto_id)
    final = service.get('alice', id)
    assert len(final['evidence']) == 2 and final['revision'] == state['revision']


# ---------------------------------------------------------------------------
# TRUST-001：DISPUTED 人工裁决出口 + 跨 Mission 冲突检测
# ---------------------------------------------------------------------------

class TestAdjudication:
    def _disputed_mission(self, tmp_path, owner='alice'):
        store = Store(tmp_path)
        intents = IntentWorkspace(store)
        intent = intents.compile(owner, '分析项目\n', {
            'action': 'analyze', 'target': '/p', 'purpose': '评估',
            'scope': 'comprehensive', 'constraints': []})
        mission_id = intents.confirm(owner, intent['id'], 1)['mission_id']
        service = TrustStore(store)
        revision = service.get(owner, mission_id)['revision']
        e1 = service.add_evidence(owner, mission_id, revision,
                                  content='观测一：version=1', source='Owner')
        e2 = service.add_evidence(owner, mission_id, revision + 1,
                                  content='观测二：version=2', source='Owner')
        left = claim(service, mission_id, service.get(owner, mission_id)['revision'],
                     value='1', evidence_ids=[e1['evidence']['id']])
        right = claim(service, mission_id, service.get(owner, mission_id)['revision'],
                      value='2', statement='版本为 2', evidence_ids=[e2['evidence']['id']])
        return store, service, mission_id, left['claim']['id'], right['claim']['id']

    def test_adjudication_frees_disputed_pair_with_record(self, tmp_path):
        store, service, mission_id, left_id, right_id = self._disputed_mission(tmp_path)
        state = service.get('alice', mission_id)
        both = {c['id']: c for c in state['claims']}
        assert both[left_id]['status'] == 'DISPUTED' == both[right_id]['status']
        assert state['conflicts'] and state['conflicts'][0]['adjudicated'] is False

        revision = state['revision']
        service.review('alice', mission_id, revision, left_id,
                       verdict='INFERENCE', reason='本地观测，无外部来源')
        service.review('alice', mission_id, revision + 1, right_id,
                       verdict='INFERENCE', reason='另一时点观测，无外部来源')
        still = service.get('alice', mission_id)
        both = {c['id']: c for c in still['claims']}
        assert both[left_id]['status'] == 'DISPUTED'  # 冲突未裁决，审查也解不开

        done = service.adjudicate('alice', mission_id, still['revision'], left_id,
                                  right_id, 'CONTEXT_DIFFERENCE',
                                  '两次观测的 time/scope 语境不同，不是同一命题')
        assert done['adjudication']['outcome'] == 'CONTEXT_DIFFERENCE'
        freed = service.get('alice', mission_id)
        both = {c['id']: c for c in freed['claims']}
        assert both[left_id]['status'] == 'INFERENCE'  # 回落到各自审查结论
        assert both[right_id]['status'] == 'INFERENCE'
        conflict = next(c for c in freed['conflicts']
                        if {c['left'], c['right']} == {left_id, right_id})
        assert conflict['adjudicated'] is True
        assert conflict['adjudication']['outcome'] == 'CONTEXT_DIFFERENCE'
        assert freed['adjudications'] and freed['adjudications'][0]['note']
        types = [e['type'] for e in store.events(mission_id)]
        assert 'TRUST_ADJUDICATION_RECORDED' in types

    def test_adjudication_requires_a_real_conflict_pair(self, tmp_path):
        store, service, mission_id, left_id, right_id = self._disputed_mission(tmp_path)
        evidence = service.add_evidence('alice', mission_id, service.get('alice', mission_id)['revision'],
                                        content='无冲突证据', source='Owner')
        third = claim(service, mission_id,
                      service.get('alice', mission_id)['revision'], value='9',
                      statement='无关断言', subject='other', predicate='kind')
        with pytest.raises(TrustError) as error:
            service.adjudicate('alice', mission_id, service.get('alice', mission_id)['revision'],
                               left_id, third['claim']['id'], 'OWNER_RESOLVED', '并非冲突对')
        assert error.value.status == 422


class TestCrossMissionConflicts:
    def _two_missions(self, tmp_path):
        store = Store(tmp_path)
        intents = IntentWorkspace(store)
        service = TrustStore(store)
        ids = []
        for title in ('一号', '二号'):
            intent = intents.compile('alice', f'{title}\n', {
                'action': 'analyze', 'target': '/p', 'purpose': '评估',
                'scope': 'comprehensive', 'constraints': []})
            ids.append(intents.confirm('alice', intent['id'], 1)['mission_id'])
        claim(service, ids[0], service.get('alice', ids[0])['revision'],
              value='1', statement='版本 1')
        claim(service, ids[1], service.get('alice', ids[1])['revision'],
              value='2', statement='版本 2')
        return store, service, ids

    def test_same_owner_cross_mission_pair_is_detected(self, tmp_path):
        store, service, ids = self._two_missions(tmp_path)
        conflicts = service.cross_mission_conflicts('alice')
        assert len(conflicts) == 1
        pair = {conflicts[0]['left']['mission_id'], conflicts[0]['right']['mission_id']}
        assert pair == set(ids)
        assert {conflicts[0]['left']['value'], conflicts[0]['right']['value']} == {'1', '2'}

    def test_other_owner_sees_nothing_and_retracted_leaves(self, tmp_path):
        store, service, ids = self._two_missions(tmp_path)
        assert service.cross_mission_conflicts('bob') == []
        state = service.get('alice', ids[1])
        losing = next(c for c in state['claims'] if c['value'] == '2')
        service.retract('alice', ids[1], state['revision'], losing['id'],
                        '以一号任务为准，二号观测作废')
        assert service.cross_mission_conflicts('alice') == []


class TestAdjudicationRoutes:
    def test_endpoints_gates_and_shapes(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from facai.api import create_app
        monkeypatch.setenv('FACAI_ADMIN_PASSWORD', 'adj-admin-pass')
        monkeypatch.delenv('FACAI_COOKIE_SECURE', raising=False)
        client = TestClient(create_app(tmp_path))
        session = client.post('/api/v1/auth/login',
                              json={'username': 'admin', 'password': 'adj-admin-pass'}).json()
        client.headers.update({'X-CSRF-Token': session['csrf_token']})

        created = client.post('/api/v1/missions', json={
            'title': '裁决', 'original_input': '裁决', 'steps': ['执行']})
        mission_id = created.json()['id']
        store = client.app.state.store
        service = TrustStore(store)
        revision = service.get('admin', mission_id)['revision']
        ev = service.add_evidence('admin', mission_id, revision,
                                  content='观测：version=1', source='Owner')
        left = claim(service, mission_id, service.get('admin', mission_id)['revision'],
                     value='1', owner='admin', evidence_ids=[ev['evidence']['id']])
        right = claim(service, mission_id, service.get('admin', mission_id)['revision'],
                      value='2', statement='版本 2', owner='admin',
                      evidence_ids=[ev['evidence']['id']])

        listing = client.get('/api/v1/trust/conflicts')
        assert listing.status_code == 200 and listing.json()['conflicts'] == []

        revision = service.get('admin', mission_id)['revision']
        posted = client.post(f'/api/v1/missions/{mission_id}/trust/adjudications', json={
            'expected_revision': revision,
            'left_claim_id': left['claim']['id'], 'right_claim_id': right['claim']['id'],
            'outcome': 'OWNER_RESOLVED', 'note': '以 value=1 为准，2 为误读'})
        assert posted.status_code == 201, posted.text
        state = service.get('admin', mission_id)
        assert state['conflicts'][0]['adjudicated'] is True

        tenant = client.post('/api/v1/auth/register', json={
            'username': 'adj-tenant', 'password': 'adj-pass-1234'})
        assert tenant.status_code == 201
        client.headers.update({'X-CSRF-Token': tenant.json()['csrf_token']})
        foreign = client.post(f'/api/v1/missions/{mission_id}/trust/adjudications', json={
            'expected_revision': state['revision'],
            'left_claim_id': left['claim']['id'], 'right_claim_id': right['claim']['id'],
            'outcome': 'OWNER_RESOLVED', 'note': 'x'})
        assert foreign.status_code in (403, 404)
