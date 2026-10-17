"""FACAI Conversion Operations Console (转换操作台) — the visualized pipeline
that turns one plain-language instruction into a governed, assigned, flowing
production line.

Operator doctrine (2026-10-12): 一条大白话指令进去 → 转换专业化提示词 →
按照指令分析适用什么环境以及工具 → 收录了多 Agent 模块，任务指令到达前
分配给谁最合适、谁的能力最强，细化逐级分配下去 → 流水线作业一个环节一个
环节流转；Agent 遇见需要补充情报或方向时逐级反馈回来给到大白话转化器，
弹出多选择解决方案与自填充答案，回复后下达继续流转开工。

Design invariants:

- DETERMINISTIC CORE: planning reuses facai.prompting's frozen five-phase
  plan; analysis and assignment are pure functions of the input text plus
  registry data, so the same instruction always yields the same pipeline.
  A bound model may already refine conversion elsewhere (/api/v1/missions/
  auto); the console never depends on one.
- WHO IS STRONGEST, DECIDED BY DATA: agent ranking scores the agent's
  adapter profile plus bigram overlap between the stage and every skill the
  station has distilled (facai.distill) — the more the station has absorbed,
  the sharper the assignment. The tree is hierarchical and explicit:
  primary → fallbacks → platform-core reviewer, with the matching reasons
  on the record.
- BOUNDARIES RIDE ALONG: a stage whose analysis requires remote access
  carries ``needs_owner_grant`` — remote API authority stays severed
  (facai.boundary) until the owner grants it locally. Assignment never
  widens permission.
- FEEDBACK FLOWS UP, PLAIN LANGUAGE FLOWS DOWN: an agent's professional
  "I need X" becomes a feedback gate with plain-language multi-choice
  options, each carrying an auto-filled professional answer; the user's one
  tap (or custom text) resumes the pipeline. The gate parks a stage; the
  resolution returns the exact professional line the re-dispatch carries.

The console itself owns no execution: stage truth stays in the store's
mission steps; the console overlays analysis, assignment and gates, and the
API layer re-dispatches with the resolved answer.
"""
from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime, timezone

from .prompting import MissionPlan
from .skills import enabled_only

GATE_KINDS = ("direction", "info_gap", "approval")
GATE_KIND_LABELS = {"direction": "方向选择", "info_gap": "补充情报",
                    "approval": "需要授权"}

# --- deterministic environment / tool analysis -------------------------------
# (env id, keywords). "local" is the implicit baseline and never listed twice.
_ENV_RULES = (
    ("vps", ("部署", "上线", "服务器", "nginx", "域名", "https", "ssl")),
    ("browser", ("网页", "前端", "页面", "浏览器", "截图", "ui", "html")),
    ("data", ("数据库", "表格", "csv", "excel", "统计", "报表", "数据")),
)

# Tool families reuse the frozen brokerable vocabulary verbatim
# (facai.capabilities.PERMISSION_FAMILIES): an analysis can only ever demand
# a family the authority system actually brokers, so assignment and grants
# can never drift apart.
_FAMILY_KEYWORDS = {
    "shell.exec": ("写代码", "开发", "修复", "编译", "运行", "脚本", "git", "提交", "命令"),
    "filesystem.write": ("生成文件", "写文件", "导出", "保存", "文档", "图表", "报表"),
    "network.*": ("爬", "抓取", "下载", "接口", "api", "网页", "查资料", "搜索", "请求"),
    "docker.run": ("容器", "docker", "镜像", "构建"),
    "secret.request": ("密钥", "密码", "token", "凭证", "登录"),
    "provider.invoke": ("模型", "ai", "gpt", "生成文案", "翻译", "总结"),
}

_RISK_HIGH = "high"
_RISK_MEDIUM = "medium"
_RISK_LOW = "low"

# --- assignment profiles -------------------------------------------------------
# (base score, matching keywords). The scores are the deterministic floor;
# distilled skills add on top. opencode is the engineering-grade CLI, goose
# the automation runner — the same two adapters the station ships.
_AGENT_PROFILES = {
    "opencode": (1.0, ("代码", "开发", "修复", "编译", "部署", "脚本", "git", "测试", "工程")),
    "goose": (0.8, ("自动化", "抓取", "监控", "批量", "轮询", "整理")),
}
_SKILL_BONUS = 2.0
_KEYWORD_BONUS = 0.5
_BIGRAM = 2


