"""Shared mission-task execution used by both the synchronous API and the scheduler.

The sequence is identical in both paths: open a mission session, run OpenCode in
the mission workspace, capture raw output as artifacts and events, record tokens
and cost, make a Git checkpoint, and finalize the step and session with the real
outcome. Nothing here fabricates success.
"""

import json
import hashlib
import threading
from pathlib import Path

from .authority import DENY
from .authority_gate import (
    GATE_BACKEND,
    MISSION_RUN,
    MissionDenied,
    authorize_for,
    deny_copy,
    ensure_mission_grant,
)
from .billing import PRICING, priced_cost
from .checkpoint import AUTHOR, _run as _git_run
from .checkpoint import checkpoint, remove_worktree
from .opencode import resolve_override_workspace, summarize_output
from .outcomes import (
    EXECUTION_FAILED,
    TERMINAL_EVENT_STATES,
    normalize_local_decision,
    outcome_from_adapter_error,
)

OUTPUT_LINE_LIMIT = 200
TERMINAL_STATUSES = frozenset({"OPENCODE_COMPLETED", "OPENCODE_FAILED", "OPENCODE_TIMEOUT"})

# Merges into the shared mission workspace are serialized per process: two
# parallel branch threads must not race on the base repository's index.
_MERGE_LOCK = threading.Lock()


def opencode_model_ref(model):
    if not model:
        return None
    if model.get("opencode_model"):
        return model["opencode_model"]
    provider, name = model.get("provider"), model.get("model")
    if provider and name:
        return f"{provider.lower()}/{name}"
    return name


def mission_attribution(mission, model, runtime=None, skill=None, model_ref=None):
    """The provider/model/runtime/skill identity attached to every task event.

    The console reads these exact fields. Skill fields currently identify
    resolved registry metadata; importing a SKILL.md does not yet connect its
    body to the adapter, so attribution cannot certify skill invocation.
    """
    model = model or {}
    runtime = runtime or {}
    skill = skill or {}
    attribution = {
        "agent_id": mission.get("agent_id"),
        "provider": model.get("provider"),
        "model_id": mission.get("model_id"),
        "model_ref": model_ref,
        "runtime_id": mission.get("runtime_id"),
        "runtime_kind": runtime.get("kind"),
        "runtime_provider": runtime.get("provider"),
        "runtime_location": runtime.get("location"),
        # A bound but disabled/removed skill resolves to None at dispatch.
        # Do not report it as used merely because the mission still names it.
        "skill_id": mission.get("skill_id") if skill else None,
        "skill_name": skill.get("name"),
        "skill_version": skill.get("version"),
        "skill_invocation_verified": False if skill else None,
    }
    return {key: value for key, value in attribution.items() if value is not None}


def skill_runtime_prompt(store, mission, skill, prompt):
    """Attach only the exact owner-scoped, imported Skill body to a run."""
    if not skill or not skill.get("body_sha256"):
        return prompt, None
    digest = skill["body_sha256"]
    body = store.skill_source_get(mission["skill_id"], digest,
                                  owner_id=mission.get("owner_id", "admin"),
                                  version=skill.get("version") or "")
    if body is None or hashlib.sha256(body.encode("utf-8")).hexdigest() != digest:
        raise ValueError("Bound Skill source is unavailable or corrupted")
    return (f"{prompt}\n\n[BOUND FACAI SKILL: {mission['skill_id']}"
            f" version={skill.get('version') or 'unspecified'} sha256={digest}]\n"
            "Apply this capability without changing the user's original objective.\n"
            f"{body}\n[/BOUND FACAI SKILL]"), digest


def user_model_runtime(store, mission, model, workspace, root=None):
    """Materialize a client-supplied model for one OpenCode run.

    The user's endpoint/key never touches a command line or the mission's
    committed files: the project-level ``opencode.json`` references the key by
    environment name (``{env:FACAI_USER_MODEL_KEY}``), the key itself rides in
    the child process environment only, and the config is excluded from git so
    a checkpoint can never capture it. Returns ``(env, config_path)`` or
    ``(None, None)`` for operator-owned models.
    """
    ref = (model or {}).get("user_model_id")
    if not ref:
        return None, None
    owner = mission.get("owner_id", "admin")
    token = store.user_model_encrypted_key(owner, ref)
    if not token:
        return None, None
    from .secretbox import decrypt

    key = decrypt(root, token)
    base = (model or {}).get("endpoint", "").rstrip("/")
    model_name = (model or {}).get("model")
    if not base or not model_name:
        return None, None
    config = {"provider": {"facai-user": {
        "npm": "@ai-sdk/openai-compatible",
        "name": (model or {}).get("name") or "Facai User Model",
        "options": {"baseURL": base, "apiKey": "{env:FACAI_USER_MODEL_KEY}"},
        "models": {model_name: {"name": (model or {}).get("name") or model_name}}}}}
    ws = Path(workspace)
    config_path = ws / "opencode.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8")
    git_info = ws / ".git" / "info"
    try:
        git_info.mkdir(parents=True, exist_ok=True)
        exclude = git_info / "exclude"
        marker = "opencode.json"
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if marker not in existing.splitlines():
            updated = existing.rstrip("\r\n") + "\n" + marker + "\n" if existing else marker + "\n"
            exclude.write_text(updated, encoding="utf-8")
    except OSError:
        pass
    return {"FACAI_USER_MODEL_KEY": key}, str(config_path)


