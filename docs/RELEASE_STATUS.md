# FACAI / ICS 发布状态

**NOT_READY_FOR_PRODUCTION_V1。** 当前交付为已验证的开发版，不是完整产品验收。

## 本轮已实现

- 新本机/Setup：一次性Owner初始化、密码哈希、单次登录恢复、真实环境检查、模型加密配置/显式探测。
- 原话/修订/确认/授权边界保留；确认同事务生成不可变Mission Specification。
- Evidence/Claim/人工审查、四维冲突与保留历史的撤回。
- Proof快照、实际导出字节SHA、ZIP清单、独立Owner验收及变更失效。
- 显式Knowledge收录/隔离搜索/来源追溯，保持推断分类和历史状态。
- 凭据不经校验错误回显；既有API、计费、渠道、模型、Runtime能力保留。
- 源码/脚本/验证记录/方向文档分步提交到GitHub；开发包、完整Git bundle可重建。

## 最新验证

- 开发ZIP从零解压全量：1319通过/13跳过/0失败，真实Chromium启用，228.014秒。
- Git工作树此前全量1318通过/12跳过；最后安全/打包修正已包括在上述ZIP全量验收。
- 真实桌面和手机覆盖Setup、Intent、Evidence、审查、Artifact、Owner验收、Knowledge和恢复。
- 实际start.py启动与重启、Owner哈希/同会话/Intent历史恢复通过。
- Docker非root内部服务、鉴权、重启Mission规格/证据恢复、Proof字节哈希通过。
- Docker宿主映射端口在当前云命名空间未验证；CI状态以对应GitHub Actions提交为准。

## 仍需完成

20+实际Agent协作及隔离、可测0.01%计量、完整语义Compiler、独立可信来源/自动采证及校准、
真实Provider/Runtime全流程、Windows/macOS正式安装包、HTTPS跨设备和VPS最终验收。
没有执行上述外部验收；不把Owner审查、记录存在、哈希完整性或测试总数当作事实认证。

接手AGENTS.md→AGENT_HANDOFF.md；任务与验收在docs/engineering/。
证据在docs/validation/，任何Agent不得删掉未完成项来宣布完成。

## 远端CI覆盖补齐

真实nginx解析权限问题已复现并修复。官方nginx1.30.6/UID10001、默认和自定义域名的HTTP/TLS检查通过。
验证没有改生产模板或部署VPS。最终CI结果以该提交的GitHub Actions为准；机器记录见docs/validation/nginx-validation.json。
