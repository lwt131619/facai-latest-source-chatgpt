"""Durable mission scheduler.

Jobs live in SQLite, so queued work, leases and recovery survive a process
restart. A worker claims a job under a lease, executes it with the same
mission-scoped execution path as the synchronous API, and finalizes it with the
real outcome. Concurrency is bounded per scheduler instance.
"""

import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import checkpoint
from . import router
from .execution import execute_mission_task, opencode_model_ref
from .skills import dispatchable_skill

# Documented default and upper bound for scheduler concurrency.
DEFAULT_CONCURRENCY = 2
CONCURRENCY_LIMIT = 8

# 逆向克隆防护（2026-10-06）：平台自身崩溃（except Exception 路径）的文本会
# 作为 job error 落库，并经 /jobs 行与 JOB_FAILED/STEP_FAILED 事件到达客户。
# OSError 等异常的 str() 带服务器文件路径，httpx 连接错误带运营方网关主机名
# ——落库前把文件系统路径与 URL 主机形状打码；暂态标记词（429/locked/
# timeout 等路由分类依据）不受影响，分类仍读原始文本。
_CRASH_PATH_SHAPE = re.compile(
    r"(?:[A-Za-z]:\\[^\s\"']+)|(?:\b/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)"
    r"|(?:https?://[^\s\"']+)")


def mask_filesystem_paths(text):
    """Crash-path text for customer-visible storage: layout shapes become [路径]."""
    return _CRASH_PATH_SHAPE.sub("[路径]", str(text))


def _is_rate_limited(text):
    """True when failure evidence names an upstream 429 / rate limit.

    Drives the pool's exponential cooldown ladder (quota.report) — same
    evidence the router/gateway classifiers already read, kept as a cheap
    marker check so the scheduler never re-parses full classification here.
    """
    blob = (text or "").lower()
    return "429" in blob or "rate_limit" in blob or "rate limited" in blob
# Hard ceiling on parallel RUNNING jobs per mission: same-mission branches run
# in isolated git worktrees, so beyond this they simply queue.
MAX_PARALLEL_PER_MISSION = 3


def _worker_limit(value):
    """Parse the configured concurrency without ever aborting startup.

    ``FACAI_MAX_WORKERS`` comes from the systemd ``EnvironmentFile``, where a
    blank value is a normal way to leave a setting at its default. A missing,
    empty or non-numeric value therefore falls back to the documented default
    instead of raising ``ValueError`` while the application is still importing;
    an out-of-range number is clamped to the supported range.
    """
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = DEFAULT_CONCURRENCY
    return max(1, min(CONCURRENCY_LIMIT, parsed))


