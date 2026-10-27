# 需求—源码—任务—验收对照

由 task-registry.json 生成；状态是证据范围，不是完成百分比。

| ID | 方向 | 状态 | 优先级 | 依赖 |
|---|---|---|---|---|
| DIR-001 | 统一方向与接手入口 | VERIFIED_LOCAL | P0 |  |
| AUD-001 | 真实源码审计与测试基线 | VERIFIED_LOCAL | P0 | DIR-001 |
| SYNC-001 | GitHub检查点与断点备份 | VERIFIED_LOCAL | P0 | DIR-001 |
| AUTH-001 | Owner Control与授权分离 | PARTIAL | P1 | AUD-001 |
| SETUP-001 | 首次配置向导与Owner初始化 | PARTIAL | P1 | AUTH-001 |
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

**下一步**：2026-10-21 Git工作树全量测试1447通过/7跳过/0失败(df7deb9)，真实Chromium启用；上一个ZIP从零解压全量测试1441通过/8跳过/0失败，源码快照938ebe5，额外跳过项仅因部署树无.git。新ZIP需另行验证，包内结果以dist/package-validation.json为准；尚非生产验收。

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

**现有定位**：`facai/onboarding.py`、`facai/setup_routes.py`、`facai/static/setup.html`、`scripts/start.py`、`tests/test_onboarding.py`、`tests/test_setup_browser.py`

**验收**：

- 初始化受loopback+单次bootstrap保护，完成后bootstrap失效
- Owner密码哈希复用既有身份体系，APIKey加密不回显
- 重启保留Owner，不通过全局auth bypass开放初始化

**下一步**：已本地实现/验证单进程新安装loopback一次性Owner初始化、PBKDF2持久化、单次登录恢复、模型配置/显式探测/系统检测；仍需Windows/macOS、真实Provider/Runtime与VPS交付验收。恢复密钥仅用于登录，不伪称Runtime控制权限。

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

**下一步**：已建成并有测试钉死(tests/test_eventbus.py 8例三连绿):规范词表CANONICAL_TYPES写入端校验(publish拒绝未知类型/非dict)、owner级全局流(events_for_owner)、/api/v1/events?after=与SSE流同光标契约、legacy类型原样流过、跨owner隔离、端点鉴权;新增并发追加测试:8线程24事件全局序号严格有序无重复,同光标双客户端重放逐字节一致(幂等)。剩余:首批消费端出现时把新路径的发布切到规范名(现无客户端消费CamelCase,不预先改语义)。

## PKG-001 — 一键启动与交付包

解压→启动→控制台→实际任务→可验证结果

**现有定位**：`scripts/start.py`、`scripts/package.py`、`start.bat`、`start.sh`、`Dockerfile`、`docker-compose.yml`

**验收**：

- 包与当前源码manifest逐文件校验一致
- 保留前面已验证的启动/Docker/持久化能力
- 新说明/备份脚本进入包；Windows/macOS分开实测

**下一步**：已实现一键Owner初始化/恢复、原生和容器内部重启验证、ZIP逐文件哈希与全包解压测试。2026-10-16接管代理在a871959重建交付包并独立QA通过(scripts/package.py + verify_package.py):408文件archive+manifest逐文件哈希一致,包内validate_handoff 27方向无环全文件在位,package_source_commit=a871959,source_manifest_verified=true(包重建即源码快照)。过程发现并修正:改task-registry.json后必须--render重生成REQUIREMENTS_TRACEABILITY,否则包内handoff校验如实失败。剩余:Windows/macOS分开实测(Windows已多次本地实跑:start链路+冒烟+包验证)、宿主跨设备/VPS交付验收(阻塞于SSH),不将开发包标Production。

## MODEL-001 — 多Provider/多Key模型资源池

配额水位、冷却、故障转移、延迟和成本可观察

