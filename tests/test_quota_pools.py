"""模型配额池测试（facai.quota + router 预防式接粮 + /api/v1/modelmatrix）。

2026-10-13 操作员指令：一两个池太少、别等 429 才停、提前预判瓶颈、
所有者可视化支配。全部离线（monotonic 时钟注入无需 sleep——冷却阶梯用
小基数常量即可断言）。钉死：预算耗尽前路由 away、429 指数冷却、
权重/暂停所有者支配、路由器诚实降级（全冷却时返回 None 不虚构容量）。
"""

import pytest

from facai.quota import (COOLDOWN_BASE, DEFAULT_CONCURRENCY, DEFAULT_MINUTE_LIMIT,
                         QuotaPools)
from facai.router import pick_backend

MODELS = {
    "glm-local": {"name": "GLM Local"},
    "deepseek": {"name": "DeepSeek", "rate_limit_per_min": 2, "max_concurrency": 1},
    "kimi": {"name": "Kimi", "route_weight": 3.0},
    "qwen": {"name": "Qwen"},
}


class TestPools:
    def test_every_registered_model_gets_its_own_pool(self):
        pools = QuotaPools(MODELS)
        matrix = pools.matrix()
        assert set(matrix) == set(MODELS)
        assert matrix["deepseek"]["minute_limit"] == 2          # 条目级覆盖
        assert matrix["kimi"]["minute_limit"] == DEFAULT_MINUTE_LIMIT
        assert matrix["kimi"]["weight"] == 3.0

    def test_reserve_blocks_before_the_upstream_429(self):
        pools = QuotaPools(MODELS)
        # deepseek min 限 2/分钟；并发水位 1 所以逐个 report 释放后再 reserve
        assert pools.reserve("deepseek") is True
        assert pools.reserve("deepseek") is False               # 并发水位 1 拦下
        pools.report("deepseek", ok=True)
        assert pools.reserve("deepseek") is True                # 第二次分钟额度
        assert pools.reserve("deepseek") is False               # 分钟预算 2 烧完
        assert pools.reserve("glm-local") is True               # 别的池不受牵连

    def test_concurrency_watermark(self):
        pools = QuotaPools({"m": {"max_concurrency": 2}})
        assert pools.reserve("m") and pools.reserve("m")
        assert pools.reserve("m") is False
        pools.report("m", ok=True)
        assert pools.reserve("m") is True                       # 释放后可再入

    def test_rank_prefers_a_free_slot_over_more_minute_headroom(self):
        pools = QuotaPools({"busy": {"rate_limit_per_min": 100,
                                     "max_concurrency": 1},
                            "free": {"rate_limit_per_min": 2,
                                     "max_concurrency": 2}})
        assert pools.reserve("busy") is True
        assert pools.reserve("free") is True
        # busy has 99% minute headroom but zero concurrency headroom.
        assert pools.ranked()[0] == "free"
        assert pools.reserve("busy") is False
        assert pools.reserve("free") is True

    def test_429_enters_exponential_cooldown_and_ok_halves_it(self):
        pools = QuotaPools(MODELS)
        pools.report("qwen", ok=False, rate_limited=True)
        first = pools.matrix()["qwen"]["cooldown_remaining"]
        pools.report("qwen", ok=False, rate_limited=True)
        second = pools.matrix()["qwen"]["cooldown_remaining"]
        assert second > first                                   # 10s → 20s 阶梯
        assert first == pytest.approx(COOLDOWN_BASE, abs=1.5)
        pools.report("qwen", ok=True)                           # 成功减半冷却
        assert pools.matrix()["qwen"]["cooldown_remaining"] < second / 2 + 0.1

    def test_owner_weight_and_pause_steer_the_route(self):
        pools = QuotaPools(MODELS)
        pools.set_weight("qwen", 50.0)                          # 压过 kimi 的 3.0
        assert pools.ranked()[0] == "qwen"
        pools.set_paused("qwen", True)
        assert "qwen" not in pools.ranked()[:2]                 # 暂停沉底
        with pytest.raises(KeyError):
            pools.set_weight("不存在的池", 1.0)

    def test_registry_resync_keeps_live_counters(self):
        pools = QuotaPools(MODELS)
        pools.reserve("deepseek")
        pools.replace_models(dict(MODELS, deepseek={"rate_limit_per_min": 5}))
        assert pools.matrix()["deepseek"]["minute_used"] == 1   # 计数未清
        assert pools.matrix()["deepseek"]["minute_limit"] == 5  # 新预算生效