def _fleet_ceiling(value):
    """Parse the optional fleet-wide running ceiling; ``None`` = unlimited.

    ``FACAI_MAX_RUNNING_JOBS`` bounds RUNNING jobs across ALL workers at
    claim time. It must never default to one worker's own concurrency: the
    store's ceiling is global, so a fleet of N workers would be throttled to
    whichever worker claimed first. Unset or blank means no global ceiling —
    each worker's own concurrency still binds locally.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except ValueError:
        return None
    return max(1, parsed)


class Scheduler:
    def __init__(self, store, adapter, registries, root, concurrency=None,
                 lease_seconds=600, worker_id=None, adapter_resolver=None,
                 quota_pools=None):
        self.store = store
        self.adapter = adapter
        # Optional callable (mission -> adapter) for per-mission agent selection
        # via the agent registry ("opencode", "goose", ...). None keeps the
        # single-adapter behavior.
        self.adapter_resolver = adapter_resolver
        # MODEL-001（2026-10-16）：可选的配额池（facai.quota.QuotaPools）。
        # None 或模型未注册池 = 零改动直发（池是可选增强，不是新闸门）；
        # 注册了池的模型派发前 reserve、终态后 report（_execute）。
        self.quota_pools = quota_pools
        self.registries = registries
        self.root = root
        configured = (concurrency if concurrency is not None
                      else os.getenv("FACAI_MAX_WORKERS", str(DEFAULT_CONCURRENCY)))
        self.concurrency = _worker_limit(configured)
        # Optional fleet-wide claim ceiling (RUNTIME-001 capacity): see
        # _fleet_ceiling. Never defaults to this worker's own concurrency —
        # the store's ceiling is global, and a fleet must not be throttled to
        # whichever worker claimed first.
        self.max_running_jobs = _fleet_ceiling(os.getenv("FACAI_MAX_RUNNING_JOBS"))
        # Floor for every claim's lease: the store extends it to cover the
        # job's own timeout, so a long-running job is never recovered while its
        # worker is still executing it.
        self.lease_seconds = lease_seconds
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._health_lock = threading.Lock()
        self._last_attempt_at = None
        self._last_success_at = None
        self._last_error = None
        self._consecutive_errors = 0

    def enqueue(self, mission_id, prompt, **kwargs):
        return self.store.enqueue_job(mission_id, prompt, **kwargs)

    def recover(self):
        # Expired conversion claims join the same periodic recovery path as job
        # leases, so a crashed channel worker is replayed without operator action.
        self.store.recover_stale_channel_conversions()
        return self.store.requeue_expired()

    def stats(self):
        with self._health_lock:
            health = {
                "background_running": bool(self._thread and self._thread.is_alive()),
                "last_attempt_at": self._last_attempt_at,
                "last_success_at": self._last_success_at,
                "last_error": self._last_error,
                "consecutive_errors": self._consecutive_errors,
            }
        return {"concurrency": self.concurrency, "running": self.store.count_running_jobs(),
                "worker": self.worker_id, **health}

    def _is_parallel_branch(self, job, batch_counts):
        """Use the atomic admission reservation, never a racy sibling query."""
        return job.get('workspace_mode') == 'ISOLATED'

    def _isolation_ready(self, mission_id):
        mission = self.store.get(mission_id)
        return bool(mission and checkpoint.isolation_ready(mission['workspace']))

    def _model_with_key(self, model, owner):
        """Attach the decrypted credential of an owner-registered model.

        The plaintext key exists in memory for the duration of one dispatch
        only; it is never logged, never stored and never returned.
        """
        ref = (model or {}).get("user_model_id")
        if not ref:
            return model
        token = self.store.user_model_encrypted_key(owner, ref)
        if not token:
            return model
        try:
            from .secretbox import decrypt

            return {**model, "api_key": decrypt(self.root, token)}
        except Exception:
            return model

    def _report_pool(self, backend, ok, text="", latency_ms=None):
        """Terminal-state report to the dispatched model's quota pool.

        No-op unless a pool exists for this backend — unregistered models
        keep exactly the pre-MODEL-001 behavior. rate_limited is read from
        the same failure evidence the router classifies.
        """
        if self.quota_pools is None:
            return
        self.quota_pools.report(backend, ok, rate_limited=_is_rate_limited(text),
                                latency_ms=latency_ms)

    def _pool_unavailable(self, job, mission, backend):
        """The model's pool declined (paused/cooldown/watermark) pre-dispatch.

        绝不假成功：先走既有响应式故障转移（router.reroute，传入
        quota_pools 让 pick_backend 按池健康序选目标——冷却池沉底），
        并按既有语义留痕（BACKEND_ATTEMPT + MISSION_REROUTED）。没有
        可路由替代时诚实退避重排队——冷却的池会自愈，直接 FAILED 与
        假成功一样不诚实。既有故障转移上限（每作业 2 次）原样生效。
        """
        rerouted = router.reroute(
            self.store, self.registries, job, mission,
            state="BACKEND_UNAVAILABLE", failed_backend=backend,
            error=f"model quota pool unavailable (paused/cooldown/watermark): {backend}",
            quota_pools=self.quota_pools)
        if rerouted is not None:
            return rerouted
        delay = min(300 * (2 ** max(0, job["attempts"] - 1)),
                    int(os.getenv("FACAI_RETRY_BACKOFF_CAP", "1800")))
        reason = f"quota pool unavailable, auto-retry in {delay}s: {backend}"[:500]
        self.store.requeue_job_backoff(job["id"], delay, reason)
        return self.store.get_job(job["id"])

    def _execute(self, job, batch_counts=None):
        mission = self.store.get(job["mission_id"])
        if mission is None:
            self.store.finish_job(job["id"], "FAILED", "mission missing")
            return
        owner = mission.get("owner_id", "admin")
        model = self._model_with_key(
            self.registries.resolve("models", mission["model_id"], owner_id=owner), owner)
        runtime = self.registries.resolve("runtimes", mission["runtime_id"], owner_id=owner)
        # Disabled skills resolve to None here — same gate as the conversion
        # scoring and the API execution paths (facai.skills.dispatchable_skill).
        skill = dispatchable_skill(
            self.registries.resolve("skills", mission["skill_id"], owner_id=owner))
        # A step-level model binding, when present and still resolvable, names the
        # model that actually runs this job: it drives the OpenCode --model flag
        # and the provider/cost attribution (the job's model_ref, set at dispatch
        # time, already carries the same model's OpenCode ref).
        step_model_id = (self.store.step_model_id(job["mission_id"], job["step_id"])
                         if job["step_id"] else None)
        step_model = (self.registries.resolve("models", step_model_id, owner_id=owner)
                      if step_model_id else None)
        if step_model is not None:
            model = self._model_with_key(step_model, owner)
        # P0-14 Router: a rerouted job carries its replacement backend in the
        # error field's machine marker. That binding outranks mission and step
        # bindings for this run, so the adapter flag (model_ref, repointed by
        # store.reroute_job), the credentials, and the session/cost/attribution
        # identity all follow the backend that will actually execute.
        rerouted_to = router.reroute_target(job.get("error"))
        if rerouted_to:
            entry = self.registries.resolve("models", rerouted_to, owner_id=owner)
            if entry is not None:
                model = self._model_with_key(entry, owner)
                step_model = entry
                step_model_id = rerouted_to
        # The backend to blame (and reroute away from) if this run fails.
        failed_backend = step_model_id if step_model is not None else mission["model_id"]
        model_ref = job["model_ref"] or opencode_model_ref(model)
        adapter = self.adapter
        if self.adapter_resolver is not None:
            adapter = self.adapter_resolver(mission)
            if adapter is None:
                self.store.finish_job(job["id"], "FAILED",
                                      f"Unsupported agent adapter: {mission.get('agent_id')}")
                return
        # Admission reserves isolation before any sibling can run. If creating
        # that workspace fails, do not silently join a concurrently written tree.
        worktree = None
        if self._is_parallel_branch(job, batch_counts or {}):
            base_workspace = mission.get("workspace") or \
                str(Path(self.root) / "workspaces" / mission["id"])
            worktree = checkpoint.prepare_worktree(base_workspace,
                                                   suffix=job["step_id"][:8]+'-'+job['id'])
            if worktree is None:
                self.store.defer_isolation(job['id'], self.worker_id)
                return
        # The worker holds execution from here: re-arm the lease so a peer
        # scheduler's recovery pass cannot requeue a live job that already
        # spent part of its claim lease queued (serial lock, backoff, batch).
        self.store.touch_job_lease(job["id"], job["timeout"])
        # MODEL-001（2026-10-16）预防式配额池接线：派发前向绑定模型的池
        # 申请容量。池未注册的模型在这里零改动（has_pool 门）；池拒绝
        # （暂停/冷却/水位满）不假成功——走既有故障转移（健康序）或诚实
        # 退避重排队（_pool_unavailable）。
        if self.quota_pools is not None and self.quota_pools.has_pool(failed_backend) \
                and not self.quota_pools.reserve(failed_backend):
            return self._pool_unavailable(job, mission, failed_backend)
        dispatch_started = time.monotonic()
        try:
            result = execute_mission_task(self.store, adapter, mission, model, self.root,
                                          job["prompt"], job["step_id"], model_ref, job["timeout"],
                                          runtime=runtime, skill=skill,
                                          step_model_id=step_model_id if step_model is not None else None,
                                          agent_id=mission.get("agent_id"),
                                          workspace_override=worktree["path"] if worktree else None,
                                          worktree=worktree)
            latency_ms = (time.monotonic() - dispatch_started) * 1000
            merge = result.get("merge")
            if result["status"] == "OPENCODE_COMPLETED" and merge is not None \
                    and not merge.get("merged"):
                self._report_pool(failed_backend, False, text="merge conflict",
                                  latency_ms=latency_ms)
                self._finish_or_requeue(
                    job, f"merge conflict: {(worktree or {}).get('branch', '')}",
                    mission=mission, failed_backend=failed_backend)
            elif result["status"] == "OPENCODE_COMPLETED" and (merge is None or merge.get("merged")):
                self._report_pool(failed_backend, True, latency_ms=latency_ms)
                self.store.finish_job(job["id"], "COMPLETED")
            else:
                detail = result["status"]
                if result.get("status") == "OPENCODE_FAILED" and result.get("stderr"):
                    # The stderr tail carries the real transient markers
                    # (e.g. SQLite "database is locked" from the CLI).
                    detail = f"{detail}: {result['stderr'][-400:]}"
                self._report_pool(failed_backend, False, text=detail,
                                  latency_ms=latency_ms)
                self._finish_or_requeue(
                    job, detail, mission=mission, failed_backend=failed_backend,
                    state=router.outcome_state(status=result["status"], text=detail))
        except Exception as exc:
            # Attribute the failure truthfully; never mark a failed run complete.
            # 存储文本打码（见 mask_filesystem_paths），分类仍用原始文本。
            self._report_pool(failed_backend, False, text=str(exc))
            self._finish_or_requeue(job, mask_filesystem_paths(str(exc))[:500],
                                    mission=mission, failed_backend=failed_backend,
                                    state=router.outcome_state(text=str(exc)))

    def _finish_or_requeue(self, job, error, mission=None, state=None, failed_backend=None):
        """Finish a failed job, requeue it when the failure is transient, or
        reroute it to another backend when the failure names a backend state.

        A 24/7 station cannot have an operator babysitting every hiccup: an
        upstream timeout, a rate limit, a provider resource error or a
        transport/SQLite hiccup is re-run automatically with exponential
        backoff while the job still has attempts left. Permanent failures
        (auth, refusal, missing bindings) finish FAILED and stay that way --
        honesty about what actually broke is never retried away.

        Before a terminal FAILED, though, §15/§16 (P0-14 Router) applies: a
        backend state (PROVIDER_REFUSAL / BACKEND_UNAVAILABLE / TIMEOUT) is
        THIS BACKEND DID NOT EXECUTE, not a mission verdict. When another
        registered backend exists and the per-job reroute ceiling is not
        reached, the job is rerouted instead of failed; with no alternative
        it fails exactly as before. A gate denial (MISSION_DENIED) is a
        local decision and is never rerouted.
        """
        text = (error or "").lower()
        transient = any(marker in text for marker in (
            "timeout", "rate_limit", "rate limited", "resource_unavailable",
            "resource unavailable", "transport", "database is locked",
            "model_resource", "connection", "502", "503", "504",
            # A service restart/redeploy kills the agent subprocess mid-run.
            # That is an interruption, not a verdict on the work: the step must
            # re-run rather than die because a deploy happened to ship while it
            # was executing. A 24/7 station redeploys unattended.
            "interrupt", "sigterm", "sigkill", "killed", "terminated",
            "broken pipe", "connection reset", "unexpected eof",
            "shutting down", "canceled", "cancelled",
            # A parallel-branch merge conflict resolves on a solo re-run (the
            # sibling branch has completed by then), so one automatic retry is
            # the difference between a stalled mission and a finished one.
            "merge conflict"))
        if transient and job["attempts"] < job["max_attempts"]:
            delay = min(300 * (2 ** max(0, job["attempts"] - 1)),
                        int(os.getenv("FACAI_RETRY_BACKOFF_CAP", "1800")))
            reason = f"transient failure, auto-retry in {delay}s: {error}"[:500]
            # A rerouted job keeps its replacement-backend binding across
            # transient backoffs: the marker must survive the requeue or the
            # next claim would silently fall back to the failed backend.
            marker = router.reroute_marker_prefix(job.get("error"))
            if marker:
                reason = (marker + reason)[:500]
            self.store.requeue_job_backoff(job["id"], delay, reason)
            return self.store.get_job(job["id"])
        rerouted = None
        if mission is not None:
            rerouted = router.reroute(
                self.store, self.registries, job, mission,
                state=state or router.outcome_state(text=error),
                failed_backend=failed_backend or (mission or {}).get("model_id"),
                error=error)
        if rerouted is not None:
            return rerouted
        self.store.finish_job(job["id"], "FAILED", error)
        return self.store.get_job(job["id"])

    def run_once(self):
        with self._lock:
            self.recover()
            slots = max(0, self.concurrency - self.store.count_running_jobs())
            jobs = []
            for _ in range(slots):
                job = self.store.claim_job(self.worker_id, self.lease_seconds,
                                           parallel_mission_jobs=min(self.concurrency,
                                                                     MAX_PARALLEL_PER_MISSION),
                                           isolation_ready=self._isolation_ready,
                                           max_running_jobs=self.max_running_jobs)
                if job is None:
                    break
                jobs.append(job)
            if not jobs:
                return []
            batch_counts = {}
            for job in jobs:
                batch_counts[job["mission_id"]] = batch_counts.get(job["mission_id"], 0) + 1
            if len(jobs) == 1:
                self._execute(jobs[0], batch_counts)
            else:
                with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                    list(pool.map(lambda job: self._execute(job, batch_counts), jobs))
            return [job["id"] for job in jobs]

    def run_pending(self):
        total = 0
        while True:
            done = self.run_once()
            total += len(done)
            if not done:
                break
        return total

    def start(self, interval=1.0):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, args=(interval,), daemon=True)
        self._thread.start()

    def _serve(self, interval):
        while not self._stop.is_set():
            attempt_at = datetime.now(timezone.utc).isoformat()
            with self._health_lock:
                self._last_attempt_at = attempt_at
            try:
                self.run_once()
                with self._health_lock:
                    self._last_success_at = datetime.now(timezone.utc).isoformat()
                    self._last_error = None
                    self._consecutive_errors = 0
            except Exception as exc:
                # Keep operational state useful without exposing exception text,
                # which can contain webhook paths, credentials or user data.
                with self._health_lock:
                    self._last_error = type(exc).__name__
                    self._consecutive_errors += 1
            self._stop.wait(interval)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
