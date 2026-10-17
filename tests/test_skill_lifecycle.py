"""SKILL-001 ①②③ 生命周期实施测试。

- ① version 落库：build_skill_metadata 保留 version，注册表校验接受合法
  version、拒绝非法 version；重注册携带新版本值；存量无 version 行保持无
  version（不造假回填）。
- ② 禁用生效：enabled is False 的技能不参与 conversion 打分路由
  （assign_stage / build_assignment），enabled 缺省视为启用。
- ③ 软删与治理 API：store 层 tombstone（列表排除、快照可查、幂等），
  API 层 POST .../enabled 与 DELETE 软删（管理员闸、非管理员 403）、
  boundary journal 留下 SKILL_DISABLED / SKILL_REMOVED 事件。
"""

import json

import pytest

from facai.conversion import assign_stage, build_assignment
from facai.execution import skill_runtime_prompt
from facai.registries import Registries, _validate_skill_metadata
from facai.skills import (build_skill_metadata, dispatchable_skill,
                          enabled_only, parse_skill_md)
from facai.store import Store

pytestmark = pytest.mark.raw_budget

SKILL_MD_V1 = """---
name: code-review
description: Automated code review for pull requests
license: MIT
version: 1.2.0
source_url: https://github.com/example/code-review
---

Body never enters the registry.
"""

SKILL_MD_V2 = SKILL_MD_V1.replace("version: 1.2.0", "version: 2.0.0")


# ---------------------------------------------------------------------------
# ① version 落库
# ---------------------------------------------------------------------------

class TestVersionLandsInMetadata:
    def test_version_carried_into_metadata(self):
        metadata = build_skill_metadata(parse_skill_md(SKILL_MD_V1))
        assert metadata["version"] == "1.2.0"
        _validate_skill_metadata("code-review", metadata)  # must not raise

    def test_no_version_stays_absent_never_fabricated(self):
        text = SKILL_MD_V1.replace("version: 1.2.0\n", "")
        metadata = build_skill_metadata(parse_skill_md(text))
        assert "version" not in metadata
        _validate_skill_metadata("code-review", metadata)  # must not raise

    def test_reregistration_carries_the_new_version(self, tmp_path):
        store = Store(tmp_path)
        reg = Registries(store)
        reg.register("skills", "code-review",
                     build_skill_metadata(parse_skill_md(SKILL_MD_V1)))
        assert reg.list("skills")["code-review"]["version"] == "1.2.0"
        reg.register("skills", "code-review",
                     build_skill_metadata(parse_skill_md(SKILL_MD_V2)))
        # 行被整条替换：新版本值生效，旧值不残留。
        assert reg.list("skills")["code-review"]["version"] == "2.0.0"

    def test_import_endpoint_persists_version(self, tmp_path, monkeypatch):
        client = _admin_client(tmp_path, monkeypatch)
        response = client.post("/api/v1/skills/import", json={
            "content": SKILL_MD_V1,
            "source_url": "https://github.com/example/code-review"})
        assert response.status_code == 201, response.text
        skills = client.get("/api/v1/registries/skills").json()
        assert skills["code-review"]["version"] == "1.2.0"
        digest = skills["code-review"]["body_sha256"]
        store = Store(tmp_path)
        assert store.skill_source_get("code-review", digest, owner_id="admin") == parse_skill_md(SKILL_MD_V1)["_body"]
        assert store.skill_source_get("code-review", digest, owner_id="other") is None
        assert "Body never enters" not in json.dumps(skills)

    def test_runtime_prompt_uses_exact_owner_scoped_source(self, tmp_path):
        store = Store(tmp_path)
        body = "Review the change against acceptance criteria."
        digest = store.skill_source_register("code-review", "1.2.0", body, "alice")
        mission = {"skill_id": "code-review", "owner_id": "alice"}
        skill = {"name": "code-review", "version": "1.2.0", "body_sha256": digest}
        runtime_prompt, loaded = skill_runtime_prompt(store, mission, skill, "Check project")
        assert runtime_prompt.startswith("Check project\n")
        assert body in runtime_prompt and loaded == digest
        with pytest.raises(ValueError, match="unavailable"):
            skill_runtime_prompt(store, {**mission, "owner_id": "bob"}, skill, "Check project")
        assert skill_runtime_prompt(store, mission, None, "Check project") == ("Check project", None)

    def test_import_without_version_leaves_no_version_key(self, tmp_path, monkeypatch):
        client = _admin_client(tmp_path, monkeypatch)
        content = SKILL_MD_V1.replace("version: 1.2.0\n", "")
        response = client.post("/api/v1/skills/import", json={
            "content": content,
            "source_url": "https://github.com/example/code-review"})
        assert response.status_code == 201, response.text
        skills = client.get("/api/v1/registries/skills").json()
        assert "version" not in skills["code-review"]