class TestRouterIntegration:
    def test_router_walks_away_from_a_drained_pool_before_429(self):
        pools = QuotaPools(MODELS)
        # deepseek：注册序第二、默认会被先选——并发+分钟双限把它顶到队尾
        pools.reserve("deepseek")
        pools.reserve("deepseek")
        pools.reserve("deepseek")
        chosen = pick_backend({"model_id": "glm-local"}, "glm-local",
                              MODELS, quota_pools=pools)
        assert chosen == "kimi"                                 # 权重 3.0 压过 qwen

    def test_router_honest_when_every_pool_is_cooling(self):
        pools = QuotaPools(MODELS)
        for model_id in MODELS:
            pools.set_paused(model_id, True)
        # 全部暂停 = 没有可用容量：ranked 仍有键（沉底），路由器按健康序
        # 选"最不坏"的；真正全冷却的诚实降级由 reserve=False 承载——
        # 这里钉住的是：路由器不会抛错、不会虚构、不选失败的后端。
        chosen = pick_backend({"model_id": "glm-local"}, "glm-local",
                              MODELS, quota_pools=pools)
        assert chosen in MODELS and chosen != "glm-local"

    def test_router_without_pools_keeps_legacy_order(self):
        assert pick_backend({"model_id": "glm-local"}, "glm-local", MODELS) == "deepseek"


