# 2026-10-16 更新包 · 未打通清单（交接 Codex 接手实施）

- 仓库：`lwt131619/facai-latest-source-chatgpt`，分支 `main`
- 同步状态：本包入仓即推送；推送前 HEAD 为 `bd3dcd3`（本地 == origin/main，CI 绿）
- CI：连续两次绿 —— `79954b5` 与 `bd3dcd3`（ubuntu-latest 全量 pytest + 打包）
- 全量测试基线：`dist/package-validation.json`（@196fd56）记录 1332 项 / 1319 通过 / 13 跳过 / 0 失败，real_chromium_enabled=true
- 本包内容（全部为新增文件，未改动任何现有源码）：
  - `README.md` —— 本清单
  - `mobile-001-audit.md` —— MOBILE-001 只读审计报告（13/13 端点核对 + PWA 链路分析 + 逐条验收裁决）
  - `ci-linux-portability.md` —— CI Linux 可移植性审计（ubuntu 上最可能翻车的 Top 5 非确定性用例 + 已核查安全项）
  - `vps-deploy-runbook.md` —— VPS 部署 Runbook（SSH 一到手即可执行）

---

## A. MOBILE-001 未打通项（审计已定位到 file:line，按最小步骤实施）

审计报告全文见本包 `mobile-001-audit.md`。裁决：验收线 1 PARTIAL、线 2 VERIFIED_LOCAL、线 3 PARTIAL。以下按优先级排序：

| # | 未打通项 | 位置与最小步骤 |
|---|---|---|
| 1 | **文件 Tab 是占位**（"产物中心待接"），而后端端点已有 | `facai/static/mobile.html:335-339` 是占位块。接 `GET /api/v1/artifacts`（`facai/api.py:3038`，跨任务产物中心，行字段 `{id, mission_id, name, created_at, title, owner_id}`，`public_artifact` 会剥掉 `path`）；下载/查看内容走 `GET /api/v1/missions/{id}/artifacts/{aid}`（`api.py:3055`，返回 `content` 前 20000 字符可调 1..200000）。审计原文建议接 per-mission 端点（`api.py:3045`，仅 `{id, kind, created_at}` 元数据），但文件 Tab 不进任务也能到达，**推荐中心端点**（占位文案自己写的就是 `/api/v1/artifacts`）。同时给 `facaiGo` 的刷新钩子（`mobile.html:1341-1344` 一带）加 `screen-files` 分支。未登录不展示演示数据——与其他 Tab 同一诚实口径 |
| 2 | **mobile.html 从不注册 Service Worker** | `boot()`（`mobile.html:1417-1440`）内复制 `facai/static/index.html:2091-2092` 原样：`if('serviceWorker' in navigator&&location.protocol==='https:'){navigator.serviceWorker.register('/sw.js').catch(()=>{});}` |
| 3 | **sw.js SHELL 不含手机页** | `facai/static/sw.js`：`SHELL` 加入 `"/static/mobile.html"`；`CACHE` 从 `"facai-shell-v1"` 升到 `"facai-shell-v2"`（activate 会自动清 v1）。`/api/*` 直穿不缓存的既有策略保持不变 |
| 4 | **manifest start_url 指向桌面台** | `facai/static/manifest.webmanifest:5` `start_url="/"`——从手机安装后启动的是 Desktop 总控台。二选一：给 mobile 单独 manifest（`start_url=/static/mobile.html`、`scope=/static/`），或明确产品口径"安装的是总控台"并写进文档 |
| 5 | **手机端无 Agent 视图**（goal 六项中唯一全缺） | 监控页 steps 清单（`mobile.html:1048-1063`）下方加只读 Agent 行，数据源复用 `GET /api/v1/missions/{id}/events` 的 `OPENCODE_*` 事件（前端已有 `EVENT_ZH` 中文译表 `mobile.html:1122`），无需新端点 |
| 6 | **手机端无设置摘要** | 「我的」页加只读摘要：`GET /api/v1/billing/me`（`api.py:1557`）返回 `{username, allowed, reason, monthly_limit_usd, spend, alert, pricing, estimated_cost}`。或把"设置"正式拆给 Desktop 任务并在注册表注明（现状即有意导向 Desktop，`mobile.html:290`） |
| 7 | **测试断言需同步** | `tests/test_mobile_surface.py` 补字面量断言：`"/api/v1/artifacts"` 出现在 mobile.html；SW 注册片段；sw.js 的 `"/static/mobile.html"` 与 `"facai-shell-v2"`。外链白名单 `ALLOWED_HOSTS` 不变（本包改动不引入新外链） |
| 8 | **A20 真机验收未执行**（注册表 next_action 的本体） | Android Chrome 390x844 + HTTPS（内网反代或自签）走查：登录→下单→监控→时间线→渠道绑定→测试推送。截图与命令写入 `docs/ACCEPTANCE_MATRIX.md` A20 行 + task-registry MOBILE-001 状态。此步需要真机，Windows 开发机无法替代 |
| 9 | （可选）测试推送真实上游链路 | 本地测试用 `FACAI_SKIP_CREDENTIAL_PREFILL=1` 跳过预检（`tests/test_mobile_ui.py:268`），DELIVERED 无真实凭证证据。用一条真实 Telegram bot token 做端到端测试推送，结果与其余渠道未实测的外部局限一并入档 |

实施完成后 MOBILE-001 才可据实升格（当前 PARTIAL 定位准确）。

## B. CI 非确定性风险（Linux 移植审计 Top 5，按下次翻车概率排序）