def _now():
    return datetime.now(timezone.utc).isoformat()


def _bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"\s+", "", (text or "").lower())
    return {cleaned[i:i + _BIGRAM] for i in range(max(0, len(cleaned) - _BIGRAM + 1))}


def analyze_environment(text: str) -> dict:
    """What environment and tool families does this instruction need?

    Pure substring matching over the instruction plus (implicitly) its
    professional expansion — deliberately coarse, deliberately explainable:
    every verdict carries the keyword that fired it.
    """
    haystack = (text or "").lower()
    environments = ["local"]
    evidence = {"local": ["默认本机工作区"]}
    for env_id, keywords in _ENV_RULES:
        hits = [k for k in keywords if k in haystack]
        if hits:
            environments.append(env_id)
            evidence[env_id] = hits
    families = []
    for family, keywords in _FAMILY_KEYWORDS.items():
        hits = [k for k in keywords if k in haystack]
        if hits:
            families.append({"family": family, "evidence": hits})
    risk = _RISK_LOW
    matched = {item["family"] for item in families}
    if "shell.exec" in matched or "docker.run" in matched:
        risk = _RISK_HIGH
    elif matched:
        risk = _RISK_MEDIUM
    return {"environments": environments, "environment_evidence": evidence,
            "tool_families": families, "risk": risk,
            "remote_access_required": "network.*" in matched,
            "note": ("该任务需要远程访问：远程端 API 权限默认切断，"
                     "需所有者在本地 /owner 签发后才能放行"
                     if "network.*" in matched else "无需远程访问，本地权限即可执行")}


def _skill_score(label: str, professional: str, skill: dict) -> float:
    """Bigram overlap between the stage text and one distilled skill record.

    正文窗口取前 400 字：模板开场白（保留用户原意…）不承载需求差异，
    用户正文与技能描述的对齐通常发生在 120 字之后。
    """
    needle = _bigrams(label) | _bigrams(professional[:400])
    corpus = " ".join([str(skill.get("description", "")),
                       " ".join(skill.get("steps") or [])])
    haystack = _bigrams(corpus)
    if not needle or not haystack:
        return 0.0
    return len(needle & haystack) / len(needle)


def assign_stage(label: str, professional: str, agents: dict,
                 skills: dict) -> dict:
    """Who is strongest for this stage — ranked, with reasons on the record.

    Score = adapter base + keyword hits + best distilled-skill overlap.
    Registry agents without a profile still rank (base 0.5) so an operator's
    custom agent never silently disappears from the tree; ties break on the
    agent id, keeping the assignment deterministic.

    SKILL-001 ②: a disabled skill (``enabled is False``, absent means
    enabled for legacy rows) never participates in scoring — the operator's
    disable switch takes effect at routing time, not just in bookkeeping.
    """
    routable = enabled_only(skills)
    ranked = []
    for agent_id, entry in sorted((agents or {}).items()):
        adapter = str(entry.get("adapter", agent_id))
        base, keywords = _AGENT_PROFILES.get(adapter, (0.5, ()))
        keyword_hits = [k for k in keywords if k in (label or "").lower()]
        best_skill, best_skill_id = 0.0, ""
        for skill_id, skill in routable.items():
            score = _skill_score(label, professional, skill or {})
            if score > best_skill:
                best_skill, best_skill_id = score, skill_id
        score = base + _KEYWORD_BONUS * len(keyword_hits) + _SKILL_BONUS * best_skill
        reasons = [f"适配器基准 {base:g}"]
        if keyword_hits:
            reasons.append(f"关键词命中 {','.join(keyword_hits)}")
        if best_skill_id:
            reasons.append(f"技能库匹配 {best_skill_id}（重叠 {best_skill:.2f}）")
        ranked.append({"agent_id": agent_id, "adapter": adapter,
                       "name": entry.get("name", agent_id), "score": round(score, 4),
                       "reasons": reasons,
                       "matched_skill_id": best_skill_id or None,
                       "matched_skill_score": round(best_skill, 4)})
    ranked.sort(key=lambda row: (-row["score"], row["agent_id"]))
    primary = ranked[0] if ranked else None
    fallbacks = ranked[1:3]
    return {"primary": primary, "fallbacks": fallbacks,
            # 全量候选（含各自分数与 reasons）：展示层仍只读 primary/fallbacks，
            # 这份完整名单供 ASSIGNMENT_RECORDED 事件做事实源（ROUTER-001）。
            "candidates": ranked,
            "reviewer": {"agent_id": "facai-native", "name": "平台核（T0）",
                         "role": "阶段产物复核：越权/偏差行为由平台核拦下"}}


