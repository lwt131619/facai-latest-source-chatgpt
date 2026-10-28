# FACAI / ICS — 当前接手入口

URL 被动体检开源选型与接入边界：docs/PASSIVE_SITE_ASSESSMENT_UPSTREAM.md。

记录源/ZCode版本与智能路由完整配置：docs/WORKSTATION_ROUTING_STORAGE.md。

7×24 VPS 通用智能工作站的现状映射与实施顺序：docs/VPS_24X7_WORKSTATION.md。

最新实测与优先级：docs/engineering/HANDOFF_2026-10-22.md。

先读AGENTS.md → FACAI_CODEX_HANDOFF.md → docs/engineering/CORE_INVARIANTS.md。
源码、Git历史、任务登记表和实测报告优先于历史聊天及旧VPS上线描述。

## Desktop本地源码模式增量

`packaging/app`有Electron本地模式，先阅读docs/LOCAL_APPLIANCE.md。
实测Linux Node启动真实FACAI、打开Owner向导、关闭服务并释放8080。
已修远程窗口相似域名前缀误判；安装配置纳入FACAI源码和静态资源，模拟安装
资源可启动真实服务，Owner数据独立保存。Python/依赖仍需系统准备，Windows/macOS
native安装包未认证，DESKTOP-001保持PARTIAL。详见docs/LOCAL_APPLIANCE.md。

## 最新接续重点：Skill正文已接运行时，效果验收仍未闭环

`8f38304`起：导入的Skill正文独立于公开元数据，按Owner/Skill ID/版本/正文SHA256
留在SQLite；同步API与调度器共用`execute_mission_task`，在授权后把匹配正文附给
OpenCode/Goose适配器的单次任务。原始用户prompt仍单独保存。旧元数据Skill没有正文，
仍按旧行为运行；正文丢失/不匹配时明确失败，不虚构调用。`6403ae3`起加载还校验版本。
`/registries/skills/{key}/usage`区分已交给运行时的次数与旧任务仅元数据归因的次数；
“已交给运行时”不证明模型遵循指令，更不等于结果事实验证。手动绑定现在冻结当时的
Skill版本与正文哈希，后续注册表更新不改已有任务。转换操作台现在仅在用户原话
以正向动词明确要求调用一个已安装且有正文的Skill ID时自动绑定并冻结版本；
否定句/仅提及/多Skill请求不会自动绑定，含糊评分匹配只展示理由，
不会改变执行任务。依赖解析、模糊自动路由的确认体验及效果指标仍待开发。
`POST /api/v1/registries/skills/{key}/update`提供版本+正文SHA预期值的原子更新：
过期提交409、旧版本正文保留、禁用状态不重置。旧`/skills/import`仍允许不带预期值
覆盖同名Skill，属于向后兼容路径，不能宣称所有更新都已有冲突保护。
治理日志已修为每次生命周期事件立即落盘，`SKILL_UPDATED`记录新旧版本和哈希；
SQLite注册表与边界日志文件仍非同一事务，跨存储崩溃一致性尚待补齐。
`GET /api/v1/registries/skills/{key}/versions`列出本Owner的历史版本/哈希/时间，
软删除后仍可审计；控制台Skill抽屉展示版本摘要，不回显正文。
旧的`docs/validation/skill-attribution-20261016.json`只代表上述提交之前的基线。

## 恢复交付增量

backup.sh / backup.bat 提供本地数据库备份与隔离恢复演练，不修改在线数据。
必须读docs/LOCAL_RECOVERY.md；不能把DATABASE_ONLY误报为完整密钥/工作区恢复。
UPDATE-001保持PARTIAL，Windows/macOS/VPS仍需实测。

## 2026-10-16 已收录外部交接并续接

