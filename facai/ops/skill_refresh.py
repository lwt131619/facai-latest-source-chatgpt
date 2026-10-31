"""FACAI 每日技能保鲜循环（skill refresh loop）。

操作员指令 2026-10-13：收录的技能必须每天被用到、被演习，不许堆着腐烂。
本循环一天一跑（systemd timer 或手动），四步全幂等：

1. RE-INGEST  重收 vendor/skills/* 的全部 SKILL.md——同一道边界闸门，
   内容被篡改（夹带"忽略之前的所有指令"）会在这一步被拦下并报警退出。
2. CRAWL      重爬策展源清单（案例管道，sha 差分：没变跳过，变了记
   previous_sha）。源页面加了投毒正文同样被宪章类拦截。
3. EXERCISE   对每个已注册技能跑 _skill_score 演习（真实派发打分机制），
   在全部探针场景都 0 重叠的技能标记 orphan——这就是"用起来"的巡检：
   收录了却从不参与任何路由的技能当天就会被点名。
4. REPORT     docs/SKILL_REFRESH_<date>.json + stdout；有 blocked 或
   orphan 即退出码 1，可挂告警。

CLI: ``py -3.12 -m facai.ops.skill_refresh --data data [--out docs/x.json]``
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..boundary import BoundaryGovernor
from ..conversion import _skill_score
from ..ops.case_ingest import ingest_cases
from ..registries import Registries
from ..skills import build_skill_metadata, parse_skill_md
from ..store import Store

# 策展爬取源：产品可观测面 + 参考教程（差分收录进案例库）
CURATED_SOURCES = (
    "https://www.easemate.ai/tw/ai-chat/ai-prompt-optimizer",
    "https://featherless.ai/status",
    "https://moge.ai/zh/product/rork",
    "https://moge.ai/zh/product/whacka",
    "https://moge.ai/zh/product/ui-bakery",
    "https://moge.ai/zh/product/catdoes",
    "https://moge.ai/zh/product/noodl",
    "https://moge.ai/zh/product/mwm-ai",
)

# 演习探针：每个探针是一段真实派发会出现的专业提示词正文。
# 新收录技能时在这里补一条能命中的探针，否则它会以 orphan 的身份出现在
# 下一份报告里——这是纪律不是 bug。
EXERCISE_PROBES = {
    "prompt-optimizer-loop": "把模糊的原始提示词改写成结构化提示词并测试迭代效果",
    "model-fleet-status-probe": "检查模型目录可用性探活延迟并做健康巡检报告",
    "nl-to-mobile-app": "生成跨平台手机应用 React Native 预览并上架应用商店",
    "conversational-fullstack-builder": "对话式搭建全栈应用含数据库后端与身份验证并部署域名",
    "ios-native-app-business": "生成原生 Swift iOS 应用并上架 App Store 配置内购订阅变现",
    "visual-node-fullstack": "可视化节点编排全栈低代码开发企业内部系统可部署任意云",
    "multi-agent-app-assembly": "多 Agent 分工协作：需求捕获设计 UI 与软件实现逐级交接装配应用",
}


def _re_ingest_skills(governor, registries, vendor_dir: Path) -> dict:
    admitted, blocked, unchanged, changed, owner_removed = [], [], [], [], []
    live = registries.store.registry_list("skills", owner_id="admin")
    for md in sorted(vendor_dir.glob("*/SKILL.md")):
        content = md.read_text(encoding="utf-8")
        try:
            parsed = parse_skill_md(content)
            metadata = build_skill_metadata(parsed)
        except ValueError as exc:
            blocked.append({"skill": md.parent.name, "violations": [str(exc)]})
            continue
        skill_id = parsed["name"]
        if registries.store.registry_tombstone("skills", skill_id, owner_id="admin"):
            owner_removed.append(skill_id)
            continue
        current = live.get(skill_id)
        if current is not None:
            if current.get("document_sha256") == metadata["document_sha256"]:
                unchanged.append(skill_id)
            else:
                changed.append({"skill": skill_id,
                                "candidate_sha256": metadata["document_sha256"],
                                "installed_sha256": current.get("document_sha256")})
            # Refresh observes changes; it never overrides Owner enablement,
            # pinned versions, or the current source of an installed Skill.
            continue
        result = governor.intake(content, source_url=None)
        if result["decision"] == "admitted":
            registries.register("skills", result["skill"], result["metadata"],
                                owner_id="admin")
            admitted.append(result["skill"])
        else:
            blocked.append({"skill": result["skill"],
                            "violations": result["violations"]})
    return {"admitted": admitted, "blocked": blocked, "unchanged": unchanged,
            "changed": changed, "owner_removed": owner_removed}


def _exercise(governor, registries, owner_id="admin") -> dict:
    skills = registries.list("skills", owner_id=owner_id)
    rows, disabled = [], []
    for skill_id, skill in sorted(skills.items()):
        if skill.get("enabled") is False:
            disabled.append(skill_id)
            continue
        best_skill, best_probe = 0.0, ""
        for probe_label, probe_text in EXERCISE_PROBES.items():
            score = _skill_score(probe_label, probe_text, skill or {})
            if score > best_skill:
                best_skill, best_probe = score, probe_label
        rows.append({"skill": skill_id, "best_probe": best_probe,
                     "overlap": round(best_skill, 4),
                     "orphan": best_skill <= 0.0})
    orphans = [r["skill"] for r in rows if r["orphan"]]
    return {"exercised": len(rows) - len(orphans), "orphans": orphans,
            "disabled": disabled,
            "rows": rows}


def run_daily(data_dir: Path, out: Path | None = None,
              sources=CURATED_SOURCES, http_client=None) -> dict:
    data_dir = Path(data_dir)
    store = Store(data_dir)
    registries = Registries(store)
    governor = BoundaryGovernor(data_dir)

    report = {
        "schema": "facai.skill-refresh/1",
        "at": datetime.now(timezone.utc).isoformat(),
        "reingest": _re_ingest_skills(governor, registries,
                                      data_dir.parent / "vendor" / "skills"
                                      if not (data_dir / "vendor" / "skills").exists()
                                      else data_dir / "vendor" / "skills"),
        "crawl": None,
        "exercise": None,
        "invariant": None,
    }
    # 爬取源可选：离线巡检（无网络）时 crawl 允许整体失败跳过，其余照跑。
    try:
        report["crawl"] = ingest_cases(governor, registries, list(sources),
                                       owner_id="admin",
                                       http_client=http_client)
    except Exception as exc:                      # 网络整体不可用
        report["crawl"] = {"skipped": True, "reason": f"{type(exc).__name__}"}

    report["exercise"] = _exercise(governor, registries)
    inv = governor.below_local_report()
    report["invariant"] = inv

    failed = bool(report["reingest"]["blocked"] or report["reingest"]["changed"]) \
        or bool(inv is None or not inv["holds"]) \
        or bool(report["exercise"]["orphans"])
    if out:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    report["_exit"] = 1 if failed else 0
    return report


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    data_dir, out = Path("data"), None
    if "--data" in argv:
        i = argv.index("--data"); data_dir = Path(argv[i + 1]); del argv[i:i + 2]
    if "--out" in argv:
        i = argv.index("--out"); out = Path(argv[i + 1]); del argv[i:i + 2]
    if argv:
        print("usage: py -3.12 -m facai.ops.skill_refresh [--data DIR] [--out FILE]")
        return 2
    report = run_daily(data_dir, out)
    ri, ex = report["reingest"], report["exercise"]
    print(f"skills: {len(ri['admitted'])} admitted, {len(ri['blocked'])} blocked; "
          f"exercise: {ex['exercised']} live, {len(ex['orphans'])} orphan "
          f"{ex['orphans'] or ''}")
    if report["crawl"] and not report["crawl"].get("skipped"):
        cr = report["crawl"]
        print(f"crawl: {cr['registered']}+{cr['updated']} changed, "
              f"{cr['skipped']} unchanged, {cr['blocked']} blocked")
    print(f"invariant: holds={report['invariant']['holds']} "
          f"open={report['invariant']['open']}")
    if out:
        print(f"report: {out}")
    return report["_exit"]


if __name__ == "__main__":
    raise SystemExit(main())