class TestVersionValidation:
    def test_valid_version_accepted(self):
        reg = _reg()
        result = reg.register("skills", "code-review",
                              _full_skill({"version": "1.0.0"}))
        assert result["version"] == "1.0.0"

    def test_whitespace_version_rejected(self):
        reg = _reg()
        with pytest.raises(ValueError, match="version"):
            reg.register("skills", "code-review", _full_skill({"version": "   "}))

    def test_integer_version_rejected(self):
        reg = _reg()
        with pytest.raises(ValueError, match="version"):
            reg.register("skills", "code-review", _full_skill({"version": 2}))

    def test_overlong_version_rejected(self):
        reg = _reg()
        with pytest.raises(ValueError, match="version"):
            reg.register("skills", "code-review",
                         _full_skill({"version": "v" * 65}))


# ---------------------------------------------------------------------------
# ② 禁用生效（conversion 层）
# ---------------------------------------------------------------------------

AGENTS = {"opencode": {"name": "OpenCode", "adapter": "opencode"},
          "goose": {"name": "Goose", "adapter": "goose"}}

CRAWLER_SKILL = {"distilled-crawler": {
    "description": "写爬虫抓取商品价格并落库",
    "steps": ["环境准备", "抓取", "落表"],
    "agent_id": "goose"}}


class TestDisabledSkillSkipsScoring:
    def test_enabled_skill_still_matches(self):
        tree = assign_stage("抓取阶段", "", AGENTS, CRAWLER_SKILL)
        assert tree["primary"]["agent_id"] == "goose"
        assert any("技能库匹配" in r for r in tree["primary"]["reasons"])

    def test_disabled_skill_never_matches(self):
        disabled = {"distilled-crawler": {**CRAWLER_SKILL["distilled-crawler"],
                                          "enabled": False}}
        # 阶段词刻意带 goose 关键词（"自动化""抓取"）：技能在时 goose 靠
        # 关键词+技能加成赢；技能被禁后加成消失，但关键词命中仍在——本用例
        # 只钉"禁用技能不产生任何技能匹配记录"，排位断言交给下面两个用例。
        tree = assign_stage("自动化批量抓取", "写爬虫抓取商品价格并存成表格",
                            AGENTS, disabled)
        assert all("技能库匹配" not in r
                   for row in [tree["primary"]] + tree["fallbacks"]
                   for r in row["reasons"])

    def test_enabled_skill_flips_ranking_via_professional(self):
        # 技能加权对所有 Agent 一视同仁，但 goose 的技能库归属（agent_id）
        # 不参与打分：真正翻盘的是把技能描述与 goose 关键词对齐。
        tree = assign_stage("自动化批量抓取", "写爬虫抓取商品价格并存成表格",
                            AGENTS, CRAWLER_SKILL)
        assert tree["primary"]["agent_id"] == "goose"
        assert any("技能库匹配" in r for r in tree["primary"]["reasons"])

    def test_mixed_disabled_and_enabled_ranks_only_enabled(self):
        skills = {**CRAWLER_SKILL,
                  "distilled-dead": {"description": "写爬虫抓取商品价格并落库",
                                     "enabled": False}}
        tree = assign_stage("抓取阶段", "", AGENTS, skills)
        matched = [r for row in [tree["primary"]] + tree["fallbacks"]
                   for r in row["reasons"] if "技能库匹配" in r]
        assert matched and "distilled-dead" not in matched[0]

    def test_assignment_tree_path_filters_disabled(self):
        steps = [{"id": "s1", "label": "主执行阶段"}]
        disabled = {"distilled-crawler": {**CRAWLER_SKILL["distilled-crawler"],
                                          "enabled": False}}
        tree = build_assignment(steps, AGENTS, disabled,
                                professional="写爬虫抓取商品价格并存成表格")
        assert tree[0]["assignment"]["primary"]["agent_id"] == "opencode"

    def test_enabled_only_view_semantics(self):
        assert set(enabled_only(CRAWLER_SKILL)) == {"distilled-crawler"}  # 缺省=启用
        disabled = {"a": {"enabled": False}, "b": {"enabled": True},
                    "c": {}, "d": None}
        # None 值条目与旧打分口径一致（skill or {}）照常放行。
        assert set(enabled_only(disabled)) == {"b", "c", "d"}
        assert enabled_only(None) == {}

    def test_dispatchable_skill_semantics(self):
        assert dispatchable_skill({"name": "x"}) == {"name": "x"}
        assert dispatchable_skill({"name": "x", "enabled": True}) is not None
        assert dispatchable_skill({"name": "x", "enabled": False}) is None
        assert dispatchable_skill(None) is None