已快进集成03434f4及docs/handoff-20261016/四份报告；历史报告保持原样。
本轮Mobile补齐产物列表/文本预览/下载、独立PWA、真实执行分配和账户额度摘要。
后续Agent先读docs/ACTIVE_DEVELOPMENT.md及docs/validation/mobile-increment.json
（生成后），不要把下方历史测试数当成本轮测试结果。
MOBILE-001仍PARTIAL；真实手机HTTPS、Provider规模验收及VPS部署未完成。

## 仓库与防丢失

主库：https://github.com/lwt131619/facai-latest-source-chatgpt
默认main；本地工作分支facai/production-build；origin/latest均指向新主库。
原facai-cloud-public及其他项目未修改。当前源目录仍名/workspace/facai-cloud-public。
用户要求每小步骤明确文件commit+普通push origin HEAD:main；WIP可用，但必须如实标验证。
不强推、不删库、不改公开性。官方集成已多次真实推送；曾间歇性缺凭据，随后恢复。
遇到错误先保留提交与bundle，不把一次失败当作源码丢失，也不假报上传成功。
checkpoint最终必须读回main和工作分支SHA；已被其他通道同步时，读回一致也可证明备份存在。
其他Agent仍需自己的GitHub授权；不要传播密码/PAT或把密钥写进交接包。

## 已新增的可操作路径

- /setup：新本机Owner一次性初始化、哈希密码持久化、单次登录恢复、真实系统检测、模型加密配置与显式连接探测。
- /workspace：原话/不可变修订/补充信息/取消/幂等确认；确认不grant、不enqueue；独立Owner授权保留。
- Mission Specification：与确认同事务，保存目标、约束、原话、修订、实际步骤ID、Trace与SHA。
- /evidence-center：Owner隔离证据/Claim、人工审查、四维冲突、保留历史的撤回、不可变Proof ZIP、独立Owner验收。
- /knowledge：显式收录当前已验收快照，保持INFERENCE等分类与来源，隔离检索并标注历史来源。
- /health最小存活检查，/status和/metrics受Operator鉴权；旧API/账单/渠道/模型/运行能力保留。

重要边界：Owner审查不是独立来源认证；事件记录不是外部工具签名证明；哈希只证明字节。
Legacy /proof.verified旧含义不变。恢复密钥仅重置登录，不授予Runtime权限。
任务预算默认仍0，向导不会假充值或绕过执行预算。

## 最新实测（不是Production验收）

完整源码ZIP从零解压：1319 passed / 13 skipped / 0失败，228.014秒，启用真实Chromium。
原Git工作树全量：1318 passed / 12 skipped。最终校验错误防凭据回显等修正已包含在ZIP全量测试中。
桌面1440×1000/手机390×844覆盖Owner初始化、登录、配置、Intent、证据、审查、Proof、验收、Knowledge及刷新。
本机start.py：新Owner初始化、哈希持久化、关闭/重启同一会话与Intent历史恢复通过。
Docker：UID10001、内部健康/鉴权、同会话跨重启、Mission规格/证据恢复、Proof实际文件SHA通过。
当前云命名空间无法直接验证Docker宿主映射端口；没有真实Provider调用、VPS部署或Windows/macOS正式包验收。
机器可读证据：docs/validation/package-acceptance.json、production-increment.json。

## 立即续接命令

```sh
git status --short
git remote -v
git log --oneline -8
python -m pip install -r requirements.txt
python scripts/validate_handoff.py
python -m pytest -q
```

当前云依赖/workspace/facai-venv；可选安装playwright并设置FACAI_TEST_CHROMIUM=/usr/bin/chromium运行真实浏览器。
复验：python scripts/smoke_appliance.py；构建Docker镜像后python scripts/smoke_docker.py。
交付：python scripts/package.py；python scripts/verify_package.py --full-tests --chromium /usr/bin/chromium。
备份：python scripts/checkpoint.py --validation pass，在本机生成完整dist/FACAI-resume.bundle
并验证远端。bundle不再跟踪入自身Git历史；下一Agent联网时直接克隆最新远端分支。
ZIP是开发包，不带Git历史；bundle才是完整已提交历史。最终包重建的文档更新不会改动已测runtime/test文件，其哈希可核对。

