"""Crash-recovery acceptance with the REAL OpenCode CLI.

A worker that dies holding a lease must lose the job to recovery exactly once,
and the peer that picks it up must finish it on the real CLI. Staged honestly:

  1. a job is claimed by "dead-worker" under a 2s lease (the worker then
     vanishes without executing anything — no OPENCODE_STARTED may ever
     carry its name),
  2. the lease expires and a peer Scheduler's recovery pass requeues the
     orphan (attempts 0 -> 1, not-before buffer installed),
  3. the operator-verified recovery window clears the buffer and the peer
     runs the job on the REAL opencode binary.

Pass criteria: recovery returns exactly one job; the job ends COMPLETED with
exactly one OPENCODE_STARTED; the requested file exists with the requested
content; a cost row is recorded. The dead worker's zero executions are implied
by the single STARTED event plus the peer being the only runner.

Run: python scripts/smoke_recovery.py   (one tiny real job on the bound model)
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from facai.opencode import OpenCodeAdapter          # noqa: E402
from facai.registries import Registries             # noqa: E402
from facai.scheduler import Scheduler               # noqa: E402
from facai.store import Store                       # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lapse", type=float, default=0.5,
                        help="seconds to wait after the already-expired claim")
    args = parser.parse_args()

    checks = []

    def note(step, ok, detail=""):
        checks.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {step}: {detail[:220]}", flush=True)

    binary = shutil.which("opencode")
    if not binary:
        print("FAIL: opencode binary not on PATH; nothing real to recover onto")
        return 1

    data = Path(tempfile.gettempdir()) / f"facai-recovery-{os.getpid()}"
    data.mkdir(parents=True, exist_ok=True)
    store = Store(data / "data")
    registries = Registries(store)
    mission = store.create("recovery-实弹", "创建文件r0.txt，内容写：恢复验证0",
                           [{"id": "solo", "label": "恢复"}], issue_grant=False)
    store.bind(mission["id"], "opencode", "deepseek-flash")
    job = store.enqueue_job(mission["id"], "在当前工作区创建r0.txt，内容：恢复验证0",
                            max_attempts=2, timeout=120)
    mission_id, job_id = mission["id"], job["id"]

    # 1. crash: claim with an ALREADY-EXPIRED lease (non-positive lease_seconds
    # is the documented staging request — a positive value would be widened to
    # cover the job timeout, which is exactly what protects live workers) and
    # then vanish. Nothing executes under this lease.
    claimed = store.claim_job("dead-worker", lease_seconds=0)
    note("claim/dead-worker", claimed is not None and claimed["status"] == "RUNNING",
         f"job={job_id} lease_expires={claimed.get('lease_expires_at')}")
    time.sleep(args.lapse)         # the worker is gone; the lease lapses

    # 2. recovery: the peer's pass requeues the orphan exactly once.
    peer_adapter = OpenCodeAdapter(binary=binary)
    peer = Scheduler(store, peer_adapter, registries, data / "data",
                     concurrency=1, worker_id="recovery-peer", lease_seconds=600)
    recovered = peer.recover()
    note("recovery/requeue-once", len(recovered) == 1, f"requeued={recovered}")
    row = store.get_job(job_id)
    note("recovery/attempts-advanced",
         row["status"] == "QUEUED" and row["attempts"] == 1,
         f"status={row['status']} attempts={row['attempts']}")

    # The not-before buffer guards against a stale finalize; the operator-
    # verified window clears it (same contract as the controlled test).
    with store.connect() as db:
        db.execute("UPDATE jobs SET not_before=NULL")

    # 3. the peer runs the orphan on the REAL CLI.
    peer.run_once()
    deadline = time.time() + 150
    while time.time() < deadline:
        row = store.get_job(job_id)
        if row["status"] in ("COMPLETED", "FAILED", "BLOCKED", "CANCELLED"):
            break
        time.sleep(2)
    note("peer/terminal", row["status"] == "COMPLETED", f"status={row['status']}")

    # Evidence straight from the durable store.
    conn = sqlite3.connect(str(data / "data" / "facai.sqlite3"))
    try:
        started = conn.execute(
            "SELECT COUNT(*) FROM events WHERE mission_id=? AND type='OPENCODE_STARTED'",
            (mission_id,)).fetchone()[0]
        completed = conn.execute(
            "SELECT COUNT(*) FROM events WHERE mission_id=? AND type='JOB_COMPLETED'",
            (mission_id,)).fetchone()[0]
        requeued = conn.execute(
            "SELECT COUNT(*) FROM events WHERE mission_id=? AND type='JOB_REQUEUED'",
            (mission_id,)).fetchone()[0]
        costs = conn.execute(
            "SELECT COUNT(*) FROM costs WHERE mission_id=?", (mission_id,)).fetchone()[0]
    finally:
        conn.close()
    note("evidence/exactly-once", started == 1 and completed == 1,
         f"started={started} completed={completed}")
    note("recovery/requeue-event-pinned", requeued >= 1, f"JOB_REQUEUED={requeued}")
    note("costs/recorded", costs >= 1, f"rows={costs}")
    artifact = data / "data" / "workspaces" / mission_id / "r0.txt"
    content = artifact.read_text(encoding="utf-8", errors="replace").strip() \
        if artifact.exists() else "<missing>"
    note("artifact/real-cli", content == "恢复验证0", content[:60])

    print(f"\n=== RECOVERY VERDICT: {sum(checks)}/{len(checks)} passed ===", flush=True)
    print(f"isolated data kept for inspection: {data}", flush=True)
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
