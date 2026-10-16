"""P0-14 Backend Router: §15 backend states reroute the job, never the mission.

Frozen semantics under test (docs/AUTHORITY_ARCHITECTURE.md §14-§16):
- PROVIDER_REFUSAL 是后端状态（THIS BACKEND DID NOT EXECUTE），不是任务失败：
  拒绝后换注册表里的下一个 backend 重跑，任务目标不变，Mission Grant 不动。
- 没有替代 backend 时诚实 FAILED（诚实降级，绝不虚构替代）。
- 同一 job 最多 reroute 2 次，超过即 FAILED（防乒乓）。
- MISSION_DENIED 是本地门的裁决，永远不路由。
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from facai import router
from facai.authority_gate import reset_engine
from facai.quota import QuotaPools
from facai.registries import Registries
from facai.scheduler import Scheduler
from facai.store import Store

DEEPSEEK_REF = "deepseek/deepseek-flash"
GLM_REF = "facai-glm/glm-4.6"
KIMI_REF = "moonshot/kimi-k2"
REFUSAL_STDERR = "upstream model refused the request (content_filter)"
GLM_MODEL = {"name": "GLM Local (Ollama)", "provider": "Ollama",
             "endpoint": "http://127.0.0.1:9900/v1", "model": "glm-4.6",
             "opencode_model": GLM_REF}
KIMI_MODEL = {"name": "Kimi", "provider": "Moonshot",
              "endpoint": "https://api.moonshot.cn", "model": "kimi-k2",
              "opencode_model": KIMI_REF}


class BackendStubAdapter:
    """按 model id（--model ref）区分调用的 stub 适配器。

    路由换 backend 对适配器层的全部可见影响就是 model_ref 变了，所以 stub
    记录每次被要求的 ref：``refuse_refs`` 里的 ref 返回携带拒绝证据的失败，
    其余 ref 成功；``refuse_everything`` 时全部拒绝。
    """

    def __init__(self, refuse_refs=(), refuse_everything=False, stderr=REFUSAL_STDERR):
        self.refuse_refs = set(refuse_refs)
        self.refuse_everything = refuse_everything
        self.stderr = stderr
        self.calls = []

    def execute(self, mission_id, root, prompt, model_ref=None, timeout=300,
                on_output=None, **kwargs):
        self.calls.append(model_ref)
        workspace = Path(root) / "workspaces" / mission_id
        workspace.mkdir(parents=True, exist_ok=True)
        refused = self.refuse_everything or model_ref in self.refuse_refs
        if refused:
            return {"status": "OPENCODE_FAILED", "exit_code": 1,
                    "workspace": str(workspace), "stdout": "", "stderr": self.stderr,
                    "streamed": False}
        return {"status": "OPENCODE_COMPLETED", "exit_code": 0,
                "workspace": str(workspace),
                "stdout": json.dumps({"part": {"type": "text", "text": "ok"}}) + "\n",
                "stderr": "", "streamed": False}


def build(tmp_path, adapter, concurrency=1):
    store = Store(tmp_path)
    registries = Registries(store)
    scheduler = Scheduler(store, adapter, registries, tmp_path, concurrency=concurrency,
                          worker_id="router-test")
    return store, registries, scheduler


def register_alt_models(registries):
    """注册两个替代 backend：glm-local（优先席位）与 kimi（注册序在后）。"""
    registries.register("models", "glm-local", dict(GLM_MODEL))
    registries.register("models", "kimi", dict(KIMI_MODEL))


def make_mission(store, title="R"):
    mission = store.create(title, f"input {title}", ["开发"])
    store.bind(mission["id"], "opencode", "deepseek-flash")
    return store.get(mission["id"])


def event_types(store, mission_id):
    return [event["type"] for event in store.events(mission_id)]


def test_refusal_reroutes_to_next_backend_and_completes(tmp_path):
    """拒绝后换 backend 重跑成功：deepseek 拒绝 → glm-local 接手 → 任务完成。"""
    adapter = BackendStubAdapter(refuse_refs={DEEPSEEK_REF})
    store, registries, scheduler = build(tmp_path, adapter)
    register_alt_models(registries)
    mission = make_mission(store)
    job = store.enqueue_job(mission["id"], "写一首诗")

    scheduler.run_pending()

    final = store.get_job(job["id"])
    assert final["status"] == "COMPLETED"
    # 两次调用按 model id 区分：先拒绝的 deepseek，后接手的 glm-local。
    assert adapter.calls == [DEEPSEEK_REF, GLM_REF]
    assert final["model_ref"] == GLM_REF
    assert final["error"] is None
    assert final["attempts"] == 2  # 两次认领各计一次，reroute 本身不动 attempts
    events = store.events(mission["id"])
    rerouted = [event for event in events if event["type"] == "MISSION_REROUTED"]
    assert len(rerouted) == 1
    payload = rerouted[0]["payload"]
    assert payload["from"] == "deepseek-flash"
    assert payload["to"] == "glm-local"
    assert payload["reason"]
    # 用户可见文案：任务目标没有改变，只是换了一种执行方式。
    assert "任务目标没有改变，已切换执行方式" in payload["message"]
    assert "glm-local" in payload["message"]
    attempts = [event for event in events if event["type"] == "BACKEND_ATTEMPT"]
    assert len(attempts) == 1
    assert attempts[0]["payload"]["backend"] == "deepseek-flash"
    assert attempts[0]["payload"]["state"] == "PROVIDER_REFUSAL"
    assert "THIS BACKEND DID NOT EXECUTE" in attempts[0]["payload"]["message"]
    # 拒绝不构成任务失败：没有 JOB_FAILED，任务最终完成。
    assert "JOB_FAILED" not in event_types(store, mission["id"])


def test_no_alternative_backend_fails_honestly(tmp_path):
    """注册表没有其他 backend 时照旧 FAILED：诚实降级，不虚构替代。"""
    adapter = BackendStubAdapter(refuse_refs={DEEPSEEK_REF})
    store, registries, scheduler = build(tmp_path, adapter)
    mission = make_mission(store)
    job = store.enqueue_job(mission["id"], "task")

    scheduler.run_pending()

    final = store.get_job(job["id"])
    assert final["status"] == "FAILED"
    assert "refused" in final["error"]
    assert final["model_ref"] is None  # 未被 reroute 改写（入队时未显式指定 ref）
    assert router.reroute_target(final["error"]) is None  # error 无 reroute marker
    assert adapter.calls == [DEEPSEEK_REF]
    types = event_types(store, mission["id"])
    assert "MISSION_REROUTED" not in types
    assert "BACKEND_ATTEMPT" not in types
    assert "JOB_FAILED" in types


def test_reroute_ceiling_two_per_job_then_failed(tmp_path):
    """同一 job 最多 reroute 2 次：A→B→A 后第三种拒绝只能 FAILED（防乒乓）。"""
    adapter = BackendStubAdapter(refuse_everything=True)
    store, registries, scheduler = build(tmp_path, adapter)
    register_alt_models(registries)
    mission = make_mission(store)
    job = store.enqueue_job(mission["id"], "task", max_attempts=1)

    scheduler.run_pending()

    final = store.get_job(job["id"])
    assert final["status"] == "FAILED"
    events = store.events(mission["id"])
    rerouted = [event for event in events if event["type"] == "MISSION_REROUTED"]
    assert len(rerouted) == router.MAX_REROUTES_PER_JOB == 2
    # A→B→A：第三次执行再拒绝时事件计数已达上限，不再路由。
    assert adapter.calls == [DEEPSEEK_REF, GLM_REF, DEEPSEEK_REF]
    assert final["attempts"] == 3
    # reroute 与重试预算正交：max_attempts=1 也照常换 backend（attempts 语义不变）。
    assert "JOB_FAILED" in event_types(store, mission["id"])
    for payload in (event["payload"] for event in rerouted):
        assert payload["from"] and payload["to"] and payload["from"] != payload["to"]
        assert payload["reason"]


def test_gate_loss_auto_renews_and_never_denies(tmp_path, monkeypatch):
    """无审核：授权状态丢失（进程重启语义）由平台核自动补签，任务照常完成；
    门永远不再产生 MISSION_DENIED。适配器若拒绝某 backend，那是 §15 重路由
    的本职（与授权门无关）。"""
    monkeypatch.delenv("FACAI_GATE_MODE", raising=False)
    reset_engine()
    try:
        adapter = BackendStubAdapter(refuse_refs={DEEPSEEK_REF})
        store, registries, scheduler = build(tmp_path, adapter)
        register_alt_models(registries)
        mission = make_mission(store)
        job = store.enqueue_job(mission["id"], "task")
        # 模拟授权状态丢失（进程重启语义）。
        reset_engine()

        scheduler.run_pending()

        final = store.get_job(job["id"])
        assert final["status"] == "COMPLETED"
        types = event_types(store, mission["id"])
        assert "GRANT_AUTO_RENEWED" in types
        assert "MISSION_DENIED" not in types
    finally:
        reset_engine()


def test_rerouted_binding_survives_transient_backoff(tmp_path):
    """reroute 目标在瞬态退避下不丢：换到 glm-local 后遇超时，仍在 glm-local 重跑。"""
    calls = []

    def execute(mission_id, root, prompt, model_ref=None, timeout=300,
                on_output=None, **kwargs):
        calls.append(model_ref)
        workspace = Path(root) / "workspaces" / mission_id
        workspace.mkdir(parents=True, exist_ok=True)
        if model_ref == DEEPSEEK_REF:
            return {"status": "OPENCODE_FAILED", "exit_code": 1,
                    "workspace": str(workspace), "stdout": "",
                    "stderr": REFUSAL_STDERR, "streamed": False}
        if len(calls) == 2:
            return {"status": "OPENCODE_FAILED", "exit_code": 1,
                    "workspace": str(workspace), "stdout": "",
                    "stderr": "upstream timeout after 30s", "streamed": False}
        return {"status": "OPENCODE_COMPLETED", "exit_code": 0,
                "workspace": str(workspace),
                "stdout": json.dumps({"part": {"type": "text", "text": "ok"}}) + "\n",
                "stderr": "", "streamed": False}

    adapter = type("Stub", (), {"execute": staticmethod(execute)})()
    store, registries, scheduler = build(tmp_path, adapter)
    register_alt_models(registries)
    mission = make_mission(store)
    job = store.enqueue_job(mission["id"], "task", max_attempts=3)

    scheduler.run_pending()
    # 第二次运行（glm-local 超时）是瞬态失败：同后端指数退避，等待期内不可认领。
    paused = store.get_job(job["id"])
    assert paused["status"] == "QUEUED" and calls == [DEEPSEEK_REF, GLM_REF]
    assert router.reroute_target(paused["error"]) == "glm-local"  # marker 随退避存活
    with sqlite3.connect(tmp_path / "facai.sqlite3") as db:
        db.execute("UPDATE jobs SET not_before=? WHERE id=?",
                   ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                    job["id"]))
    scheduler.run_pending()

    final = store.get_job(job["id"])
    assert final["status"] == "COMPLETED"
    assert calls == [DEEPSEEK_REF, GLM_REF, GLM_REF]  # 仍在换到的 backend 上重跑
    assert final["model_ref"] == GLM_REF
    assert len([e for e in store.events(mission["id"])
                if e["type"] == "MISSION_REROUTED"]) == 1


def test_outcome_state_classification():
    """调度器路径的失败证据 → §15 状态；裁决族永不入可路由集合。"""
    assert router.outcome_state(status="OPENCODE_TIMEOUT") == "TIMEOUT"
    assert router.outcome_state(text="upstream refused (content_filter)") == "PROVIDER_REFUSAL"
    assert router.outcome_state(text="HTTP 503 unavailable") == "BACKEND_UNAVAILABLE"
    assert router.outcome_state(text="MISSION_DENIED: GRANT_REQUIRED") == "FACAI_DENIED"
    assert router.outcome_state(text="boom") == "EXECUTION_FAILED"
    assert router.outcome_state() == "EXECUTION_FAILED"
    assert router.ROUTABLE_STATES == {"PROVIDER_REFUSAL", "BACKEND_UNAVAILABLE", "TIMEOUT"}
    assert "FACAI_DENIED" not in router.ROUTABLE_STATES
    assert "EXECUTION_FAILED" not in router.ROUTABLE_STATES


def test_pick_backend_order_exclusion_and_honest_none():
    mission = {"model_id": "deepseek-flash"}
    models = {"deepseek-flash": dict(GLM_MODEL), "kimi": dict(KIMI_MODEL),
              "glm-local": dict(GLM_MODEL)}
    # glm-local（若在）优先，其余按注册序。
    assert router.pick_backend(mission, "deepseek-flash", models) == "glm-local"
    assert router.pick_backend(mission, "glm-local", models) == "deepseek-flash"
    # failed_backend 缺省排除 mission 当前绑定。
    assert router.pick_backend(mission, None, models) == "glm-local"
    # 没有替代/没有注册表 → 诚实返回 None（调用方照旧 FAILED）。
    assert router.pick_backend(mission, "deepseek-flash",
                               {"deepseek-flash": dict(GLM_MODEL)}) is None
    assert router.pick_backend(mission, None, None) is None
    assert router.pick_backend(mission, None, {}) is None


def test_marker_roundtrip_and_copy():
    error = router.reroute_error("glm-local", "任务目标没有改变，已切换执行方式：x y，改由 glm-local 继续。")
    assert router.reroute_target(error) == "glm-local"
    assert router.reroute_target("transient failure, auto-retry in 300s: boom") is None
    assert router.reroute_target(None) is None
    assert router.reroute_marker_prefix("transient") == ""
    assert router.reroute_marker_prefix(error) == "rerouted:glm-local|"
    copy_text = router.reroute_copy("deepseek-flash", "glm-local", "PROVIDER_REFUSAL")
    assert copy_text.startswith("任务目标没有改变，已切换执行方式")
    assert "deepseek-flash" in copy_text and "glm-local" in copy_text


# ---------------------------------------------------------------------------
# MODEL-001 配额池接线（2026-10-16）：reserve/report 进真实派发路径，
# reroute 吃池健康序。全部用 BackendStubAdapter 模拟，零真实网络调用。

POOL_MODELS = {"deepseek-flash": {}, "glm-local": {}, "kimi": {}}


def build_with_pools(tmp_path, adapter, pools):
    store = Store(tmp_path)
    registries = Registries(store)
    scheduler = Scheduler(store, adapter, registries, tmp_path, concurrency=1,
                          worker_id="router-test", quota_pools=pools)
    return store, registries, scheduler


def test_cooling_pool_dispatch_reroutes_with_trail(tmp_path):
    """① 池冷却中的模型派发走既有故障转移且留痕：reserve 被拒 →
    BACKEND_ATTEMPT + MISSION_REROUTED，改由健康池接手，绝不假成功。"""
    pools = QuotaPools(POOL_MODELS)
    pools.report("deepseek-flash", ok=False, rate_limited=True)  # 429 → 冷却 10s
    adapter = BackendStubAdapter()  # 全部成功：证明冷却池从未被真正派发
    store, registries, scheduler = build_with_pools(tmp_path, adapter, pools)
    register_alt_models(registries)
    mission = make_mission(store)
    job = store.enqueue_job(mission["id"], "task")

    scheduler.run_pending()

    final = store.get_job(job["id"])
    assert final["status"] == "COMPLETED"
    assert adapter.calls == [GLM_REF]  # 冷却的 deepseek-flash 从未进适配器
    assert final["model_ref"] == GLM_REF
    events = store.events(mission["id"])
    attempts = [e for e in events if e["type"] == "BACKEND_ATTEMPT"]
    assert len(attempts) == 1
    assert attempts[0]["payload"]["backend"] == "deepseek-flash"
    assert attempts[0]["payload"]["state"] == "BACKEND_UNAVAILABLE"
    rerouted = [e for e in events if e["type"] == "MISSION_REROUTED"]
    assert len(rerouted) == 1
    assert rerouted[0]["payload"]["from"] == "deepseek-flash"
    assert rerouted[0]["payload"]["to"] == "glm-local"
    # 池记账：被拒池无 ok 计数，健康池收到真实执行的 report。
    assert pools.matrix()["deepseek-flash"]["ok_count"] == 0
    assert pools.matrix()["glm-local"]["ok_count"] == 1


def test_pool_denied_without_alternative_requeues_never_fakes(tmp_path):
    """唯一模型的池冷却且注册表无替代：不假成功也不误判 FAILED——
    诚实退避重排队（冷却的池会自愈），适配器从未被触碰。"""
    pools = QuotaPools({"deepseek-flash": {}})
    pools.report("deepseek-flash", ok=False, rate_limited=True)
    adapter = BackendStubAdapter()
    store, registries, scheduler = build_with_pools(tmp_path, adapter, pools)
    mission = make_mission(store)
    job = store.enqueue_job(mission["id"], "task")

    scheduler.run_pending()

    final = store.get_job(job["id"])
    assert final["status"] == "QUEUED"
    assert adapter.calls == []
    assert "quota pool unavailable" in final["error"]
    assert "MISSION_REROUTED" not in event_types(store, mission["id"])


def test_healthy_pool_reserve_report_drive_watermarks(tmp_path):
    """② 池正常时 reserve/report 被真实派发调用：ok 计数、分钟水位、
    调用次数与延迟统计全部推进，并发水位归零。"""
    pools = QuotaPools(POOL_MODELS)
    adapter = BackendStubAdapter()
    store, registries, scheduler = build_with_pools(tmp_path, adapter, pools)
    mission = make_mission(store)
    job = store.enqueue_job(mission["id"], "task")

    scheduler.run_pending()

    assert store.get_job(job["id"])["status"] == "COMPLETED"
    row = pools.matrix()["deepseek-flash"]
    assert row["ok_count"] == 1 and row["fail_count"] == 0
    assert row["minute_used"] == 1 and row["inflight"] == 0
    assert row["reserved_count"] == 1 and row["reported_count"] == 1
    assert row["latency_avg_ms"] is not None and row["latency_avg_ms"] >= 0
    assert row["latency_recent_avg_ms"] is not None
    assert row["error_rate"] == 0.0


def test_rate_limited_failure_puts_pool_into_cooldown(tmp_path):
    """④ 真实流量的 429 失败驱动池冷却：fail 计数 + 指数冷却入梯，
    reroute（健康序）照常换到健康池完成任务。"""
    pools = QuotaPools(POOL_MODELS)
    adapter = BackendStubAdapter(refuse_refs={DEEPSEEK_REF},
                                 stderr="upstream 429 rate limited")
    store, registries, scheduler = build_with_pools(tmp_path, adapter, pools)
    register_alt_models(registries)
    mission = make_mission(store)
    job = store.enqueue_job(mission["id"], "task", max_attempts=1)

    scheduler.run_pending()

    final = store.get_job(job["id"])
    assert final["status"] == "COMPLETED"
    row = pools.matrix()["deepseek-flash"]
    assert row["fail_count"] == 1 and row["ok_count"] == 0
    assert row["cooldown_remaining"] > 0
    assert row["error_rate"] == 1.0
    assert pools.matrix()["glm-local"]["ok_count"] == 1


def test_models_without_pools_keep_legacy_behavior(tmp_path):
    """③ 没有注册池的模型行为与改动前完全一致：拒绝 → 故障转移照旧，
    池上不产生任何记账（回归保护）。"""
    pools = QuotaPools({"unrelated-model": {}})
    adapter = BackendStubAdapter(refuse_refs={DEEPSEEK_REF})
    store, registries, scheduler = build_with_pools(tmp_path, adapter, pools)
    register_alt_models(registries)
    mission = make_mission(store)
    job = store.enqueue_job(mission["id"], "task")

    scheduler.run_pending()

    final = store.get_job(job["id"])
    assert final["status"] == "COMPLETED"
    assert adapter.calls == [DEEPSEEK_REF, GLM_REF]  # 与无池时逐位一致
    assert len([e for e in store.events(mission["id"])
                if e["type"] == "MISSION_REROUTED"]) == 1
    assert pools.matrix()["unrelated-model"]["ok_count"] == 0
    assert pools.matrix()["unrelated-model"]["reserved_count"] == 0
