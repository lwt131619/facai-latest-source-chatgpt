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

## 本轮增量：Mission Specification 与 Trust v1

- 确认任务同事务保存不可变任务规格及SHA，旧任务不伪造规格。
- /evidence-center：持久证据/Claim/显式Owner审查/上下文冲突/Proof快照/ZIP/独立验收。
- 新增14项Trust测试（含参数化），27项Trust+Intent+legacy定向通过。
- 完整回归1282通过/28跳过；另2项新桌面/手机真实Chromium验收通过。
- GitHub官方集成已恢复，多个小步骤已普通推送；当前状态以Git远端SHA与checkpoint为准。
- 没有真实Provider执行或VPS部署。Owner审查不是独立来源认证，事件引用不是外部工具签名证明。

## 发布阻塞仍存在

1. 首次Setup向导、Owner初始化、模型/Runtime安装连接仍待闭环。
2. 自然语言理解仍是明确规则和人工修订，没有完整LLM语义与校准置信度。
3. 任务规格已落地；用户自定义输出契约及真实执行全流程仍待验收。
4. 20+Agent实际协作、0.01%真实计量、独立来源信任图/采证、知识与native安装包待完成。
5. Proof路径已增加但未完成真实Provider/工具/HTTPS跨设备端到端验收，不能宣称Production完成。

详见docs/TRUST_WORKFLOW.md、AGENT_HANDOFF.md和工程任务登记表。