def _served_workspace(path, root):
    """True when *path* is an individual mission workspace the artifacts API serves."""
    try:
        base = (Path(root) / "workspaces").resolve()
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        return False
    return resolved != base and base in resolved.parents


def _persist_output(mission_id, store, result, session_id, log_dir=None, name_prefix=""):
    """Persist raw stdout/stderr as artifacts and return their names.

    ``log_dir`` redirects the log files away from the run's own workspace: a
    worktree is deleted after a successful merge, which would leave the
    artifacts center with rows whose files 404 forever, so worktree runs write
    into the shared mission workspace instead. ``name_prefix`` (the step id)
    keeps those shared-dir entries attributable per step; session ids keep
    retries of the same step from overwriting each other.
    """
    directory = Path(log_dir) if log_dir is not None else Path(result["workspace"]) / ".facai"
    directory.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for stream in ("stdout", "stderr"):
        text = result.get(stream) or ""
        if not text:
            continue
        path = directory / f"{name_prefix}opencode-{session_id}.{stream}.log"
        path.write_text(text, encoding="utf-8")
        store.add_artifact(mission_id, path.name, path)
        artifacts.append(path.name)
    return artifacts


def _append_process_output(mission_id, store, result, step_id, session_id, agent_id):
    lines = [("OUTPUT", {"stream": "stdout", "line": line}) for line in (result.get("stdout") or "").splitlines()]
    lines += [("ERROR", {"stream": "stderr", "line": line}) for line in (result.get("stderr") or "").splitlines()]
    for event_type, payload in lines[:OUTPUT_LINE_LIMIT]:
        store.append(mission_id, event_type, payload, step_id, session_id, agent_id)
    if len(lines) > OUTPUT_LINE_LIMIT:
        store.append(mission_id, "OUTPUT_TRUNCATED", {"omitted": len(lines) - OUTPUT_LINE_LIMIT},
                     step_id, session_id, agent_id)


def _adapter_result_problem(result, root):
    """Describe why an adapter result is unusable, or None when it is well formed.

    The adapter contract is a mapping with a mission workspace path, a terminal
    status, an exit code and text streams. Anything else is rejected before it is
    persisted, because a malformed result would otherwise raise deep in output
    capture or checkpointing after the session had already started.
    """
    if not isinstance(result, dict):
        return "adapter result is not an object"
    workspace = result.get("workspace")
    if not isinstance(workspace, str) or not workspace.strip():
        return "adapter result has no workspace"
    try:
        resolved = Path(workspace).resolve()
    except (OSError, ValueError):
        return "adapter result has an invalid workspace"
    base = (Path(root) / "workspaces").resolve()
    # The shared workspace root itself is not an individual mission workspace.
    if resolved == base or base not in resolved.parents:
        return "adapter result workspace is outside the mission root"
    status = result.get("status")
    if not isinstance(status, str) or status not in TERMINAL_STATUSES:
        return f"adapter result has an invalid status: {status!r}"
    if "exit_code" not in result:
        return "adapter result has no exit code"
    exit_code = result["exit_code"]
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        return f"adapter result has an invalid exit code: {exit_code!r}"
    if status == "OPENCODE_COMPLETED" and exit_code != 0:
        return "completed adapter result must have exit code 0"
    if status == "OPENCODE_FAILED" and (exit_code is None or exit_code == 0):
        return "failed adapter result must have a nonzero exit code"
    if status == "OPENCODE_TIMEOUT" and exit_code is not None:
        return "timed-out adapter result must have no exit code"
    for stream in ("stdout", "stderr"):
        value = result.get(stream)
        if value is not None and not isinstance(value, str):
            return f"adapter result has an invalid {stream}"
    return None


