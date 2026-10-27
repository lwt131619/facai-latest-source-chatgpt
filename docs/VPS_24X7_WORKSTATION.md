# FACAI 7×24 VPS 智能工作站：现有工程接入计划

更新基线：2026-10-27，GitHub main `411b125`。本文件是实施与验收计划，不代表
当前 VPS 已运行此提交或已通过 Production v1.0 验收。真实状态以仓库源码、
`docs/engineering/task-registry.json`、`ACCEPTANCE_MATRIX.md` 和部署实测为准。

## 1. 目标与现状

目标是复用 FACAI 的 Human Intent → Mission → Owner 授权 → Agent/Skill/模型
路由 → Runtime → Evidence/Proof/Knowledge 链路，构建持续运行、成本可控、
断线可恢复、任务可追踪的通用工作自动化站。避免另建一个聊天壳、并行数据库或
第二套任务队列。

| 能力 | 仓库现有落点 | 当前验收边界 |
| --- | --- | --- |
| Web/手机控制台、Intent、Mission | `facai/api.py`、`intent_routes.py`、`static/` | 浏览器路径已有；跨设备 HTTPS 全流程未终验 |
| 持久化调度、租约、任务恢复 | `facai/store.py`、`scheduler.py`、`execution.py` | 受控并发/恢复及少量真实 CLI 已测；20+ 真实模型并发未测 |
| Agent/Skill 路由 | `router.py`、`registries.py`、`skills.py` | 技能版本/依赖/归因已有；真实效果评估未闭环 |
| 模型网关/Key 池 | `gateway.py`、`quota.py` | 429/鉴权冷却和 SQLite 跨进程共享已有；CLI 自行发起的模型请求仍未统一接入 |
| 任务/Agent 进度 | `store.py`、`static/index.html`、`command-center.html` | 完成单位可计量；运行中没有分母就显示未计量；A10 未终验 |
| Evidence、Proof、Knowledge | `trust.py`、`trust_routes.py` 等 | 受控 runner 的 A26 脚本闭环已有；真实 Provider/Owner 浏览器流程未终验 |
| VPS 部署/备份 | `deploy/deploy.sh`、`deploy/systemd/`、`deploy/nginx/` | 2026-10-23 记录 VPS 部署 `578a3c5`、health 200；不能外推到当前 HEAD |

附属引擎不改变 FACAI Owner 控制面；通用工作站的第一阶段不依赖任何附属引擎。

## 2. 最低成本架构

单 VPS 优先。资源起点建议 2 vCPU / 4 GB RAM / 40 GB SSD，属于容量假设，
需用真实任务的内存、排队、延迟和成本数据校准；低配时减少并发而不假装可跑 20+。
沿用生产已经存在的 systemd + nginx：

```
浏览器/手机 → HTTPS 443 nginx → 127.0.0.1:8765 FACAI API
                                       ├─ SQLite + 用户数据 + Artifact
                                       ├─ 现有 scheduler/worker/租约
                                       ├─ 模型网关/Key 池/预算
                                       └─ 事件、Evidence、Proof
systemd timer → 数据备份、摘要、Skill 刷新（逐项启用和验证）
```

`docker-compose.yml` 是本地/容器交付路径，监听 `127.0.0.1:8080`；**当前 VPS
生产路径是 8765 + nginx**。不要在同一台机器上让 Compose 与 systemd 同时管理
同一生产数据目录。SQLite 继续作为单机数据库；只有实测写入竞争或多机需求后，
才设计 PostgreSQL 兼容迁移。无需先上 Kubernetes、Redis 或消息代理。

## 3. 可执行部署顺序（部署前先核验权限和备份）

1. 在开发机 `git fetch origin`，确认工作树干净及待部署 SHA。当前 main
   `411b125` 比 2026-10-23 记录的 VPS 部署 SHA `578a3c5` 更新；**不能直接认为
   VPS 已包含后续代码**。检查最新 CI、依赖与数据库迁移；在 staging 副本跑
   `python scripts/validate_handoff.py` 和与变更相称的测试。
2. 在 VPS 上读取而不改动当前版本：`systemctl status facai-cloud nginx`、
   `systemctl list-timers 'facai-*'`、`curl -fsS http://127.0.0.1:8765/api/v1/health`；
   记录当前代码 SHA、SQLite 路径、可用空间和已有备份。生产账号和 Provider 配置
   位于 `/etc/facai-cloud/`，不要复制值到日志、聊天或 Git。
