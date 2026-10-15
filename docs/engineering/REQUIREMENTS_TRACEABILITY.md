# 需求—源码—任务—验收对照

由 task-registry.json 生成；状态是证据范围，不是完成百分比。

| ID | 方向 | 状态 | 优先级 | 依赖 |
|---|---|---|---|---|
| DIR-001 | 统一方向与接手入口 | VERIFIED_LOCAL | P0 |  |
| AUD-001 | 真实源码审计与测试基线 | VERIFIED_LOCAL | P0 | DIR-001 |
| SYNC-001 | GitHub检查点与断点备份 | VERIFIED_LOCAL | P0 | DIR-001 |
| AUTH-001 | Owner Control与授权分离 | PARTIAL | P1 | AUD-001 |
| SETUP-001 | 首次配置向导与Owner初始化 | NOT_IMPLEMENTED | P1 | AUTH-001 |
| INTENT-001 | Natural Language Compiler完整合约 | PARTIAL | P1 | AUD-001 |
| MISSION-001 | 理解预览与确认到Mission | PARTIAL | P1 | INTENT-001, AUTH-001 |
| DB-001 | 生产持久化与增量迁移 | PARTIAL | P1 | AUD-001 |
| EVENT-001 | Event Bus与Trace重放 | PARTIAL | P1 | DB-001 |
| PKG-001 | 一键启动与交付包 | PARTIAL | P1 | AUD-001 |
| MODEL-001 | 多Provider/多Key模型资源池 | PARTIAL | P2 | AUTH-001 |
| SKILL-001 | Skill全生命周期 | PARTIAL | P2 | AUTH-001, DB-001 |
| ROUTER-001 | 能力到Agent/Skill/Tool路由 | PARTIAL | P2 | MODEL-001, SKILL-001 |
| RUNTIME-001 | 20+Agent并发与恢复 | PARTIAL | P2 | AUTH-001, EVENT-001, MODEL-001 |
| OPS-001 | Command Center与实时流水线 | PARTIAL | P2 | MISSION-001, EVENT-001, RUNTIME-001 |
| TRUST-001 | Evidence与Claim Intelligence | PARTIAL | P2 | EVENT-001, DB-001 |
| CONFLICT-001 | 上下文冲突语义 | PARTIAL | P2 | TRUST-001 |
| PROOF-001 | Proof Artifact与Owner接受 | PARTIAL | P2 | TRUST-001, CONFLICT-001 |
| API-001 | API/health/status/metrics | PARTIAL | P2 | SETUP-001, MISSION-001, PROOF-001 |
| KNOWLEDGE-001 | 长期知识资产与记忆 | PARTIAL | P3 | PROOF-001 |
| MOBILE-001 | 手机控制台与通知配置 | PARTIAL | P3 | OPS-001, PROOF-001 |
| DESKTOP-001 | Desktop Console本地软件 | PARTIAL | P3 | SETUP-001, PKG-001 |
| UPDATE-001 | 跨平台更新/备份/迁移/回滚 | PARTIAL | P3 | DB-001, PKG-001 |
| RUST-001 | Rust核心增量边界 | PLANNED | P3 | INTENT-001, AUTH-001, TRUST-001 |
| ENGINES-001 | Shannon/Kali/BlackArch附属接入 | PLANNED | P3 | AUTH-001, SKILL-001, RUNTIME-001 |
| SEC-001 | 既有隔离/密钥/账单/渠道能力保留 | PARTIAL | P2 | AUTH-001, DB-001 |
| ACCEPT-001 | 完整Production v1.0端到端验收 | NOT_VERIFIED | P4 | SETUP-001, MISSION-001, RUNTIME-001, OPS-001, PROOF-001, MOBILE-001, DESKTOP-001, PKG-001, UPDATE-001, SYNC-001 |

## DIR-001 — 统一方向与接手入口

锁定产品目标、不可变量、模块边界与事实状态

**现有定位**：`AGENTS.md`、`FACAI_CODEX_HANDOFF.md`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：每次修改更新方向/任务/证据，历史文档仅作参考

