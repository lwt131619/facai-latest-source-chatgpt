# FACAI发布状态 — 2026-10-15

**发布结论：NOT_READY_FOR_PRODUCTION_V1。** 已有可运行开发版，完整产品验收尚未闭环。

## 本轮实际功能新增

- `/workspace` 理解工作区：输入原话、结构化预览、修改/补信息、取消、确认Mission。
- 原话不可变，解释修订和Owner归属持久化；重复/并发确认只产生一个任务。
- 确认创建与Intent关联同事务；失败清理新工作区，确认不grant、不enqueue。
- Owner独立授权使用既有权限门；绑定和执行继续走原控制台/预算/Runtime检查。
- 桌面/手机入口已接入，手机响应式和刷新恢复已在真实浏览器通过。

## 真实验证

- 最终完整pytest：1265 passed / 28 skipped / 1上游弃用警告，90.75秒。
- 新增6项测试：原话/不可变修订/Owner隔离、并发幂等、无grant/jobs副作用、
  事务失败回滚与工作区清理、零预算402、API认证/CSRF/独立授权。
- 真实Chromium：登录→编译→缺信息→补信息→确认→独立授权→刷新恢复；
  桌面1440x1000/手机390x844无横向溢出，无JS错误。QA使用明确测试额度，未绕生产预算。
- 原控制台桌面首屏、手机布局2项真实Chromium测试通过。
- 没有真实模型/工具项目分析运行；不声称语义结果已验证。

## 发布阻塞仍存在

1. 首次Setup向导及模型/运行时安装连接没有完整闭环。
2. 当前理解编译是保守规则/人工修订，未实现完整LLM语义及校准，置信度保持未知。
3. 完整Mission Specification、20+Agent/0.01%真实计量尚未验收。
4. Evidence冲突/置信度/Proof接受、知识与native正式安装包仍待补。
5. GitHub：裸Git、临时URL GH_TOKEN、连接器写入均403。本机Windows推送成功不等于当前
   运行环境拿到同一凭据。本次GH_TOKEN存在且长度28，不能以长度单独判失效；实际push失败
   才是本次同步失败证据。checkpoint现已显式使用进程GH_TOKEN，不落远程配置和日志。

接手入口AGENTS.md与AGENT_HANDOFF.md；任务状态以task-registry为准。
开发ZIP与完整Git bundle会在本次检查点重建；远端只在remote_verified=true时视为成功备份。
