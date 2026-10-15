> 历史交接记录：当前Agent接手请先读根AGENTS.md、AGENT_HANDOFF.md与docs/engineering/task-registry.json。本文历史线上/测试断言不代表当前实现验收。

# 当前交接：FACAI Cloud → Codex++

更新时间：2026-10-02（Asia/Shanghai）
工作目录：本仓库根目录；当前 HEAD：`c09df6c Prepare FACAI source and private handoff`（以下改动**全部未提交**）

## 现在的真实状态

- 本轮已核对并保留两处启动容错改动，且都补了回归测试：
  - `facai/scheduler.py`：`FACAI_MAX_WORKERS` 增加容错（空/非法回退默认 2，超出范围限制 1–8），不再在导入期抛 `ValueError`。
  - `facai/api.py`：空 `FACAI_DATA_DIR` 回退到默认 `data/`，不再把 SQLite 落到进程工作目录。
- 全量测试（主控在仓库根目录独立运行）：`python -m pytest -q` → **391 passed、2 warnings、46.54 秒**。所有测试通过。
- 本机沙箱内 Playwright（浏览器）与 Git Bash 检查无法运行（命名管道 / signal pipe 被拒），只能跑可运行子集：`python -m pytest -q --ignore=tests/test_console_layout.py --ignore=tests/test_mobile_ui.py --ignore=tests/test_ui_theme.py --deselect tests/test_deploy_config.py::test_validate_config_script_passes` → **376 passed、0 failed、1 deselected**（23.9 秒）。沙箱里这 15 项失败纯属环境限制，不是代码回归。
- 本轮之前仓库记录的基线是 346 项；当前共 391 项，新增 45 项（启动配置 29、微信端到端 1、微信重启恢复 1、WhatsApp 回复上下文 3、并行并入的 n8n 7、WhatsApp 入站加固 3、UI 浏览器用例 1）。
- 沙箱内跑测试需要一个**未跟踪**的本地补丁 `_tmp_sandbox_pytest_plugin.py`（本沙箱把 `mkdir(mode=0o700)` 即 pytest `tmp_path` 的模式变成不可访问目录）。命令：
  `$env:PYTHONPATH="."; python -m pytest -q -p _tmp_sandbox_pytest_plugin --basetemp=".tmp-pt-run"`
  该文件、`.tmp-*` 目录、`.tmp-startup-check*`、`facai.sqlite3` 一律**不得提交**。
- 启动探针（空 `FACAI_DATA_DIR` + `FACAI_MAX_WORKERS=auto`，仓库内路径、端口 8798）实测：`/api/v1/health` 200 `ready`、`/api/v1/auth/session` 200、`/login` 200 HTML、`/api/v1/scheduler` `concurrency=2`；SQLite 落在 `<cwd>/data/facai.sqlite3`，未落到工作目录。
- `work/` 仍是 Codex++ 不完整重复克隆，保持不动且绝不提交。

## 本轮新增/验证的功能与测试

### 启动配置回归 `tests/test_startup_config.py`（29 项）
- `FACAI_DATA_DIR`：空、纯空白、未设置 → 默认 `data/`；显式值/显式覆盖参数生效；空覆盖参数也回退默认而不是 cwd；`create_app` 集成验证 DB 落在 `data/facai.sqlite3`；空值启动时 health/login/session 正常。
- `FACAI_MAX_WORKERS`：空、`auto`、非数字、浮点 → 默认 2；`0`/负数 → 1；超过 8 → 8；显式 `concurrency=` 覆盖环境变量；`create_app` 在 `auto` 下可启动。

