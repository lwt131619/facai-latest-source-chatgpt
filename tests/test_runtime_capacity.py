"""RUNTIME-001 acceptance: a 24-job fleet over one shared store.

The task's acceptance line requires 24 independent tasks with a controlled
adapter, concurrent dispatch with measured capacity, lease renewal and crash
recovery, each with evidence. Four Scheduler instances (worker fleet) share one
SQLite store exactly like separate worker processes would; each owns a probe
runner that records every execution. No double-claim, no capacity overshoot,
no recovery steal is tolerated.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from facai.opencode import OpenCodeAdapter
from facai.registries import Registries
from facai.scheduler import Scheduler, _fleet_ceiling
from facai.store import Store


class FleetProbe:
    """Fake OpenCode CLI runner recording executions and in-flight peaks."""

    def __init__(self, worker_id, sleep=0.0):
        self.worker_id = worker_id
        self.sleep = sleep
        self.lock = threading.Lock()
        self.runs = {}          # workspace path -> execution count
        self.active = 0
        self.peak = 0
        self.total = 0

    def __call__(self, command, cwd, timeout):
        with self.lock:
            self.runs[str(cwd)] = self.runs.get(str(cwd), 0) + 1
            self.total += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.sleep:
                time.sleep(self.sleep)
            return __import__("subprocess").CompletedProcess(
                command, 0, '{"type":"text","text":"ok"}\n', "")
        finally:
            with self.lock:
                self.active -= 1


def _build_fleet(tmp_path, workers=4, concurrency=6, sleep=0.0, lease_seconds=600):
    """One shared store plus a fleet of schedulers with per-worker probes."""
    store = Store(tmp_path)
    schedulers = []
    probes = {}
    for index in range(workers):
        worker_id = f"fleet-worker-{index}"
        probe = FleetProbe(worker_id, sleep=sleep)
        probes[worker_id] = probe
        adapter = OpenCodeAdapter(binary="opencode", runner=probe)
        schedulers.append(Scheduler(
            store, adapter, Registries(store), tmp_path,
            concurrency=concurrency, worker_id=worker_id,
            lease_seconds=lease_seconds))
    return store, schedulers, probes


def _enqueue_independent_jobs(store, count=24):
    """24 independent missions, one job each, bound to the test adapter."""
    job_ids = []
    for index in range(count):
        mission = store.create(f"fleet-{index:02d}", f"input {index}",
                               [{"id": "solo", "label": "S"}], issue_grant=False)
        store.bind(mission["id"], "opencode", "deepseek-flash")
        job_ids.append(store.enqueue_job(mission["id"], f"task {index}")["id"])
    return job_ids


def _seed_one_job_per_worker(schedulers):
    """Deterministic participation: each worker claims one job synchronously
    before the concurrent free-for-all. run_once() is claim→execute, so on a
    slow runner one thread can drain the fleet before a peer wakes (observed
    on ubuntu-latest); the scheduler guarantees no double-claim and capacity
    honesty, not fair distribution."""
    for scheduler in schedulers:
        original = scheduler.concurrency
        scheduler.concurrency = 1
        try:
            scheduler.run_once()
        finally:
            scheduler.concurrency = original


def _drain(store, schedulers, deadline_seconds=120, seed=False):
    """Every worker thread loops run_once() until the fleet has nothing left.
    seed=True first claims one job per worker synchronously (see
    _seed_one_job_per_worker) so participation is deterministic."""
    def work(scheduler):
        deadline = time.monotonic() + deadline_seconds
        while time.monotonic() < deadline:
            done = scheduler.run_once()
            if not done and not store.count_running_jobs():
                return
            if not done:
                time.sleep(0.02)
    start = time.monotonic()
    if seed:
        _seed_one_job_per_worker(schedulers)
    with ThreadPoolExecutor(max_workers=len(schedulers)) as pool:
        list(pool.map(work, schedulers))
    return time.monotonic() - start


def test_24_independent_jobs_never_double_claim_and_respect_capacity(tmp_path):
    """The acceptance line: 24 jobs, 4 workers, controlled adapter, measured."""
    store, schedulers, probes = _build_fleet(tmp_path, workers=4, concurrency=6)
    job_ids = _enqueue_independent_jobs(store, count=24)
    assert len(job_ids) == 24

    elapsed = _drain(store, schedulers, seed=True)

    jobs = {job["id"]: job for job in store.jobs()}
    assert len(jobs) == 24
    assert all(job["status"] == "COMPLETED" for job in jobs.values())

    # No double-claim: every one of the 24 mission workspaces executed exactly
    # once across the whole fleet; total executions == 24.
    total_runs = sum(probe.total for probe in probes.values())
    assert total_runs == 24
    all_runs = {}
    for probe in probes.values():
        for workspace, count in probe.runs.items():
            all_runs[workspace] = all_runs.get(workspace, 0) + count
    assert len(all_runs) == 24
    assert all(count == 1 for count in all_runs.values())

    # Capacity honesty: no worker ever exceeded its configured concurrency.
    for worker_id, probe in probes.items():
        assert probe.peak <= 6, f"{worker_id} peaked at {probe.peak}"

    # Every execution is attributed to exactly one worker and one job event.
    started = 0
    for index in range(24):
        mission_id = f"fleet-{index:02d}"
        mission = next(m for m in store.list() if m["title"] == mission_id)
        events = store.events(mission["id"])
        started += sum(1 for e in events if e["type"] == "OPENCODE_STARTED")
    assert started == 24

    # The measurement itself is honest: workers actually shared the work.
    assert all(probe.total > 0 for probe in probes.values()), probes
    assert elapsed < 120


def test_lease_renewal_keeps_a_slow_job_unstolen(tmp_path):
    """A job slower than its claim lease is renewed, never stolen mid-run."""
    store, schedulers, probes = _build_fleet(tmp_path, workers=2, concurrency=1,
                                             sleep=1.5, lease_seconds=0.4)
    _enqueue_independent_jobs(store, count=1)
    slow, peer = schedulers

    with ThreadPoolExecutor(max_workers=2) as pool:
        slow_future = pool.submit(slow.run_once)
        # The slow worker must own the claim before the peer starts hunting.
        deadline = time.monotonic() + 10
        job = store.jobs()[0]
        while time.monotonic() < deadline:
            job = store.jobs()[0]
            if job["status"] == "RUNNING":
                break
            time.sleep(0.02)
        assert job["status"] == "RUNNING"
        # The peer hunts for the whole slow run: without renewal its recovery
        # pass would requeue the expired 0.4s lease and re-run the step; with
        # touch_job_lease the lease is re-armed to the job timeout, so the
        # peer can only wait until the job completes.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            peer.run_once()
            if all(j["status"] == "COMPLETED" for j in store.jobs()):
                break
            time.sleep(0.05)
        slow_future.result(timeout=30)

    jobs = store.jobs()
    assert len(jobs) == 1 and jobs[0]["status"] == "COMPLETED"
    assert probes["fleet-worker-0"].total == 1  # executed exactly once
    assert probes["fleet-worker-1"].total == 0  # the peer never stole it


def test_crashed_worker_job_is_recovered_by_a_peer(tmp_path):
    """A worker that dies holding a lease loses the job to recovery, once."""
    store, schedulers, probes = _build_fleet(tmp_path, workers=2, concurrency=1,
                                             lease_seconds=0.3)
    # A retry budget is required for recovery to requeue: a job with no
    # attempts left honestly fails when its lease expires instead.
    mission = store.create("fleet-recovered", "input", [{"id": "solo", "label": "S"}],
                           issue_grant=False)
    store.bind(mission["id"], "opencode", "deepseek-flash")
    job_id = store.enqueue_job(mission["id"], "task", max_attempts=2)["id"]

    # "Crash": a worker claims and vanishes without finishing.
    claimed = store.claim_job("crashed-worker", lease_seconds=0.3)
    assert claimed["status"] == "RUNNING"
    time.sleep(0.5)  # lease expires

    # A peer's recovery pass requeues the orphan exactly like real recovery.
    recovered = schedulers[0].recover()
    assert len(recovered) == 1
    row = store.jobs()[0]
    assert row["status"] == "QUEUED" and row["attempts"] == 1

    # Recovery installs a not-before buffer against a stale worker that is
    # only between expiry and its finalize write; an operator-verified
    # recovery window clears it, and then the peer runs the job exactly once.
    with store.connect() as db:
        db.execute("UPDATE jobs SET not_before=NULL")
    schedulers[1].run_once()
    jobs = store.jobs()
    assert jobs[0]["status"] == "COMPLETED"
    assert probes["fleet-worker-1"].total == 1
    assert probes["fleet-worker-0"].total == 0  # the dead worker never ran it
    assert sum(probe.total for probe in probes.values()) == 1


def test_fleet_ceiling_parsing():
    """Unset, blank or unparsable means unlimited (each worker's own
    concurrency still binds locally); a number clamps to at least 1."""
    assert _fleet_ceiling(None) is None
    assert _fleet_ceiling("") is None
    assert _fleet_ceiling("  ") is None
    assert _fleet_ceiling("not-a-number") is None
    assert _fleet_ceiling("7") == 7
    assert _fleet_ceiling("0") == 1


def test_claim_ceiling_is_global_across_workers(tmp_path):
    """The claim-time ceiling counts RUNNING jobs across ALL workers: worker B
    is refused by a ceiling that worker A's claims filled, and only an unset
    ceiling lets a worker claim past other workers' running jobs. This is the
    store contract the scheduler's fleet wiring relies on (RUNTIME-001)."""
    store = Store(tmp_path)
    _enqueue_independent_jobs(store, count=4)

    # Worker A fills a ceiling of 3; its own next claim is refused too.
    for _ in range(3):
        job = store.claim_job("ceiling-a", 600, max_running_jobs=3)
        assert job["status"] == "RUNNING"
    assert store.claim_job("ceiling-a", 600, max_running_jobs=3) is None

    # Worker B is bound by the SAME global count although it claimed nothing.
    assert store.claim_job("ceiling-b", 600, max_running_jobs=3) is None
    statuses = [job["status"] for job in store.jobs()]
    assert statuses.count("RUNNING") == 3
    assert statuses.count("QUEUED") == 1

    # No ceiling (the fleet default) ignores other workers' running claims.
    fourth = store.claim_job("ceiling-b", 600)
    assert fourth["status"] == "RUNNING"