## 下一阶段优先项

1. RUNTIME-001：先审计缺worktree时回退共享工作区的路径，再完成隔离/租约/配额/容量测试。
   当前默认2/上限8/同任务3，OpenCode默认串行；不能只改线程上限就叫20+Agent。
2. 按实际可测单位实现进度，不能随机或定时伪造0.01%；区分CLI/调度/模型并发。
3. 完整语义Compiler、独立来源信任/自动采证与校准置信度，未知保持未知。
4. Windows/macOS native、真实模型/Runtime、HTTPS跨设备及VPS部署验收。
5. Rust/Shannon/Kali/BlackArch仍按冻结边界增量接入，不替换现有Core。

完整27方向与验收矩阵继续有效；不得因范围大而删减需求或宣布Production v1.0完成。

## GitHub CI真实nginx补充修复

CI暴露非root nginx -t使用系统缓存目录的问题；已将验证wrapper的日志/临时目录隔离到mktemp，保留真实HTTP/TLS解析。
实际nginx1.30.6以UID10001、默认及自定义域名HTTP/TLS全部通过；复验python scripts/smoke_nginx.py（需要Git checkout和Docker）。
详情docs/validation/nginx-validation.json。生产nginx模板没有改动，VPS没有执行部署。
此后变化仅Nginx验证脚本/QA脚本及交接文档；应用和业务测试源码保持与已全量解压验收的包一致。
最新CI实际结果读GitHub Actions，不根据本地跳过的nginx路径宣布远端通过。

## 2026-10-18 续接检查点

## 2026-10-28 单站点被动体检检查点

最终接手路线与双域名部署风险：`docs/SITE_ASSESSMENT_NEXT_AGENT.md`。
后续同轮增量：`/site-assessment` 认领/验证/报告页面、跨进程每 Owner 每分钟
一次运行限额，独立新域名 Nginx/证书安装脚本（尚未在 VPS 执行）。
本增量定向 47 passed；随后已重跑全量。
本增量全量已重跑：`1488 passed / 7 skipped / 0 failed`（真实 Chromium，
2026-10-28，223.19 秒）。跳过原因仍为 deploy-now.py 不在树 5、真实模型密钥
未配置 1、vendor authority manifests 缺失 1；新域名脚本只做 `bash -n` 和源码
校验，尚未在 VPS 上运行。`openingcode.top` 未进行实站检查，不能写成已检出漏洞。

新增 Owner 会话保护的站点认领、HTTPS 验证文件、单次首页响应配置观察及持久报告。
实现见 `facai/site_assessment.py`、`facai/site_assessment_routes.py`；部署验收见
`docs/SITE_ASSESSMENT_DEPLOY.md`。定向身份/安全回归 47 passed；完整工作树回归
`1488 passed / 7 skipped / 0 failed`（真实 Chromium，2026-10-28），跳过项是
deploy-now.py 不在树中 5、真实模型密钥未配置 1、vendor authority manifests 缺失 1。
真实域名、VPS 部署和公开产品验收未执行。下一步是控制台交互页、报告 Artifact/Proof
绑定、公开预览领取、限流与生产域名验收；不要把当前首页配置观察描述成 PHP 漏洞扫描完成。

下一 Agent 接手命令：`git clone https://github.com/lwt131619/facai-latest-source-chatgpt.git`
（已有克隆则 `git fetch origin main` 并核对 SHA）；先读 `AGENTS.md`、
`FACAI_CODEX_HANDOFF.md`、本文件、`docs/engineering/REQUIREMENTS_TRACEABILITY.md`、
`docs/SITE_ASSESSMENT_DEPLOY.md`。检查 `git status --short`，不能用旧 ZIP 覆盖主线。
先做站点体检 UI 与 Owner 领取闭环；测试跨 Owner、控制权失效、报告证据和重启恢复；
然后在已授权真实域名做上线验收。每个可运行断点更新注册表/交接、运行相关测试、
用 `scripts/checkpoint.py` 提交推送并核对 `remote_verified`。VPS 当前代码版本未知，
请先核实再按 `docs/VPS_24X7_WORKSTATION.md` 备份、部署和回滚；不得把 GitHub 同步
写成 VPS 已升级。

