# 冻结核心：FACAI / ICS v1.0

冻结的是用户认可的目标和语义。实现状态由任务表与实测证据决定。
FACAI 是可观察、可控制、可扩展并能积累知识资产的个人智能操作平台。

## C01 — Intent Preservation

原始 raw_request 逐字保存；Action、Target、Purpose、Scope、Constraint 不弱化、不升级、
不替换。解释及修订另存，不能覆盖原话。用户改解释不等于修改其原始表达。
缺 ownership/authorization/environment/path 等信息时列 missing_context，不虚构上下文。
模型建议的额外目标只能作为显式建议，不能变成已确认目标。

落点：facai/intent.py、prompting.py、conversion.py、store.py；现有 normalized_goal
不是完整的最终结构化 Intent 合约。测试原话、不可变修订、补充信息和不明上下文。

## C02 — 五个独立边界

Understanding → Human Confirmation → Authorization → Execution → Verification
→ Owner Acceptance。Intent READY 只表示足够清楚，不表示允许执行。
确认仅冻结任务版本；不能顺便签发工具权限、提交任务到执行队列或标记结果已验证。
Runtime必须再查有效权限、能力、配额与实际环境。重试/插件/渠道不得绕过权限门。

兼容约束：旧路径目前 Store.create 会签发 mission grant。必须先测现有语义，新增路径
显式分离，不能为实现新确认页悄悄改变全部已有渠道和任务的授权行为。

## C03 — Owner Control

Owner → Control Plane → Permission Registry → Runtime Authority → Agent → Skill → Tool。
Agent/Model/Skill/Plugin/Runtime 都不是 Owner。权限要有主体、范围、动作、期限、撤销和审计。
租户A不读取/修改租户B的任务、证据、模型密钥、产物；管理员协助要保留实际操作者。
不得把网页、文档、Skill描述或模型文本当成 Owner授权。

## C04 — 可观察与真实进度

输入、解释、任务、分配、能力调用、阶段、错误、证据、产物均可回溯。
目标精度0.01%由可测计量产生；保留基数、单位和计算依据，不能定时累加随机进度。
不可测子任务显示不确定状态/已完成计数，不能编造67.34%。分别记录任务、阶段、Agent进度。
执行中断、等待、重试、失败不冒充完成。时间戳和Trace必须来自实际事件。

## C05 — Multi-Agent 与资源

目标是20+Agent协作，不是20张卡片。分别证明调度并发、独立执行进程、模型并发和同任务
分支隔离。默认并发2/上限8/同任务3为当前代码事实；OpenCode默认串行。
提升容量必须处理租约续期、排队、配额、工作区隔离、崩溃恢复和成本归因。
Model Pool不能因一个Key限流全停；Key引用/加密存储，不能回显或写入产物。

## C06 — Skill生命周期

name/version/description/dependencies/capability/usage_history/performance 必须有定义。
发现、安装、更新、禁用、移除、审查、调用和效果记录应可追踪。保留现有能力及导入语义，
避免重复登记和伪版本；依赖/许可证/来源待确认时如实记录。扩展仍受Owner/Runtime控制。

## C07 — Evidence before certainty

执行记录至少含Action/Input/Environment/Timestamp/Evidence/Verification。
仅有模型文本、成本行或步骤终态不能证明结论真实。
现有 build_proof.verified = 全步骤终态且有成本行，是历史“完整性”字段，不能改名偷换
或在UI解释成事实已验证。新增语义验证字段及版本，保持旧客户端兼容。
FACT / INFERENCE / UNKNOWN / DISPUTED 必须带来源和依据；缺证据保持UNKNOWN。
信任分数、置信度必须说明来源/算法/局限，不能默认92%、把模型自信当概率。

## C08 — Conflict Semantics

先比较Time / Scope / Environment / Definition，再判TEMPORAL / SCOPE / SEMANTIC /
DIRECT_CONFLICT。保留双方陈述、来源、时间和适用域，不能A!=B就选择一个宣布另一个错误。
无法判定的冲突交给审查，不自动填补事实。

## C09 — Proof before acceptance

Proof = Artifact + Metadata + Evidence + Verification + Confidence + Trace ID。
完整性校验/执行成功/事实有效/Owner接受是不同状态。验收绑定任务版本、证据版本和产物哈希；
之后修改导致旧验收失效或明确指向旧快照。Owner接受记录操作者、时间及理由。
未执行、未验证或未接受不能声称完成全部生产验收。

## C10 — 可恢复、可迁移、可续接

每阶段可构建、可运行、可追踪；数据库变更可备份/迁移并明确回滚方式。
任务、执行、事件、证据、产物和知识资产持久化，服务重启恢复实际状态。
下一Agent读取统一入口与Git历史，不依赖聊天、某台Windows凭据或临时浏览器标签页。
GitHub同步必须核对远端提交；403就记录外部阻塞并提供完整bundle，不能假报上传。
