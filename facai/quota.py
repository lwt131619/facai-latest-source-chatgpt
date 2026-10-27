"""FACAI 模型配额池（Model Quota Pools）— 预防式限速治理 + 可视化支配路由。

操作员指令 2026-10-13：中转站接入后，一个模型太少了；要让子智能体在
模型矩阵上自动路由，并且别等 429 才停下来——提前预判瓶颈。

现有 router.py 的重路由是响应式的（429 发生后换后端，每作业上限 2 次）。
本模块补的是预防式的前半段：

- **多池**：models 注册表里的每一款模型（含客户端自带 user_model）各自
  是一个独立速率池——上游限速按 key/渠道计，池天然按模型分。
- **滑动窗口预算**：每池两条水位线——分钟窗（如 30 次/60s）与并发水位
  （如 5 个在飞请求）。reserve() 在派发**前**判断"这个池此刻还能接活"，
  不到 429 才发现。
- **健康分冷却**：429/超时让池进入指数冷却（10s→20s→40s…封顶 10 分钟），
  冷却期间 pick 不会再选它；成功一次减半冷却——恢复是渐进的，不是要么
  要么。
- **所有者支配**：操作员可给任意池设权重与暂停位（pause 一个模型 =
  派发永远绕开它，但保留在矩阵上可见）。权重进 pick 的预排序。

设计纪律与 router.py 同源：**路由器永不虚构备选**——全部池冷却时
pick 返回 None，作业诚实进入既有响应式路径（排队/重试），绝不假装
有容量。线程安全：一把 RLock 管全部计数（池数量级 ~几十，无争用热点）。
"""
from __future__ import annotations

import threading
import time
from collections import deque

MINUTE_WINDOW_SECONDS = 60.0
DEFAULT_MINUTE_LIMIT = 30          # 每池每分钟默认预算
DEFAULT_CONCURRENCY = 5            # 每池默认并发水位
DEFAULT_WEIGHT = 1.0
COOLDOWN_BASE = 10.0               # 首次 429 冷却 10s
COOLDOWN_MAX = 600.0               # 封顶 10 分钟
UNPENALIZED_WINDOW = 300.0         # 连续 5 分钟无失败 → 冷却阶梯重置
LATENCY_WINDOW = 20                # 延迟滚动窗口：只保留最近 N 次样本


class PoolState:
    """One model's live rate budget. Pure counters — no I/O, no clock writes.

    MODEL-001 扩展（2026-10-16）：在原有水位/冷却计数之上增加延迟
    （滚动窗口 + 全程平均）、错误率与调用次数——全部由真实派发的
    reserve/report 驱动（scheduler 接线），纯内存统计，持久化模式不变。
    """

    def __init__(self):
        self.minute_hits: deque[float] = deque()
        self.inflight = 0
        self.cooldown_until = 0.0
        self.cooldown_level = 0          # 指数阶梯：0=未冷却
        self.last_failure_at = 0.0
        self.ok_count = 0
        self.fail_count = 0
        # 延迟统计：滚动窗口（最近 N 次）+ 全程累计平均。
        self.latency_ms: deque[float] = deque(maxlen=LATENCY_WINDOW)
        self.latency_total_ms = 0.0
        self.latency_count = 0
        # 调用次数：reserve 批准数（真实进入执行的尝试）与 report 数。
        self.reserved_count = 0
        self.reported_count = 0

    def snapshot(self) -> dict:
        now = time.monotonic()
        attempts = self.ok_count + self.fail_count
        return {
            "minute_hits": len(self.minute_hits),
            "inflight": self.inflight,
            "cooldown_remaining": max(0.0, round(self.cooldown_until - now, 1)),
            "ok_count": self.ok_count,
            "fail_count": self.fail_count,
            "latency_avg_ms": (round(self.latency_total_ms / self.latency_count, 1)
                               if self.latency_count else None),
            "latency_recent_avg_ms": (round(sum(self.latency_ms) / len(self.latency_ms), 1)
                                      if self.latency_ms else None),
            "error_rate": (round(self.fail_count / attempts, 4) if attempts else 0.0),
            "reserved_count": self.reserved_count,
            "reported_count": self.reported_count,
        }


