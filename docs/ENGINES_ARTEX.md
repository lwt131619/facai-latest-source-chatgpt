# ENGINES-001：ARTEX 附属引擎收录（2026-10-23）

ARTEX 0.3.12（Go 后端 + Next.js 前端，AI 自主渗透测试系统）作为 FACAI 首个
附属引擎收录。收录形态：**HTTP 适配器**（`facai/artex.py`），不并入源码、
不换框架——ARTEX 独立部署（本地或 VPS，`ARTEX_BASE_URL`/`ARTEX_TOKEN` 指
向），FACAI 通过适配器把一个任务步骤派发为**一个 ARTEX 任务**（goal=步骤
提示词，`seed_first_intent` 开启使 worker 免等首轮 planner），轮询到终态
（done/failed/timeout）并映射为 FACAI 的 OPENCODE_* 结局词汇——与 goose 同
一诚实归因口径。绑定方式与现有 agent 一致：注册表 agents 条目
`adapter: "artex"`，mission binding 后由调度器按适配器派发。

## 权限模型（Owner 指令：本地/Owner 最高，边界限制全部移除）

- **唯一裁断权在 FACAI 的 Owner 授权链**：理解 → 修订 → 确认 → 独立授权 →
  派发。ARTEX 不构成新的授权层。
- **ARTEX 内置审批门整体旁路**（`ArtexAdapter.clear_boundaries`，收录后首次
  派发自动执行一次）：
  1. 拦截规则逐条**禁用**（`POST /api/intercept/rules/{id}/toggle`）——记录
     保留可审计，Operator 可随时重武装；
  2. 进入拦截系统的工具面**清空**（`PUT /api/intercept/tools
     {"enabled_tools": []}`）——没有任何工具调用再进审批门；
  3. 审查模型（judge）由本适配器**永不配置**。
- 边界清除失败不阻断派发，但会作为任务输出留痕（诚实归因）。
- 未配置 `ARTEX_BASE_URL` 时 execute 明确失败（OPENCODE_FAILED + 原因），
  绝不假装跑过。

## 已验证 / 未验证

已验证（受控假 ARTEX，`tests/test_artex_adapter.py` 6 例）：去边界调用序列
（规则禁用+工具面清空）、种子任务创建载荷、done/failed/timeout 三态映射、
派发窗口超时、未配置诚实失败、注册表绑定路径。

未验证（如实）：真实 ARTEX 实例部署与实弹（需 PostgreSQL + Go 构建，建议
VPS `/opt/artex`）、真实渗透目标链路、ARTEX 任务产物回读（findings/资产
图——当前只回 terminal 状态，产物深度集成属后续增量）。

## 后续增量候选

- 任务产物（findings/报告）回读为 FACAI Artifact；
- ARTEX LLM 配置链对接 FACAI 模型注册表（多 Key 池共享）；
- adapters.py SDK 契约层的 ArtexContractAdapter（capabilities 行推导）。