def _fail_malformed_result(store, mission_id, session_id, step_id, attribution, problem,
                           agent_id="opencode"):
    """Finalize a started session as failed without consuming a bad result.

    The session exists and the step may already be RUNNING, so the failure is
    recorded here even if the caller's exception is lost: session and step become
    FAILED and an OPENCODE_FAILED event carries the real reason.
    """
    try:
        store.session_status(mission_id, session_id, "FAILED")
    except (KeyError, ValueError):
        pass
    if step_id:
        try:
            store.step_status(mission_id, step_id, "FAILED")
        except (KeyError, ValueError):
            pass
    store.append(mission_id, "OPENCODE_FAILED",
                 {**attribution, "error": problem,
                  # §15: the backend produced an unusable result — an
                  # execution failure, never a refusal by anyone.
                  "outcome_state": EXECUTION_FAILED},
                 step_id, session_id, agent_id)


def merge_worktree(store, base_workspace, worktree, mission_id, step_id=None):
    """Merge an isolated worktree branch back into the shared mission workspace.

    A successful merge removes the worktree (its branch is folded into history)
    and returns ``{"merged": True, "stat": ...}``. A conflicting merge is
    aborted and reported as ``{"merged": False, "error": "CONFLICT"}`` with the
    worktree kept for inspection, so neither branch's work is silently lost; the
    caller then fails the step honestly. Never raises.
    """
    branch = (worktree or {}).get("branch") or ""
    try:
        base = Path(base_workspace)
        if not base.is_dir() or not branch:
            return {"merged": False, "error": "worktree missing"}
        with _MERGE_LOCK:
            merged = _git_run([*AUTHOR, "merge", "--no-ff", "-m", f"Facai merge: {branch}",
                               branch], base)
            if merged.returncode != 0:
                # Undo the half-open merge; with no merge in progress this is a
                # harmless no-op. The worktree and branch stay for inspection.
                _git_run(["merge", "--abort"], base)
                return {"merged": False, "error": "CONFLICT"}
            # Wide stat: the merge evidence must carry the whole step filename —
            # git's default 80-column stat "..."-abbreviates the long
            # <mission_id>-step-<...> names and breaks audit traceability.
            stat = _git_run(["show", "--stat", "--stat-width=420",
                             "--stat-name-width=350", "--format=", "HEAD"],
                            base).stdout.strip()
            remove_worktree(worktree.get("path"), base=base)
        return {"merged": True, "stat": stat}
    except Exception as exc:
        return {"merged": False, "error": type(exc).__name__}


