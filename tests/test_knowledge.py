"""Accepted snapshot curation does not upgrade claims or erase historical provenance."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from facai.store import Store
from facai.trust import TrustError, TrustStore
from test_trust_workflow import mission, reviewed, complete_fixture


def accepted(tmp_path):
    store, service, id = mission(tmp_path)
    claim_id = reviewed(service, id)
    revision = service.get('alice', id)['revision']
    service.review('alice', id, revision, claim_id, verdict='INFERENCE', reason='Manual inference, not a certified fact')
    complete_fixture(store, service, id)
    revision = service.get('alice', id)['revision']
    proof = service.snapshot('alice', id, revision)
    service.accept('alice', id, proof['id'], proof['sha256'], 'Owner accepts fixture result')
    return store, service, id, proof


def test_only_current_accepted_snapshots_can_be_curated(tmp_path):
    store, service, id = mission(tmp_path)
    proof = service.snapshot('alice', id, 0)
    with pytest.raises(TrustError, match='currently accepted'):
        service.save_knowledge('alice', id, proof['id'], proof['sha256'], 'unexecuted')
    assert service.knowledge('alice')['items'] == []


def test_knowledge_restart_search_classification_and_historical_source(tmp_path):
    store, service, id, proof = accepted(tmp_path)
    result = service.save_knowledge('alice', id, proof['id'], proof['sha256'], 'Project 100%_report', ['analysis', 'inference'])
    assert not result['duplicate']
    item = TrustStore(Store(tmp_path)).get_knowledge('alice', result['id'])
    assert item['claims'][0]['status'] == 'INFERENCE'
    assert item['confidence'] is None and item['source_current']
    assert item['source']['proof_sha256'] == proof['sha256']
    assert service.knowledge('alice', '100%_')['items'][0]['id'] == result['id']
    assert service.knowledge('alice', 'absent')['items'] == []
    assert service.knowledge('bob')['items'] == []
    with pytest.raises(TrustError) as error:
        service.get_knowledge('bob', result['id'])
    assert error.value.status == 404
    store.append(id, 'RUNTIME_CHANGED', {'test_fixture': True})
    assert not service.get_knowledge('alice', result['id'])['source_current']
    assert service.get_knowledge('alice', result['id'])['claims'][0]['status'] == 'INFERENCE'
    with pytest.raises(TrustError, match='currently accepted'):
        service.save_knowledge('alice', id, proof['id'], proof['sha256'], 'stale')


def test_concurrent_curation_idempotency_and_integrity(tmp_path):
    store, service, id, proof = accepted(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(lambda _: service.save_knowledge('alice', id, proof['id'], proof['sha256'], 'Accepted', ['tag']), range(4)))
    assert len({value['id'] for value in values}) == 1
    assert sum(not value['duplicate'] for value in values) == 1
    item_id = values[0]['id']
    with store.connect() as db:
        db.execute('UPDATE knowledge_assets SET document=? WHERE id=?', (json.dumps({'altered': True}), item_id))
    with pytest.raises(TrustError, match='integrity'):
        service.get_knowledge('alice', item_id)
    with pytest.raises(TrustError, match='integrity'):
        service.knowledge('alice')


def test_invalid_hash_and_tags_are_rejected_without_creating_assets(tmp_path):
    store, service, id, proof = accepted(tmp_path)
    with pytest.raises(TrustError, match='hash mismatch'):
        service.save_knowledge('alice', id, proof['id'], '0'*64, 'Title')
    with pytest.raises(TrustError):
        service.save_knowledge('alice', id, proof['id'], proof['sha256'], 'Title', ['x'*51])
    assert service.knowledge('alice')['items'] == []
