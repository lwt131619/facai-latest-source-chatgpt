# MOBILE-001 手机控制台与通知配置 · 只读审计报告

- 审计日期：2026-10-16
- 审计对象：`facai/static/mobile.html`（1463 行，76,291 字节）、`facai/static/manifest.webmanifest`（17 行）、`facai/static/sw.js`（44 行）
- 审计方式：只读静态审计（未改任何文件、未跑 git、未联网、未重跑测试）
- 参考背景：2026-10-16 smoke 已证实真实运行服务器上 `/static/mobile.html` 可服务完整 HTML

---

## 一、任务注册表原文（task-registry.json:470-490）

`docs/engineering/task-registry.json` 中 MOBILE-001 条目：

> **id**: "MOBILE-001"，**title**: "手机控制台与通知配置"，**status**: "PARTIAL"，**priority**: 3
> **depends_on**: ["OPS-001", "PROOF-001"]
> **source_files**: ["facai/static/mobile.html", "facai/static/sw.js", "facai/static/manifest.webmanifest"]
> **goal**: "手机任务/实时进度/Agent/结果/通知/设置"
> **next_action**: "复用已有mobile/PWA，接新流程，在真实390x844浏览器和HTTPS访问验收"

acceptance 三条验收线逐字引用：

1. `"实现达到本任务goal，相关真实API/界面可操作"`
2. `"保留已有有效行为，相关回归及失败路径测试通过"`
3. `"验证命令/结果/提交与外部局限写入交接记录"`

---

## 二、验收线 → 实现证据映射

### 验收线 1：实现达到本任务goal，相关真实API/界面可操作

goal 拆成六项（手机任务 / 实时进度 / Agent / 结果 / 通知 / 设置），逐项对照：

| goal 项 | 状态 | 证据（file:line） |
|---|---|---|
| 手机任务（列表/下单/监控） | 真实接线 | 列表 `mobile.html:852-865`（GET /api/v1/missions）→ `facai/api.py:1365`；一句话下单 `mobile.html:938-966`（POST /api/v1/missions/auto）→ `api.py:1454-1503`（含 402 预算闸、反代转售节流、规划失败回退确定性五阶段）；单任务监控 `mobile.html:997-1074` → `api.py:1681` |
| 实时进度 | 真实接线 | 4 秒轮询 `mobile.html:1076-1094` + SSE 加速 `mobile.html:1102-1118`（监听 `mission` 事件）→ SSE 后端 `api.py:1720-1732`（`event: mission` 与前端监听名一致）；进度环数值全部来自后端 `mission.progress`（`mobile.html:1012-1021`），无本地造假 |
| Agent | **缺失** | 手机端无 Agent 视图。监控页只展示阶段（steps）清单 `mobile.html:1048-1063`；事件流把 OPENCODE_* 事件译成中文（EVENT_ZH `mobile.html:1122`）算最接近的投影，但无独立 Agent 状态面板；代码库也无 /api/v1/agents 类端点 |
| 结果 | **占位** | 完成页 screen-result 是「演示分镜」（`mobile.html:600-631`，页面自标"演示分镜：真实完成状态以任务监控为准"）；文件 Tab 是占位（`mobile.html:336-339`"产物中心待接 /api/v1/artifacts"）——而后端 GET /api/v1/artifacts 与 /missions/{id}/artifacts 实际存在（`api.py:3038-3045`），前端未接 |
| 通知 | 真实接线 | 四渠道绑定/解绑/测试推送全真实：表单 `mobile.html:362-429`；绑定逻辑 `mobile.html:1234-1263`；解绑 `mobile.html:1265-1280`；测试推送 `mobile.html:1297-1321`（DELIVERED 只认上游确认，失败给稳定错误码中文释义）。后端 PUT/GET/DELETE/POST-test：`api.py:1873-2010`、`api.py:2019-2055`；绑定前真实上游预检（telegram getMe、line /v2/bot/info、discord /users/@me、slack auth.test） |
| 设置 | **缺失（有意导向 Desktop）** | 手机端无设置页；「我的」页只放会话卡 + 渠道卡 + 桌面跳转链接（`mobile.html:351-352`：/workspace、/evidence-center、/setup，三条页面路由均已核实存在：`facai/intent_routes.py:33`、`facai/trust_routes.py:62`、`facai/setup_routes.py:93`）；页面明示"复杂控制在 Desktop 总控台"（`mobile.html:290`） |