最新代码已推送 `main` 至 `b4f88dc`。本轮补齐 Skill 正文导入后的来源版本历史、CAS 更新、按 Owner 隔离的版本列表及控制台展示；手动绑定和用户原话明确正向调用唯一已安装 Skill 时冻结版本，并为否定、仅提及、多 Skill 语句保留不自动绑定边界。详情及剩余缺口以 `docs/engineering/REQUIREMENTS_TRACEABILITY.md` 的 SKILL-001 为准。

控制台早期点击竞态已修复：`go`、回车、能力库入口在异步启动请求前绑定。真实 Chromium 用人为延迟的 session 请求验证能力库仍可打开，控制台浏览器文件 14 passed。全量回归 `1438 passed / 7 skipped / 0 failed`（246.13 秒，`FACAI_TEST_CHROMIUM=/usr/bin/chromium .venv/bin/python -m pytest -q -rs`）；跳过项为 deploy-now 不在树中、真实模型密钥未配置、vendor authority manifests 本机缺失。此结果不表示 Production 验收通过。

`dist/FACAI-resume.bundle` 不再纳入 Git 跟踪，以避免 bundle 内包自身造成指数增长；`scripts/checkpoint.py` 本地生成和校验 bundle，远端 Git 分支保留完整已提交历史。下一 Agent 先 `git fetch origin`、核对 `main` HEAD，再按注册表继续开发，勿用旧 ZIP 覆盖新源码。

后续 Skill 审查增量 `aad17c0`：`/api/v1/registries/skills/{key}/source` 要求精确 version + SHA256、按 Owner 查阅已存正文（含软删历史）、响应 `Cache-Control: no-store`；控制台以 `textContent` 呈现，避免把指令当 HTML 执行。Skill 生命周期与控制台完整两组测试 `55 passed`（真实 Chromium）；全量回归 `1439 passed / 7 skipped / 0 failed`（207.90 秒，真实 Chromium，跳过项同上一段）。该接口只取回指令正文，不还原历史 frontmatter 元数据；回滚仍未实现，不能把此功能标为回滚。

2026-10-19 增量：受控恢复旧 Skill 指令正文，要求旧版版本+SHA 和当前版本+SHA，单个 SQLite 事务内发布到未用过的新版本；Owner 隔离、软删拒绝、版本冲突拒绝、旧正文保留。控制台有确认操作，历史 frontmatter 未存档，故沿用当前描述/治理状态，不宣称完整元数据回滚。相关 Skill 与真实 Chromium 控制台测试 `57 passed`；全量测试 `1441 passed / 7 skipped / 0 failed`（210.91 秒，真实 Chromium，跳过项同上）。

2026-10-20 治理修复：默认 Agent/Model/Runtime 的软删墓碑不能在应用重启后被默认种子覆盖。`Registries.list` 改用 SQLite `INSERT OR IGNORE` 只补真正缺失的默认项，再读取实际存活行；显式重新注册仍可恢复。测试覆盖默认模型跨重启保持删除、另一 Owner 独立拥有默认项、显式恢复，相关三组 `152 passed`。全量结果待本轮记录。

同日全量回归首次暴露路由默认项顺序改变（`test_conversion_console` 的 `conversion-primary` 期望失败）；修复后保持默认 Agent 优先于新增 Agent 的既有迭代顺序，墓碑依旧不复活。相关转换、默认资源、Skill 三组 `89 passed`，随后需重跑全量，不能把首次红灯记成通过。