**现有定位**：`facai/gateway.py`、`facai/router.py`、`facai/quota.py`、`facai/secretbox.py`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：池接线(①②③)与矩阵UI统计列/水位线编辑(④)与live marker(⑤)已入库：调度器reserve/report、reroute吃健康序、PoolState延迟/错误率/调用数、PUT /modelmatrix/{id}/limits管理员闸、@pytest.mark.live真实探测(FACAI_LIVE_API_KEY缺失诚实skip)。网关已接单模型多Key环境引用轮换，429与401/403自动换Key并短期冷却；配置及边界见docs/MODEL_KEY_POOL.md。仍缺CLI Agent上游请求统一接线、真实Provider验收。跨进程共享冷却已入库(2026-10-23):model_key_cooldowns表(墙钟epoch)+KeyRotation(store)双路径(内存monotonic快路径+SQLite共享),create_app自持app.state.key_rotation,complete()/key_usage全部走应用实例,多进程同库同冷却,过期行写入时清理(tests/test_gateway.py::test_shared_cooldown_visible_across_rotation_instances/test_app_owns_its_rotation_with_shared_backend)。每Key配额与成本统计已入库(2026-10-23):网关按(endpoint,环境变量名)记调用数与结局细分(completed/rate_limited/auth_failed/failed_other,被拒率列的数据基础)及上游自报input/output tokens,complete()结果带key_env归因;注册表可选price_input_per_1k/price_output_per_1k非负数字,GET /api/v1/modelmatrix/key-usage(仅admin/Owner)按Key报计数+tokens,仅在注册表带显式单价时计算estimated_cost,无单价返回null不发明依据;密钥值永不出现,计数进程内存重启清零(tests/test_gateway.py::test_key_usage_meters_failover_per_reference/test_key_usage_endpoint_reports_metering_with_explicit_pricing)。控制台「每Key计量 · KEY USAGE」块已入库(2026-10-23):按环境引用名展示调用/结局细分/tokens/成本,无单价显示—不发明估价,无多Key模型时空态如实;同一抽屉测试覆盖。 2026-10-28模型池预排序补并发槽位饱和判定（tests/test_quota_pools.py）；reserve仍是原子最终检查。

## SKILL-001 — Skill全生命周期

发现/安装/版本/依赖/调用/更新/禁用/移除/审查与效果记录

**现有定位**：`facai/skills.py`、`facai/distill.py`、`facai/boundary.py`、`facai/registries.py`、`facai/store.py`、`facai/execution.py`、`facai/api.py`、`facai/static/command-center.html`、`tests/test_skill_lifecycle.py`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：版本落库、禁用生效、软删tombstone及治理日志已入库。导入正文按Owner/版本/SHA存储；手动绑定及原话正向明确调用唯一Skill时冻结版本。否定句/仅提及/多Skill不自动绑定。usage区分正文交付与旧归因，不证明模型遵循。/registries/skills/{key}/update用预期版本+SHA在SQLite事务内CAS；/versions列出Owner隔离的历史版本，控制台可按精确版本+SHA查看已存指令正文（纯文本呈现、响应禁缓存，软删后仍可审查）。历史正文可凭精确版本+SHA、当前版本+SHA双重校验，在同一SQLite事务中以新版本重新发布；当前描述/启用状态保留，不伪称恢复旧frontmatter。旧/import仍是可覆盖的兼容路径。生命周期日志立即落盘，但SQLite与日志文件非同一事务。下一步:统一导入/更新冲突策略、跨存储崩溃一致性、模糊匹配Owner确认、完整元数据回滚、可验证效果指标。依赖字段已入库(2026-10-23):metadata.dependencies仅形状校验(≤20条,字符串或{name,version});路由期做存在性检查(enabled_only之后按名匹配,依赖被禁用视为缺失),assign_stage返回excluded_skills(排除原因随行、不静默丢弃、绝不自动安装),并随ASSIGNMENT_RECORDED事件载荷落库可审计;证据:tests/test_skill_lifecycle.py::TestDependencyShapeValidation/TestDependencyRoundtripInRegistry + tests/test_conversion_console.py::TestDependencyAwareRouting。

## ROUTER-001 — 能力到Agent/Skill/Tool路由

匹配任务能力/权限/资源且解释路由选择