3. 使用独立 staging checkout，**不要从 `/opt/facai-cloud` 运行 update**。
   先执行 `sudo ./deploy/deploy.sh backup`，确认应用备份及数据库快照存在；
   备份脚本的具体覆盖范围见 `docs/LOCAL_RECOVERY.md`，尤其确认密钥和工作区数据。
   按 `docs/DEPLOYMENT.md` 从 staging 执行 `sudo ./deploy/deploy.sh update`。
   `deploy.sh` 会拒绝来源与目标相同，并在受管代码同步前备份；不覆盖生产数据目录。
4. 部署后依次运行 `sudo ./deploy/deploy.sh status`、`nginx -t`、本机 health、
   HTTPS health、匿名接口拒绝测试，以及 Owner 登录/一个低成本 Mission 的真实链路。
   验证事件、Evidence、Artifact、重启后恢复；失败时根据部署前备份执行
   `sudo ./deploy/deploy.sh rollback`，并复测数据与旧版本。记录实际 SHA、时间、
   命令、退出码和结果；只有验证通过才更新 `docs/DEPLOYMENT_RECORD.md`。

本会话环境访问公网 health 时收到出口代理 CONNECT 403，**未由此验证 VPS 当前
在线状态**。线上验收应由能访问 VPS 的执行环境进行。

## 4. 7×24 运行与成本控制

- **存活与恢复**：`facai-cloud.service` 已设 `Restart=on-failure`、非 root 用户、
  数据目录唯一写权限；验证过期租约重排、重复事件幂等、服务重启前后同一 Trace。
  进程存活不等于任务成功，积压和错误率单独监控。
- **备份**：`facai-backup.timer` 已定义每日 01:00 UTC（北京时间 09:00）运行，
  安装与启用状态需在 VPS 上逐项确认。数据库备份之外，明确覆盖 Artifact、工作区、
  主密钥及 Provider 配置的加密离机备份与隔离恢复演练；不能把数据库快照误称全量恢复。
- **模型成本**：先按任务设置预算上限、可用能力和模型优先级；路由排除无权限、
  无能力、冷却、额度不足或预算超限者，再比较可观察的成本/延迟/失败率。
  模型响应未报告 token 或管理员未设单价时显示未知，不推测账单。
- **自动任务**：优先复用已有 Mission、Skill 和 systemd timer。定时任务仅提交
  可追踪的任务，不直接以 root 执行模型生成的命令；执行前走现有 Owner 授权和
  运行时检查。按任务幂等键、防重复派发和预算上限控制重试。
- **监控告警**：至少监视 API/DB/磁盘、队列积压、失败/重试、租约过期、
  模型 429/401、日预算、备份失败和证书到期。通知渠道必须实测到达；
  `/health` 不替代受鉴权的 `/status`/`/metrics`。
- **容量**：先测 1、4、8 路，再测 20+；调度并发、CLI 进程并发、模型侧并发
  分开记录。单 VPS 资源不足时扩资源或拆 Worker，不能只调高数字。

## 5. 分阶段工程与验收

| 阶段 | 实施增量 | 通过条件 |
| --- | --- | --- |
| P0 基线 | 远端 SHA、VPS 运行 SHA、服务/定时器、备份清点；修复过时 runbook 端口 | 基线与数据备份可核对，不改在线数据 |
| P1 稳定站 | 部署当前版本到 staging，迁移/回滚演练，运行/故障/备份告警 | 重启、断网、Worker 崩溃后任务不丢不重；恢复演练成功 |
| P2 通用任务 | 一个非模拟 Provider 的低成本 Mission 跑完 Intent、授权、Agent/Skill、Evidence、Proof、Knowledge | 同一 Trace 有真实输入、调用、证据、产物和重启后重放；A26 才可评估 |
| P3 智能路由 | CLI 模型请求接入 Key 池/预算归因；真实 429、Key 切换和成本核对 | 每次选择有候选/排除理由、Key 引用、真实用量；不泄漏密钥 |
| P4 扩容与交付 | 负载逐级扩大，手机 HTTPS、更新失败回滚、从 ZIP 启动 | 20+ 并发按四种口径分别验收，A01/A09/A10/A20/A22/A25 逐项有证据 |

每阶段结束后更新 `task-registry.json` 和验收矩阵的**实际证据**，正常提交并推送
GitHub；未实测的项目保留 PARTIAL/NOT_VERIFIED。正式 Production v1.0 发布仍
以全部必要验收项通过为准。
