# 最终合约与兼容实现边界

以下是目标合约，不是已上线API清单。候选新增路由可在审计后调整，保持语义不变。
现有API前缀 `/api/v1`、SQLite Store、渠道/适配器/权限接口优先复用。

## Intent（独立的理解产物）

```json
{"raw_request":"用户原话","intent_preserved":true,"action":"analyze",
 "target":"明确对象","purpose":"明确目的或未知","scope":"明确范围或未知",
 "constraints":[],"missing_context":[],"assumptions":[],"confidence":null,
 "status":"READY","request_id":"...","revision":1}
```

status = READY | NEED_CONTEXT | REVIEW。confidence缺校准依据为null或明确说明的未校准值，
不能显示成已测概率。assumptions必须是列出的假设，不提升为事实。
修订用expected_revision做乐观锁；确认是幂等操作并绑定该修订。
候选入口：/intents（编译/列表）、/intents/{id}（读取/修订）、/{id}/confirm。
所有数据按当前认证Owner归属；确认不越过预算/租户/权限检查，不触发模型执行。

## Mission Specification

```json
{"objective":"...","required_inputs":[],"expected_outputs":[],
 "execution_steps":[],"verification_method":"...","evidence_required":[],
 "intent_id":"...","intent_revision":1,"trace_id":"..."}
```

执行DAG复用现有步骤/depends_on。任务状态适配目标CREATED/PLANNING/RUNNING/WAITING/
VERIFYING/COMPLETED/FAILED；旧Store使用WAITING/RUNNING/COMPLETED等，先写映射，不无迁移替换。
编译、授权、执行、验证分别记录状态；COMPLETED不能同时代表Owner接受。

## Authorization与Execution

记录请求主体、实际操作者、mission版本、能力/动作、目标范围、到期和撤销。
Authority gate是已有真实执行门。新路由不能以intent_preserved、READY或任意字符串令牌绕过它。
派发前预算/配额/能力检查继续生效；无模型/无CLI/无权限分别真实失败或等待。
事件至少覆盖IntentCreated、MissionCreated、AgentAssigned、ExecutionStarted、
EvidenceCollected、ArtifactCreated；保持旧事件名兼容，定义规范映射而非重复生成假事件。

## Evidence / Claim / Conflict

证据有ID、所属任务/Owner、来源类型、来源位置、收集时间、内容摘要/哈希和采集动作。
Claim保留原文、标准化断言、来源证据ID、时间/范围/环境/定义上下文、状态和理由。
冲突关联两个Claim及分类依据。外部源未抓取不能写“已验证可达”。
新增字段区分 evidence_completeness、integrity_verified、semantic_verification、owner_acceptance。
历史 /missions/{id}/proof 的 verified 形状先保持不变，升级接口版本或增加独立摘要。

## Proof Artifact

report.md / evidence.json / trace.json / metadata.json / verification.json；manifest列文件哈希。
导出路径必须受任务工作区/产物根目录约束，不能任意读服务端文件。每个下载仍要租户鉴权。
不把数据库文件、模型Key、系统环境、token、隐私审计清单或其他任务内容装入导出包。

## 数据库/事件/知识

优先对现有Store增量迁移：Users/Owner/Agents/Skills/Missions/Executions/Events/
Evidence/Claims/Artifacts/Knowledge/Permissions。模型Key使用secretbox/环境引用。
新增不变修订与确认关联用同一事务；避免确认重复创建任务或提交前授权。
事件outbox、重放游标、幂等key和重启恢复应可测试。Knowledge保存来源和Trace，不能把
未验证结果自动蒸馏成FACT；保留原有distill技能行为，升级需显式版本/兼容策略。

## Rust、Shannon与工具子系统

Rust Core是用户指定的后续增量工程方向，不授权替换现有FastAPI/SQLite/调度器。
先记录边界与接口对照，逐模块测试后再接入。Shannon是受FACAI控制的执行附属引擎；
Kali/BlackArch是独立工具库子系统，不接管Owner/Control Plane。依赖是否安装/真实可用要实测。