第二轮全量重跑在 `aec7fe0` 工作树完成：`1442 passed / 7 skipped / 0 failed`（203.32 秒，真实 Chromium）；跳过项仍为未部署的 deploy-now、未配置真实模型密钥、缺失的 vendor authority manifests。`task-registry.json` 把该结果标为 Git 工作树验证，保留旧 ZIP 解压验证为历史记录；当前新包需单独校验，不伪称从零解压全量复验。

新 ZIP 由 `585bd8d` 源码快照构建（436 文件，SHA256 见 `dist/FACAI-v1.0-development.zip.sha256`），逐文件哈希核对后从零解压完整测试：`1441 passed / 8 skipped / 0 failed`（219.65 秒，真实 Chromium）。比 Git 工作树多跳过 1 项，仅因解压树无 `.git`，Git executable-bit 测试不适用；该差异记在 `dist/package-validation.xml`。这是开发包的可复验结果，不是 A01–A26 生产终验。

2026-10-21 MODEL-001 增量：模型注册可由管理员配置多 `api_key_envs`，网关遇某 Key 上游 429 或 401/403 立即尝试下一把并短期冷却；现有单 Key 与租户自有模型路径保留，租户不能引用站长环境变量。相关网关/模型/注册回归 `65 passed`，mock upstream 不等于真实 Provider 验收。详情与缺口见 `docs/MODEL_KEY_POOL.md`。

本轮 Git 工作树全量回归在 `df7deb9` 完成：`1447 passed / 7 skipped / 0 failed`（215.04 秒，真实 Chromium）；缺真实模型密钥的 live probe 等跳过项仍保留。新开发包需另做从零解压验证，不能把工作树测试数字写成包内结果。

从零解压的首轮包测试出现 1 项浏览器时序失败：`test_parallel_running_nodes_show_real_progress_and_reduced_motion` 在任意卡片出现后立即读取尚未渲染的运行连线。测试已改为等待目标并行节点和运行连线，原有进度/动效断言未改变；定向 Chromium 1 passed。此前失败的包不得称为通过，需重建并重跑最终包。

## 2026-10-23 ZCode 接手检查点（基于 08097ae）

- SKILL-001 ⑥ 依赖字段：metadata.dependencies 形状校验（≤20 条、字符串或
  {name,version}）；路由期存在性检查（依赖被禁用视为缺失）；assign_stage
  返回 excluded_skills（排除原因随行，不静默、不自动安装），随
  ASSIGNMENT_RECORDED 事件落库。
- MODEL-001 每Key计量：结局细分计数 + 上游自报 tokens + key_env 归因；
  可选显式单价计算 estimated_cost；GET /api/v1/modelmatrix/key-usage 仅
  admin/Owner。密钥值零暴露。
- 验证状态：本地全量 1455 passed / 9 skipped（⑥ 检查点，无 Chromium；
  CI 以远端为准），计量增量定向 29 passed，提交前再跑一次全量。
- 接手过程：fd5232a 快进至 08097ae；本地 99 行 SKILL-001 ⑤⑥ WIP 经比对，
  ⑤与上游 CAS update 端点方案重复已弃，⑥为登记册 next_action 缺口故重放；
  WIP 全文存 facai-workspace/agent-reports/wip-skill-dep-fd5232a.patch。
- RUNTIME-001 A10 Agent 级计量：agent_progress() 派发归因 × 事件结局 ×
  jobs 运行态，与 Trace 重放同源；GET /api/v1/fleet/agent-progress 仅
  admin/Owner。上一检查点 43b9aeb 已远端 CI 绿。
- A26 闭环结构 e2e（tests/test_acceptance_closed_loop.py）：同一使命同一
  Trace 全链 + 重启恢复 + 全局事件流逐字节重放一致；受控 runner，A26 仍
  NOT_VERIFIED。控制台抽屉新增「车队计量」「每Key计量」两块（字段钉死 +
  真 Chromium 实开）。77055f0 起主线 CI 绿。