# --- ROUTER-001: assignment decisions become durable events -------------------
# 评分分配快照曾经只活在 ConversionConsole._pipelines（进程内存 dict，重启即
# 失）。ASSIGNMENT_RECORDED 事件才是事实源；内存 pipeline 降级为展示加速。
ASSIGNMENT_EVENT = "ASSIGNMENT_RECORDED"
# ROUTER-001 ③: 分配树首选（如 goose）与实际派发（opencode）不一致时的解释
# 事件——展示与实际的分歧从此有留痕，不再静默。
AGENT_OVERRIDDEN_EVENT = "AGENT_OVERRIDDEN"
# 紧凑性上限：单事件里每阶段最多保留这么多候选，超出截断并以 truncated 标注。
ASSIGNMENT_EVENT_AGENT_CAP = 8


def _compact_candidate(row: dict) -> dict:
    """One ranked candidate as a lean event entry: 分数两位小数，reasons 原文。"""
    return {"agent_id": row.get("agent_id"), "adapter": row.get("adapter"),
            "score": round(float(row.get("score") or 0.0), 2),
            "matched_skill_id": row.get("matched_skill_id"),
            "matched_skill_score": row.get("matched_skill_score", 0),
            "reasons": list(row.get("reasons") or [])}


def assignment_event_payload(assignment_tree, mission_id: str = "") -> dict:
    """Compact durable snapshot of the per-stage scoring decision.

    每阶段给出 chosen（选中者）、candidates（候选列表：分数 + 评分理由）、
    fallbacks / excluded（排除者及排除原因）与 truncated 截断标注——这正是
    next_action 要的"记录候选/排除原因"，且事件表可跨重启审计。
    """
    stages = []
    for row in assignment_tree or []:
        assignment = row.get("assignment") or {}
        ranked = list(assignment.get("candidates") or [])
        primary = assignment.get("primary") or {}
        chosen_id = primary.get("agent_id")
        fallbacks = []
        for item in assignment.get("fallbacks") or []:
            entry = _compact_candidate(item)
            entry["reason"] = "评分次于首选，列为后备（不执行本阶段）"
            fallbacks.append(entry)
        fallback_ids = [entry["agent_id"] for entry in fallbacks]
        kept = ranked[:ASSIGNMENT_EVENT_AGENT_CAP]
        excluded = []
        for cand in kept:
            if cand.get("agent_id") in (chosen_id, *fallback_ids):
                continue
            entry = _compact_candidate(cand)
            entry["reason"] = "评分未进首选与后备，本阶段落选"
            excluded.append(entry)
        stages.append({
            "stage": row.get("stage"),
            "chosen": chosen_id,
            "candidates": [_compact_candidate(cand) for cand in kept],
            "fallbacks": fallbacks,
            "excluded": excluded,
            "truncated": len(ranked) > ASSIGNMENT_EVENT_AGENT_CAP,
        })
    return {"mission_id": mission_id, "stages": stages}


def _sub_tasks(families: list[str]) -> list[dict]:
    """The stage's hierarchical decomposition, boundaries included."""
    subs = [{"sub_task": "环境与输入核验", "owner": "primary",
             "note": "确认工作区、依赖与输入完整"}]
    if "network.*" in families:
        subs.append({"sub_task": "远程访问子任务", "owner": "primary",
                     "needs_owner_grant": True,
                     "note": "远程端 API 权限默认切断；需本地 /owner 签发后执行"})
    subs.append({"sub_task": "主执行", "owner": "primary", "note": "阶段主体工作"})
    subs.append({"sub_task": "结果复核", "owner": "reviewer",
                 "note": "平台核复核阶段产物与权限边界"})
    return subs


def build_assignment(steps, agents: dict, skills: dict,
                     professional: str = "") -> list[dict]:
    """The full per-stage assignment tree for a plan's steps.

    ``professional`` rides into skill scoring so routing is content-aware:
    阶段标签只有五阶段词汇，真正区分"应用构建/模型巡检/提示词优化"的
    是专业提示词正文与技能库描述的重叠。
    """
    tree = []
    for step in steps:
        label = step["label"] if isinstance(step, dict) else str(step)
        analysis = analyze_environment(label)
        assignment = assign_stage(label, professional, agents, skills)
        tree.append({
            "stage": label,
            "assignment": assignment,
            "sub_tasks": _sub_tasks([item["family"] for item in analysis["tool_families"]]),
            "remote_access_required": analysis["remote_access_required"],
        })
    return tree