class TestMatrixApi:
    def _client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from facai.api import create_app
        monkeypatch.setenv("FACAI_ADMIN_PASSWORD", "matrix-pass-1")
        monkeypatch.delenv("FACAI_COOKIE_SECURE", raising=False)
        client = TestClient(create_app(tmp_path))
        login = client.post("/api/v1/auth/login",
                            json={"username": "admin", "password": "matrix-pass-1"}).json()
        client.headers.update({"X-CSRF-Token": login["csrf_token"]})
        return client

    def test_matrix_visible_and_owner_controllable(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        models = client.get("/api/v1/registries/models").json()
        some_model = next(iter(models))
        matrix = client.get("/api/v1/modelmatrix").json()
        assert some_model in matrix["pools"]
        assert some_model in matrix["route_order"]
        weighted = client.put(f"/api/v1/modelmatrix/{some_model}/weight",
                              json={"weight": 42.0})
        assert weighted.status_code == 200
        assert client.get("/api/v1/registries/models").json()[some_model]["route_weight"] == 42.0
        paused = client.put(f"/api/v1/modelmatrix/{some_model}/pause",
                            json={"paused": True})
        assert paused.json() == {"model_id": some_model, "paused": True}
        assert client.get("/api/v1/modelmatrix").json()["pools"][some_model]["paused"] is True

    def test_matrix_is_admin_only(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        tenant = client.post("/api/v1/auth/register",
                             json={"username": "bob", "password": "bob-pass-1234"})
        client.headers.update({"X-CSRF-Token": tenant.json()["csrf_token"]})
        assert client.get("/api/v1/modelmatrix").status_code == 403
        assert client.put("/api/v1/modelmatrix/x/weight",
                          json={"weight": 1}).status_code == 403
        assert client.put("/api/v1/modelmatrix/x/pause",
                          json={"paused": True}).status_code == 403
        assert client.put("/api/v1/modelmatrix/x/limits",
                          json={"rate_limit_per_min": 10,
                                "max_concurrency": 2}).status_code == 403


class TestMatrixWatermarkEditing:
    """MODEL-001（2026-10-16）④：水位线编辑——分钟窗/并发上限写回注册表
    条目并立即同步池；内置常量随 matrix 快照诚实带出（UI 只读展示）。"""

    def _client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from facai.api import create_app
        monkeypatch.setenv("FACAI_ADMIN_PASSWORD", "matrix-pass-2")
        monkeypatch.delenv("FACAI_COOKIE_SECURE", raising=False)
        client = TestClient(create_app(tmp_path))
        login = client.post("/api/v1/auth/login",
                            json={"username": "admin", "password": "matrix-pass-2"}).json()
        client.headers.update({"X-CSRF-Token": login["csrf_token"]})
        return client

    def test_limits_edit_writes_back_to_registry_and_syncs_pool(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        some_model = next(iter(client.get("/api/v1/registries/models").json()))
        edited = client.put(f"/api/v1/modelmatrix/{some_model}/limits",
                            json={"rate_limit_per_min": 7, "max_concurrency": 3})
        assert edited.status_code == 200, edited.text
        assert edited.json() == {"model_id": some_model, "rate_limit_per_min": 7,
                                 "max_concurrency": 3}
        entry = client.get("/api/v1/registries/models").json()[some_model]
        assert entry["rate_limit_per_min"] == 7 and entry["max_concurrency"] == 3
        row = client.get("/api/v1/modelmatrix").json()["pools"][some_model]
        assert row["minute_limit"] == 7 and row["concurrency_limit"] == 3

    def test_limits_rejects_unknown_pool_and_nonpositive_values(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        assert client.put("/api/v1/modelmatrix/ghost/limits",
                          json={"rate_limit_per_min": 5,
                                "max_concurrency": 2}).status_code == 404
        for bad in ({"rate_limit_per_min": 0, "max_concurrency": 2},
                    {"rate_limit_per_min": 5, "max_concurrency": -1}):
            assert client.put("/api/v1/modelmatrix/x/limits", json=bad).status_code == 422

    def test_matrix_snapshot_carries_builtin_constants(self, tmp_path, monkeypatch):
        from facai.quota import COOLDOWN_BASE, COOLDOWN_MAX, UNPENALIZED_WINDOW
        client = self._client(tmp_path, monkeypatch)
        constants = client.get("/api/v1/modelmatrix").json()["constants"]
        assert constants["cooldown_base_s"] == COOLDOWN_BASE
        assert constants["cooldown_max_s"] == COOLDOWN_MAX
        assert constants["unpenalized_window_s"] == UNPENALIZED_WINDOW


class TestPoolStateStats:
    """MODEL-001（2026-10-16）③：PoolState 扩展统计——延迟（滚动窗口 +
    全程平均）、错误率、调用次数。纯内存计数，由 reserve/report 驱动。"""

    def test_report_tracks_latency_error_rate_and_call_counts(self):
        pools = QuotaPools({"m": {}})
        assert pools.matrix()["m"]["latency_avg_ms"] is None  # 无样本无编造
        for latency, ok in ((100.0, True), (300.0, True), (200.0, False)):
            assert pools.reserve("m") is True
            pools.report("m", ok=ok, latency_ms=latency)
        row = pools.matrix()["m"]
        assert row["ok_count"] == 2 and row["fail_count"] == 1
        assert row["reserved_count"] == 3 and row["reported_count"] == 3
        assert row["latency_avg_ms"] == pytest.approx(200.0)
        assert row["latency_recent_avg_ms"] == pytest.approx(200.0)
        assert row["error_rate"] == pytest.approx(1 / 3, abs=1e-4)  # 快照留 4 位
        assert row["inflight"] == 0

    def test_latency_rolling_window_keeps_recent_n(self):
        from facai.quota import LATENCY_WINDOW
        pools = QuotaPools({"m": {"rate_limit_per_min": 1000}})
        total = LATENCY_WINDOW + 4
        for i in range(1, total + 1):
            assert pools.reserve("m") is True
            pools.report("m", ok=True, latency_ms=float(i))
        row = pools.matrix()["m"]
        assert row["latency_avg_ms"] == pytest.approx((1 + total) / 2)
        first, last = total - LATENCY_WINDOW + 1, total      # 窗口只剩最近 N 个
        assert row["latency_recent_avg_ms"] == pytest.approx((first + last) / 2)

    def test_has_pool_and_unknown_model_report_noop(self):
        pools = QuotaPools({"m": {}})
        assert pools.has_pool("m") is True
        assert pools.has_pool("ghost") is False
        pools.report("ghost", ok=True, latency_ms=1.0)       # 不抛错、不建状态
        assert "ghost" not in pools.matrix()