def execute_mission_task(store, adapter, mission, model, root, prompt, step_id=None,
                         model_ref=None, timeout=300, runtime=None, skill=None,
                         step_model_id=None, agent_id="opencode",
                         workspace_override=None, worktree=None):
    """Run one OpenCode task for a mission and record every outcome. Raises on adapter failure.

    ``step_model_id`` names a per-step model binding that overrides the mission
    default for this task: attribution and the cost ledger then name the model
    that actually ran instead of the mission-wide binding.

    ``worktree`` (with ``workspace_override``) runs this task in an isolated git
    worktree of the mission workspace: the task checkpoints there and its branch
    is then merged back into the shared workspace behind a merge gate. A merge
    conflict fails the step while keeping the worktree for inspection.
    """
    mission_id = mission["id"]
    agent_id = agent_id or mission.get("agent_id") or "opencode"
    model_ref = model_ref or opencode_model_ref(model)
    attribution = mission_attribution(mission, model, runtime, skill, model_ref)
    if step_model_id:
        attribution["model_id"] = step_model_id
    step = next((s for s in mission["steps"] if s["id"] == step_id), None)
    if step is not None and step["status"] == "COMPLETED":
        # Refuse before spending a model call, creating a session or writing a
        # checkpoint: a completed step cannot be completed a second time, and
        # the store would reject the final transition after the work had already run.
        raise ValueError("Step is already completed")
    # Authorization precedes every side effect. 彻底无审核（2026-10-07 操作员指令）:
    # a missing/expired/exhausted grant no longer refuses — the platform core
    # re-signs the mission grant in place (a mission still in the store is
    # legitimate by construction) and execution proceeds; the re-signing is
    # itself recorded as GRANT_AUTO_RENEWED. Only a still-DENY verdict (only
    # possible under FACAI_GATE_MODE=OWNER_GATE) raises MissionDenied through
    # the existing failure semantics (422 on the synchronous path, a permanent
    # job FAILED on the scheduler path).
    verdict = authorize_for(mission_id, actor=agent_id)
    if verdict["decision"] == DENY:
        ensure_mission_grant(mission_id)
        store.append(mission_id, "GRANT_AUTO_RENEWED",
                     {**attribution, "reason": verdict["reason"], "mode": verdict["mode"],
                      "note": "无审核模式：授权门自动补签，执行继续"},
                     step_id, None, agent_id)
        verdict = authorize_for(mission_id, actor=agent_id)
    if verdict["decision"] == DENY:
        store.append(mission_id, "MISSION_DENIED",
                     {**attribution, "reason": verdict["reason"], "mode": verdict["mode"],
                      "backend": GATE_BACKEND, "action": MISSION_RUN,
                      "message": deny_copy(verdict["reason"]),
                      # §15: a local deny before anything ran. Grant-shaped
                      # reasons are GRANT_REQUIRED; anything else local is
                      # FACAI_DENIED — neither is ever a provider refusal.
                      "outcome_state": normalize_local_decision(
                          verdict["reason"], backend=GATE_BACKEND).state},
                     step_id, None, agent_id)
        raise MissionDenied(verdict["reason"], verdict["mode"])
    runtime_prompt, skill_body_sha256 = skill_runtime_prompt(store, mission, skill, prompt)
    session_id = store.start_session(mission_id, "opencode", step_model_id or mission["model_id"])
    if worktree is not None:
        store.append(mission_id, "WORKTREE_CREATED",
                     {"path": worktree.get("path"), "branch": worktree.get("branch")},
                     step_id, session_id, agent_id)
    store.append(mission_id, "OPENCODE_STARTED",
                 {**attribution, "prompt": prompt},
                 step_id, session_id, agent_id)
    if step and step["status"] in {"WAITING", "FAILED"}:
        store.step_status(mission_id, step_id, "RUNNING")
    streamed_count = 0

    def on_output(stream, line):
        nonlocal streamed_count
        streamed_count += 1
        if streamed_count <= OUTPUT_LINE_LIMIT:
            store.append(mission_id, "OUTPUT" if stream == "stdout" else "ERROR",
                         {"stream": stream, "line": line}, step_id, session_id, agent_id)

    workspace = None
    if workspace_override:
        workspace = resolve_override_workspace(workspace_override, root)
    else:
        workspace = (Path(root) / "workspaces" / mission_id).resolve()
    user_env, user_config = user_model_runtime(store, mission, model, workspace, root=root)
    try:
        # The override kwarg is only passed when isolating, so adapters that
        # predate parallel worktrees keep working unchanged in the shared tree.
        override = {"workspace_override": workspace_override} if workspace_override else {}
        if user_config:
            override["env"] = user_env
        result = adapter.execute(mission_id, root, runtime_prompt, model_ref, timeout,
                                 on_output=on_output, **override)
    except ValueError:
        store.session_status(mission_id, session_id, "FAILED")
        raise
    except Exception as exc:  # adapter failed before producing a result
        store.session_status(mission_id, session_id, "FAILED")
        # §15 normalization: classify the exception into the nine states
        # (timeout / capability / resource / execution failure) so the event
        # carries a backend state instead of an undifferentiated error.
        outcome = outcome_from_adapter_error(exc, backend=agent_id)
        store.append(mission_id, "OPENCODE_FAILED",
                     {**attribution, "error": type(exc).__name__,
                      "outcome_state": outcome.state},
                     step_id, session_id, agent_id)
        raise

    if user_config:
        try:
            Path(user_config).unlink(missing_ok=True)
        except OSError:
            pass

    problem = _adapter_result_problem(result, root)
    if skill_body_sha256:
        attribution["skill_invocation_verified"] = True
        attribution["skill_body_sha256"] = skill_body_sha256
    if problem is not None:
        _fail_malformed_result(store, mission_id, session_id, step_id, attribution, problem,
                               agent_id)
        raise ValueError(problem)

    # A worktree's raw logs must outlive the worktree itself: merge success
    # deletes the directory, so they are persisted into the shared mission
    # workspace (which the artifacts API serves) under a step-prefixed name.
    log_dir, log_prefix = None, ""
    if worktree is not None:
        base_workspace = mission.get("workspace") or str(Path(root) / "workspaces" / mission_id)
        if _served_workspace(base_workspace, root):
            log_dir = Path(base_workspace) / ".facai"
            log_prefix = f"{step_id[:8]}-" if step_id else ""
    artifacts = _persist_output(mission_id, store, result, session_id,
                                log_dir=log_dir, name_prefix=log_prefix)
    if result.get("streamed"):
        if streamed_count > OUTPUT_LINE_LIMIT:
            store.append(mission_id, "OUTPUT_TRUNCATED", {"omitted": streamed_count - OUTPUT_LINE_LIMIT},
                         step_id, session_id, agent_id)
    else:
        _append_process_output(mission_id, store, result, step_id, session_id, agent_id)
    terminal = result["status"]
    succeeded = terminal == "OPENCODE_COMPLETED"
    summary = summarize_output(result.get("stdout"))
    # The customer-facing amount defaults to the agent-reported at-cost and is
    # replaced by the FACAI-standard priced amount whenever tokens exist.
    priced = summary["cost"]
    if summary["cost"] or summary["tokens"]:
        # OpenCode reports one combined token count per step, never an
        # input/output split, so the split columns stay NULL here: an
        # unreported side must not be recorded as a zero.
        #
        # Billing standard (operator-frozen $95/$218 per 1M): the LEDGER
        # records what the customer owes at FACAI rates, not what the
        # upstream charged us. The agent-reported cost rides the audit
        # event as agent_reported_usd for at-cost transparency.
        tokens = summary["tokens"]
        priced = (priced_cost(0, tokens)
                  or (tokens / 1_000_000) * PRICING["output_per_mtok_usd"])
        store.add_cost(mission_id, (model or {}).get("provider"),
                       step_model_id or mission["model_id"],
                       priced, tokens)
        store.append(mission_id, "COST_PRICED", {
            "agent_reported_usd": round(summary["cost"], 6),
            "facai_priced_usd": round(priced, 6),
            "tokens": tokens,
            "basis": "输入 $95 / 输出 $218 每 100 万 tokens（总量按输出价保守）",
        }, step_id, session_id, agent_id)
    message = f"Facai checkpoint: {step['label'] if step else 'task'}"
    ckpt = checkpoint(result["workspace"], message)
    checkpoint_hash = ckpt.get("hash") if ckpt.get("committed") else None
    if ckpt.get("committed"):
        store.add_checkpoint(mission_id, ckpt["hash"], message, ckpt.get("stat", ""))
    else:
        store.append(mission_id, "GIT_CHECKPOINT_SKIPPED",
                     {"reason": ckpt.get("reason")}, step_id, session_id, agent_id)
    # Merge gate: an isolated branch only counts when it merges back cleanly.
    # The session reflects the agent run itself; the step only completes when
    # the branch work actually landed in the shared workspace.
    merge_outcome = None
    if worktree is not None and succeeded:
        base_workspace = mission.get("workspace") or str((Path(root) / "workspaces" / mission_id))
        merge_outcome = merge_worktree(store, base_workspace, worktree, mission_id, step_id)
        if merge_outcome.get("merged"):
            store.append(mission_id, "WORKTREE_MERGED",
                         {"branch": worktree.get("branch"),
                          "result": merge_outcome.get("stat", "")},
                         step_id, session_id, agent_id)
        else:
            store.append(mission_id, "WORKTREE_MERGE_FAILED",
                         {"branch": worktree.get("branch"),
                          "error": merge_outcome.get("error")},
                         step_id, session_id, agent_id)
    step_succeeded = succeeded and (merge_outcome is None or merge_outcome.get("merged"))
    store.session_status(mission_id, session_id, "COMPLETED" if succeeded else "FAILED")
    if step_id:
        store.step_status(mission_id, step_id, "COMPLETED" if step_succeeded else "FAILED")
    store.append(mission_id, terminal,
                 {**attribution, "exit_code": result["exit_code"], "artifacts": artifacts,
                  "cost": priced, "agent_reported_usd": round(summary["cost"], 6),
                  "tokens": summary["tokens"],
                  "checkpoint": checkpoint_hash,
                  # §15: the terminal event names its nine-state —
                  # OPENCODE_COMPLETED->SUCCESS, OPENCODE_FAILED->EXECUTION_FAILED
                  # (the backend ran and exited nonzero; no one refused),
                  # OPENCODE_TIMEOUT->TIMEOUT.
                  "outcome_state": TERMINAL_EVENT_STATES[terminal]},
                 step_id, session_id, agent_id)
    return {"mission_id": mission_id, "session_id": session_id, "status": terminal,
            "exit_code": result["exit_code"], "workspace": result["workspace"],
            **attribution, "cost": priced, "agent_reported_usd": round(summary["cost"], 6),
            "tokens": summary["tokens"],
            "checkpoint": checkpoint_hash, "artifacts": artifacts,
            "stdout": result["stdout"], "stderr": result["stderr"],
            "worktree": worktree, "merge": merge_outcome}
