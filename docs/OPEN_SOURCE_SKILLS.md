# FACAI 开源 Agent Skill 情报目录

2026-10-31 增量：格式、组件漏洞情报与资产检查候选的固定提交和许可证实证见
`docs/CAPABILITY_SOURCE_CATALOG_2026-10-31.md`；后续 Skill 原件收录用
`docs/SKILL_INTAKE_TEMPLATE.md`。该增量不改变本文件早期候选的启用状态。

> 用途：为 FACAI 收集、登记与筛选可用的开源 Agent Skill 资源来源。
> 范围：workflow orchestration、代码质量/测试、API 集成、消息渠道、安全审核等方向。
> 说明：本目录基于公开一手来源做了情报核对，**不复制第三方正文或代码**；FACAI 运行时不会因本目录自动调用外网/API，也不表示任何技能已安装或已导入。所有候选均需逐项复核后才能启用。

## 一、已核对的一手来源

| 编号 | 来源 | 链接 | 已核对事实 |
| --- | --- | --- | --- |
| S1 | Agent Skills 格式规范 | https://github.com/agentskills/agentskills/blob/main/docs/specification.mdx | 定义 Skill 的 `name`/`description`/frontmatter；可选 `scripts`/`references`/`assets`；采用渐进披露（progressive disclosure）；提供 `skills-ref validate` 校验。 |
| S2 | 格式仓库许可证 | https://github.com/agentskills/agentskills/blob/main/LICENSE | 该格式仓库为 Apache-2.0。 |
| S3 | 安全审查型候选仓库 | https://github.com/skills-commons/skills-commons | README 已核实：仓库标 Apache-2.0，自述每项技能经安全/质量审核；当前目录约 22 个 skill，含 `task-handoff-brief`、`session-summary-writer`、`tool-failure-triage`、`honest-status-report`、`bug-report-pro`、`issue-triage-pro`、`sql-query-reviewer`、`readme-architect`、`release-notes-writer` 等；仍须逐 skill 落实到具体 commit 与授权文件复核。 |
| S4 | Anthropic 示例仓库 | https://github.com/anthropics/skills | 官方示例技能仓库；**必须逐 skill 核实授权**，不可笼统宣称统一许可。 |

> 注：S3 的 README 事实已在本轮核实；S4 仍须逐项核实许可证。本文件仅登记事实与链接，**不复制第三方正文或代码**。

## 二、分类：格式/工具 与 可导入具体技能

按用途区分，避免把"规范/工具"与"可直接导入的技能"混为一谈。

### A. 格式与工具（非具体技能）
- **S1（agentskills 规范）**：定义 Skill 的文件结构与 frontmatter 字段、可选目录、渐进披露机制，以及 `skills-ref validate` 校验工具。属于格式与工具层，**不是可导入技能**，作为 FACAI 制作/校验 Skill 的基准参考。
- **S2（agentskills 许可证）**：许可证信息，非技能。

### B. 候选技能来源（需逐项核实后才能导入）
- **S3（skills-commons/skills-commons）**：Apache-2.0，自述每项技能经安全/质量审核，当前约 22 个 skill；已确认与 FACAI 的可用映射见第三节。**仍需逐 skill 复核**：各自实际授权文件、审核结论的可复现性、以及是否含脚本/外联能力。
- **S4（anthropics/skills）**：官方示例技能仓库，作为待检索来源；目前尚未确认哪些具体技能适合 FACAI。**待逐项复核**：每个 skill 的独立授权，不可按仓库整体推断许可。

## 三、S3 已确认技能对 FACAI 的可用映射

| FACAI 需求方向 | S3 已确认技能 | 说明 |
| --- | --- | --- |
| 任务交接 | `task-handoff-brief` | 生成任务交接简报，用于跨会话/跨人接力 |
| 上下文摘要 | `session-summary-writer` | 会话/上下文摘要 |
| 工具错误归因 | `tool-failure-triage` | 工具调用失败的分诊与归因 |
| 发布记录 | `release-notes-writer` | 发布说明/变更记录撰写 |
| 状态汇报 | `honest-status-report` | 如实状态汇报 |
| 缺陷报告 | `bug-report-pro` | 缺陷报告撰写 |
| 问题分诊 | `issue-triage-pro` | Issue 分诊 |
| SQL 审查 | `sql-query-reviewer` | SQL 查询审查 |
| 文档架构 | `readme-architect` | README/文档结构设计 |

> 以上仅表示 S3 目录中存在这些技能名，**不代表已导入或已验证其内容**。

### 未确认覆盖的方向（不得写成 S3 已覆盖）
- **API 集成**：目前未确认 S3 有对应的现成 API 集成技能。
- **消息渠道**：目前未确认 S3 有消息渠道类现成技能。
- **完整多线程 workflow 编排**：目前未确认 S3 有完整的多线程 workflow orchestration 现成技能。

## 四、准入门槛（引入任何技能前必须通过）

1. **许可证检查**
   - 逐仓库、逐 skill 核实许可证与版权归属，以仓库内实际 LICENSE/声明为准。
   - 禁止笼统以仓库名或自述推断统一许可（尤其针对 S4）。
   - 记录许可证名称、SPDX 标识、来源文件路径。

2. **恶意指令 / 外传脚本审查**
   - 审查 `scripts` 及任何可执行内容，识别外联、数据外传、凭据读取、破坏性操作。
   - 审查 frontmatter/指令文本是否存在提示注入、越权指令或隐蔽行为。
   - 对网络访问、文件写入、命令执行等能力逐项评估。

3. **人工启用**
   - 任何技能导入前须经人工复核并显式启用，默认不自动加载。
   - 保留审查记录（来源、版本/commit、审查人、结论）。

4. **格式合规**
   - 使用 S1 规范与 `skills-ref validate` 校验 frontmatter 与目录结构。

5. **可追溯性**
   - 固定来源链接与版本（commit/tag），记录引入时间与用途。

## 五、待逐项复核清单

- [ ] S3：各 skill 的实际授权文件与具体 commit（URL 已核实，授权文件仍需落到条目）。
- [ ] S3：自述"逐项安全/质量审查"的方法与可复现性。
- [ ] S4：每个 skill 的独立许可证（不可统一推断）。
- [ ] S4：是否符合 S1 规范，能否通过 `skills-ref validate`。
- [ ] S3 已确认技能（任务交接/上下文摘要/工具错误归因/发布记录等）的实际内容与可用性验证。
- [ ] 是否存在恶意指令或外传脚本。
- [ ] 是否已人工启用并留存审查记录。

## 六、来源汇总

- Agent Skills 规范：https://github.com/agentskills/agentskills/blob/main/docs/specification.mdx
- Agent Skills 许可证（Apache-2.0）：https://github.com/agentskills/agentskills/blob/main/LICENSE
- skills-commons：https://github.com/skills-commons/skills-commons
- Anthropic 示例仓库：https://github.com/anthropics/skills