**现有定位**：`facai/router.py`、`facai/capabilities.py`、`facai/registries.py`、`facai/conversion.py`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：ASSIGNMENT_RECORDED、BINDING_CHANGED、AGENT_OVERRIDDEN与CONVERSION_FALLBACK已留痕。JOB_REPROMPTED现包含同Mission FEEDBACK_RESOLVED事件引用、gate ID、新旧prompt SHA256；不重复存敏感全文。下一步:Skill自动路由(依赖SKILL-001④)、跨重启恢复链路验收。 2026-10-28增量：模型池ranked在分钟余量前先区分并发槽满/可接单；统一存储源、决策与质量反馈后续步骤见docs/WORKSTATION_ROUTING_STORAGE.md。真实质量指标尚未接入，不得宣称最优路由。

## RUNTIME-001 — 20+Agent并发与恢复

实际20+协作、隔离、取消、重试、配额、租约恢复

**现有定位**：`facai/scheduler.py`、`facai/execution.py`、`facai/opencode.py`、`facai/goose.py`

**验收**：

- 24独立任务受控适配器并发/租约续期/恢复有实测
- 线程/CLI进程/模型并发分别计量，OpenCode默认串行不默默移除
- 同任务分支隔离/超时/失败/取消/结果幂等有证据

**下一步**：受控适配器实测+车队上限修复已入库(d28e996):24独立任务/4worker零双重认领;FACAI_MAX_RUNNING_JOBS三层确定性测试钉死。真实CLI实弹已入库(scripts/smoke_local.py 11/11两连跑;scripts/smoke_fleet.py 16/16:4真实任务并发派发6.5秒全COMPLETED,零双重认领,产物逐一精确匹配,4行成本记账)。真实CLI崩溃恢复实弹已入库(scripts/smoke_recovery.py 8/8):死worker以已过期租约认领后消失→peer恢复恰好重排一次(JOB_REQUEUED事件钉住,attempts 0→1)→真CLI补跑COMPLETED,恰好1次OPENCODE_STARTED,产物r0.txt内容精确匹配,1行成本记账。剩余:20+真实并发规模(需真实配额支撑);调度并发2/上限8/同任务3不变。Agent级可计量单位已入库(2026-10-23):store.agent_progress()按DISPATCH_QUEUED事件归因(每任务恰好一次、稳定不漂移),结局取同一事件日志的JOB_COMPLETED/JOB_FAILED,运行中取jobs表,与Trace重放同源(事件一致);progress_measured=completed_jobs_of_attributed_jobs两位小数,运行中未完成任务只压低百分比绝不发明;无派发归因的任务不进入任何Agent的账;GET /api/v1/fleet/agent-progress仅admin/Owner,站点口径(tests/test_progress.py::test_agent_progress_is_attributed_and_measured/test_attribution_is_stable_and_completion_lands_on_dispatcher/test_fleet_agent_progress_requires_admin_session);控制台展示已入库(2026-10-23):command-center能力库抽屉新增「车队计量 · AGENT PROGRESS」块(agent_id/已完成c/assigned/失败/运行中/计量进度%,口径文案completed_jobs_of_attributed_jobs),无归因任务时空态诚实(没有分母的Agent不显示);tests/test_console_layout.py静态字段钉死+真Chromium抽屉实开断言。

## OPS-001 — Command Center与实时流水线

Dashboard/Missions/Agents/Skills/Models/Timeline/Evidence/Artifacts真实可操作

**现有定位**：`facai/static/command-center.html`、`facai/static/index.html`、`facai/api.py`

**验收**：

- 真实Dashboard/任务/Agent/Skill/资源/时间线可操作
- 0.01%来源于可测基数；不可测时不编造精确小数
- 缺模型/CLI/权限显示真实等待或错误

**下一步**：Mission/Agent计量与控制台显示已入库。下一步依 docs/VPS_24X7_WORKSTATION.md 做VPS健康、积压、模型/预算、备份告警实测；A10仍须跨端及真实并发事件一致性验收。

## TRUST-001 — Evidence与Claim Intelligence

Evidence Collector→Claim Normalizer→Source Trust→Conflict→Confidence

**现有定位**：`facai/trust.py`、`facai/trust_routes.py`、`facai/static/evidence-center.html`、`tests/test_trust_workflow.py`