其他可操作面：登录/注册/退出（`mobile.html:800-845` → `api.py:1197/1295/1333`）、会话探测（`mobile.html:1417-1440` → `api.py:1175`）、任务时间线（`mobile.html:1123-1169` → `api.py:1695`）。失败路径如实透出：401 会话失效回登录（`mobile.html:739-747`）、402 余额（`mobile.html:952-954`）、422 原样展示、网络失败如实文案。

另有占位未接：语音输入/附件按钮（`mobile.html:310-311`，toast 明示"占位，待接"）；授权/重新规划页为演示分镜（`mobile.html:544-598`，页面自标"接口待接，不做假授权"）。

### 验收线 2：保留已有有效行为，相关回归及失败路径测试通过

| 证据 | 位置 |
|---|---|
| 静态契约测试 8 项：真实请求路径字面量、登录登出接线、写请求带 CSRF、渠道表单走真端点、未登录诚实拦截、监控为轮询驱动非假数、无凭据形状泄漏、外连主机白名单 | `tests/test_mobile_surface.py:31-118` |
| 真浏览器（Playwright）路径 2 条：注册即登录→错密码诚实报错→一句话真下单→监控进度==后端真值→时间线含 PROMPT_CONVERTED；渠道绑定空表单本地拦截→非法 token 422 透出→合法绑定回显→解绑→退出 | `tests/test_mobile_ui.py:185-257`、`tests/test_mobile_ui.py:260-314` |
| 布局/可点性（390x844 手机视口） | `tests/test_mobile_ui.py:42、106、128` |
| 注册表记录：real_chromium_desktop_mobile_passed=2；全量回归 1282 通过/28 跳过 | `docs/engineering/task-registry.json:640-650` |
| 打包验收把 mobile.html 与两份测试文件按 SHA256 纳入源清单 | `docs/validation/package-acceptance.json:117、200-201` |

失败路径覆盖面（代码侧）：401/402/422/404/429 与网络 catch 均有专门分支与如实文案；测试侧覆盖错密码、空表单、非法凭据。本审计未重跑测试（只读约束），按已有记录采信，故评 VERIFIED_LOCAL 而非 VERIFIED。

### 验收线 3：验证命令/结果/提交与外部局限写入交接记录

| 证据 | 位置 | 内容 |
|---|---|---|
| 交接纪要 | `HANDOFF-NEXT.md:550-556`（§22.5） | Agent C 交付记录：commits `a9e607d`+`428c3a7`，mobile.html 从静态演示转真实可用，test_mobile_surface 8 项 + test_mobile_ui 2 条真浏览器路径，全量 1169 绿，"生产实测含真实端点字面量" |
| 排队项入档 | `HANDOFF-NEXT.md:557` | "绑定渠道测试推送按钮/端点"当时列为排队项，现已实现（api.py:2019），说明记录与实现演进一致 |
| 验证汇总 | `docs/engineering/task-registry.json:628-639` | latest_validation：package_suite_passed 1319、source_manifest_verified、live_provider_execution=false、production_acceptance=false |
| **未完成项如实保留** | `docs/engineering/ACCEPTANCE_MATRIX.md` A20 | "桌面/手机全流程操作：1440x900和390x844真实浏览器；HTTPS跨设备及通知/设置" = **NOT_VERIFIED** |

结论：提交与命令有记录、外部局限（真实 Provider 执行、HTTPS 跨设备）明确保留为 false/NOT_VERIFIED，没有假报；但 next_action 要求的"真实390x844浏览器和HTTPS访问验收"本身仍未执行（2026-10-16 smoke 只证明页面能服务，不等于真机走查）。

---

## 三、mobile.html 调用端点 ↔ api.py 路由核对（13/13 全部为真实路由，无死端点）

| # | 前端调用 | 前端位置 | 后端路由 | 核对 |
|---|---|---|---|---|
| 1 | POST /api/v1/auth/login | mobile.html:805-809 | api.py:1197 | 真实（节流 429、401） |
| 2 | POST /api/v1/auth/register | mobile.html:805-809 | api.py:1295 | 真实（201） |
| 3 | POST /api/v1/auth/logout | mobile.html:834 | api.py:1333 | 真实 |
| 4 | GET /api/v1/auth/session | mobile.html:1424 | api.py:1175 | 真实（返回 csrf_token/must_change_password/assist） |
| 5 | GET /api/v1/missions | mobile.html:854 | api.py:1365 | 真实 |
| 6 | POST /api/v1/missions/auto | mobile.html:950 | api.py:1454 | 真实（402/422/回退规划/dispatch） |
| 7 | GET /api/v1/missions/{id} | mobile.html:998、1078 | api.py:1681 | 真实（owner 域校验 require_mission） |
| 8 | GET /api/v1/missions/{id}/events | mobile.html:1134 | api.py:1695 | 真实 |
| 9 | EventSource /api/v1/missions/{id}/stream | mobile.html:1105 | api.py:1720 | 真实 SSE，`event: mission` 与前端监听器匹配 |
| 10 | GET /api/v1/my/channels | mobile.html:1196 | api.py:1893 | 真实 |
| 11 | PUT /api/v1/my/channels/{telegram,line,discord,slack} | mobile.html:687-692、1250 | api.py:1873/1917/1955/1983 | 真实（格式校验+secretbox 加密+上游预检） |
| 12 | DELETE 同四渠道 | mobile.html:1271 | api.py:1904/1944/1976/2005 | 真实（404 语义） |
| 13 | POST /api/v1/my/channels/{channel}/test | mobile.html:1304 | api.py:2019 | 真实（与 NOTIFY 同信任路径，DELIVERED 只认上游确认） |