## AUD-001 — 真实源码审计与测试基线

源码真实状态优先，已验证范围可复现

**现有定位**：`FACAI_CURRENT_STATE_REPORT.md`、`docs/BUILD_VALIDATION_2026-10-15.md`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：以bc6ee5a的1259通过/28跳过为基线，不冒称全产品验收

## SYNC-001 — GitHub检查点与断点备份

每个可运行阶段提交并核对远端SHA，保留完整Git历史

**现有定位**：`scripts/checkpoint.py`、`AGENT_HANDOFF.md`、`github-backup.json`、`docs/GITHUB_AGENT_ACCESS.md`

**验收**：

- 本地commit与完整bundle验证成功
- 远端SHA==本地HEAD且remote_verified=true才算已上传
- 错误不包含凭据，不强推/删库/重建远端

**下一步**：官方集成原生Git推送已恢复；origin指向facai-latest-source-chatgpt，多个小步骤已普通push并核对main。checkpoint优先继承Git凭据，继续核对main与工作分支。

## AUTH-001 — Owner Control与授权分离

理解/确认/授权/执行/验证/Owner接受独立

**现有定位**：`facai/auth.py`、`facai/authority.py`、`facai/authority_gate.py`、`facai/store.py`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：先定位旧Store.create自动grant行为，新路径增量明确授权，不破坏旧渠道

## SETUP-001 — 首次配置向导与Owner初始化

浏览器完成系统检测、模型连接、Owner账号与控制/恢复密钥

**现有定位**：`scripts/start.py`、`facai/auth.py`、`facai/secretbox.py`、`facai/api.py`

**验收**：

- 初始化受loopback+单次bootstrap保护，完成后bootstrap失效
- Owner密码哈希复用既有身份体系，APIKey加密不回显
- 重启保留Owner，不通过全局auth bypass开放初始化

**下一步**：SetupManager/router加loopback单次bootstrap；旧env和local-owner.json保持兼容

## INTENT-001 — Natural Language Compiler完整合约

Human→Machine边界保存原话及action/target/purpose/scope/constraints

**现有定位**：`facai/intent.py`、`facai/prompting.py`、`facai/conversion.py`、`facai/intent_workspace.py`、`facai/intent_routes.py`、`facai/static/workspace.html`、`tests/test_intent_workspace.py`

**验收**：

- 原话逐字保存，结构修订不可变/按Owner隔离
- READY/NEED_CONTEXT/REVIEW真实区分，缺上下文不虚构
- 置信度未知或有可说明依据，不硬编码92%

**下一步**：已新增规则/人工修订预览、不可变修订及持久化；继续补外部目标上下文、完整Mission Specification与模型语义编译/校准，保持原话。

## MISSION-001 — 理解预览与确认到Mission

明确Confirm/Edit/补信息/Cancel，生成任务规范与验证要求

**现有定位**：`facai/intent_workspace.py`、`facai/mission_spec.py`、`facai/intent_routes.py`、`facai/store.py`

**验收**：

- Confirm/Edit/补信息/Cancel操作可用
- expected_revision冲突拒绝，重复确认只一个任务
- 确认不签grant/不排队/不执行，DAG复用现有Store

**下一步**：已新增同事务持久化Mission Specification、原话/确认修订/目标/约束/真实步骤ID与SHA；需继续完善用户自定义输出契约及真实Runtime全流程。

## DB-001 — 生产持久化与增量迁移

Users/Owner/Agents/Skills/Missions/Executions/Events/Evidence/Claims/Artifacts/Knowledge/Permissions持久化

**现有定位**：`facai/store.py`、`facai/backup.py`

**验收**：

- 既有数据/授权/账单保留，增量迁移版本明确
- 确认/任务关联同事务，重试幂等，重启能恢复
- DB/配置引用备份恢复与失败路径实测

**下一步**：审现有表与_migrate后增量扩展，同一SQLite事务链接Intent与Mission，不创建平行身份系统

## EVENT-001 — Event Bus与Trace重放