# ---------------------------------------------------------------------------
# ③ 软删 + 治理 API + 事件留痕
# ---------------------------------------------------------------------------

class TestStoreSoftDelete:
    def test_deleted_skill_invisible_in_list_but_tombstone_queryable(self, tmp_path):
        store = Store(tmp_path)
        reg = Registries(store)
        reg.register("skills", "code-review",
                     build_skill_metadata(parse_skill_md(SKILL_MD_V1)))
        tombstone = reg.delete("skills", "code-review", deleted_by="admin")
        assert tombstone is not None
        assert tombstone["deleted_by"] == "admin"
        assert tombstone["snapshot"]["version"] == "1.2.0"
        assert tombstone["snapshot_sha256"]

        # 列表与 resolve 不可见；tombstone 可查。
        assert "code-review" not in store.registry_list("skills", owner_id="admin")
        assert reg.resolve("skills", "code-review") is None
        stone = store.registry_tombstone("skills", "code-review",
                                         owner_id="admin")
        assert stone["id"] == "code-review"
        assert stone["deleted_by"] == "admin"
        assert stone["snapshot"]["version"] == "1.2.0"

    def test_hard_delete_not_reachable_via_store_delete(self, tmp_path):
        """store.registry_delete 是软删；SQL 硬删只剩内部 _registry_hard_delete。"""
        store = Store(tmp_path)
        store.registry_register("skills", "x", {"name": "x"}, owner_id="admin")
        assert store.registry_delete("skills", "x", deleted_by="admin") is not None
        # 行还在（tombstone），不是 SQL DELETE。
        assert store.registry_tombstone("skills", "x", owner_id="admin") is not None
        # 内部维护函数才真正硬删。
        assert store._registry_hard_delete("skills", "x", owner_id="admin") is True
        assert store.registry_tombstone("skills", "x", owner_id="admin") is None

    def test_re_register_resurrects_the_key(self, tmp_path):
        store = Store(tmp_path)
        reg = Registries(store)
        reg.register("skills", "code-review",
                     build_skill_metadata(parse_skill_md(SKILL_MD_V1)))
        reg.delete("skills", "code-review", deleted_by="admin")
        reg.register("skills", "code-review",
                     build_skill_metadata(parse_skill_md(SKILL_MD_V2)))
        assert "code-review" in store.registry_list("skills", owner_id="admin")
        assert store.registry_tombstone("skills", "code-review",
                                        owner_id="admin") is None

    def test_other_kinds_still_soft_delete(self, tmp_path):
        store = Store(tmp_path)
        store.registry_register("models", "m-1", {"provider": "x"},
                                owner_id="admin")
        assert store.registry_delete("models", "m-1", deleted_by="admin") is not None
        assert "m-1" not in store.registry_list("models", owner_id="admin")
        assert store.registry_tombstone("models", "m-1",
                                        owner_id="admin") is not None


