# FACAI / ICS 完整工程路线（统一方向）

产品目标与27个任务详见 docs/engineering/task-registry.json 及 REQUIREMENTS_TRACEABILITY.md。
所有阶段保留真实已有能力；任务状态不是产品完成百分比。

1. **P0 方向/审计/续接**：DIR-001/AUD-001/SYNC-001。统一入口、冻结核心、真实基线、
   本地提交/bundle/GitHub核验。方向文档本轮整合，写权限403仍待解决。
2. **P1 可启动及理解到任务**：AUTH-001/SETUP-001/INTENT-001/MISSION-001/DB-001/
   EVENT-001/PKG-001。先事务与边界，再向导、结构化理解、可修订预览和幂等确认。
   确认不授权不执行；原话不可变。复用现有认证/数据库/规划/事件，启动能力已部分验证。
3. **P2 执行与信任闭环**：MODEL-001/SKILL-001/ROUTER-001/RUNTIME-001/OPS-001/
   TRUST-001/CONFLICT-001/PROOF-001/API-001/SEC-001。权限、资源和真实并发先做好，
   再把可观察执行、证据分类/冲突/置信度与Owner接受连起来。现有legacy proof不改义。
4. **P3 产品交付与增量引擎**：KNOWLEDGE-001/MOBILE-001/DESKTOP-001/UPDATE-001/
   RUST-001/ENGINES-001。长期记忆、手机/桌面、更新/迁移/回滚，Rust与附属引擎逐模块接入。
5. **P4 完整验收**：ACCEPT-001。原生平台/真实Provider和Runtime/20+Agent/全链路/恢复/
   GitHub备份与正式包逐项有证据后才声明Production v1.0。

最近完整测试：bc6ee5a对应1259通过/28跳过。当前代码调度默认2/上限8/同任务3，
OpenCode串行；没有RustCore、完整Setup/Intent确认/TrustEngine新实现。
这次优先把全部大方向标清并融合旧开发ZIP；没有用未验证的中途核心改动混入包。

下一个可运行阶段：先按MISSION-001的next_action实现Store原子事务/不授权新钩子及测试，
再实现Owner隔离的Intent修订/幂等确认。并行时Setup/Trust用新模块，根api由集成人统一接线。
每个可运行阶段执行checkpoint，不等所有开发结束才备份。
