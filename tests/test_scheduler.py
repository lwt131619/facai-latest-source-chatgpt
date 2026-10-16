"""Durable scheduler: queue, priority, leases, recovery, retry and isolation."""

import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from facai.api import create_app
from facai.opencode import OpenCodeAdapter
from facai.quota import QuotaPools
from facai.registries import Registries
from facai.scheduler import Scheduler
from facai.store import Store


class SequencedRunner:
    """Returns prepared outcomes in order; never calls a model."""

    def __init__(self, results):
        self.results = results
        self.index = 0
        self.cwds = []

    def __call__(self, command, cwd, timeout):
        self.cwds.append(str(cwd))
        outcome = self.results[min(self.index, len(self.results) - 1)]
        self.index += 1
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, timeout)
        returncode, stdout, stderr = outcome
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def build(tmp_path, runner, concurrency=1, lease_seconds=600):
    store = Store(tmp_path)
    registries = Registries(store)
    adapter = OpenCodeAdapter(binary='opencode', runner=runner)
    scheduler = Scheduler(store, adapter, registries, tmp_path, concurrency=concurrency,
                          lease_seconds=lease_seconds, worker_id='test-worker')
    return store, scheduler


def make_mission(store, title='M', steps=('开发',)):
    mission = store.create(title, f'input {title}', list(steps))
    store.bind(mission['id'], 'opencode', 'deepseek-flash')
    return store.get(mission['id'])


def test_run_pending_executes_jobs_and_isolates_missions(tmp_path):
    runner = SequencedRunner([(0, '{"type":"text","text":"ok"}\n', '')] * 4)
    store, scheduler = build(tmp_path, runner)
    a, b = make_mission(store, 'A'), make_mission(store, 'B')
    store.enqueue_job(a['id'], 'task A', step_id=a['steps'][0]['id'])
    store.enqueue_job(b['id'], 'task B', step_id=b['steps'][0]['id'])

    assert scheduler.run_pending() == 2
    assert all(job['status'] == 'COMPLETED' for job in store.jobs())
    # Each job ran inside its own mission workspace.
    assert a['id'] in runner.cwds[0] and b['id'] in runner.cwds[1]
    for mission in (a, b):
        events = store.events(mission['id'])
        assert any(event['type'] == 'JOB_COMPLETED' for event in events)
        assert all(event['mission_id'] == mission['id'] for event in events)


def test_priority_orders_claim(tmp_path):
    runner = SequencedRunner([(0, '', '')] * 2)
    store, scheduler = build(tmp_path, runner, concurrency=1)
    mission = make_mission(store)
    low = store.enqueue_job(mission['id'], 'low', priority=0)
    high = store.enqueue_job(mission['id'], 'high', priority=5)

    first_batch = scheduler.run_once()
    assert first_batch == [high['id']]
    assert store.get_job(high['id'])['status'] == 'COMPLETED'
    assert store.get_job(low['id'])['status'] == 'QUEUED'
    scheduler.run_pending()
    assert store.get_job(low['id'])['status'] == 'COMPLETED'


def test_concurrency_limit_bounds_batch(tmp_path):
    runner = SequencedRunner([(0, '', '')] * 3)
    store, scheduler = build(tmp_path, runner, concurrency=2)
    mission_a = make_mission(store, 'A')
    mission_b = make_mission(store, 'B')
    mission_c = make_mission(store, 'C')
    store.enqueue_job(mission_a['id'], 'task A')
    store.enqueue_job(mission_b['id'], 'task B')
    store.enqueue_job(mission_c['id'], 'task C')
    assert len(scheduler.run_once()) == 2
    assert store.count_running_jobs() == 0
    assert sum(job['status'] == 'COMPLETED' for job in store.jobs()) == 2


def test_same_mission_never_claims_two_jobs_concurrently(tmp_path):
    runner = SequencedRunner([(0, '', '')] * 3)
    store, scheduler = build(tmp_path, runner, concurrency=3)
    mission = make_mission(store)
    for _ in range(3):
        store.enqueue_job(mission['id'], 'task')
    claimed = store.claim_job('external-worker', lease_seconds=600)
    assert claimed is not None
    # A separate scheduler worker must leave this mission's other jobs queued.
    assert scheduler.run_once() == []
    assert sum(job['status'] == 'QUEUED' for job in store.jobs(mission['id'])) == 2