所有核心动作持久事件化，断线和重启可回溯

**现有定位**：`facai/eventbus.py`、`facai/store.py`、`facai/api.py`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：定义规范核心事件与旧事件映射，新增路径同事务写事件，补游标重放/幂等

## PKG-001 — 一键启动与交付包

解压→启动→控制台→实际任务→可验证结果

**现有定位**：`scripts/start.py`、`scripts/package.py`、`start.bat`、`start.sh`、`Dockerfile`、`docker-compose.yml`

**验收**：

- 包与当前源码manifest逐文件校验一致
- 保留前面已验证的启动/Docker/持久化能力
- 新说明/备份脚本进入包；Windows/macOS分开实测

**下一步**：前面111文件开发包与源码逐文件一致；接入向导后继续native/安装包验收

## MODEL-001 — 多Provider/多Key模型资源池

配额水位、冷却、故障转移、延迟和成本可观察

**现有定位**：`facai/gateway.py`、`facai/router.py`、`facai/quota.py`、`facai/secretbox.py`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：审已有quota pools能力，补Key级统计与配置UI，真实连接和模拟失败分开验证

## SKILL-001 — Skill全生命周期

发现/安装/版本/依赖/调用/更新/禁用/移除/审查与效果记录

**现有定位**：`facai/skills.py`、`facai/distill.py`、`facai/boundary.py`、`facai/registries.py`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：补manifest与版本/usage/performance记录，保留现有导入蒸馏能力和历史证据

## ROUTER-001 — 能力到Agent/Skill/Tool路由

匹配任务能力/权限/资源且解释路由选择

**现有定位**：`facai/router.py`、`facai/capabilities.py`、`facai/registries.py`、`facai/conversion.py`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：记录候选/排除/切换原因，不能静默改变原目标和约束

## RUNTIME-001 — 20+Agent并发与恢复

实际20+协作、隔离、取消、重试、配额、租约恢复

**现有定位**：`facai/scheduler.py`、`facai/execution.py`、`facai/opencode.py`、`facai/goose.py`

**验收**：

- 24独立任务受控适配器并发/租约续期/恢复有实测
- 线程/CLI进程/模型并发分别计量，OpenCode默认串行不默默移除
- 同任务分支隔离/超时/失败/取消/结果幂等有证据

**下一步**：当前默认2/上限8/同任务3/OpenCode串行；先补排队租约续期，再提升容量并分别实测

## OPS-001 — Command Center与实时流水线

Dashboard/Missions/Agents/Skills/Models/Timeline/Evidence/Artifacts真实可操作

**现有定位**：`facai/static/command-center.html`、`facai/static/index.html`、`facai/api.py`

**验收**：

- 真实Dashboard/任务/Agent/Skill/资源/时间线可操作
- 0.01%来源于可测基数；不可测时不编造精确小数
- 缺模型/CLI/权限显示真实等待或错误

**下一步**：接新Intent确认/授权与事件，任务/阶段/Agent进度精度0.01%有真实计量依据

## TRUST-001 — Evidence与Claim Intelligence

Evidence Collector→Claim Normalizer→Source Trust→Conflict→Confidence

**现有定位**：`facai/trust.py`、`facai/trust_routes.py`、`facai/static/evidence-center.html`、`tests/test_trust_workflow.py`

**验收**：

- FACT/INFERENCE/UNKNOWN/DISPUTED可追到证据和理由
- 采集时间/来源/环境/哈希真实，无执行证据不标语义验证
- legacy verified不偷换，新完整性/语义验证字段独立

**下一步**：Trust v1已持久化来源/证据/规范化Claim/人工审查；默认UNKNOWN，未校准confidence=null。后续补独立来源验证、来源信任图和自动采集；事件记录不构成外部执行认证。

## CONFLICT-001 — 上下文冲突语义

比较时间/范围/环境/定义后分类TEMPORAL/SCOPE/SEMANTIC/DIRECT_CONFLICT

**现有定位**：`facai/trust.py`、`tests/test_trust_workflow.py`