# --- feedback gates: plain language down, professional back up -----------------
# Each option: (plain-language label, auto-filled professional answer). The
# answer is what the re-dispatch prompt carries verbatim — the user taps one
# button and the pipeline resumes with a precise, audited instruction.
GATE_OPTIONS = {
    "direction": (
        ("按原方向继续", "按既定方向继续执行，不需要额外输入。"),
        ("改用最稳妥的方案继续", "改用最稳妥、改动最小的方案继续；完成后在交付说明中列出取舍。"),
        ("让 Agent 自行判断", "信息不足时按业界最佳实践自行判断；完成后在交付说明中列出全部假设。"),
        ("暂停该环节，等我补充", "暂停当前环节，等待用户补充后再继续。"),
    ),
    "info_gap": (
        ("我来补充（填在下面）", "以下为用户补充的情报，请据此继续："),
        ("先用占位信息继续", "暂缺信息用明确的占位内容继续推进，交付说明中列出待补清单。"),
        ("跳过该环节", "跳过当前环节直接进入下一阶段；交付说明中说明跳过的影响。"),
        ("终止流水线", "终止流水线，保留已完成部分并输出现状说明。"),
    ),
    "approval": (
        ("批准（按最小权限继续）", "批准：按最小权限原则继续，仅使用完成任务所必需的访问。"),
        ("不批准，改用本地替代方案", "不批准远程/特权访问；改用本地可完成的替代方案。"),
    ),
}

GATE_TERMINAL_CHOICES = {"info_gap": 3}   # 终止流水线 → pipeline CLOSED
GATE_RESUME_HINT = {
    "direction": "方向已确认，继续流转",
    "info_gap": "情报已补充，继续流转",
    "approval": "授权裁决已下达，继续流转",
}


class ConsoleError(ValueError):
    """Raised when a console operation would break the pipeline invariants."""


