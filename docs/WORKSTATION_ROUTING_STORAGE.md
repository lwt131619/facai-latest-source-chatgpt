# FACAI 工作站：记录源、智能路由与完整配置基线

核对日期：2026-10-28（北京时间）。源码基线为 GitHub `main` 的 `2c3aadf`，
`facai-agent/master` 为 `e4f74eb`，其整合包内的源码快照停在 `17a1d5a`。
ZCode 本地目录没有挂载进当前 `/workspace`；仓库中只有 ZCode 的能力/权威清单，
`docs/AUTHORITY_SURFACE_INVENTORY.md` 将其记为外部二进制。不能声称已读取 ZCode
私有记录或把它当作 FACAI 的持久化事实源。

## 1. 唯一事实源与存储归属

| 数据 | 当前权威落点 | 规则 |
| --- | --- | --- |
| Mission、步骤、Job、租约、事件、成本、模型 Key 冷却 | `facai/store.py` 的 SQLite schema/迁移 | 保留 Owner、Mission、Trace 归属；事件是分配/状态重放的事实源 |
| Skill 版本正文及注册表 | `store.py:skill_sources/registries`、`registries.py` | 版本/SHA 冻结，禁用/依赖在派发前生效 |
| Intent 修订与 Mission Specification | `intent_workspace.py`、`mission_spec.py` 及 Store | 原话不可覆盖；理解、确认、授权分开 |
| Evidence、Claim、冲突、Proof、Knowledge | `facai/trust.py` 的 SQLite 增量表及产物文件 | 来源和验收分别留痕，未知不自动成为事实 |
| 模型配置 | `registries.py`、`gateway.py`、`quota.py` | 注册表保存环境引用，不保存明文 Key；每 Key 计量目前进程内，重启清零 |
| 运行日志与文件产物 | FACAI data 目录、Journal、Mission workspace | 备份必须同时覆盖 DB、产物、工作区和恢复所需密钥；当前 DB 定时器不是全量恢复 |

不要从 `facai-agent` 旧 ZIP/bundle 回填或覆盖现有 `facai-cloud-public`。
如将来提供 ZCode 本地数据或 API，先做只读来源清单与版本/哈希比对，再设计
适配器导入；不得直接把二进制的私有状态写进 FACAI 主数据库。

## 2. 路由决策：从现状到可校准的工作站

现有三条路径分别存在：`conversion.assign_stage` 评分 Agent/Skill 并以
`ASSIGNMENT_RECORDED` 留痕；`QuotaPools.reserve/report/ranked` 按模型池容量、
冷却和 Owner 权重预防性选路；`router.reroute` 在有证据的后端拒绝/不可用/超时
时至多换两次后端，保留 Mission 授权与原目标。本轮增量让 `ranked()` 在分钟
余量之前考虑**并发槽位是否已满**，避免“分钟额度多但此刻不能接活”的池
排在可执行池前。`reserve` 仍是派发时的原子最终检查。

目标路由次序（每级都要有事实来源）：

1. **硬条件**：Owner/任务授权、目标与范围、Agent 能力、Skill 可用版本/依赖、
   Provider 实际可用、预算/配额、并发槽、工作区隔离。不通过者留排除原因。
2. **任务契约**：输入、预期产物、质量要求、时限和允许的成本上限。缺参数显示
   `NEED_CONTEXT`，不能自改用户目标或编造质量分数。
3. **候选排序**：在已满足硬条件者中使用真实成功率、延迟、计价和任务类型的
   历史效果。没有足够同类任务验证数据时保留 Owner 权重/稳定顺序，标明
   “质量未知”；执行成功率不等于结果事实正确率。
4. **派发与回退**：原子租约、预算预留、Key 池选择、执行事件、Evidence；
   失败归类后只在兼容候选之间重路由，记录旧/新候选及原因。所有候选耗尽时
   WAITING/FAILED 与真实原因，不制造“已完成”。
