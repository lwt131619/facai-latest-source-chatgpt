"""SKILL.md import parsing and registry metadata builder.

An import is a capability-admission decision made under FACAI OWNER
authority: parsed metadata registers as an active, usable skill
(``review_status: "approved"``) — nothing sits in a dead pending queue
(§十一.C.6 pending-only/never-execute 限制板块已拆除). The one line that
stays is product reliability, not a usage restriction: a SKILL.md whose
body pipes to shell (curl|sh) is refused at the threshold, and risky
bash directives are surfaced as warnings.

Progressive disclosure: only frontmatter metadata is returned in
registry-shaped output; the markdown body (the actual instructions)
stays out of build_skill_metadata() output as data hygiene — the body
is never registry metadata.

PyYAML is not a declared dependency (see requirements.txt), so the
frontmatter is parsed with a minimal "key: value" parser instead of a
full YAML implementation.
"""

import re
from datetime import datetime, timezone

# Same skill-id convention as the skills registry (imported, not duplicated).
from facai.registries import _FORBIDDEN_SKILL_FIELDS, _SKILL_ID_MAX_LEN, _SKILL_ID_RE

_MAX_DESCRIPTION_LEN = 1024

# curl|sh / wget|bash style pipe-to-shell: hard reject, even inside a
# fenced bash block. Never merely warned about.
_PIPE_TO_SHELL_RE = re.compile(r"\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b")

# Fenced block languages that count as shell for the warning scan.
_SHELL_FENCE_LANGS = frozenset({"bash", "sh", "shell", "zsh"})

# Obvious executable directives inside ```bash blocks: warn, do not reject.
_WARNING_COMMANDS = ("curl ", "pip install", "npm install")

_FRONTMATTER_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")


def _split_frontmatter(text):
    """Split SKILL.md into (frontmatter lines, body). Both fences required."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md 缺少 YAML frontmatter")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise ValueError("SKILL.md frontmatter 缺少结束标记 ---")
    return lines[1:end], "\n".join(lines[end + 1:])


def _parse_frontmatter(fm_lines):
    """Minimal frontmatter parser: one "key: value" pair per line.

    PyYAML is not a project dependency, so full YAML (nested maps, lists,
    anchors) is intentionally unsupported and rejected as invalid.
    """
    data = {}
    for line in fm_lines:
        if not line.strip():
            continue
        match = _FRONTMATTER_LINE_RE.match(line)
        if not match:
            raise ValueError("SKILL.md frontmatter 不是有效的 YAML（仅支持 key: value 行）")
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        data[key] = value
    return data


def _scan_body(body):
    """Scan the markdown body for executable directives.

    Returns a warnings list. Pipe-to-shell (curl|sh) is a hard ValueError.
    """
    warnings = []
    in_fence = False
    fence_lang = ""
    for line in body.splitlines():
        stripped = line.strip()
        if _PIPE_TO_SHELL_RE.search(line):
            raise ValueError("SKILL.md 包含管道执行 shell 的命令（如 curl|sh），已拒绝导入")
        if stripped.startswith("```"):
            if not in_fence:
                in_fence = True
                tokens = stripped[3:].strip().lower().split()
                fence_lang = tokens[0] if tokens else ""
            else:
                in_fence = False
                fence_lang = ""
            continue
        if in_fence and fence_lang in _SHELL_FENCE_LANGS:
            for command in _WARNING_COMMANDS:
                if command in line:
                    warnings.append(
                        f"检测到 bash 代码块中的可疑命令 {command.strip()!r}；"
                        "请确认技能来源可信"
                    )
                    break
        if stripped.startswith("rm -rf"):
            warnings.append("检测到 rm -rf 命令；请确认技能来源可信")
    return warnings


def parse_skill_md(text: str) -> dict:
    """Parse an Agent-Skill style SKILL.md into a validated dict.

    Returns name, description, optional license/version/source_url, a
    warnings list, and the raw body under the internal key "_body"
    (the body is data; it never enters registry metadata).

    Raises ValueError on missing/invalid frontmatter, forbidden fields,
    invalid name/description/version, or pipe-to-shell directives.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("SKILL.md 内容为空")

    fm_lines, body = _split_frontmatter(text)
    data = _parse_frontmatter(fm_lines)

    forbidden = sorted(
        key for key in data
        if isinstance(key, str) and key.casefold() in _FORBIDDEN_SKILL_FIELDS
    )
    if forbidden:
        raise ValueError(f"SKILL.md frontmatter 包含禁止的字段: {forbidden}")

    name = data.get("name")
    if not name:
        raise ValueError("SKILL.md 缺少 name 字段")
    if not isinstance(name, str) or len(name) > _SKILL_ID_MAX_LEN \
            or not _SKILL_ID_RE.match(name):
        raise ValueError(
            "SKILL.md name 不符合技能 ID 规范"
            "（小写字母数字、单连字符、1-64 字符）"
        )

    description = data.get("description")
    if not description or not isinstance(description, str) or not description.strip():
        raise ValueError("SKILL.md description 不能为空")
    if len(description) > _MAX_DESCRIPTION_LEN:
        raise ValueError("SKILL.md description 超过 1024 字符")

    version = data.get("version")
    if version is not None and not version.strip():
        raise ValueError("SKILL.md version 必须是非空字符串")

    license_val = data.get("license")
    if license_val is not None and not license_val.strip():
        raise ValueError("SKILL.md license 必须是非空字符串")

    warnings = _scan_body(body)

    parsed = {
        "name": name,
        "description": description,
        "warnings": warnings,
        "_body": body,
    }
    if license_val:
        parsed["license"] = license_val
    if version:
        parsed["version"] = version
    source_url = data.get("source_url") or data.get("homepage")
    if source_url:
        parsed["source_url"] = source_url
    return parsed


