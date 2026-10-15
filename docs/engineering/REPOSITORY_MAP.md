# 真实源码定位图

基线：public snapshot 933b431 → 本地首批工程提交bc6ee5a。行号易变，按符号/文件定位。
未检查运行VPS；旧部署记录仅作历史参考。

| 方向 | 当前文件 | 接手检查重点 |
|---|---|---|
| API与应用生命周期 | facai/api.py:create_app | middleware租户/CSRF，应用state，路由兼容，后台循环 |
| 原话/理解 | facai/intent.py:IntentObject/compile_intent | 当前合约不完整且不能误当已接主执行链 |
| 转译/规划 | facai/prompting.py, conversion.py | deterministic_plan/model plan、确认、交互门、不可自动改目标 |
| 数据持久化 | facai/store.py:Store | schema/migrate/connect，事务，所有owner_id过滤 |
| 任务创建 | Store.create | 创建工作区/步骤/事件，旧路径会ensure_mission_grant |
| 权限 | auth.py, authority.py, authority_gate.py | session、root主体、grant有效性/撤销/重启 |
| 调度执行 | scheduler.py, execution.py | leases/recovery、依赖DAG、预算、失败/重试归因 |
| Agent适配 | opencode.py, goose.py, adapters.py | CLI存在性，OpenCode串行锁，工作区隔离 |
| 模型资源 | gateway.py, router.py, quota.py, outcomes.py | Key失败转移、池配额、429冷却、延迟与成本 |
| 资源登记 | registries.py, capabilities.py | Agent/Model/Skill/Runtime分离，租户与公共默认 |
| Skills | skills.py, distill.py, boundary.py | 版本/依赖/效果历史/禁用移除与审查 |
| Legacy Proof | evidence.py:build_proof | verified只代表终态+成本行，不能偷换成语义真实性 |
| Event Bus | eventbus.py, Store._event/append/events | 实际事件、重放、Trace、outbox |
| 产物/检查点 | checkpoint.py, store.py artifacts/checkpoints | 文件路径、内容哈希、diff、归属与导出 |
| 通道 | channels/, notify.py, n8n.py | 站长私人渠道与客户自带渠道隔离，外部配置诚实 |
| 密钥 | secretbox.py | 加密/引用，SSRF端点检查，禁止回显 |
| 账单 | billing.py, Store预算/充值, ops/tron_sync.py | 现有能力必须保留，不为新UI绕预算 |
| 桌面Web | static/index.html, command-center.html, conversion.html | 任务/流水线/证据/产物，身份与真实数据 |
| 手机/PWA | static/mobile.html, manifest.webmanifest, sw.js | 手机访问、通知、历史、设置与响应式 |
| Electron | packaging/app/main.js | 当前远程站点壳，不是已内嵌后端的离线产品 |
| 本地启动 | start.bat/start.sh, scripts/start.py | .venv、凭据、127.0.0.1、首次设置、重启 |
| Docker/部署 | Dockerfile, docker-compose.yml, deploy/ | 原服务内置SQLite/调度器；不无依据复制五服务 |
| 工程备份 | scripts/checkpoint.py, package.py | bundle/提交/远端SHA验证；源码allowlist与checksum |

外部库vendor/与历史ZIP不要自动视作已安装可用能力；先审核来源、许可与接入位置。