def test_failure_can_be_retried(tmp_path):
    runner = SequencedRunner([(1, '', 'boom'), (0, 'ok', '')])
    store, scheduler = build(tmp_path, runner)
    mission = make_mission(store)
    job = store.enqueue_job(mission['id'], 'task', max_attempts=2)
    scheduler.run_pending()
    assert store.get_job(job['id'])['status'] == 'FAILED'

    retried = store.retry_job(job['id'])
    assert retried['status'] == 'QUEUED' and retried['attempts'] == 0
    scheduler.run_pending()
    assert store.get_job(job['id'])['status'] == 'COMPLETED'


def test_lease_covers_the_job_timeout(tmp_path):
    """A live job's lease outlives its own execution window.

    ``timeout`` is what the worker waits for the agent, so a lease shorter than
    the job's own timeout would let ``requeue_expired`` return a still-running
    job to QUEUED and a second worker would re-run the same step concurrently in
    the shared mission workspace.
    """
    store, scheduler = build(tmp_path, SequencedRunner([(0, '', '')]))
    mission = make_mission(store)
    long_job = store.enqueue_job(mission['id'], 'long task', timeout=3600)

    claimed = store.claim_job('worker-a', lease_seconds=600)
    assert claimed['id'] == long_job['id']
    remaining = (datetime.fromisoformat(claimed['lease_expires_at'])
                 - datetime.now(timezone.utc)).total_seconds()
    # The lease covers serial-lock queueing (up to one more timeout) on top of
    # the run itself: 2*3600 + 300.
    assert remaining > 7200
    # Recovery leaves the live lease alone, so no second worker can claim and
    # re-run the step while its first worker is still executing it.
    assert store.requeue_expired() == []
    assert store.get_job(long_job['id'])['status'] == 'RUNNING'

    # A job whose doubled budget is inside the caller's floor keeps that floor.
    other = make_mission(store, 'Other')
    short_job = store.enqueue_job(other['id'], 'short task', timeout=60)
    claimed_short = store.claim_job('worker-a', lease_seconds=600)
    assert claimed_short['id'] == short_job['id']
    floor_remaining = (datetime.fromisoformat(claimed_short['lease_expires_at'])
                       - datetime.now(timezone.utc)).total_seconds()
    assert 550 < floor_remaining <= 600


def test_expired_lease_is_recovered(tmp_path):
    runner = SequencedRunner([(0, '', '')])
    store, scheduler = build(tmp_path, runner)
    mission = make_mission(store)

    requeueable = store.enqueue_job(mission['id'], 'task', max_attempts=2)
    claimed = store.claim_job('dead-worker', lease_seconds=-1)
    assert claimed['id'] == requeueable['id'] and claimed['status'] == 'RUNNING'
    assert store.requeue_expired() == [requeueable['id']]
    assert store.get_job(requeueable['id'])['status'] == 'QUEUED'

    exhausted = store.enqueue_job(mission['id'], 'task', max_attempts=1, priority=5)
    claimed = store.claim_job('dead-worker', lease_seconds=-1)
    assert claimed['id'] == exhausted['id']
    assert store.requeue_expired() == [exhausted['id']]
    assert store.get_job(exhausted['id'])['status'] == 'FAILED'


def test_claim_lease_covers_serial_lock_queueing(tmp_path):
    """A parallel branch's wall time approaches two timeouts.

    Branches take turns on the OpenCode CLI serial lock, so a branch can spend
    its whole timeout waiting behind a sibling before running for its own
    timeout. The claim lease must cover both plus margin, or a peer scheduler
    requeues a live job mid-queue and re-executes the step.
    """
    store, _ = build(tmp_path, SequencedRunner([(0, '', '')]))
    expected = {60: 600, 300: 900, 400: 1100, 3600: 7500}
    for timeout, lease in expected.items():
        mission = make_mission(store, f'T{timeout}')
        job = store.enqueue_job(mission['id'], 'task', timeout=timeout)
        claimed = store.claim_job('worker-a', lease_seconds=1)
        assert claimed['id'] == job['id'], timeout
        remaining = (datetime.fromisoformat(claimed['lease_expires_at'])
                     - datetime.now(timezone.utc)).total_seconds()
        assert lease - 5 < remaining <= lease, timeout
        store.finish_job(job['id'], 'COMPLETED')