**验收**：

- FACT/INFERENCE/UNKNOWN/DISPUTED可追到证据和理由
- 采集时间/来源/环境/哈希真实，无执行证据不标语义验证
- legacy verified不偷换，新完整性/语义验证字段独立

**下一步**：来源验证记录+信任图v0(①②)与事件侧自动采集/产物漂移钩子(③④)已入库：trust_source_verifications、trust_sources(owner隔离)、RUNTIME_TYPES同事务自动采集(FACAI_TRUST_AUTO_COLLECT默认开,幂等,失败不阻断)、TRUST_ARTIFACT_DRIFT留痕。独立来源验证的外部凭证面仍缺。confidence保持null。DISPUTED人工裁决出口与跨mission冲突检测已入库(2026-10-23):trust_adjudications表+TrustStore.adjudicate(outcome∈CONTEXT_DIFFERENCE/OWNER_RESOLVED,note必填,校验真冲突对,TRUST_ADJUDICATION_RECORDED事件);DISPUTED保持纯派生,已裁决对不再强制DISPUTED(状态回落各自review verdict,验收闸不放松);conflicts列表带adjudicated标注与裁决记录;cross_mission_conflicts(owner)同(subject,predicate)不同value跨mission检测(撤回不参与,owner隔离,限界);路由POST /trust/adjudications(Owner会话门)+GET /api/v1/trust/conflicts(tests/test_trust_workflow.py::TestAdjudication/TestCrossMissionConflicts/TestAdjudicationRoutes)。 URL公开体检的证据/评级/报告领取接入先按docs/PASSIVE_SITE_ASSESSMENT_UPSTREAM.md实施；开源选型已核验，采集器和站点控制权验证尚未实现。

## CONFLICT-001 — 上下文冲突语义

比较时间/范围/环境/定义后分类TEMPORAL/SCOPE/SEMANTIC/DIRECT_CONFLICT

**现有定位**：`facai/trust.py`、`tests/test_trust_workflow.py`

**验收**：

- 保留双方陈述、来源及时间/范围/环境/定义
- 四类冲突与无法分类样例可测试
- 不能A!=B就无依据判一方正确

**下一步**：classify_conflict 四维合约已由 tests/test_conflict_semantics.py 钉死(13例:不可比/TEMPORAL/SCOPE含environment映射/SEMANTIC/多维报告/双端缺失即REVIEW/空串视为缺失/仅同上下文才DIRECT_CONFLICT/对称性)。剩余:把TrustStore.add_claim的冲突事件流与/evidence-center控制台的展示接线补验收证据。

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

**现有定位**：`facai/trust.py`、`facai/trust_routes.py`、`facai/static/knowledge.html`、`tests/test_knowledge.py`、`docs/KNOWLEDGE_MEMORY.md`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：已实现Owner显式收录当前已验收Proof、来源/分类/生命周期保持、SHA及Trace、隔离检索/重启/过期标注；后续补检索增强的显式授权与独立来源验证，不自动把INFERENCE变FACT。

## MOBILE-001 — 手机控制台与通知配置

手机任务/实时进度/Agent/结果/通知/设置

**现有定位**：`facai/static/mobile.html`、`facai/static/sw.js`、`facai/static/manifest.webmanifest`、`facai/static/mobile.webmanifest`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：已接真实Owner产物列表、受鉴权文本预览/文本副本下载、Agent绑定/执行Job分配、账户额度摘要；独立Mobile manifest及逐资源SW离线缓存已实现。真实Chromium手机专项15通过，全量1355通过/6跳过/0失败；保持PARTIAL，继续真机HTTPS、通知真实上游及完整任务闭环验收，不将Job数当Agent并发数。

## DESKTOP-001 — Desktop Console本地软件

启动软件即有可操作本地FACAI服务