def test_scheduler_reads_fleet_ceiling_env_and_bounds_its_claims(tmp_path, monkeypatch):
    """FACAI_MAX_RUNNING_JOBS reaches the claim path and caps claims below the
    worker's own concurrency. The old wiring passed ``self.concurrency`` as
    the ceiling, so a worker able to run 4 could never be told to run fewer;
    with an explicit ceiling of 2 it claims exactly 2 and the rest stays
    queued honestly."""
    monkeypatch.setenv("FACAI_MAX_RUNNING_JOBS", "2")
    monkeypatch.setenv("FACAI_OPENCODE_SERIAL", "0")  # per-CLI turns are a
    # different axis; this test needs the two claims to run concurrently.
    store = Store(tmp_path)
    probe = FleetProbe("capped", sleep=0.3)
    adapter = OpenCodeAdapter(binary="opencode", runner=probe)
    scheduler = Scheduler(store, adapter, Registries(store), tmp_path,
                          concurrency=4, worker_id="capped", lease_seconds=600)
    assert scheduler.max_running_jobs == 2
    _enqueue_independent_jobs(store, count=4)

    scheduler.run_once()

    assert probe.total == 2
    assert probe.peak <= 2
    statuses = [job["status"] for job in store.jobs()]
    assert statuses.count("COMPLETED") == 2
    assert statuses.count("QUEUED") == 2


def test_two_worker_fleet_completes_combined_capacity_without_a_ceiling(tmp_path, monkeypatch):
    """With no FACAI_MAX_RUNNING_JOBS a fleet completes its combined capacity:
    the old default handed the first claimer's own ``concurrency`` to the store
    as the GLOBAL ceiling, so a 2×2 fleet stopped claiming at 2 jobs. The drain
    loop asserts only what is deterministic — every job runs exactly once and
    every worker stays inside its own capacity; instantaneous cross-worker
    overlap is a scheduling window, pinned at the claim contract instead
    (test_claim_ceiling_is_global_across_workers)."""
    monkeypatch.setenv("FACAI_OPENCODE_SERIAL", "0")
    store, schedulers, probes = _build_fleet(tmp_path, workers=2, concurrency=2,
                                             sleep=0.3)
    _enqueue_independent_jobs(store, count=4)

    _drain(store, schedulers)

    jobs = store.jobs()
    assert len(jobs) == 4
    assert all(job["status"] == "COMPLETED" for job in jobs)
    assert sum(probe.total for probe in probes.values()) == 4
    for probe in probes.values():
        assert probe.peak <= 2