### 消息渠道入口（本地模拟，非真实授权）
- 个人微信端到端 `tests/test_wechat_flow.py`：真实 `create_app` + 模拟 iLink 服务，走通 二维码登录 → `AUTHORIZED` → 后台长轮询收到文本 → 原话逐字保存 + 专业提示 + 建 Mission + 默认绑定入队（`DISPATCH_QUEUED`）→ `/api/v1/inbox` → 依原 `context_token` 回复 `SENT` 并记录 OUTBOUND。只替换 `httpx.AsyncClient`，API/存储/监控/派发都是真实代码；测试给应用注入独立凭据映射，并断言 `os.environ` 前后不变，扫码凭据不会写入外部进程环境。
- 微信凭据重启恢复 `tests/test_wechat.py`：扫码写入 `providers/wechat.env` 后，新实例能重新加载 token/user/base_url 并保持 `CONFIGURED`（对应文档“重启后恢复”的承诺）。
- WhatsApp：入站只消费官方 `messages` 变更、要求 Meta `wamid` 与发送方 ID、结构异常正文不崩溃且原始对象仍保留审计；出站支持 `reply_to` → Cloud API `context.message_id`（对齐 Telegram 的统一 `OutboundReply` 语义）。
- n8n（并行工作流已并入工作树）：`N8N_WEBHOOK_URL` 用 `httpx.URL` 结构校验、`408→TIMEOUT`（可重试）、注入 client 也应用配置超时、relay 只发送新 `PENDING` 行且不突破 `max_attempts`。`tests/test_n8n.py` 已覆盖。
- 工作流 UI：新增 `BLOCKED` 状态样式与阶段“最近事件”展示；浏览器用例在本沙箱无法执行，仅本地静态与后端验证，未做真机走查。

## 开发优先级

1. 在干净环境按部署说明启动并核对 health、登录页、data 目录行为（本轮已本地验证，仍待实机）。
2. 消息渠道入口：微信扫码后的收/发路径本地已端到端验证；下一步需管理员扫码后做真机实收实发，再依次 Telegram、WhatsApp。**本地模拟通过不等于真实授权/线上接入完成**。
3. 任务原文→专业提示词→分发→执行→事件/百分比的端到端可见性；进度只能来自真实状态，不能造数。
4. 每完成一块都跑相应测试并报告事实。禁止部署、触碰生产账号/服务器、访问其他项目或打印/提交密钥。

## 当前工作树改动清单（未提交）

- 启动容错：`facai/api.py`、`facai/scheduler.py`
- 渠道：`facai/channels/whatsapp.py`（入站加固 + 出站 `reply_to`）
- n8n：`facai/n8n.py`
- UI：`facai/static/index.html`
- 测试：`tests/test_startup_config.py`（新）、`tests/test_wechat_flow.py`（新）、`tests/test_wechat.py`、`tests/test_channels.py`、`tests/test_n8n.py`、`tests/test_console_layout.py`、`tests/test_ui_theme.py`
- 文档：`docs/HANDOFF_NOW.md`、`docs/CHANNELS.md`
- **排除**：`work/`、`.tmp-*`、`_tmp_sandbox_pytest_plugin.py`、`facai.sqlite3`、`.tmp-startup-check*`

## 发布和外部状态

- 2026-10-02 已将当前工作树同步到 VPS `/opt/facai-cloud`，更新前备份保存于 `/var/backups/facai-cloud/predeploy-20261002T074142Z.tgz`；服务重启后已恢复 `active`，本机健康接口返回 `ready`，公网 DNS/TLS/HTTPS/匿名 401 校验通过。

- GitHub 私有仓库尚未成功同步；不要假称上传完成。生产部署已完成本轮同步和备份，但真实渠道授权、备份恢复演练仍待验收。
- 微信扫码、Telegram Bot、Meta/WhatsApp 等需要管理员自己授权；不得代扫码。客户多租户隔离尚未完成，不能宣称已支持五个隔离付费用户。
- 以前的全量测试仅证明本地代码基线。更新所有进度数字前必须根据当前 diff 和验证证据重新检查。

## 密钥安全

聊天中粘贴过的 API keys 按已暴露处理，建议用户在供应商端轮换。只从安全环境变量读取；本交接、日志、Git 均不得包含密钥值。