- 页面链接：/workspace、/evidence-center、/setup、/billing 路由全部存在（intent_routes.py:33、trust_routes.py:62、setup_routes.py:93、api.py:1138）。
- 静态资源：/static/mobile.html 经 StaticFiles 挂载（api.py:1168）；/manifest.webmanifest（api.py:1157）、/sw.js（api.py:1162）均单独挂路由；icon-192/512.png 实际存在。
- 安全口径吻合：前端写请求带 X-CSRF-Token（mobile.html:721）与后端会话中间件 CSRF 校验（api.py:1031-1034）匹配；/manifest、/sw.js、/static/* 列入公开路径白名单（api.py:504-508），锁屏刷新/安装提示不会被 401 拦截——设计自洽。

**死/缺失端点：无。**

---

## 四、Service Worker 与 PWA 诚实静态分析

`facai/static/sw.js`（44 行）：

- **缓存策略**：仅外壳 network-first——SHELL = ["/", "/login", "/static/icon-192.png", "/static/icon-512.png"]（sw.js:5），成功响应才回填缓存（sw.js:35-38），离线时回退缓存命中（sw.js:41）；/api/* 一律直穿不缓存（sw.js:28）；非 GET 与跨源直穿（sw.js:23-25）。对"控制台是活数据"的定位是正确设计。
- **离线回退**：仅上述 4 个路径有离线回退；**/static/mobile.html 不在 SHELL 内**——手机页离线打不开。安装期 addAll 失败整体放弃（sw.js:9 catch→null），靠运行期缓存兜底，意味着必须成功在线访问过 / 与 /login 后离线外壳才可用。
- **更新流**：CACHE 名 facai-shell-v1，activate 时删旧缓存 + skipWaiting + clients.claim（sw.js:8-19）。浏览器导航时按字节差异自动更新，机制可用但无版本提示 UI；更新粒度最小、可接受。
- **对真机使用的影响（关键缺口）**：
  1. **mobile.html 从不注册 service worker**。全仓只有 login.html:159-160 与 index.html:2091-2092 注册 /sw.js，且都以 `location.protocol==='https:'` 为前置条件。用户直接访问 /static/mobile.html 时，除非此前在 / 或 /login 已注册过（根 scope + clients.claim 会覆盖手机页），否则手机页无 SW、无离线外壳、Android Chrome 不满足完整 PWA 安装条件。
  2. **HTTPS 门控**：局域网 HTTP（如 http://192.168.x.x:8080）下 SW 永不注册——与注册表"HTTPS访问验收"待办一致，是真实保留的外部局限而非 bug。
  3. **manifest 指向桌面台**：manifest start_url="/"（manifest.webmanifest:5），从手机页"安装"后启动的是 Desktop 总控台而非手机遥控器。头部注释（mobile.html:9-12）声称"真机安装复用根 manifest…Service Worker /sw.js"，"挂载"属实（api.py 确实服务 sw.js），但安装链路在手机页自身是断的。
  4. SSE/轮询全部在 /api/ 下被 SW 绕开——不会缓存事件流，无缓存污染问题；SSE 断线只关不重连（mobile.html:1113-1116），靠 4 秒轮询兜底，功能不丢但有双通道浪费的改进空间。
- 图标：192/512 + maskable 齐备；manifest 与页面 theme-color（#050b16）一致。

尺寸交叉核对：mobile.html 磁盘 76,291 字节（约 74.5 KiB），smoke 记录 68KB——量级一致，疑为版本或计量口径差异，不构成阻塞疑点。

---

## 五、逐条裁决与最小下一步

