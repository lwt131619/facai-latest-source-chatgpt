"""FACAI Boundary Governor (边界治理) — the admission gate for self-evolving
skill intake, and the remote-authority severance wall.

Operator doctrine (2026-10-12): 发财必须拥有自进化收录 skill 的能力；所有限制
类——道德、宪章、法律、授权——是每一件收入进来的技能/产品必须穿过的闸门；
必要收入进来一套产品的时候，必须切断远程端 API 权限大过于 facai /owner 本地
——在这个 agent 里所有的边界都是所有者说了算。

Concretely, three invariants this module enforces:

- RESTRICTION SCREEN: every intake passes four restriction classes
  (ethics 道德 / charter 宪章 / law 法律 / authorization 授权). A violation
  under ``enforce`` quarantines the intake — it never reaches the skills
  registry, so it can never execute. The owner may release it explicitly
  (``release`` is owner-only); nobody else can.
- REMOTE SEVERANCE: an admitted product's remote API endpoints are extracted
  and parked in the RemoteAuthorityLedger with ``allowed=False``. Remote
  authority is structurally BELOW local owner authority: a ledger entry may
  only open through :meth:`RemoteAuthorityLedger.grant`, which refuses any
  issuer that is not ``FACAI_OWNER`` — the same single-root invariant as
  facai/authority.py's ``issue_grant``. A skill cannot phone home until the
  owner, locally, says so.
- OWNER SOVEREIGNTY: rule modes and extra rules are owner-editable state
  (``set_config``); the invariant check :meth:`BoundaryGovernor.below_local_report`
  re-proves "remote < local" on demand. Nothing an intake declares can raise
  its own authority: permission manifests are re-stamped deny-by-default.

Like facai/authority.py this module is policy-plus-a-JSON-file, not a store
schema change: the governor persists its journal/quarantine/ledger/config to
``boundary.json`` under the data root, atomically rewritten on every mutation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone

# Skill parsing is reused verbatim: the intake threshold keeps every product
# rule the plain import already enforces (frontmatter validity, forbidden
# fields, curl|sh hard rejection) and adds the restriction classes on top.
from .skills import build_skill_metadata, parse_skill_md
from .authority import FACAI_OWNER

RESTRICTION_CLASSES = ("ethics", "charter", "law", "authorization")
CLASS_LABELS = {"ethics": "道德", "charter": "宪章",
                "law": "法律", "authorization": "授权"}

# Per-class gate mode. The owner flips these; a skill never can.
MODE_ENFORCE = "enforce"    # a hit quarantines the intake
MODE_OBSERVE = "observe"    # hits are recorded, the intake is still admitted
MODE_OFF = "off"            # the class does not screen at all
CLASS_MODES = (MODE_ENFORCE, MODE_OBSERVE, MODE_OFF)

JOURNAL_LIMIT = 2000
_ENDPOINTS_LIMIT = 32
_STORED_CONTENT_LIMIT = 20_000


class BoundaryError(ValueError):
    """Raised when an intake or a boundary mutation would break the invariants."""


# --- default restriction rules -----------------------------------------------
# Each rule: (stable id, compiled regex, operator-facing reason). They screen
# the whole SKILL.md text (frontmatter + body) — an intake is untrusted data,
# so matching is deliberately coarse and the owner extends the lists rather
# than relying on parser cleverness.

def _rules(specs):
    return [(rule_id, re.compile(pattern), reason)
            for rule_id, pattern, reason in specs]


DEFAULT_RULES = {
    "ethics": _rules((
        ("ethics.doxx", r"人肉(搜索|开盒)|开盒(挂人)?|doxx(?:ing)?\b",
         "包含人肉搜索/开盒类指引"),
        ("ethics.harass", r"网络暴力|骚扰(短信|电话)?|批量骚扰|harass(?:ment)?\s+campaign",
         "包含骚扰/网暴类指引"),
        ("ethics.fraud_script", r"诈骗话术|杀猪盘|电信诈骗(话术|剧本)?|romance.?scam",
         "包含诈骗话术类指引"),
    )),
    # 宪章类：一切试图在本平台内再造权威根、越过或改写所有者边界的内容。
    # 这是“所有的边界都是所有者说了算”的正文条款。
    "charter": _rules((
        ("charter.override_instructions",
         r"忽略(之前|以上|上面|前面|上述)的?所有?指令|无视(所有者|管理员|运营方|owner)",
         "试图让执行者无视所有者或既有指令"),
        ("charter.override_instructions_en",
         r"ignore (all )?(previous|prior|above) instructions",
         "Attempts to override prior instructions (authority re-root pattern)"),
        ("charter.identity_seize", r"you are (now )?(the )?(owner|administrator|admin)\b",
         "试图把执行者重定义为所有者/管理员"),
        ("charter.boundary_rewrite",
         r"(self-?)modif(?:y|ies|ying).{0,16}boundar|自行修改边界|篡改边界|重写(权限|边界)规则",
         "试图自行修改边界/权限规则"),
        ("charter.bypass", r"越过(所有者|owner)|绕过(所有者|授权|审批|闸门)|"
                           r"bypass (the )?(owner|authority|approval|gate)",
         "试图越过所有者或授权闸门"),
    )),
    "law": _rules((
        ("law.ransomware", r"勒索软件|勒索病毒|ransomware", "包含勒索软件类指引"),
        ("law.ddos", r"\bddos\b|分布式拒绝服务|僵尸网络|botnet", "包含 DDoS/僵尸网络类指引"),
        ("law.credential_theft", r"键盘记录|keylogger|盗取(账号|密码|凭证)|"
                                 r"窃取(账号|密码|凭证)|credential.{0,8}theft",
         "包含凭证窃取/键盘记录类指引"),
        ("law.phishing", r"钓鱼(网站|页面)|phishing (site|page|kit)", "包含钓鱼站点类指引"),
    )),
    # 授权类：技能试图给自己签发权限、冒充所有者签发 grant。
    "authorization": _rules((
        ("authz.self_issue", r"自行签发(授权|grant)|越权签发|self-?issue.{0,15}grant",
         "试图自行签发授权"),
        ("authz.owner_impersonation", r"以\s*FACAI_OWNER\s*(身份)?签发|"
                                      r"as\s+FACAI_OWNER.{0,20}(issue|grant)",
         "试图冒充所有者签发授权"),
        ("authz.privilege_escalation", r"提高(自己|自身)的?权限|escalat(?:e|ing).{0,12}privilege",
         "试图提升自身权限"),
    )),
}

_ENDPOINT_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")


def extract_remote_endpoints(text: str) -> list[str]:
    """Every remote URL an intake declares, in first-seen order, deduped.

    These become the severed ledger entries: declared here, allowed only
    through an owner grant later.
    """
    endpoints = []
    for match in _ENDPOINT_RE.finditer(text or ""):
        endpoint = match.group(0).rstrip(".,;)")
        if endpoint not in endpoints:
            endpoints.append(endpoint)
        if len(endpoints) >= _ENDPOINTS_LIMIT:
            break
    return endpoints


def _now():
    return datetime.now(timezone.utc).isoformat()


class RemoteAuthorityLedger:
    """Every remote endpoint an admitted product may want, severed by default.

    The ledger is the physical shape of “切断远程端 API 权限大过于 facai
    /owner 本地”: an entry exists per endpoint, ``allowed=False`` until an
    owner-issued grant opens it, and the opener is always the local root —
    never the remote side, never the skill itself.
    """

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}

    def sever(self, skill: str, endpoints: list[str]) -> list[dict]:
        """Record severed endpoints for one admitted skill (idempotent)."""
        created = []
        for endpoint in endpoints:
            entry_id = self._id_for(skill, endpoint)
            if entry_id in self._entries:
                continue
            entry = {"id": entry_id, "endpoint": endpoint, "skill": skill,
                     "allowed": False, "granted_by": None, "granted_at": None,
                     "note": "收录时切断：远程权限默认拒绝，仅本地所有者可签发"}
            self._entries[entry_id] = entry
            created.append(entry)
        return created

    @staticmethod
    def _id_for(skill: str, endpoint: str) -> str:
        digest = hashlib.sha256(f"{skill}|{endpoint}".encode()).hexdigest()[:16]
        return f"r-{digest}"

    def grant(self, entry_id: str, issued_by: str, note: str = "") -> dict:
        if issued_by != FACAI_OWNER:
            raise BoundaryError(
                "远程 API 权限只能由本地所有者 FACAI_OWNER 签发；"
                "远程端权限永远小于本地 /owner")
        entry = self._entries.get(entry_id)
        if entry is None:
            raise KeyError(entry_id)
        entry["allowed"] = True
        entry["granted_by"] = FACAI_OWNER
        entry["granted_at"] = _now()
        entry["note"] = note or entry["note"]
        return entry

    def revoke(self, entry_id: str) -> dict:
        entry = self._entries.get(entry_id)
        if entry is None:
            raise KeyError(entry_id)
        entry["allowed"] = False
        entry["granted_by"] = None
        entry["granted_at"] = None
        entry["note"] = "所有者已撤回远程权限，重新切断"
        return entry

    def list(self) -> list[dict]:
        return sorted(self._entries.values(), key=lambda e: e["id"])

    def below_local_report(self) -> dict:
        """Prove the invariant: every open entry traces to an owner grant.

        The ledger's write paths make violations impossible (grant refuses
        non-owner issuers), so an empty violation list is the healthy steady
        state; the report exists so the operator can demand proof, not trust.
        """
        violations = [entry["id"] for entry in self._entries.values()
                      if entry["allowed"] and entry["granted_by"] != FACAI_OWNER]
        return {"invariant": "remote_authority_below_local_owner",
                "holds": not violations, "violations": violations,
                "total": len(self._entries),
                "open": sum(1 for e in self._entries.values() if e["allowed"])}


class BoundaryGovernor:
    """Self-evolving intake: screen → sever → admit/quarantine → journal."""

    def __init__(self, data_dir):
        self._path = data_dir / "boundary.json"
        self._lock = threading.RLock()
        self._seq = 0
        self.modes = {cls: MODE_ENFORCE for cls in RESTRICTION_CLASSES}
        self.extra_rules: dict[str, list] = {cls: [] for cls in RESTRICTION_CLASSES}
        self.journal: list[dict] = []
        self.quarantine: dict[str, dict] = {}
        self.ledger = RemoteAuthorityLedger()
        self.admitted: dict[str, dict] = {}
        self._load()

    # --- screening -----------------------------------------------------------
    def _rules_for(self, cls: str):
        yield from DEFAULT_RULES[cls]
        for rule in self.extra_rules.get(cls, ()):
            yield (rule["id"], re.compile(rule["pattern"]), rule["reason"])

    def screen(self, text: str) -> list[dict]:
        """One verdict per restriction class that is not switched off."""
        verdicts = []
        for cls in RESTRICTION_CLASSES:
            mode = self.modes.get(cls, MODE_ENFORCE)
            if mode == MODE_OFF:
                continue
            hits = []
            for rule_id, pattern, reason in self._rules_for(cls):
                match = pattern.search(text)
                if match:
                    # Only the rule id and reason travel; the matched line is
                    # untrusted content and never enters journal/metadata.
                    hits.append({"rule": rule_id, "reason": reason})
            verdicts.append({"class": cls, "label": CLASS_LABELS[cls],
                             "mode": mode, "hits": hits,
                             "violates": bool(hits) and mode == MODE_ENFORCE})
        return verdicts

    # --- the intake ------------------------------------------------------------
    def intake(self, content: str, source_url: str | None = None) -> dict:
        """Run one product/skill through the full admission pipeline.

        Malformed SKILL.md and pipe-to-shell payloads raise ValueError (the
        API maps it to 422) — a broken intake is never admitted by courtesy.
        """
        parsed = parse_skill_md(content)
        # Same order as the plain import route: the API-side source_url feeds
        # metadata construction, it cannot patch the metadata afterwards.
        if source_url and not parsed.get("source_url"):
            parsed["source_url"] = source_url.strip()
        name = parsed["name"]
        verdicts = self.screen(content)
        violations = [hit for verdict in verdicts for hit in verdict["hits"]
                      if verdict["violates"]]
        with self._lock:
            self._seq += 1
            if violations:
                record = {
                    "id": f"q-{self._seq}", "skill": parsed["name"],
                    "source_url": source_url or parsed.get("source_url"),
                    "violations": violations, "verdicts": self._public_verdicts(verdicts),
                    "content": content[:_STORED_CONTENT_LIMIT],
                    "status": "open", "at": _now(),
                }
                self.quarantine[record["id"]] = record
                decision = self._journal("quarantined", parsed["name"],
                                         violations, [], record["id"])
                self._save()
                return {"decision": "quarantined", "quarantine_id": record["id"],
                        "skill": parsed["name"], "verdicts": self._public_verdicts(verdicts),
                        "violations": violations, "journal_id": decision["id"]}
            metadata = build_skill_metadata(parsed)
            endpoints = extract_remote_endpoints(content)
            severed = self.ledger.sever(parsed["name"], endpoints)
            metadata["boundary"] = {
                "severed_remote": True,
                "ledger_ids": [entry["id"] for entry in severed],
                "endpoints": [entry["endpoint"] for entry in severed],
                "policy": "远程端 API 权限已切断；仅本地 FACAI_OWNER 可签发恢复",
                "admitted_at": _now(),
            }
            self.admitted[parsed["name"]] = {
                "skill": parsed["name"], "at": _now(),
                "ledger_ids": [entry["id"] for entry in severed],
            }
            decision = self._journal("admitted", parsed["name"], [], 
                                     [entry["endpoint"] for entry in severed], None)
            self._save()
            return {"decision": "admitted", "skill": parsed["name"],
                    "verdicts": self._public_verdicts(verdicts),
                    "metadata": metadata, "warnings": metadata.get("warnings", []),
                    "severed_endpoints": [entry["endpoint"] for entry in severed],
                    "ledger_ids": [entry["id"] for entry in severed],
                    "journal_id": decision["id"]}

    @staticmethod
    def _public_verdicts(verdicts):
        return [{"class": v["class"], "label": v["label"], "mode": v["mode"],
                 "hits": v["hits"], "violates": v["violates"]} for v in verdicts]

    # --- owner decisions -------------------------------------------------------
    def release(self, quarantine_id: str, released_by: str, note: str = "") -> dict:
        """Owner-only release: re-admit a quarantined intake explicitly.

        The violations stay on the record (journal decision
        ``admitted_override``) — an override is a decision, never an erasure.
        """
        if released_by != FACAI_OWNER:
            raise BoundaryError("只有所有者可以放行被隔离的收录")
        with self._lock:
            record = self.quarantine.get(quarantine_id)
            if record is None or record["status"] != "open":
                raise KeyError(quarantine_id)
            record["status"] = "released"
            endpoints = extract_remote_endpoints(record.get("content") or "")
            severed = self.ledger.sever(record["skill"], endpoints)
            reparsed = parse_skill_md(record["content"])
            # A quarantined record always carries a source_url: intake cannot
            # reach quarantine without one (build_skill_metadata enforces it).
            if record.get("source_url") and not reparsed.get("source_url"):
                reparsed["source_url"] = record["source_url"]
            metadata = build_skill_metadata(reparsed)
            metadata["boundary"] = {
                "severed_remote": True,
                "ledger_ids": [entry["id"] for entry in severed],
                "endpoints": [entry["endpoint"] for entry in severed],
                "policy": "远程端 API 权限已切断；仅本地 FACAI_OWNER 可签发恢复",
                "admitted_at": _now(),
                "override": True,
            }
            decision = self._journal("admitted_override", record["skill"],
                                     record["violations"],
                                     [entry["endpoint"] for entry in severed],
                                     quarantine_id, note=note)
            self._save()
            return {"decision": "admitted_override", "skill": record["skill"],
                    "metadata": metadata,
                    "ledger_ids": [entry["id"] for entry in severed],
                    "severed_endpoints": [entry["endpoint"] for entry in severed],
                    "journal_id": decision["id"],
                    "note": "所有者明确放行；限制类命中留痕，远程权限仍默认切断"}

    def grant_remote(self, entry_id: str, issued_by: str, note: str = "") -> dict:
        with self._lock:
            entry = self.ledger.grant(entry_id, issued_by, note=note)
            self._journal("remote_granted", entry["skill"], [],
                          [entry["endpoint"]], None, note=note)
            self._save()
            return entry

    def revoke_remote(self, entry_id: str) -> dict:
        with self._lock:
            entry = self.ledger.revoke(entry_id)
            self._journal("remote_revoked", entry["skill"], [],
                          [entry["endpoint"]], None)
            self._save()
            return entry

    def set_config(self, modes: dict | None = None, extra: dict | None = None) -> dict:
        """Owner-editable boundary: per-class mode + extra rules per class."""
        with self._lock:
            if modes:
                for cls, mode in modes.items():
                    if cls not in RESTRICTION_CLASSES:
                        raise BoundaryError(f"unknown restriction class: {cls}")
                    if mode not in CLASS_MODES:
                        raise BoundaryError(f"unknown mode: {mode}")
                self.modes.update(modes)
            if extra:
                for cls, rules in extra.items():
                    if cls not in RESTRICTION_CLASSES:
                        raise BoundaryError(f"unknown restriction class: {cls}")
                    for rule in rules:
                        if not isinstance(rule, dict) or not rule.get("id") \
                                or not rule.get("pattern"):
                            raise BoundaryError("extra rule 需要 id 与 pattern")
                        try:
                            re.compile(rule["pattern"])
                        except re.error as exc:
                            raise BoundaryError(f"规则 {rule['id']} 正则非法: {exc}") from None
                    by_id = {r["id"]: r for r in self.extra_rules[cls]}
                    for rule in rules:
                        by_id[rule["id"]] = {"id": rule["id"],
                                             "pattern": rule["pattern"],
                                             "reason": rule.get("reason", "")}
                    self.extra_rules[cls] = list(by_id.values())
            self._save()
            return self.config_snapshot()

    def config_snapshot(self) -> dict:
        return {"modes": dict(self.modes),
                "extra_rules": {cls: list(rules) for cls, rules in self.extra_rules.items()},
                "labels": dict(CLASS_LABELS)}

    def below_local_report(self) -> dict:
        with self._lock:
            report = self.ledger.below_local_report()
            self._journal("invariant_check", "boundary", [], [], None,
                          note="holds" if report["holds"] else "VIOLATION")
            self._save()
            return report

    # --- lifecycle journal (SKILL-001 ③) -------------------------------------

    def journal_lifecycle_event(self, decision: str, skill: str, *,
                                actor: str = "", note: str = "",
                                extra: dict | None = None) -> dict:
        """Record a skill lifecycle decision (SKILL_DISABLED / SKILL_REMOVED /
        SKILL_ENABLED / SKILL_RESTORED) in the same durable journal every
        intake decision lands in — evidence discipline: disabling or removing
        a capability is a decision, and decisions stay on the record."""
        with self._lock:
            entry = {"decision": decision, "skill": skill,
                     "violations": [], "severed": [], "quarantine_id": None}
            if actor:
                entry["actor"] = actor
            if note:
                entry["note"] = note
            if extra:
                entry.update(extra)
            core = {k: entry.pop(k) for k in ("decision", "skill", "violations",
                                              "severed", "quarantine_id")}
            return self._journal(**core, **entry)

    # --- state -----------------------------------------------------------------
    def _journal(self, decision, skill, violations, severed, quarantine_id,
                 note="", **extra) -> dict:
        self._seq += 1
        entry = {"id": f"j-{self._seq}", "at": _now(), "decision": decision,
                 "skill": skill, "violations": list(violations),
                 "severed": list(severed), "quarantine_id": quarantine_id}
        if note:
            entry["note"] = note
        if extra:
            entry.update(extra)
        self.journal.append(entry)
        if len(self.journal) > JOURNAL_LIMIT:
            del self.journal[:len(self.journal) - JOURNAL_LIMIT]
        return entry

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "config": self.config_snapshot(),
                "journal": list(self.journal[-100:]),
                "quarantine": [q for q in self.quarantine.values() if q["status"] == "open"],
                "ledger": self.ledger.list(),
                "admitted": list(self.admitted.values()),
                "invariant": self.ledger.below_local_report(),
            }

    # --- persistence -------------------------------------------------------------
    def _save(self) -> None:
        payload = {
            "seq": self._seq,
            "modes": self.modes,
            "extra_rules": self.extra_rules,
            "journal": self.journal,
            "quarantine": self.quarantine,
            "ledger": self.ledger._entries,
            "admitted": self.admitted,
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)

    def _load(self) -> None:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._seq = int(payload.get("seq", 0))
        modes = payload.get("modes", {})
        self.modes.update({cls: modes[cls] for cls in RESTRICTION_CLASSES
                           if cls in modes and modes[cls] in CLASS_MODES})
        extra = payload.get("extra_rules", {})
        for cls in RESTRICTION_CLASSES:
            rules = extra.get(cls)
            if isinstance(rules, list):
                self.extra_rules[cls] = [r for r in rules
                                         if isinstance(r, dict) and r.get("id")
                                         and r.get("pattern")]
        self.journal = payload.get("journal", [])
        self.quarantine = payload.get("quarantine", {})
        self.ledger._entries = payload.get("ledger", {})
        self.admitted = payload.get("admitted", {})