def test_touch_job_lease_extends_and_never_shortens(tmp_path):
    store, _ = build(tmp_path, SequencedRunner([(0, '', '')]))
    mission = make_mission(store)
    job = store.enqueue_job(mission['id'], 'task', timeout=300)

    claimed = store.claim_job('worker-a', lease_seconds=-1)  # simulated dead worker
    assert claimed['status'] == 'RUNNING'
    assert store.touch_job_lease(job['id'], 300) is True
    remaining = (datetime.fromisoformat(store.get_job(job['id'])['lease_expires_at'])
                 - datetime.now(timezone.utc)).total_seconds()
    # now + timeout + margin.
    assert 550 < remaining <= 600

    # A claim lease that already reaches further is never shortened: a long
    # timeout's doubled lease covers serial queueing that a short touch must
    # not cancel.
    long_mission = make_mission(store, 'Long')
    long_job = store.enqueue_job(long_mission['id'], 'task', timeout=3600)
    store.claim_job('worker-a', lease_seconds=7200)
    assert store.touch_job_lease(long_job['id'], 1) is True
    long_remaining = (datetime.fromisoformat(store.get_job(long_job['id'])['lease_expires_at'])
                      - datetime.now(timezone.utc)).total_seconds()
    assert long_remaining > 7000

    # Only a RUNNING job holds a lease to touch.
    store.finish_job(job['id'], 'COMPLETED')
    assert store.touch_job_lease(job['id'], 300) is False


def test_requeued_job_gets_claim_buffer_before_retry(tmp_path):
    """A requeued job waits out a buffer before any worker may re-claim it.

    The lease expiring is not proof the worker died; the buffer keeps a second
    worker from immediately re-running a step whose first worker may still be
    finalizing, without ever losing the job.
    """
    store, _ = build(tmp_path, SequencedRunner([(0, '', '')]))
    mission = make_mission(store)
    job = store.enqueue_job(mission['id'], 'task', max_attempts=2)
    store.claim_job('dead-worker', lease_seconds=-1)

    assert store.requeue_expired() == [job['id']]
    requeued = store.get_job(job['id'])
    assert requeued['status'] == 'QUEUED'
    buffer = (datetime.fromisoformat(requeued['not_before'])
              - datetime.now(timezone.utc)).total_seconds()
    assert 30 < buffer <= 60
    # The buffer is real: an immediate claim finds nothing to run.
    assert store.claim_job('worker-b', lease_seconds=600) is None
    # Once it passes, the job is claimable again -- buffered, not lost.
    with sqlite3.connect(tmp_path / 'facai.sqlite3') as db:
        db.execute("UPDATE jobs SET not_before=? WHERE id=?",
                   ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                    job['id']))
    assert store.claim_job('worker-b', lease_seconds=600)['id'] == job['id']


class GatedRunner:
    """Holds the first call until released, mirroring a slow agent run."""

    def __init__(self):
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, command, cwd, timeout):
        self.calls += 1
        self.started.set()
        if not self.release.wait(timeout=20):
            raise AssertionError('gated runner was never released')
        return subprocess.CompletedProcess(command, 0, '', '')


