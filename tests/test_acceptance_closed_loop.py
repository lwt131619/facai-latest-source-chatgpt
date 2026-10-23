"""A26 scripted closed loop, all on ONE mission trace.

自然语言 → 理解修订 → 确认（使命诞生、原话保留）→ 独立授权 → 真实执行路径
（OpenCodeAdapter 本体 + 确定性 runner，不调用任何模型）→ 事件侧自动采集
RUNTIME 证据 → Owner 补充证据与独立复核 → 断言审查（未审→INFERENCE）→
证据快照 Artifact → 下载 → 收录知识资产 → 重启恢复 → 同一 Trace 逐事件
可重放（重启前后全局事件流逐字节一致）。

诚实边界：受控 runner 证明的是闭环结构与持久性，不是真实 Provider 执行；
A26 总体验收仍以真实链路 + 真实界面验收为准（见 ACCEPTANCE_MATRIX）。
"""

import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from facai.api import create_app
from facai.opencode import OpenCodeAdapter


class FakeRunner:
    """Deterministic OpenCode CLI stand-in: writes one artifact file, never calls a model."""

    def __call__(self, command, cwd, timeout):
        (cwd / "run.txt").write_text("version = 1\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, '{"type":"text","text":"版本是 1"}\n', "")


def make_client(tmp_path, monkeypatch):
    monkeypatch.setenv("FACAI_ADMIN_PASSWORD", "loop-admin-pass")
    monkeypatch.delenv("FACAI_COOKIE_SECURE", raising=False)
    client = TestClient(create_app(tmp_path, opencode_adapter=OpenCodeAdapter(
        binary="opencode", runner=FakeRunner())))
    session = client.post("/api/v1/auth/login",
                          json={"username": "admin", "password": "loop-admin-pass"}).json()
    client.headers.update({"X-CSRF-Token": session["csrf_token"]})
    return client


def revision_of(client, mission_id):
    return client.get(f"/api/v1/missions/{mission_id}/trust").json()["revision"]


def test_closed_loop_nl_to_knowledge_survives_restart_on_one_trace(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)

    # ① 自然语言 → 编译理解 → 修订 → 确认：使命诞生，原话不可改写
    compiled = client.post("/api/v1/intents",
                           json={"raw_request": "分析这个项目，给出版本结论"})
    assert compiled.status_code == 201, compiled.text
    intent = compiled.json()
    assert intent["revision"] == 1
    revised = client.patch(f"/api/v1/intents/{intent['id']}", json={
        "expected_revision": 1,
        "interpretation": {"action": "analyze", "target": "/owner/project",
                           "purpose": "评估版本", "scope": "comprehensive",
                           "constraints": ["只读，不修改文件"]}})
    assert revised.status_code == 200, revised.text
    assert revised.json()["revision"] == 2
    confirmed = client.post(f"/api/v1/intents/{intent['id']}/confirm",
                            json={"expected_revision": 2})
    assert confirmed.status_code == 200, confirmed.text
    mission_id = confirmed.json()["intent"]["mission_id"]
    assert mission_id
    raw_after = client.get(f"/api/v1/intents/{intent['id']}").json()["raw_request"]
    assert raw_after == "分析这个项目，给出版本结论"  # 原始意图原样保留

    # ② 独立授权：与确认分开的显式闸门，授权不等于派发
    authorized = client.post(f"/api/v1/intents/{intent['id']}/authorize",
                             json={"expected_revision": 2})
    assert authorized.status_code == 200, authorized.text
    assert authorized.json()["execution"] == "NOT_DISPATCHED"

    # ③ 真实执行路径（受控 runner）：绑定 → 派发 → 产物落盘
    bound = client.put(f"/api/v1/missions/{mission_id}/binding",
                       json={"agent_id": "opencode", "model_id": "deepseek-flash"})
    assert bound.status_code == 200, bound.text
    mission = client.get(f"/api/v1/missions/{mission_id}").json()
    # 每个步骤都走一次真实执行路径：验收闸要求每步都有执行证据（事件在案）
    for step in mission["steps"]:
        run = client.post(f"/api/v1/missions/{mission_id}/opencode/run", json={
            "prompt": f"执行阶段：{step['label']}", "step_id": step["id"]})
        assert run.status_code == 200, run.text
        assert run.json()["status"] == "OPENCODE_COMPLETED"
        assert run.json()["model_id"] == "deepseek-flash"
    mission = client.get(f"/api/v1/missions/{mission_id}").json()
    assert all(s["status"] == "COMPLETED" for s in mission["steps"])
    assert mission["status"] == "COMPLETED"
    assert len(mission["artifacts"]) == len(mission["steps"])
    assert all(a["name"].endswith(".stdout.log") for a in mission["artifacts"])

    # ④ 证据：执行事件已被自动采集为 RUNTIME_EVENT；Owner 再补一条独立观测
    trust = client.get(f"/api/v1/missions/{mission_id}/trust").json()
    assert any(e["kind"] == "RUNTIME_EVENT" for e in trust["evidence"]), (
        "执行事件必须在同事务自动采集为证据")
    evidence = client.post(f"/api/v1/missions/{mission_id}/trust/evidence", json={
        "expected_revision": revision_of(client, mission_id),
        "content": "workspace run.txt 内容：version = 1",
        "source": "Owner 本地观测（非外部认证）"})
    assert evidence.status_code == 201, evidence.text
    evidence_id = evidence.json()["evidence"]["id"]
    verified = client.post(
        f"/api/v1/missions/{mission_id}/trust/evidence/{evidence_id}/verification", json={
            "expected_revision": revision_of(client, mission_id),
            "method": "OWNER_RECHECK", "result": "CORROBORATED",
            "note": "Owner 打开 run.txt 逐行核对，本地复现"})
    assert verified.status_code == 201, verified.text

    # ⑤ 断言：新断言未审时是 UNKNOWN；Owner 审查后 INFERENCE 且理由在案
    claim = client.post(f"/api/v1/missions/{mission_id}/trust/claims", json={
        "expected_revision": revision_of(client, mission_id),
        "statement": "项目版本为 1", "subject": "project",
        "predicate": "version", "value": "1",
        "context": {"time": "2026-10-23", "scope": "core",
                    "environment": "local", "definition": "run.txt 首行"},
        "evidence_ids": [evidence_id]})
    assert claim.status_code == 201, claim.text
    claim_id = claim.json()["claim"]["id"]
    unreviewed = client.get(f"/api/v1/missions/{mission_id}/trust").json()
    row = next(c for c in unreviewed["claims"] if c["id"] == claim_id)
    assert row.get("review") is None  # 审查前不冒充结论
    reviewed = client.post(f"/api/v1/missions/{mission_id}/trust/claims/{claim_id}/reviews", json={
        "expected_revision": revision_of(client, mission_id),
        "verdict": "INFERENCE",
        "reason": "Owner 明确复核本地输出；无独立外部来源，不标 FACT"})
    assert reviewed.status_code == 201, reviewed.text
    assert reviewed.json()["review"]["verdict"] == "INFERENCE"

    # ⑥ 证据快照 Artifact + 下载
    snapshotted = client.post(f"/api/v1/missions/{mission_id}/trust/proofs", json={
        "expected_revision": revision_of(client, mission_id)})
    assert snapshotted.status_code == 201, snapshotted.text
    proof = {"id": snapshotted.json()["id"], "sha256": snapshotted.json()["sha256"]}
    trust = client.get(f"/api/v1/missions/{mission_id}/trust").json()
    assert any(p.get("current") for p in trust["proofs"])

    # ⑦ Owner 验收快照：执行证据在案，验收待办应为空；验收是独立的人类动作
    assert trust["verification"]["acceptance_blockers"] == [], trust["verification"]
    accepted = client.post(f"/api/v1/missions/{mission_id}/trust/proofs/{proof['id']}/accept",
                           json={"sha256": proof["sha256"],
                                 "reason": "Owner 核对证据链与产物后接受本快照"})
    assert accepted.status_code == 200, accepted.text

    downloaded = client.get(
        f"/api/v1/missions/{mission_id}/trust/proofs/{proof['id']}/download")
    assert downloaded.status_code == 200 and downloaded.content[:2] == b"PK"

    # ⑧ 知识资产：只从已验收口径的快照收录
    knowledge = client.post(
        f"/api/v1/missions/{mission_id}/trust/proofs/{proof['id']}/knowledge", json={
            "sha256": proof["sha256"], "title": "版本结论（闭环 e2e）",
            "tags": ["e2e", "closed-loop"]})
    assert knowledge.status_code == 201, knowledge.text
    listing = client.get("/api/v1/knowledge").json()
    items = listing.get("items", listing) if isinstance(listing, dict) else listing
    assert any(isinstance(i, dict) and i.get("title") == "版本结论（闭环 e2e）"
               for i in items), \
        f"knowledge listing: {json.dumps(listing, ensure_ascii=False)[:400]}"

    # ⑧ 同一 Trace：使命事件流从头到尾是同一条链，且次序有据
    types = [e["type"] for e in client.get(f"/api/v1/missions/{mission_id}/events").json()]
    chain = ["INTENT_CONFIRMED", "INTENT_EXECUTION_AUTHORIZED",
             "OPENCODE_STARTED", "OUTPUT", "OPENCODE_COMPLETED",
             "TRUST_CLAIM_CREATED", "TRUST_KNOWLEDGE_SAVED"]
    positions = [types.index(t) for t in chain]
    assert all(t in types for t in chain), f"事件链缺失：{[t for t in chain if t not in types]}"
    assert positions == sorted(positions), "同一 Trace 的事件次序必须与因果一致"

    # ⑨ 重启恢复：新进程同一数据目录，一切可读、可下载、可重放
    client2 = make_client(tmp_path, monkeypatch)
    survived = client2.get(f"/api/v1/missions/{mission_id}").json()
    assert survived["id"] == mission_id and survived["steps"] == mission["steps"]
    assert survived["artifacts"] == mission["artifacts"]
    trust2 = client2.get(f"/api/v1/missions/{mission_id}/trust").json()
    assert trust2["revision"] == trust["revision"]
    row2 = next(c for c in trust2["claims"] if c["id"] == claim_id)
    assert row2["review"]["verdict"] == "INFERENCE"
    again = client2.get(f"/api/v1/missions/{mission_id}/trust/proofs/{proof['id']}/download")
    assert again.status_code == 200 and again.content == downloaded.content
    listing2 = client2.get("/api/v1/knowledge").json()
    items2 = listing2.get("items", listing2) if isinstance(listing2, dict) else listing2
    assert any(isinstance(i, dict) and i.get("title") == "版本结论（闭环 e2e）"
               for i in items2)
    # 全局事件流逐字节一致：重启前后同一份可重放 Trace
    replay_before = client.get("/api/v1/events?after=0").content
    replay_after = client2.get("/api/v1/events?after=0").content
    assert replay_before == replay_after and len(replay_before) > 1000