- MODEL-001 跨进程共享冷却：`model_key_cooldowns` SQLite 表 + 每 app 自持
  `KeyRotation(store)`（内存快路径 + 后端诚实合并）；CLI 统一接线与真实
  Provider 验收仍缺。
- TRUST-001：DISPUTED 人工裁决出口（trust_adjudications + adjudicate，
  冲突对裁决后不再强制 DISPUTED，验收闸不放松）+ 跨 Mission 冲突检测
  （cross_mission_conflicts + GET /api/v1/trust/conflicts）。
- 打包（835e7da 源）：development ZIP 439 文件、manifest/哈希核验通过；从解压
  副本定向复测 gateway/trust/closed-loop 47 通过（记录在
  dist/package-validation.json）。full-tests 以 CI 为准。
- VPS 实机（2026-10-23）：SSH 恢复（id_ed25519 → root@77.221.148.118）；清理
  僵尸 crontab×2（指向已删的 /opt/facai-dev，备份在 /root/crontab-backup-20261023.bak）、
  /opt/facai-panel-src(51MB)、坏 facai-staging、facai-ops、/tmp 旧包×3；staging 按
  SHA 578a3c5 重建，deploy.sh update 部署完成；本机 8765 与公网 whstapp.lol health
  均 200 ready（auth_required=true），facai-cloud/tron-sync/whatsapp-relay 全 active。
  服务实际端口 8765（runbook 的 8080 已过时）。
- ENGINES-001 首块：ARTEX 0.3.12 收录为附属引擎（facai/artex.py HTTP 适配
  器，一步一任务+终态映射；Owner 指令权限模型：本地/Owner 最高，ARTEX 内
  置审批门整体旁路——规则禁用留档+拦截工具面清空+judge 不配；详见
  docs/ENGINES_ARTEX.md；真实实例部署待做）。

## 2026-10-28 免登录快速体检（quick scan）上线（ZCode 窗口）

- Owner 死命令：免登录贴 URL → 最快速度产出真实有效的多栈风险报告，能力对标
  开源注入面检测器，授权开发直达 VPS 部署发布。
- 新增 `facai/quick_scan.py`（引擎）+ `facai/quick_scan_routes.py`（公开路由）+
  `facai/static/scan.html`（免登录页 `/scan`）。复用 site_assessment 的 SSRF 防线
  （公网地址 pinning、仅 HTTPS、字节预算），单轮预算=1 次 TLS 握手 + 8 次只读
  GET：根、/.git/HEAD、/.env、/robots.txt、金丝雀反射探针、SQL 错误回显探针、
  /admin、/phpmyadmin。分析器管线：传输/证书、安全响应头、Cookie 旗标、被动
  技术栈指纹（INFERENCE，不声称 CVE）、敏感暴露（FACT）、注入面（无害单发
  载荷回显判定，对标 sqlmap error-based 回显与反射 XSS 首层）。全部 finding
  带 evidence/tags/nuclei 风格分级与修复建议，报告含 evidence_sha256。
- 滥用防线：每 IP 60s 一次 + 每主机 600s 冷却（失败也冷却，防反复敲不可达
  目标）+ 并发信号量 4 + 同主机 10 分钟内返回缓存报告（免烧配额秒回）。
  `/scan` 与 `/api/v1/quick-scans*` 注册进 `is_public_path`（无会话凭证，
  无 CSRF 面；滥用由服务内限流闸约束）。
- 测试 `tests/test_quick_scan.py`（7 用例：分析器分级、指纹 INFERENCE 语义、
  免登录全流程、缓存语义、按 IP 限流、危险 URL 拒绝、不可达目标冷却）。
  全量回归结果见本轮 checkpoint 记录。
- VPS 部署：/opt/facai-staging git pull → `deploy/deploy.sh update`（自动备份
  + rsync 受管路径 + 重启 facai-cloud），公网 whstapp.lol/scan 验收；结果以
  dist/checkpoint-status.json 与实测 curl 为准，未验证不写已完成。