class QuotaPools:
    """Per-model rate pools with owner-tunable weights and pause flags.

    Limits come from the models registry entry when present
    (``rate_limit_per_min`` / ``max_concurrency``), falling back to the
    defaults above — the owner edits the registry, not this module.
    """

    def __init__(self, model_entries: dict | None = None):
        self._lock = threading.RLock()
        self._states: dict[str, PoolState] = {}
        self._weights: dict[str, float] = {}
        self._paused: set[str] = set()
        self._limits: dict[str, tuple[int, int]] = {}
        self.replace_models(model_entries or {})

    # --- 配置（所有者支配面） ---------------------------------------------
    def replace_models(self, model_entries: dict) -> None:
        """Sync pool config from the models registry. Counters persist across
        registry edits — a rebind must not wipe live rate accounting."""
        with self._lock:
            for model_id, entry in (model_entries or {}).items():
                entry = entry if isinstance(entry, dict) else {}
                self._limits[model_id] = (
                    int(entry.get("rate_limit_per_min") or DEFAULT_MINUTE_LIMIT),
                    int(entry.get("max_concurrency") or DEFAULT_CONCURRENCY),
                )
                self._weights[model_id] = float(entry.get("route_weight") or DEFAULT_WEIGHT)
                self._states.setdefault(model_id, PoolState())
            for gone in [m for m in self._limits if m not in (model_entries or {})]:
                self._limits.pop(gone, None)
                self._weights.pop(gone, None)
                self._states.pop(gone, None)

    def set_weight(self, model_id: str, weight: float) -> float:
        with self._lock:
            if model_id not in self._limits:
                raise KeyError(model_id)
            self._weights[model_id] = max(0.0, float(weight))
            return self._weights[model_id]

    def set_paused(self, model_id: str, paused: bool) -> None:
        with self._lock:
            if model_id not in self._limits:
                raise KeyError(model_id)
            (self._paused.add if paused else self._paused.discard)(model_id)

    # --- 派发面：预留 / 上报 -------------------------------------------------
    def has_pool(self, model_id: str) -> bool:
        """Whether a pool is registered for this model at all.

        Scheduler dispatch 用它区分"池没注册（零改动直发）"与"池拒绝了"：
        池是可选增强，不是新闸门——没注册的模型必须走原有路径。
        """
        with self._lock:
            return model_id in self._states and model_id in self._limits

    def reserve(self, model_id: str) -> bool:
        """Ask before dispatch: is this pool runnable *right now*?

        False when paused, cooling down, minute budget spent, or at the
        concurrency watermark — the caller routes elsewhere BEFORE the
        upstream 429, which is the entire point of this module.
        """
        with self._lock:
            state = self._states.get(model_id)
            limits = self._limits.get(model_id)
            if state is None or limits is None:
                return False
            minute_limit, concurrency_limit = limits
            now = time.monotonic()
            if model_id in self._paused:
                return False
            if state.cooldown_until > now:
                return False
            while state.minute_hits and now - state.minute_hits[0] >= MINUTE_WINDOW_SECONDS:
                state.minute_hits.popleft()
            if len(state.minute_hits) >= minute_limit:
                return False
            if state.inflight >= concurrency_limit:
                return False
            state.minute_hits.append(now)
            state.inflight += 1
            state.reserved_count += 1
            return True

    def report(self, model_id: str, ok: bool, rate_limited: bool = False,
               latency_ms: float | None = None) -> None:
        """Report one attempt's outcome. rate_limited=True enters the
        exponential cooldown ladder; ok halves any live cooldown.

        ``latency_ms``（MODEL-001）为一次真实派发的端到端耗时：进滚动窗口
        与全程累计；None（如异常路径拿不到耗时）只跳过延迟记账，不影响
        ok/fail 与冷却语义。
        """
        with self._lock:
            state = self._states.get(model_id)
            if state is None:
                return
            state.inflight = max(0, state.inflight - 1)
            state.reported_count += 1
            if latency_ms is not None:
                state.latency_ms.append(max(0.0, float(latency_ms)))
                state.latency_total_ms += max(0.0, float(latency_ms))
                state.latency_count += 1
            now = time.monotonic()
            if ok:
                state.ok_count += 1
                if state.cooldown_until > now:
                    state.cooldown_until = min(
                        state.cooldown_until,
                        now + max(0.0, (state.cooldown_until - now) / 2))
                if now - state.last_failure_at > UNPENALIZED_WINDOW:
                    state.cooldown_level = 0
            else:
                state.fail_count += 1
                state.last_failure_at = now
                if rate_limited:
                    state.cooldown_level = min(state.cooldown_level + 1, 10)
                    state.cooldown_until = now + min(
                        COOLDOWN_BASE * (2 ** (state.cooldown_level - 1)),
                        COOLDOWN_MAX)

    # ——— 路由面：健康度预排序 -------------------------------------------------
    def ranked(self) -> list[str]:
        """Model ids ordered by routed preference: paused/cooling last
        (in cooldown-remaining order), then pools that can reserve now,
        then by minute-budget headroom, owner weight (desc), and id.

        A pool at its concurrency watermark must not outrank one with a free
        slot merely because it has more minute requests left. ``reserve``
        remains the authoritative atomic check after this advisory ranking.
        """
        with self._lock:
            now = time.monotonic()
            rows = []
            for model_id in self._limits:
                state = self._states[model_id]
                minute_limit, concurrency_limit = self._limits[model_id]
                while state.minute_hits and now - state.minute_hits[0] >= MINUTE_WINDOW_SECONDS:
                    state.minute_hits.popleft()
                cooling = state.cooldown_until > now
                paused = model_id in self._paused
                headroom = (minute_limit - len(state.minute_hits)) / max(1, minute_limit)
                saturated = (len(state.minute_hits) >= minute_limit
                             or state.inflight >= concurrency_limit)
                rows.append((paused, cooling, saturated,
                             state.cooldown_until if cooling else 0.0,
                             -headroom,
                             -self._weights.get(model_id, DEFAULT_WEIGHT),
                             model_id))
            rows.sort()
            return [row[6] for row in rows]

    def matrix(self) -> dict:
        """The visualization feed: per-pool limits, live state, owner knobs."""
        with self._lock:
            now = time.monotonic()
            out = {}
            for model_id in sorted(self._limits):
                state = self._states[model_id]
                minute_limit, concurrency_limit = self._limits[model_id]
                while state.minute_hits and now - state.minute_hits[0] >= MINUTE_WINDOW_SECONDS:
                    state.minute_hits.popleft()
                snap = state.snapshot()
                snap.update({
                    "minute_limit": minute_limit,
                    "concurrency_limit": concurrency_limit,
                    "minute_used": len(state.minute_hits),
                    "headroom_pct": round(
                        100 * (minute_limit - len(state.minute_hits)) / max(1, minute_limit)),
                    "weight": self._weights.get(model_id, DEFAULT_WEIGHT),
                    "paused": model_id in self._paused,
                    "cooling": state.cooldown_until > now,
                })
                out[model_id] = snap
            return out
