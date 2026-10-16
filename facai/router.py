"""Backend Router (P0-14): §15 backend states reroute, they never become
MISSION_FAILED.

Frozen semantics implemented here (docs/AUTHORITY_ARCHITECTURE.md §14-§16):

- A backend-side refusal (quota, auth, upstream 4xx/5xx) is a backend
  execution state: THIS BACKEND DID NOT EXECUTE. It is never read as a
  mission verdict and never touches the Owner's mission grant.
- Of the nine §15 states exactly three are routable -- PROVIDER_REFUSAL,
  BACKEND_UNAVAILABLE, TIMEOUT: the mission may continue on another backend
  the Owner has registered. The rest stay verdicts on purpose:
  FACAI_DENIED/GRANT_REQUIRED are the local gate's own decisions (no other
  backend can cure them), EXECUTION_FAILED is the honest terminal for
  crashes that name no backend state.
- 诚实降级: when no alternative backend exists, or the per-job reroute
  ceiling is reached, the caller fails the job exactly as before. The
  router never fabricates an alternative.

Pick order: ``glm-local`` first when present (the local seat cannot quota-
refuse), then registry order. Anti-ping-pong ceiling: at most
``MAX_REROUTES_PER_JOB`` MISSION_REROUTED events per job, counted from the
mission event log (chosen over a job-error counter because events are
already durable, queryable by the console timeline, and need no schema
change). The CURRENT reroute target additionally rides the job error field
as a machine marker so the next claim resolves the replacement model even
after an unrelated transient backoff has rewritten the error text.
"""
from __future__ import annotations

from .execution import opencode_model_ref

# The §15 states that hand the mission to another backend. Everything else
# (FACAI_DENIED, GRANT_REQUIRED, CAPABILITY_MISSING, RESOURCE_UNAVAILABLE,
# EXECUTION_FAILED) is a verdict the router must not retry away.
ROUTABLE_STATES = frozenset({"PROVIDER_REFUSAL", "BACKEND_UNAVAILABLE", "TIMEOUT"})

# Anti-ping-pong ceiling: A→B→A is allowed, a third reroute of the same job
# is not. Beyond this the job fails FAILED with the real cause.
MAX_REROUTES_PER_JOB = 2

REROUTED_EVENT = "MISSION_REROUTED"
BACKEND_ATTEMPT_EVENT = "BACKEND_ATTEMPT"

# Machine marker carried in the job error field: "rerouted:<backend>|<text>".
# The console still shows the human text after the marker; parsers that only
# know the old plain text see a non-empty error string exactly as before.
MARKER_PREFIX = "rerouted:"
MARKER_SUFFIX = "|"

# 用户可见文案跟状态走 (§15): every state names its cause in Chinese.
STATE_LABELS = {
    "EXECUTED": "执行成功",
    "PROVIDER_REFUSAL": "上游执行方拒绝执行",
    "BACKEND_UNAVAILABLE": "执行后端当前不可用",
    "TIMEOUT": "执行超时",
}

REROUTE_COPY = "任务目标没有改变，已切换执行方式"

# Evidence markers for classifying scheduler-path failures. The scheduler's
# transient list (scheduler._finish_or_requeue) owns same-backend retries;
# this classifier only decides whether a reached-the-end failure names a
# backend state that another backend could cure.
_DENY_MARKERS = ("mission_denied", "grant_required", "grant_expired", "grant_exhausted")
_TIMEOUT_MARKERS = ("timeout", "timed out", "超时")
_REFUSAL_MARKERS = ("refus", "content_filter", "content filter", "moderation",
                    "unauthorized", "auth failed", "authentication failed", "forbidden",
                    "rate_limit", "rate limited", "quota", "401", "403", "429", "拒绝")
_UNAVAILABLE_MARKERS = ("unavailable", "backend_unavailable", "502", "503", "507",
                        "transport", "connection", "econnrefused", "不可用")


def outcome_state(status=None, text=None):
    """Classify one scheduler-path failure into its §15 state.

    ``status`` is the adapter terminal status (OPENCODE_TIMEOUT, ...),
    ``text`` the error/stderr evidence. Local gate denials are recognized
    first: a MISSION_DENIED failure is FACAI_DENIED -- the platform's own
    decision, never a backend state, and never routable.
    """
    blob = f"{status or ''} {text or ''}".lower()
    if any(marker in blob for marker in _DENY_MARKERS):
        return "FACAI_DENIED"
    if any(marker in blob for marker in _TIMEOUT_MARKERS):
        return "TIMEOUT"
    if any(marker in blob for marker in _REFUSAL_MARKERS):
        return "PROVIDER_REFUSAL"
    if any(marker in blob for marker in _UNAVAILABLE_MARKERS):
        return "BACKEND_UNAVAILABLE"
    return "EXECUTION_FAILED"