**验收**：

- 保留双方陈述、来源及时间/范围/环境/定义
- 四类冲突与无法分类样例可测试
- 不能A!=B就无依据判一方正确

**下一步**：已实现明确规范化subject/predicate/value及四维上下文比较，四类冲突和缺上下文REVIEW有测试；后续补自然语言同义/时间区间推理和可审计冲突解决。

## PROOF-001 — Proof Artifact与Owner接受

产物/元数据/证据/验证/置信度/Trace统一导出

**现有定位**：`facai/trust.py`、`facai/trust_routes.py`、`facai/static/evidence-center.html`、`tests/test_trust_browser.py`

**验收**：

- Artifact/Metadata/Evidence/Verification/Confidence/Trace完整
- 导出哈希可复算，无密钥/路径穿越/跨租户泄漏
- Owner接受绑定任务/证据修订，修改后不复用旧接受

**下一步**：已实现不可变快照、ZIP清单、产物字节SHA、Owner会话独立验收、变更失效；继续补可信自动执行采集、真实Provider验收与长期知识联动。

## API-001 — API/health/status/metrics

完整合约与真实健康状态，继续权限/CSRF/租户校验

**现有定位**：`facai/api.py`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：新增router/helpers，health不等于模型/Runtime就绪，status/metrics不泄密

## KNOWLEDGE-001 — 长期知识资产与记忆

结果按来源/证据状态/任务/产物版本可检索

**现有定位**：`facai/distill.py`、`facai/store.py`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：新增knowledge schema与索引，UNKNOWN不得因进入memory变FACT，保留蒸馏兼容

## MOBILE-001 — 手机控制台与通知配置

手机任务/实时进度/Agent/结果/通知/设置

**现有定位**：`facai/static/mobile.html`、`facai/static/sw.js`、`facai/static/manifest.webmanifest`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：复用已有mobile/PWA，接新流程，在真实390x844浏览器和HTTPS访问验收

## DESKTOP-001 — Desktop Console本地软件

启动软件即有可操作本地FACAI服务

**现有定位**：`packaging/app/main.js`、`packaging/app/package.json`、`scripts/start.py`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：现Electron仅远程站点壳；增加本地模式/生命周期/端口检测，再实测native安装包

## UPDATE-001 — 跨平台更新/备份/迁移/回滚

update.bat/update.sh升级前备份，失败可恢复

**现有定位**：`facai/backup.py`、`deploy/deploy.sh`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：保留已有Linux部署update/rollback，补本地入口和真实恢复演练

## RUST-001 — Rust核心增量边界

按冻结语义增量接入Rust，不替换全部Python服务

**现有定位**：`docs/总控架构-Shannon接入与现状盘点-20261013.md`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：先ADR写Python对应模块与ABI/服务契约，再逐模块构建和一致性验证

## ENGINES-001 — Shannon/Kali/BlackArch附属接入

FACAI Core总控，Shannon执行附属，工具库独立子系统

**现有定位**：`docs/总控架构-Shannon接入与现状盘点-20261013.md`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：先适配器合约/来源/版本/可用性/权限/证据接入，再实测，不能接管Owner

## SEC-001 — 既有隔离/密钥/账单/渠道能力保留

不因新UI绕过预算或泄漏模型密钥/其他租户/站长私人渠道

**现有定位**：`facai/secretbox.py`、`facai/billing.py`、`facai/channels/__init__.py`、`facai/notify.py`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：复跑现有隔离/模型/账单/通道测试；新功能按相同边界接入

## ACCEPT-001 — 完整Production v1.0端到端验收

完整自然语言→理解→确认→执行→证据→产物→Trace→恢复

**现有定位**：`docs/engineering/ACCEPTANCE_MATRIX.md`

**验收**：

- 所有最终用户步骤有对应提交与实际证据
- 外部阻塞和native/browser未验证项明确保留
- GitHub备份与正式安装包可复现，不假报Production完成

**下一步**：按验收矩阵逐项实测；未通过项不删除、不用单元测试总数替代最终交付