def _full_skill(overrides=None, key="code-review"):
    base = {
        "name": key,
        "description": "Automated code review skill",
        "source_url": "https://github.com/example/code-review",
        "license": "MIT",
        "review_status": "approved",
        "enabled": True,
    }
    if overrides:
        base.update(overrides)
    return base


def _reg():
    return Registries(store=None)


def _admin_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from facai.api import create_app
    monkeypatch.setenv("FACAI_ADMIN_PASSWORD", "lifecycle-pass-1")
    monkeypatch.delenv("FACAI_COOKIE_SECURE", raising=False)
    client = TestClient(create_app(tmp_path))
    session = client.post("/api/v1/auth/login",
                          json={"username": "admin",
                                "password": "lifecycle-pass-1"}).json()
    client.headers.update({"X-CSRF-Token": session["csrf_token"]})
    return client


def _register_via_import(client, content=SKILL_MD_V1):
    response = client.post("/api/v1/skills/import", json={
        "content": content,
        "source_url": "https://github.com/example/code-review"})
    assert response.status_code == 201, response.text
    return response.json()


class TestSkillLifecycleApi:
    def test_disable_and_enable_round_trip_with_journal(self, tmp_path, monkeypatch):
        client = _admin_client(tmp_path, monkeypatch)
        _register_via_import(client)
        disabled = client.post("/api/v1/registries/skills/code-review/enabled",
                               json={"enabled": False, "note": "暂停使用"})
        assert disabled.status_code == 200, disabled.text
        assert disabled.json() == {"id": "code-review", "enabled": False}
        entry = client.get("/api/v1/registries/skills").json()["code-review"]
        assert entry["enabled"] is False

        journal = client.get("/api/v1/boundary/journal").json()["journal"]
        decisions = [e["decision"] for e in journal]
        assert "SKILL_DISABLED" in decisions
        disabled_event = next(e for e in journal
                              if e["decision"] == "SKILL_DISABLED")
        assert disabled_event["skill"] == "code-review"
        assert disabled_event["actor"] == "admin"

        enabled = client.post("/api/v1/registries/skills/code-review/enabled",
                              json={"enabled": True})
        assert enabled.status_code == 200
        journal = client.get("/api/v1/boundary/journal").json()["journal"]
        assert "SKILL_ENABLED" in [e["decision"] for e in journal]

    def test_disable_unknown_skill_404(self, tmp_path, monkeypatch):
        client = _admin_client(tmp_path, monkeypatch)
        missing = client.post("/api/v1/registries/skills/nope/enabled",
                              json={"enabled": False})
        assert missing.status_code == 404

    def test_delete_is_soft_with_tombstone_and_journal(self, tmp_path, monkeypatch):
        client = _admin_client(tmp_path, monkeypatch)
        _register_via_import(client)
        removed = client.delete("/api/v1/registries/skills/code-review")
        assert removed.status_code == 200, removed.text
        body = removed.json()
        assert body["deleted"] is True and body["deleted_by"] == "admin"
        assert body["snapshot_sha256"]

        # 列表不可见。
        assert "code-review" not in client.get("/api/v1/registries/skills").json()
        # tombstone 可查，快照保住 version。
        stone = client.get(
            "/api/v1/registries/skills/code-review/tombstone").json()
        assert stone["id"] == "code-review"
        assert stone["deleted_by"] == "admin"
        assert stone["snapshot"]["version"] == "1.2.0"
        # journal 留痕。
        journal = client.get("/api/v1/boundary/journal").json()["journal"]
        removed_events = [e for e in journal
                          if e["decision"] == "SKILL_REMOVED"
                          and e["skill"] == "code-review"]
        assert removed_events
        assert removed_events[0]["snapshot_sha256"] == body["snapshot_sha256"]

    def test_delete_is_idempotent(self, tmp_path, monkeypatch):
        """重复删除同一个技能：第二次删除幂等地 404（已删等同于不存在）。"""
        client = _admin_client(tmp_path, monkeypatch)
        _register_via_import(client)
        first = client.delete("/api/v1/registries/skills/code-review")
        assert first.status_code == 200
        second = client.delete("/api/v1/registries/skills/code-review")
        assert second.status_code == 404
        # tombstone 依然在（第一次删除的证据没有被第二次动过）。
        stone = client.get(
            "/api/v1/registries/skills/code-review/tombstone")
        assert stone.status_code == 200

    def test_tombstone_endpoint_for_live_skill_404(self, tmp_path, monkeypatch):
        client = _admin_client(tmp_path, monkeypatch)
        _register_via_import(client)
        assert client.get(
            "/api/v1/registries/skills/code-review/tombstone").status_code == 404

    def test_tenant_forbidden_on_lifecycle_surfaces(self, tmp_path, monkeypatch):
        """非管理员（自注册租户）对禁用/删除/tombstone 一律 403。"""
        client = _admin_client(tmp_path, monkeypatch)
        _register_via_import(client)
        tenant = client.post("/api/v1/auth/register",
                             json={"username": "mallory",
                                   "password": "mallory-pass-1"})
        assert tenant.status_code == 201, tenant.text
        client.headers.update({"X-CSRF-Token": tenant.json()["csrf_token"]})
        assert client.post(
            "/api/v1/registries/skills/code-review/enabled",
            json={"enabled": False}).status_code == 403
        assert client.delete(
            "/api/v1/registries/skills/code-review").status_code == 403
        assert client.get(
            "/api/v1/registries/skills/code-review/tombstone").status_code == 403
        # 租户的动作没有留下任何 journal 事件（闸门前就挡住了）。
        client.headers.update({"X-CSRF-Token": _relogin_admin(
            client, monkeypatch)})
        journal = client.get("/api/v1/boundary/journal").json()["journal"]
        assert all(e.get("actor") != "mallory" for e in journal)

    def test_lifecycle_endpoints_require_authentication(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from facai.api import create_app
        monkeypatch.setenv("FACAI_ADMIN_PASSWORD", "lifecycle-pass-1")
        client = TestClient(create_app(tmp_path))
        assert client.post("/api/v1/registries/skills/x/enabled",
                           json={"enabled": False}).status_code == 401
        assert client.delete("/api/v1/registries/skills/x").status_code == 401


def _relogin_admin(client, monkeypatch):
    session = client.post("/api/v1/auth/login",
                          json={"username": "admin",
                                "password": "lifecycle-pass-1"}).json()
    return session["csrf_token"]


class TestDisabledSkillApiSurface:
    def test_conversion_plan_skips_disabled_skill(self, tmp_path, monkeypatch):
        client = _admin_client(tmp_path, monkeypatch)
        _register_via_import(client)
        client.post("/api/v1/registries/skills/code-review/enabled",
                    json={"enabled": False})
        plan = client.post("/api/v1/conversion/plan",
                           json={"text": "做代码评审自动化"})
        assert plan.status_code == 200, plan.text
        dumped = json.dumps(plan.json()["assignment_tree"], ensure_ascii=False)
        assert "code-review" not in dumped

    def test_conversion_plan_lists_enabled_skill(self, tmp_path, monkeypatch):
        client = _admin_client(tmp_path, monkeypatch)
        _register_via_import(client)
        plan = client.post("/api/v1/conversion/plan",
                           json={"text": "做代码评审自动化"})
        dumped = json.dumps(plan.json()["assignment_tree"], ensure_ascii=False)
        assert "code-review" in dumped

    def test_deleted_skill_not_listed_in_registry(self, tmp_path, monkeypatch):
        client = _admin_client(tmp_path, monkeypatch)
        _register_via_import(client)
        client.delete("/api/v1/registries/skills/code-review")
        assert "code-review" not in client.get("/api/v1/registries/skills").json()


class TestSkillUsage:
    def test_attribution_requires_resolved_skill_and_captures_version(self):
        from facai.execution import mission_attribution
        mission = {"skill_id": "code-review", "agent_id": "opencode"}
        assert "skill_id" not in mission_attribution(mission, {}, skill=None)
        active = mission_attribution(mission, {}, skill={
            "name": "code-review", "version": "1.2.0"})
        assert active["skill_id"] == "code-review"
        assert active["skill_version"] == "1.2.0"
        assert active["skill_invocation_verified"] is False

    def test_usage_counts_only_resolved_outcomes_by_owner_and_version(self, tmp_path):
        store = Store(tmp_path)
        own = store.create("Own", "request", ["step"], owner_id="admin")["id"]
        other = store.create("Other", "request", ["step"], owner_id="tenant")["id"]
        for mission_id, status, payload in [
            (own, "OPENCODE_STARTED", {"skill_id": "code-review", "skill_name": "code-review"}),
            (own, "OPENCODE_COMPLETED", {"skill_id": "code-review", "skill_name": "code-review",
                                             "skill_version": "1.2.0"}),
            (own, "MODEL_RATE_LIMITED", {"skill_id": "code-review", "skill_name": "code-review",
                                           "skill_version": "2.0.0"}),
            (own, "OPENCODE_FAILED", {"skill_id": "code-review"}),
            (other, "OPENCODE_COMPLETED", {"skill_id": "code-review", "skill_name": "code-review"}),
        ]:
            store.append(mission_id, status, payload)
        usage = store.skill_usage("code-review", owner_id="admin")
        assert usage["observed_results"] == 2
        assert usage["completed"] == 1
        assert usage["other_outcomes"] == 1
        assert usage["completion_rate"] == 0.5
        assert usage["skill_invocation_verified"] is False
        assert usage["verified_invocations"] == 0
        assert usage["attribution_only_results"] == 2
        assert usage["source"] == "resolved_skill_attributed_terminal_events"
        assert {row["version"]: row["observed_results"] for row in usage["by_version"]} \
            == {"1.2.0": 1, "2.0.0": 1}
        assert {row["mission_id"] for row in usage["recent"]} == {own}
        assert store.skill_usage("code-review", owner_id="missing")["completion_rate"] is None

    def test_usage_separates_verified_body_attachment_from_legacy_attribution(self, tmp_path):
        store = Store(tmp_path)
        mission_id = store.create("Review", "request", ["step"], owner_id="admin")["id"]
        base = {"skill_id": "code-review", "skill_name": "code-review",
                "skill_version": "1.2.0"}
        store.append(mission_id, "OPENCODE_COMPLETED", base)
        store.append(mission_id, "OPENCODE_FAILED", {
            **base, "skill_invocation_verified": True, "skill_body_sha256": "a" * 64})
        usage = store.skill_usage("code-review")
        assert usage["observed_results"] == 2
        assert usage["verified_invocations"] == 1
        assert usage["attribution_only_results"] == 1
        assert usage["skill_invocation_verified"] is False
        assert store.skill_usage("code-review", owner_id="other")["observed_results"] == 0

    def test_usage_endpoint_survives_soft_remove_without_cross_tenant_leak(self, tmp_path, monkeypatch):
        client = _admin_client(tmp_path, monkeypatch)
        _register_via_import(client)
        store = client.app.state.store
        mission_id = store.create("Review", "request", ["step"], owner_id="admin")["id"]
        store.append(mission_id, "OPENCODE_COMPLETED", {
            "skill_id": "code-review", "skill_name": "code-review", "skill_version": "1.2.0"})
        path = "/api/v1/registries/skills/code-review/usage"
        assert client.get(path).json()["observed_results"] == 1
        assert client.delete("/api/v1/registries/skills/code-review").status_code == 200
        assert client.get(path).json()["observed_results"] == 1
        tenant = client.post("/api/v1/auth/register", json={
            "username": "usage-tenant", "password": "usage-pass-1234"})
        assert tenant.status_code == 201
        client.headers.update({"X-CSRF-Token": tenant.json()["csrf_token"]})
        assert client.get(path).status_code == 404