class ConversionConsole:
    """Thread-safe pipeline registry: one plain instruction, one pipeline."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pipelines: dict[str, dict] = {}

    def open_pipeline(self, plan: MissionPlan, mission_id: str, owner_id: str,
                      steps: list[dict], agents: dict, skills: dict,
                      original_input: str = "", channel: str = "console") -> dict:
        """Register the pipeline for a mission the caller already created.

        ``steps`` are the store's step rows (id + label) — the store stays the
        single source of stage truth; the console overlays analysis, assignment
        and gates on those ids.
        """
        analysis = analyze_environment(f"{original_input}\n{plan.professional}")
        assignment = build_assignment(plan.steps, agents, skills,
                                      professional=plan.professional)
        stages = {}
        for step in steps:
            label = step["label"]
            tree = next((row for row in assignment if row["stage"] == label),
                        {"stage": label, "assignment": {"primary": None,
                         "fallbacks": [], "reviewer": {}},
                         "sub_tasks": [], "remote_access_required": False})
            stages[label] = {"step_id": step["id"], "assignment": tree}
        pipeline = {
            "id": uuid.uuid4().hex, "mission_id": mission_id,
            "owner_id": owner_id, "channel": channel,
            "created_at": _now(), "status": "RUNNING",
            "title": plan.title, "original_input": original_input,
            "professional_prompt": plan.professional, "basis": list(plan.basis),
            "plan_source": plan.source, "analysis": analysis,
            "stage_order": list(stages), "stages": stages,
            "gates": {}, "assignment_tree": assignment,
        }
        with self._lock:
            self._pipelines[pipeline["id"]] = pipeline
        return self.snapshot(pipeline["id"])

    def get(self, pipeline_id: str) -> dict | None:
        with self._lock:
            pipeline = self._pipelines.get(pipeline_id)
            return dict(pipeline) if pipeline else None

    def for_mission(self, mission_id: str) -> dict | None:
        with self._lock:
            for pipeline in self._pipelines.values():
                if pipeline["mission_id"] == mission_id:
                    return dict(pipeline)
            return None

    def list_for_owner(self, owner_id: str) -> list[dict]:
        """Home-screen rows for one owner, newest first.

        Deliberately light: the per-pipeline snapshot endpoint serves stages
        and gates; the list carries only what a pipeline card needs to show.
        """
        with self._lock:
            rows = [dict(pipeline) for pipeline in self._pipelines.values()
                    if pipeline["owner_id"] == owner_id]
        for row in rows:
            row.pop("stages", None)
            row.pop("assignment_tree", None)
            row["stage_count"] = len(row.pop("stage_order", ()))
        rows.sort(key=lambda p: p.get("created_at", ""), reverse=True)
        return rows

    def open_gate(self, pipeline_id: str, stage_label: str, kind: str,
                  question: str, asked_by: str = "agent") -> dict:
        """Park a stage and surface plain-language multi-choice options.

        The agent's professional question is recorded as-is; the OPTIONS are
        the plain-language layer — one tap picks a solution, the auto-filled
        professional answer rides back down on resume.
        """
        if kind not in GATE_KINDS:
            raise ConsoleError(f"unknown gate kind: {kind}")
        if not str(question or "").strip():
            raise ConsoleError("反馈问题不能为空")
        with self._lock:
            pipeline = self._pipelines.get(pipeline_id)
            if pipeline is None:
                raise KeyError(pipeline_id)
            if stage_label not in pipeline["stages"]:
                raise ConsoleError(f"阶段不在该流水线中: {stage_label}")
            if pipeline["stages"][stage_label].get("gate_id"):
                raise ConsoleError(f"该环节已有待裁决的反馈门: {stage_label}")
            if pipeline["status"] != "RUNNING":
                raise ConsoleError(f"流水线已结束（{pipeline['status']}），不能再开反馈门")
            gate_id = uuid.uuid4().hex
            gate = {
                "id": gate_id, "stage": stage_label, "kind": kind,
                "kind_label": GATE_KIND_LABELS[kind],
                "question": str(question).strip(), "asked_by": asked_by,
                "options": [{"index": i, "label": label, "auto_fill": answer}
                            for i, (label, answer) in enumerate(GATE_OPTIONS[kind])],
                "status": "open", "created_at": _now(),
            }
            pipeline["gates"][gate_id] = gate
            pipeline["stages"][stage_label]["gate_id"] = gate_id
            return dict(gate)

    def resolve_gate(self, pipeline_id: str, gate_id: str, choice: int,
                     custom_answer: str = "") -> dict:
        """Turn one tap into the exact professional line the resume carries."""
        with self._lock:
            pipeline = self._pipelines.get(pipeline_id)
            if pipeline is None:
                raise KeyError(pipeline_id)
            gate = pipeline["gates"].get(gate_id)
            if gate is None:
                raise KeyError(gate_id)
            if gate["status"] != "open":
                raise ConsoleError("该反馈门已裁决")
            options = gate["options"]
            if not isinstance(choice, int) or not 0 <= choice < len(options):
                raise ConsoleError(f"选项序号必须在 0-{len(options) - 1} 之间")
            option = options[choice]
            answer = option["auto_fill"]
            if str(custom_answer or "").strip():
                answer = f"{answer}{custom_answer.strip()}"
            gate["status"] = "resolved"
            gate["resolved_at"] = _now()
            gate["choice"] = {"index": choice, "label": option["label"]}
            gate["answer"] = answer
            pipeline["stages"].get(gate["stage"], {}).pop("gate_id", None)
            terminal = gate["kind"] in GATE_TERMINAL_CHOICES and \
                choice == GATE_TERMINAL_CHOICES[gate["kind"]]
            if terminal:
                pipeline["status"] = "CLOSED"
            return {"gate_id": gate_id, "stage": gate["stage"],
                    "kind": gate["kind"], "choice_label": option["label"],
                    "answer": answer, "resume_hint": GATE_RESUME_HINT[gate["kind"]],
                    "pipeline_status": pipeline["status"],
                    "terminal": terminal}

    def snapshot(self, pipeline_id: str) -> dict:
        with self._lock:
            pipeline = self._pipelines.get(pipeline_id)
            if pipeline is None:
                raise KeyError(pipeline_id)
            return {**pipeline, "gates": {gid: dict(gate)
                                          for gid, gate in pipeline["gates"].items()},
                    "open_gates": [dict(gate) for gate in pipeline["gates"].values()
                                   if gate["status"] == "open"]}