**现有定位**：`packaging/app/main.js`、`packaging/app/package.json`、`scripts/start.py`、`packaging/app/local-runtime.js`、`packaging/app/bundled-source.js`、`packaging/app/navigation.js`、`packaging/app/start-local.js`、`packaging/app/test/local-runtime.test.js`、`packaging/app/test/bundled-source.test.js`、`packaging/app/test/electron-wiring.test.js`、`packaging/app/test/navigation.test.js`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：Electron安装配置已纳入FACAI源码/静态资源，模拟安装资源与真实FastAPI启动、Owner数据隔离、窗口接线Node测试12通过；系统Python与联网装依赖仍必需。继续真实Windows/macOS native安装包构建与生命周期验收；此前Python全量1364通过/6跳过/0失败，不代表安装包验收。

## UPDATE-001 — 跨平台更新/备份/迁移/回滚

update.bat/update.sh升级前备份，失败可恢复

**现有定位**：`facai/backup.py`、`deploy/deploy.sh`、`backup.sh`、`backup.bat`、`scripts/backup_local.py`、`docs/LOCAL_RECOVERY.md`、`tests/test_backup.py`

**验收**：

- 实现达到本任务goal，相关真实API/界面可操作
- 保留已有有效行为，相关回归及失败路径测试通过
- 验证命令/结果/提交与外部局限写入交接记录

**下一步**：已补本地backup.sh/backup.bat、私有不可覆盖SQLite快照、隔离恢复演练与退出码；在线变化与恢复错误分开，新增表纳入核对。继续完整数据/密钥/工作区一致恢复和更新失败回滚，Windows/macOS未验收。VPS部署已实测(2026-10-23):SSH通道恢复(id_ed25519->root@77.221.148.118),清残留(僵尸crontab两条指向已删的/opt/facai-dev已移除并备份/root/crontab-backup-20261023.bak;/opt/facai-panel-src 51MB旧面板源码、坏的facai-staging、孤儿facai-ops、/tmp三个旧包已删;保留facai-cloud运行实例/whatsapp-relay会话/tron-sync/var-backups),staging按SHA固定重建于578a3c5,deploy.sh update完成(secrets保留/providers四env未动,nginx TLS重装,自动备份已打),验证:本机8765与公网https://whstapp.lol/api/v1/health均200（status=ready,phase=2,auth_required=true）,三服务active;浏览器登录冒烟与真实链路验收仍待(端口已从旧文档8080变为8765,runbook相应过时)。 7×24 VPS低成本实施顺序与分阶段证据见docs/VPS_24X7_WORKSTATION.md；当前GitHub main 411b125不等于2026-10-23记录的VPS部署578a3c5，线上版本需重新核验。

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

**下一步**：先适配器合约/来源/版本/可用性/权限/证据接入，再实测，不能接管OwnerARTEX 0.3.12已收录为首个附属引擎(2026-10-23):facai/artex.py HTTP适配器(一步一任务,goal=提示词,seed_first_intent开,终态done/failed/timeout映射OPENCODE_*词汇,goose同款诚实归因);权限模型按Owner指令本地/Owner最高:ARTEX内置审批门整体旁路(规则逐条禁用保留记录+拦截工具面清空+judge永不配置),唯一裁断权在FACAI Owner授权链;未配ARTEX_BASE_URL诚实失败;绑定走注册表agents adapter=artex;受控假ARTEX 6例钉死契约(tests/test_artex_adapter.py),真实实例部署与实弹未验证(需PG+Go构建,建议VPS /opt/artex);文档docs/ENGINES_ARTEX.md。

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

**下一步**：按验收矩阵逐项实测；未通过项不删除、不用单元测试总数替代最终交付。A26 闭环结构已钉住(2026-10-23):tests/test_acceptance_closed_loop.py 在同一使命同一Trace上全链通过:自然语言→修订→确认(原话保留)→独立授权→每步骤真实执行路径(OpenCodeAdapter+受控runner,不调模型)→执行事件同事务自动采集为证据→Owner补充证据+独立复核→断言未审UNKNOWN→Owner审查INFERENCE→证据快照→Owner验收(验收待办为空)→下载ZIP→知识收录→重启恢复→全局事件流重启前后逐字节一致。受控runner不等于真实Provider执行:矩阵A26保持NOT_VERIFIED,直至真实链路与真实界面验收通过。