def test_second_scheduler_cannot_rerun_live_step_after_lease_expiry(tmp_path):
    """Repro for the lease x serial-lock hole: a lease that expires mid-run
    must not hand a live step to a peer scheduler.

    Two schedulers share one store and one serial adapter. Worker A claims and
    runs slowly; its lease is rolled back underneath it (exactly what the old
    claim arithmetic achieved by ignoring serial-lock queueing). Worker B's
    recovery must requeue the job with a not-before buffer and claim nothing,
    so A finishes the step exactly once.
    """
    store = Store(tmp_path)
    runner = GatedRunner()
    adapter = OpenCodeAdapter(binary='opencode', runner=runner)
    registries = Registries(store)
    worker_a = Scheduler(store, adapter, registries, tmp_path, concurrency=1,
                         worker_id='worker-a')
    worker_b = Scheduler(store, adapter, registries, tmp_path, concurrency=1,
                         worker_id='worker-b')
    mission = make_mission(store)
    step_id = mission['steps'][0]['id']
    job = store.enqueue_job(mission['id'], 'task', step_id=step_id, max_attempts=2,
                            timeout=300)

    thread = threading.Thread(target=worker_a.run_once)
    thread.start()
    deadline = datetime.now(timezone.utc) + timedelta(seconds=10)
    while store.get_job(job['id'])['status'] != 'RUNNING':
        assert datetime.now(timezone.utc) < deadline, 'worker A never claimed the job'
        time.sleep(0.01)
    assert runner.started.wait(timeout=10)

    # Roll the lease back under the live worker.
    with sqlite3.connect(tmp_path / 'facai.sqlite3') as db:
        db.execute("UPDATE jobs SET lease_expires_at=? WHERE id=?",
                   ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
                    job['id']))

    # Worker B requeues the job but cannot claim it: the not-before buffer
    # keeps the live step away from the second worker.
    assert worker_b.run_once() == []
    requeued = store.get_job(job['id'])
    assert requeued['status'] == 'QUEUED' and requeued['attempts'] == 1
    buffer = (datetime.fromisoformat(requeued['not_before'])
              - datetime.now(timezone.utc)).total_seconds()
    assert buffer > 30
    assert [e['type'] for e in store.events(mission['id'])].count('JOB_REQUEUED') == 1
    assert runner.calls == 1

    # Worker A was alive all along and finalizes the step exactly once.
    runner.release.set()
    thread.join(timeout=20)
    assert not thread.is_alive()
    finished = store.get_job(job['id'])
    assert finished['status'] == 'COMPLETED' and finished['attempts'] == 1
    assert runner.calls == 1
    types = [e['type'] for e in store.events(mission['id'])]
    assert 'JOB_COMPLETED' in types and 'JOB_FAILED' not in types


def test_paused_and_cancelled_jobs_are_not_run(tmp_path):
    runner = SequencedRunner([(0, '', '')])
    store, scheduler = build(tmp_path, runner)
    mission = make_mission(store)
    paused = store.enqueue_job(mission['id'], 'paused')
    store.pause_job(paused['id'])
    cancelled = store.enqueue_job(mission['id'], 'cancelled')
    store.cancel_job(cancelled['id'])

    assert scheduler.run_once() == []
    assert store.get_job(paused['id'])['status'] == 'PAUSED'
    assert store.get_job(cancelled['id'])['status'] == 'CANCELLED'
    store.resume_job(paused['id'])
    assert scheduler.run_pending() == 1
    assert store.get_job(paused['id'])['status'] == 'COMPLETED'


def test_five_missions_concurrency_two_queues_safely(tmp_path):
    runner = SequencedRunner([(0, '{"type":"text","text":"ok"}\n', '')] * 5)
    store, scheduler = build(tmp_path, runner, concurrency=2)
    missions = [make_mission(store, f'M{i}') for i in range(5)]
    for m in missions:
        store.enqueue_job(m['id'], f'task {m["title"]}', step_id=m['steps'][0]['id'])

    first = scheduler.run_once()
    assert len(first) == 2
    assert sum(job['status'] == 'COMPLETED' for job in store.jobs()) == 2
    assert sum(job['status'] == 'QUEUED' for job in store.jobs()) == 3

    second = scheduler.run_once()
    assert len(second) == 2
    assert sum(job['status'] == 'COMPLETED' for job in store.jobs()) == 4

    third = scheduler.run_once()
    assert len(third) == 1
    assert all(job['status'] == 'COMPLETED' for job in store.jobs())

    mission_ids = {m['id'] for m in missions}
    for cwd in runner.cwds:
        assert any(mid in cwd for mid in mission_ids)
    seen = set()
    for m in missions:
        matches = [c for c in runner.cwds if m['id'] in c]
        assert len(matches) >= 1
        seen.add(m['id'])
    assert seen == mission_ids