def pick_backend(mission, failed_backend=None, registry_models=None,
                 quota_pools=None):
    """The next runnable backend: not the failed one, glm-local first.

    ``registry_models`` is the owner's models registry ({id: entry}); a
    missing catalog means no alternatives are known and None is returned --
    an honest "cannot reroute" rather than a guess. ``failed_backend``
    defaults to the mission's current binding, the backend that just failed
    for a mission-level default run.

    ``quota_pools`` (facai.quota.QuotaPools) adds PREVENTIVE routing: when
    present, candidates are ordered by live pool health (paused/cooling last,
    then minute-budget headroom, then owner weight) so dispatch walks away
    from a drained pool BEFORE the upstream 429 — the reactive reroute below
    stays as the second line of defense. Pools that cannot reserve right now
    sink to the tail but are not removed: the router still never fabricates
    capacity, it just stops preferring a pool it knows is spent.
    """
    if not isinstance(registry_models, dict) or not registry_models:
        return None
    failed = failed_backend or (mission or {}).get("model_id")
    order = list(registry_models)
    candidates = [key for key in order
                  if key != failed and isinstance(registry_models.get(key), dict)
                  and registry_models[key]]
    if not candidates:
        return None
    if quota_pools is not None:
        health_order = {model_id: rank for rank, model_id
                        in enumerate(quota_pools.ranked())}
        candidates = [key for key in candidates if key in health_order] or candidates
        ordered = sorted(candidates, key=lambda key: (health_order.get(key, 0),
                                                      0 if key == "glm-local" else 1,
                                                      order.index(key)))
        return ordered[0]
    ordered = sorted(candidates, key=lambda key: (0 if key == "glm-local" else 1,
                                                  order.index(key)))
    return ordered[0]


def record_attempt(store, mission_id, backend, state):
    """留痕: one BACKEND_ATTEMPT event naming the backend and its §15 state.

    A refused attempt is recorded as exactly that -- this backend did not
    execute -- so the timeline can never be read as the Owner withdrawing
    the mission's grant (§14).
    """
    label = STATE_LABELS.get(state, state)
    if state == "EXECUTED":
        message = f"后端 {backend} {label}"
    else:
        message = (f"后端 {backend} 本次未完成执行：{label}"
                   f"（THIS BACKEND DID NOT EXECUTE，不影响任务授权）")
    return store.append(mission_id, BACKEND_ATTEMPT_EVENT,
                        {"backend": backend, "state": state, "message": message})


def reroute_copy(from_backend, to_backend, state):
    """The Chinese user copy for one reroute; the lead sentence is stable."""
    label = STATE_LABELS.get(state, state)
    return f"{REROUTE_COPY}：{from_backend} {label}，改由 {to_backend} 继续。"


def reroute_marker(to_backend):
    return f"{MARKER_PREFIX}{to_backend}{MARKER_SUFFIX}"


def reroute_target(error):
    """The backend a rerouted job must run on next, parsed from its error
    field marker; None for any plain error text (including historical rows)."""
    text = error or ""
    if not text.startswith(MARKER_PREFIX):
        return None
    return text[len(MARKER_PREFIX):].split(MARKER_SUFFIX, 1)[0] or None


def reroute_marker_prefix(error):
    target = reroute_target(error)
    return reroute_marker(target) if target else ""


def reroute_error(to_backend, text):
    return f"{reroute_marker(to_backend)}{text}"


def reroute_count(store, mission_id, job_id):
    """How many times this job has already been rerouted (durable event count)."""
    return sum(1 for event in store.events(mission_id)
               if event["type"] == REROUTED_EVENT
               and (event.get("payload") or {}).get("job_id") == job_id)


def reroute(store, registries, job, mission, state, failed_backend, error="",
            quota_pools=None):
    """Move a failed job onto the next registered backend, or decline.

    Returns the refreshed (requeued) job, or None when the caller must fail
    the job honestly: state not routable, per-job ceiling reached, or no
    alternative backend registered. Side effects on success, in timeline
    order: BACKEND_ATTEMPT (the failed backend, its §15 state), the job
    requeued with its model_ref repointed (attempts untouched -- a reroute
    is not a retry), and MISSION_REROUTED with from/to/reason plus the
    stable Chinese copy.

    ``quota_pools``（MODEL-001, 2026-10-16）透传给 pick_backend：响应式
    故障转移也吃池健康序——冷却中/水量枯竭的池沉到队尾，不再被选为
    reroute 目标。None 保持既有挑选序（可选增强，不改变无池行为）。
    """
    if state not in ROUTABLE_STATES:
        return None
    mission_id = job["mission_id"]
    owner = (mission or {}).get("owner_id", "admin")
    if reroute_count(store, mission_id, job["id"]) >= MAX_REROUTES_PER_JOB:
        return None
    models = registries.list("models", owner_id=owner)
    target = pick_backend(mission, failed_backend=failed_backend,
                          registry_models=models, quota_pools=quota_pools)
    if target is None:
        return None
    model_ref = opencode_model_ref(models.get(target) or {})
    label = STATE_LABELS.get(state, state)
    copy_text = reroute_copy(failed_backend, target, state)
    record_attempt(store, mission_id, failed_backend, state)
    store.reroute_job(job["id"], model_ref, reroute_error(target, copy_text))
    store.append(mission_id, REROUTED_EVENT,
                 {"job_id": job["id"], "from": failed_backend, "to": target,
                  "state": state, "reason": f"{label}（{state}）",
                  "model_ref": model_ref, "message": copy_text},
                 job.get("step_id"), None, (mission or {}).get("agent_id") or "opencode")
    return store.get_job(job["id"])