基线事实：486fed8 那次 CI 失败**只有** sed 一个用例，且 pytest 单失败不中止套件 → 其余全部用例（含浏览器/并发）已在 ubuntu-latest 通过。当前 HEAD 相对该提交只多 sed 一行修复 + 纯文档，确定性平台 bug 概率极低；风险集中在非确定性/环境敏感用例。全文见本包 `ci-linux-portability.md`（含位置/机理/修法）：

| # | 风险 | 修法 |
|---|---|---|
| 1 | `tests/test_mission_control_flow.py:24-27` —— 全套件唯一漏网的真实 DNS 调用（PUT 模型注册 endpoint=api.deepseek.com，经 `facai/secretbox.py:120` getaddrinfo；conftest.py:132 DNS 桩白名单不含本模块）。CI 上 DNS 一抖 → 422 → 断言失败 | conftest.py:132 白名单加 `'test_mission_control_flow'`（一行，零行为影响） |
| 2 | `tests/test_runtime_capacity.py:136-167,184,251` —— 0.4s 租约 vs 1.5s 慢任务的续约时序窗口，慢速 runner 上任务被 peer 偷走 | 租约余量放大 3-5 倍或改事件同步 |
| 3 | 浏览器像素断言（`scrollWidth <= innerWidth + 1`，散布 console_layout/mobile_ui/ui_theme/setup_browser/trust_browser）依赖 ubuntu 回退 CJK 字体度量；另 `test_ui_theme.py:19`、`test_billing.py:233`、`test_daily_progress.py:221` 三处 launch 漏 `--no-sandbox` | 补 sandbox 参数 + CI 预装 fonts-noto-cjk |
| 4 | `tests/conftest.py:64-69` `_free_port()` bind/close/rebind TOCTOU，Linux 临时端口复用更激进，一炸报废全部浏览器用例 | uvicorn 绑 0 端口后读实际端口 |
| 5 | `tests/test_eventbus.py:68-96` 8 线程并发写单 SQLite（store.py:458 timeout=10 + WAL），慢 runner 上可能锁超时 | 加大 busy timeout 或降并发 |

已核查安全（无需动作）：validate_config.sh 在 ubuntu 无 nginx 整段 SKIP；`dist/pytest.xml` 父目录自动创建；所有 `os.name=='nt'` 分支守卫正确且 0600 由 `os.open` 显式创建；无 gbk/winreg/cmd.exe/CRLF 问题；requirements 无 Linux 分歧行为；package.py 必需文件齐备且 SECRET 扫描无命中；git 依赖测试显式 `-b` 不依赖默认分支。

## C. 其他未打通项（按优先级）

| # | 项 | 状态与阻塞 |
|---|---|---|
| 1 | **VPS 部署**（77.221.148.118 / whstapp.lol / `/opt/facai-cloud`） | 唯一阻塞 = SSH 通道（用户名/端口/公钥安装方式）。Runbook 全文见本包 `vps-deploy-runbook.md`：A 路线 VPS 直拉 GitHub / B 路线 bundle 推送；`deploy/deploy.sh` 幂等（自动备份、data/ 永不触碰、拒绝自拷贝）。部署后按 health 200 → https://whstapp.lol → 冒烟任务 → 事件流水顺序验证 |
| 2 | **RUNTIME-001：20+ 真实 CLI 并发规模验收** | 受控适配器车队已入库（d28e996：24 任务 / 4 worker 零双重认领）；真实 Provider 配额下的规模验收待做 |
| 3 | **ACCEPT-001（NOT_VERIFIED）** | 完整 Production v1.0 端到端验收，按验收矩阵逐项实测；未通过项不删除、不用单元测试总数替代 |
| 4 | **DESKTOP-001** | Electron 仅远程站点壳；本地模式/生命周期/端口检测 + native 安装包实测 |
| 5 | 其余 PARTIAL 项（AUTH/SETUP/INTENT/MISSION/DB/EVENT/PKG/MODEL/SKILL/ROUTER/OPS/TRUST/CONFLICT/PROOF/API/KNOWLEDGE/SEC/UPDATE） | 逐项 next_action 见 `docs/engineering/task-registry.json`；RUST-001、ENGINES-001 为 PLANNED |

## D. 已打通 / 无需重做（防止重复劳动）

- 手机端 13/13 网络调用全部命中 `facai/api.py` 真实路由，**零死端点**（审计报告第三节逐条核对表）
- 登录/注册/退出、一句话下单（`POST /api/v1/missions/auto` 含 402 预算闸与回退规划）、4 秒轮询 + SSE 加速监控、时间线、四渠道（telegram/line/discord/slack）绑定/解绑/测试推送 —— 全部真实接线，失败路径（401/402/422/404/429/网络断连）如实透出
- 回归基线：`tests/test_mobile_surface.py` 8 项静态契约 + `tests/test_mobile_ui.py` 2 条真 Chromium 路径，全量 1319 通过 / 13 跳过
- CI 双绿：`79954b5` + `bd3dcd3`（根因修复见 `486fed8`：nginx -t 验证副本改听 18080/18443；`79954b5`：GNU sed 每个 -E 表达式需显式 `-e`）
- 交付包 QA：`dist/FACAI-v1.0-development.zip`（408 文件，逐文件 SHA256 清单，独立 verify 通过，@a871959）

## E. 纪律（接手方同样适用）

1. 不强推、不删仓库、不改公开性、不重写远端历史
2. 密钥/令牌不入源码、文档、日志、Git 远程 URL
3. `docs/engineering/task-registry.json` 任何改动后必须 `python scripts/validate_handoff.py --render`，否则包内校验失败
4. 本地绿 ≠ 远端绿：以 GitHub Actions API 结论为准
5. 注册表状态只按证据升级；缺证据保持 UNKNOWN / NOT_VERIFIED，不做假报
6. GNU sed：每个表达式显式 `-e`；后台运行 git push 会挂起，一律前台执行
