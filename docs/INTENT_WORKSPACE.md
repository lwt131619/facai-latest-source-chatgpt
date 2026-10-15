# 已实现：理解预览 → 确认任务 → 独立授权

入口 `/workspace`；桌面控制台和Mobile我的页面均有链接。

- POST /api/v1/intents：原话规则提取或显式interpretation，保存理解版本1。
- GET /api/v1/intents、/{id}：只读当前Owner的理解与修订历史。
- PATCH /api/v1/intents/{id}：expected_revision+interpretation，原话不变，追加修订。
- POST /{id}/cancel：取消未确认理解。
- POST /{id}/confirm：核对revision/READY/预算，原子创建一个WAITING Mission；重复幂等。
- POST /{id}/authorize：Owner单独授权，使用现有authority gate，不派发作业。

后续执行在现有任务控制台绑定Agent/Model/Runtime并调度，保留预算/租户/权限/能力检查。
没有Key/CLI/预算时按现有机制明确拒绝/等待。Owner在本实例认证账户语义下操作；
多租户不可通过本路径给他人任务授权。API Key仍沿用旧范围，不自动扩权至新理解接口。

数据在同一个SQLite中新增intent_requests/intent_revisions；确认关联与任务创建同事务。
原话与历史interpretation不更新；expected_revision冲突409；其他Owner404。
Store.create增加可选connection/issue_grant/mission_id，旧调用仍提交后自动grant。
新确认强制issue_grant=false，授权是另一动作；失败回滚数据库并清理新工作区。

当前编译是明确规则/人工修订，不宣称完整LLM语义理解。缺Action/Target保持NEED_CONTEXT，
置信度为null并明确未校准，不补不存在的上下文。完整Mission Specification、外部目标上下文、
证据/Proof验收仍有缺口；本功能不等于完整Production v1.0。

验证：6项SQLite/API测试覆盖跨Owner、CSRF、并发幂等、失败回滚、预算拒绝、独立授权。
真实Chromium用临时数据和明确测试额度运行桌面1440x1000/手机390x844完整操作；
确认后没有jobs，授权后仍没有jobs；刷新恢复预览，无JS错误/横向溢出。
测试没有真实模型/工具调用，不声称真实项目分析已执行。