def test_job_http_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv('FACAI_SCHEDULER_ENABLED', '0')
    runner = SequencedRunner([(0, '{"type":"text","text":"done"}', '')])
    client = TestClient(create_app(tmp_path, opencode_adapter=OpenCodeAdapter(binary='opencode', runner=runner)))
    mission = client.post('/api/v1/missions', json={
        'title': 'J', 'original_input': 'in', 'steps': ['开发']}).json()
    client.put(f"/api/v1/missions/{mission['id']}/binding",
               json={'agent_id': 'opencode', 'model_id': 'deepseek-flash'})

    queued = client.post(f"/api/v1/missions/{mission['id']}/jobs",
                         json={'prompt': 'task', 'step_id': mission['steps'][0]['id']})
    assert queued.status_code == 201 and queued.json()['status'] == 'QUEUED'
    assert len(client.get(f"/api/v1/missions/{mission['id']}/jobs").json()) == 1

    ran = client.post('/api/v1/scheduler/run')
    assert ran.status_code == 200 and ran.json()['executed']
    job = client.get(f"/api/v1/jobs/{queued.json()['id']}").json()
    assert job['status'] == 'COMPLETED'
    assert client.get('/api/v1/scheduler').json()['concurrency'] >= 1

    assert client.post('/api/v1/missions/%s/jobs' % mission['id'],
                       json={'prompt': 'x', 'step_id': 'wrong'}).status_code == 404
    assert client.post('/api/v1/jobs/unknown/cancel').status_code == 404


def test_background_scheduler_health_records_safe_error_and_recovers(tmp_path):
    store, scheduler = build(tmp_path, SequencedRunner([(0, '', '')]))

    class StopAfterOneCycle:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _interval):
            self.stopped = True
            return True

    stop = StopAfterOneCycle()
    scheduler._stop = stop
    scheduler.run_once = lambda: (_ for _ in ()).throw(RuntimeError("secret webhook path"))
    scheduler._serve(0.01)
    failed = scheduler.stats()
    assert failed["background_running"] is False
    assert failed["last_attempt_at"] is not None
    assert failed["last_success_at"] is None
    assert failed["last_error"] == "RuntimeError"
    assert failed["consecutive_errors"] == 1
    assert "secret webhook path" not in str(failed)

    stop.stopped = False
    scheduler.run_once = lambda: []
    scheduler._serve(0.01)
    recovered = scheduler.stats()
    assert recovered["last_success_at"] is not None
    assert recovered["last_error"] is None
    assert recovered["consecutive_errors"] == 0


def test_quota_pool_stats_driven_by_real_dispatch(tmp_path):
    """MODEL-001（2026-10-16）④：调度器真实流量驱动池统计——ok 计数、
    分钟水位、调用次数与延迟字段全部被派发推进，全程零真实网络调用
    （SequencedRunner 模拟适配器）。无池模型不受影响（未注册即无键）。"""
    runner = SequencedRunner([(0, '{"type":"text","text":"ok"}\n', '')] * 2)
    store = Store(tmp_path)
    registries = Registries(store)
    pools = QuotaPools({"deepseek-flash": {"rate_limit_per_min": 10}})
    adapter = OpenCodeAdapter(binary='opencode', runner=runner)
    scheduler = Scheduler(store, adapter, registries, tmp_path, concurrency=1,
                          worker_id='pool-worker', quota_pools=pools)
    mission = make_mission(store)
    store.enqueue_job(mission['id'], 'task A')
    store.enqueue_job(mission['id'], 'task B')

    scheduler.run_pending()

    assert all(job['status'] == 'COMPLETED' for job in store.jobs())
    row = pools.matrix()['deepseek-flash']
    assert row['ok_count'] == 2 and row['fail_count'] == 0
    assert row['reserved_count'] == 2 and row['reported_count'] == 2
    assert row['minute_used'] == 2 and row['inflight'] == 0
    assert row['error_rate'] == 0.0
    assert row['latency_avg_ms'] is not None and row['latency_avg_ms'] >= 0
    assert row['latency_recent_avg_ms'] is not None
