"""Database reservations survive racing schedulers and old-schema recovery."""
from concurrent.futures import ThreadPoolExecutor

from facai.store import Store


def test_shared_writer_blocks_new_peer_even_after_isolation_becomes_available(tmp_path):
    store=Store(tmp_path)
    mission=store.create('reservation','fixture',[{'id':'a','label':'A'},{'id':'b','label':'B'}],issue_grant=False)
    a=store.enqueue_job(mission['id'],'a',step_id='a')
    claimed=store.claim_job('first',600,parallel_mission_jobs=3,isolation_ready=lambda _:True)
    assert claimed['workspace_mode']=='SHARED'  # No queued peers at admission.
    b=store.enqueue_job(mission['id'],'b',step_id='b')
    assert store.claim_job('second',600,parallel_mission_jobs=3,isolation_ready=lambda _:True) is None
    assert not store.defer_isolation(a['id'],'wrong-worker')
    store.finish_job(a['id'],'COMPLETED')
    assert store.claim_job('second',600,parallel_mission_jobs=3,isolation_ready=lambda _:True)['id']==b['id']


def test_atomic_global_ceiling_under_twenty_racing_claims(tmp_path):
    store=Store(tmp_path)
    for i in range(20):
        mission=store.create(str(i),'fixture',['step'],issue_grant=False)
        store.enqueue_job(mission['id'],'fixture')
    with ThreadPoolExecutor(max_workers=20) as pool:
        claims=list(pool.map(lambda i:store.claim_job(str(i),600,max_running_jobs=4),range(20)))
    assert sum(claim is not None for claim in claims)==store.count_running_jobs()==4
    assert sum(job['status']=='QUEUED' for job in store.jobs())==16


def test_legacy_running_jobs_migrate_to_exclusive_shared_reservations(tmp_path):
    store=Store(tmp_path)
    mission=store.create('old worker','fixture',[{'id':'a','label':'A'},{'id':'b','label':'B'}],issue_grant=False)
    store.enqueue_job(mission['id'],'a',step_id='a')
    first=store.claim_job('old-worker',600)
    store.enqueue_job(mission['id'],'b',step_id='b')
    with store.connect() as db:db.execute('ALTER TABLE jobs DROP COLUMN workspace_mode')
    migrated=Store(tmp_path)
    assert migrated.get_job(first['id'])['workspace_mode']=='SHARED'
    assert migrated.get_job(first['id'])['attempts']==1
    assert migrated.claim_job('new-worker',600,parallel_mission_jobs=3,isolation_ready=lambda _:True) is None