5. **反馈闭环**：按任务类型统计经验证产物的成功率、实际费用和耗时，
   人工裁决与来源证据分开；冷启动不能把模型自报置信度当为校准质量。

“最快、最精准、最低成本”是三项目标，可能互相冲突。每个 Mission 应明确
优先级和硬预算；默认以**满足任务契约与可验证结果**优先，再优化耗时和成本。

## 3. 完整配置清单（目标合约，尚非全部已实现）

| 配置域 | 必填或默认 | 当前状态/下一步 |
| --- | --- | --- |
| Owner 与权限 | Owner ID、能力、目标范围、到期、撤销、审计 | 授权路径已有；后台定时任务也必须携带实际授权 |
| Mission 契约 | 原话、修订、目标、输出、验证方法、证据需求、幂等键 | 现有结构化 Mission；补按任务类型校准的验证契约 |
| Agent/Skill | 角色、能力、版本、依赖、可用性、并发上限、效果历史 | Registry/分配留痕已有；补真实效果统计 |
| 模型池 | endpoint/model、Key 环境引用、分钟/并发水位、冷却、Owner 权重 | 网关多 Key/SQLite 冷却已有；CLI 自发模型调用尚未统一接入 |
| 成本 | 每任务/每日上限、显式单价、真实 token、预算预留与结算 | 现有预算和 Key 计量分散；跨进程 Key 计量仍缺 |
| Runtime | Worker 数、租约、续期、重试、超时、工作区隔离、取消 | 已有真实小规模 CLI/恢复；20+ 模型并发待实测 |
| 记录/信任 | 事件、Trace、Evidence、Claim、Proof、Artifact 哈希 | SQLite + 产物已有；真实 Provider 与外部来源验证未终验 |
| 运维 | HTTPS、服务重启、队列/成本/备份告警、全量离机备份、回滚 | systemd/nginx/DB timer 已有；线上最新 SHA 与完整恢复待验 |

真实凭据放 `/etc/facai-cloud/providers/*.env` 或现有加密存储，配置文件/日志/
Artifact/交接包只可出现引用名。VPS 生产入口是 nginx HTTPS →
`127.0.0.1:8765`；Compose `8080` 是另一条本地/容器交付路径。

## 4. 直接开发顺序与通过条件

1. **对齐记录源**：核对主库 HEAD、VPS 实际 SHA、SQLite schema 及备份范围；
   修复过时 handoff 中的版本/端口描述。验收：无旧快照覆盖，恢复演练能重放
   同一 Mission 的事件、证据和产物哈希。
2. **路由决策记录**：统一 Agent/Skill/模型候选、硬条件排除原因、Owner 优先级、
   成本与路由策略快照到持久事件。验收：两个并发 Worker 不双重认领，重启后
   决策和理由可重放；无合适候选不派发。
3. **真实模型统一路径**：CLI 模型调用接网关/Key 池，按 Key 跨进程计量；
   真实 Provider 429/401/超时/预算/冷却分别实测。验收：不泄漏 Key、不
   双计费、不将远程拒绝误写成 Owner 撤权。
4. **质量反馈**：按任务类型、验证方法、来源、产物版本建立效果样本；
   只在样本足够时用质量指标调权。验收：同一任务集比较前后成功率、
   p95 时延、成本和人工纠错，且能解释每次选路。
5. **7×24 与规模**：先 1→4→8，再 20+；重启、Worker 崩溃、网络异常、
   磁盘/备份告警、恢复/回滚与手机 HTTPS。验收以
   `docs/engineering/ACCEPTANCE_MATRIX.md` 为准；不可用单元测试数代替生产验收。

每完成一步运行相关测试、更新 `task-registry.json`、普通提交推送并核对远端 SHA。
VPS 与真实 Provider 的外部验收必须记录命令、时间、退出码和 Trace；无法访问时
保持 NOT_VERIFIED。