| 验收线 | 裁决 | 依据摘要 | 最小下一步（描述，不实施） |
|---|---|---|---|
| 1. 实现达到本任务goal，相关真实API/界面可操作 | **PARTIAL** | 任务/实时进度/通知三项真实可操作（13/13 端点真实路由，失败路径如实）；Agent 视图缺失、结果页为演示分镜+产物占位（后端 /api/v1/artifacts 已有而前端未接）、设置无手机端 | ① screen-files 改接 GET /api/v1/missions/{id}/artifacts（api.py:3045）渲染产物清单与下载链接（api.py:3055），删"待接"占位；② 监控页 steps 下方加只读 Agent 行（数据源复用 /events 的 OPENCODE_* 事件，不需新端点）；③ 「我的」页加只读设置摘要（会话/绑定/余额 /api/v1/billing/me）或把"设置"从本任务 goal 正式拆给 Desktop 任务并在注册表注明 |
| 2. 保留已有有效行为，相关回归及失败路径测试通过 | **VERIFIED_LOCAL** | test_mobile_surface 8 项静态契约 + test_mobile_ui 2 条真浏览器路径（含错密码/空表单/非法凭据失败路径）+ 布局可点性；注册表记 real_chromium_desktop_mobile_passed=2、全量 1282 绿；本审计未重跑，按记录采信 | 在真机验收（见第 3 行）同批用 `pytest tests/test_mobile_surface.py tests/test_mobile_ui.py` 重跑一次留档，把命令与通过数写入交接记录，使 VERIFIED_LOCAL 升格为有当日证据 |
| 3. 验证命令/结果/提交与外部局限写入交接记录 | **PARTIAL** | HANDOFF-NEXT §22.5 有提交（a9e607d+428c3a7）与实测记录；latest_validation 如实保留 live_provider_execution=false；但 A20（390x844 真浏览器 + HTTPS 跨设备 + 通知/设置）= NOT_VERIFIED，next_action 指定的验收本体未执行；2026-10-16 smoke 只证明可服务 | 执行一次真机走查：Android Chrome 390x844 + HTTPS（内网反代或自签），按 test_mobile_ui 两条用例的手工等价路径走 登录→下单→监控→时间线→渠道绑定→测试推送，截图与命令写入 ACCEPTANCE_MATRIX.md A20 行，并同步 task-registry MOBILE-001 status 与 next_action |

补充缺口（不属三条验收线、但影响"真机可用"承诺，随验收线 1 一并处理）：

| 缺口 | 影响 | 最小下一步 |
|---|---|---|
| mobile.html 不注册 /sw.js（仅 login/index 注册，且 https 门控） | 手机页无离线外壳、不满足 Android 完整 PWA 安装链 | 在 mobile.html boot() 复制 index.html:2091-2092 同款 https 门控注册，并在 test_mobile_surface.py 加一条字面量断言 |
| sw.js SHELL 不含 /static/mobile.html | 已装 SW 的设备上手机页离线仍打不开 | SHELL 加入该路径并把 CACHE 升到 v2（activate 会自动清 v1），sw 断言同步更新 |
| manifest start_url="/" | 从手机安装后启动桌面台 | 明确产品口径：要么给 mobile 单独 manifest（start_url=/static/mobile.html、scope=/static/），要么接受"安装的是总控台"并写进文档 |
| 测试推送真实上游链路未实测 | 本地测试用 FACAI_SKIP_CREDENTIAL_PREFLIGHT=1 跳过预检（test_mobile_ui.py:268），DELIVERED 无真实凭证证据 | 用一条真实 Telegram bot token 做一次端到端测试推送，结果与外部局限（其余渠道未实测）入档交接记录 |

---

## 六、总结

MOBILE-001 的真实化改造是扎实的：手机端 13 个网络调用全部命中 facai/api.py 真实路由，无一死端点；登录/下单/监控/时间线/四渠道绑定解绑测试推送均走后端真值，失败路径（401/402/422/404/429/网络断连）如实透出，未登录不展示演示数据，演示分镜全部显式标注；回归与真浏览器测试有记录（VERIFIED_LOCAL）。差距集中在三处：goal 里 Agent/结果/设置三项在手机端缺失或占位（后端 artifacts 端点已有、前端未接）；PWA 安装链在手机页断裂（mobile.html 不注册 SW、SHELL 不含手机页、manifest 指向桌面台）；next_action 指定的真机 390x844 + HTTPS 验收（验收矩阵 A20）未执行。当前 PARTIAL 定位准确，不应升格。

建议：先补 screen-files 产物接线与 mobile.html 的 SW 注册/SHELL 两处小改，再用真实 390x844 + HTTPS 完成一次端到端真机走查并把证据写入 A20 与交接记录，MOBILE-001 即可据实升格。
