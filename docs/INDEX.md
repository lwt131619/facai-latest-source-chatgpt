# FACAI 文档导航

本页按**使用目的**索引文档。状态取决于最新源码、测试和验收矩阵；带日期的报告只记录当时事实。

| 先解决什么 | 当前权威入口 | 后续材料 |
| --- | --- | --- |
| 新 Agent 续接 | [统一入口](../AGENTS.md)、[当前断点](../AGENT_HANDOFF.md) | [恢复运行手册](engineering/RESUME_RUNBOOK.md) |
| 产品方向 | [接管协议](../FACAI_CODEX_HANDOFF.md)、[冻结核心](engineering/CORE_INVARIANTS.md) | [架构决策](engineering/ARCHITECTURE_DECISIONS.md) |
| 任务状态与验收 | [任务注册表](engineering/task-registry.json)、[任务对照](engineering/REQUIREMENTS_TRACEABILITY.md)、[验收矩阵](engineering/ACCEPTANCE_MATRIX.md) | [发布状态](RELEASE_STATUS.md) |
| 找到实现 | [仓库地图](engineering/REPOSITORY_MAP.md)、[核心契约](engineering/CORE_CONTRACTS.md) | [模块清单](../MODULE_INVENTORY.md) |
| 本地运行/恢复 | [本地交付](LOCAL_APPLIANCE.md)、[设置向导](SETUP_WIZARD.md)、[本地恢复](LOCAL_RECOVERY.md) | [7×24 工作站现状](VPS_24X7_WORKSTATION.md) |
| 服务部署 | [部署说明](DEPLOYMENT.md)、[部署记录](DEPLOYMENT_RECORD.md) | [GitHub Agent 连接](GITHUB_AGENT_ACCESS.md) |
| 能力与信任 | [模型池](MODEL_KEY_POOL.md)、[信任工作流](TRUST_WORKFLOW.md)、[目标聚焦与取证缺口](GOAL_FOCUS_AND_EVIDENCE_GAPS.md)、[知识资产](KNOWLEDGE_MEMORY.md) | [Skill 收录模板](SKILL_INTAKE_TEMPLATE.md) |
| 测试证据 | [本地验证记录](validation/)、[开发日志](ACTIVE_DEVELOPMENT.md) | [历史交接](handoff-20261016/) |

## 历史材料和构建物

- `docs/archive/` 保存早期长版 README；它的线上部署结论和测试数量均是历史记录。
- `docs/handoff-*`、带日期的审计/验证报告是当时的快照，不能覆盖当前任务注册表。
- 根目录 `HANDOFF-NEXT.md`、`TASKQUEUE.md`、`快速继续开发.md` 是旧交接入口；新 Agent 从 `AGENTS.md` 开始。
- `dist/` 已跟踪的开发 ZIP、bundle 和验证 JSON 是旧产物。它们可用于历史追溯，但不是当前 HEAD 的自动生成包。
- `vendor/` 是候选来源与许可证/能力资料；文件存在不代表已安装、执行或验证。

不移动运行中的 Python、Docker、脚本入口，也不因整理文档而变更任何任务的验收状态。
