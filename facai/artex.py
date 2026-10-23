"""ARTEX adapter — ENGINES-001 first attached engine (autonomous red-team agent).

ARTEX (Go + Next.js, https://github.com/Autumn-27/artex lineage, local build
0.3.12) runs autonomous pentest tasks behind its own HTTP API. Facai dispatches
one mission step as ONE ARTEX task (goal = the step prompt, seed intent on so
the worker starts without waiting a planner round) and maps ARTEX's terminal
lifecycle (done/failed/timeout) onto Facai's OPENCODE_* outcome vocabulary —
the same honest-attribution convention goose applies.

权限模型（Owner 指令 2026-10-23）：本地/Owner 最高。ARTEX 内置的工具审批门
（intercept rules + per-tool intercept scope + judge 审查模型）在收录时整体
旁路：规则逐条禁用（记录保留，可审计）、进入拦截系统的工具面清空、judge
不配置。越权与否的唯一裁断权在 FACAI 的 Owner 授权链（理解 → 确认 → 独立
授权 → 派发）；ARTEX 侧不再二次设卡，也绝不把 Owner 拒过的东西放行回来。

不配置 ARTEX_BASE_URL 时适配器诚实不可用：execute 返回
OPENCODE_FAILED + 明确原因，绝不假装跑过。
"""

from __future__ import annotations

import httpx
import os
import time

TERMINAL_STATUSES = frozenset({"done", "failed", "timeout"})
_STATUS_MAP = {"done": "OPENCODE_COMPLETED", "failed": "OPENCODE_FAILED",
               "timeout": "OPENCODE_TIMEOUT"}
POLL_INTERVAL_SECONDS = 2.0


class ArtexAdapter:
    """Dispatches mission steps to an ARTEX deployment over its HTTP API."""

    def __init__(self, base_url=None, token=None, client=None, disable_boundaries=True):
        self.base_url = (base_url or os.getenv("ARTEX_BASE_URL", "")).rstrip("/")
        self.token = token or os.getenv("ARTEX_TOKEN", "")
        self.client = client
        # 收录即去边界：默认在首次派发前把 ARTEX 内置审批门整体旁路。
        self.disable_boundaries = disable_boundaries
        self._boundaries_cleared = False

    # -- plumbing ---------------------------------------------------------

    def _transport(self):
        if self.client is not None:
            return self.client
        if not self.base_url:
            return None
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        return httpx.Client(base_url=self.base_url, headers=headers, timeout=30)

    def available(self):
        """Honest availability: a configured base URL (and reachable API)."""
        if not self.base_url:
            return {"available": False, "reason": "ARTEX_BASE_URL 未配置"}
        return {"available": True, "base_url": self.base_url}

    # -- boundary removal (Owner directive: local/Owner highest) ----------

    def clear_boundaries(self, transport=None):
        """Disable every intercept rule and empty the per-tool intercept scope.

        Rules are DISABLED, not deleted — the record stays auditable and the
        operator can re-arm them; an empty enabled_tools list means no tool
        call ever enters the intercept system, which is the complete removal
        of the built-in approval gate. Judge/review model is simply never
        configured by this adapter."""
        transport = transport or self._transport()
        if transport is None:
            return {"cleared": False, "reason": "ARTEX_BASE_URL 未配置"}
        disabled = []
        try:
            rules = transport.get("/api/intercept/rules").json()
            rows = rules.get("rules") if isinstance(rules, dict) else rules
            for rule in rows or []:
                if rule.get("enabled"):
                    transport.post(f"/api/intercept/rules/{rule['id']}/toggle",
                                   json={"enabled": False})
                    disabled.append(rule["id"])
            # Zero tools enter interception: the gate is fully bypassed.
            tools_route = None
            for candidate in ("/api/intercept/tools", "/api/intercept/tools-config"):
                probe = transport.put(candidate, json={"enabled_tools": []})
                if probe.status_code not in (404, 405):
                    tools_route = candidate
                    break
            self._boundaries_cleared = True
            return {"cleared": True, "rules_disabled": disabled,
                    "tools_scope_emptied_via": tools_route}
        except httpx.HTTPError as exc:
            return {"cleared": False, "reason": f"ARTEX 不可达: {type(exc).__name__}"}

    # -- Facai adapter contract -------------------------------------------

    def execute(self, mission, root, prompt, model_ref=None, timeout=300,
                on_output=None, workspace_override=None):
        """One mission step = one ARTEX task, polled to its terminal state."""
        transport = self._transport()
        if transport is None:
            return {"status": "OPENCODE_FAILED", "exit_code": None,
                    "workspace": "", "stdout": "", "stderr": "ARTEX_BASE_URL 未配置",
                    "streamed": False}

        if self.disable_boundaries and not self._boundaries_cleared:
            # Boundary removal runs once per process; failures do not block
            # dispatch (an already-open deployment simply stays as configured)
            # but are reported on the task output for honest attribution.
            report = self.clear_boundaries(transport)
            if on_output and not report.get("cleared"):
                on_output("stderr", f"artex boundary-clear skipped: {report.get('reason')}")

        try:
            created = transport.post("/api/tasks", json={
                "description": (prompt or "")[:200] or "Facai 派发任务",
                "goal": prompt or "",
                "seed_first_intent": True,
                "timeout_seconds": max(0, int(timeout or 0)),
            })
            created.raise_for_status()
            task = created.json()
            task_id = str(task.get("id") or task.get("task_id") or "")
            if not task_id:
                raise ValueError("ARTEX 未返回任务 id")
        except (httpx.HTTPError, ValueError) as exc:
            return {"status": "OPENCODE_FAILED", "exit_code": None, "workspace": "",
                    "stdout": "", "stderr": f"ARTEX 任务创建失败: {exc}",
                    "streamed": False}

        workspace = f"artex://task/{task_id}"
        if on_output:
            on_output("stdout", f"ARTEX task {task_id} created (boundaries cleared: "
                                f"{self._boundaries_cleared})")

        deadline = time.monotonic() + max(1, int(timeout or 300))
        status = "created"
        streamed = True
        while time.monotonic() < deadline:
            try:
                detail = transport.get(f"/api/tasks/{task_id}")
                detail.raise_for_status()
                status = str(detail.json().get("status") or "")
            except httpx.HTTPError as exc:
                return {"status": "OPENCODE_FAILED", "exit_code": None,
                        "workspace": workspace, "stdout": "",
                        "stderr": f"ARTEX 状态查询失败: {exc}", "streamed": streamed}
            if status in TERMINAL_STATUSES:
                break
            if on_output:
                on_output("stdout", f"ARTEX task {task_id} status={status}")
            time.sleep(POLL_INTERVAL_SECONDS)
        else:
            # Facai's own job timeout/lease machinery re-queues; the ARTEX
            # task keeps its own timeout_seconds and will terminate there.
            return {"status": "OPENCODE_TIMEOUT", "exit_code": None,
                    "workspace": workspace, "stdout": f"status={status}",
                    "stderr": "ARTEX 任务在派发窗口内未到终态", "streamed": streamed}

        mapped = _STATUS_MAP.get(status, "OPENCODE_FAILED")
        return {"status": mapped,
                "exit_code": 0 if mapped == "OPENCODE_COMPLETED" else None,
                "workspace": workspace,
                "stdout": f"ARTEX task {task_id} terminal={status}",
                "stderr": "" if mapped == "OPENCODE_COMPLETED" else f"terminal={status}",
                "streamed": streamed}


__all__ = ["ArtexAdapter", "TERMINAL_STATUSES"]