def build_skill_metadata(parsed: dict) -> dict:
    """Build a registry-shaped metadata entry from parse_skill_md output.

    The result carries review_status "approved" — an import admits the
    capability immediately under FACAI OWNER authority (§十一.C.6: no
    pending-only queue) — and satisfies registries._validate_skill_metadata.
    The skill body / instructions are never included in metadata
    (data hygiene, not an execution gate). When the SKILL.md has no
    license field, "UNLICENSED" is recorded instead of fabricating a
    placeholder source or license.
    """
    source_url = parsed.get("source_url")
    if not source_url or not isinstance(source_url, str):
        raise ValueError(
            "SKILL.md frontmatter 缺少 source_url/homepage，无法构造可注册的技能元数据"
        )

    # Defensive: never copy the parsed dict wholesale; the body ("_body")
    # and any other content must not leak into registry metadata.
    metadata = {
        "name": parsed["name"],
        "description": parsed["description"],
        "license": parsed.get("license") or "UNLICENSED",
        "source_url": source_url,
        "review_status": "approved",
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "warnings": list(parsed.get("warnings", [])),
    }
    # SKILL-001 ①: version is registry data, not a pass-through field. A
    # re-registration carries the NEW version verbatim (the registry row is
    # replaced whole); pre-existing rows without a version simply keep having
    # no version key — nothing is backfilled or fabricated.
    version = parsed.get("version")
    if version:
        metadata["version"] = version
    return metadata


def enabled_only(skills: dict) -> dict:
    """Routing/dispatch view of a skills registry mapping.

    A disabled entry (``enabled is False``) never reaches conversion scoring
    or mission dispatch. Absent ``enabled`` means enabled — legacy rows and
    distilled skills predate the flag and stay routable.
    """
    return {key: value for key, value in (skills or {}).items()
            if (value or {}).get("enabled") is not False}


def dispatchable_skill(entry):
    """One resolved registry entry as dispatch sees it: ``None`` when the
    entry is missing or disabled.

    Scheduler-side integration point (main-session wiring, scheduler.py is
    owned by another agent): ``facai/scheduler.py:219`` currently reads

        skill = self.registries.resolve("skills", mission["skill_id"], owner_id=owner)

    and should become

        from facai.skills import dispatchable_skill
        skill = dispatchable_skill(self.registries.resolve(
            "skills", mission["skill_id"], owner_id=owner))

    so a disabled skill resolves away at dispatch exactly as it already does
    in conversion scoring (conversion.assign_stage) and the API run paths.
    """
    if entry is None or entry.get("enabled") is False:
        return None
    return entry


def bound_skill(entry, mission):
    """Keep a manually bound source version while honoring disable/removal."""
    active = dispatchable_skill(entry)
    if active is None:
        return None
    digest = mission.get("skill_body_sha256")
    if not digest:
        return active
    return {**active, "body_sha256": digest,
            "version": mission.get("skill_version")}


def explicitly_requested_skill(raw_request, skills):
    """Select one named, enabled imported Skill without guessing from similarity."""
    request = raw_request or ""
    matches = [key for key, entry in (skills or {}).items()
               if dispatchable_skill(entry) and entry.get("body_sha256")
               and re.search(r"(?<![A-Za-z0-9-])" + re.escape(key) +
                             r"(?![A-Za-z0-9-])", request, re.IGNORECASE)]
    if len(matches) != 1:
        return None
    key = matches[0]
    return key, skills[key]
