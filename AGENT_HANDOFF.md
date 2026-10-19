# FACAI / ICS — 当前接手入口

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

最新代码已推送 `main` 至 `b4f88dc`。本轮补齐 Skill 正文导入后的来源版本历史、CAS 更新、按 Owner 隔离的版本列表及控制台展示；手动绑定和用户原话明确正向调用唯一已安装 Skill 时冻结版本，并为否定、仅提及、多 Skill 语句保留不自动绑定边界。详情及剩余缺口以 `docs/engineering/REQUIREMENTS_TRACEABILITY.md` 的 SKILL-001 为准。

控制台早期点击竞态已修复：`go`、回车、能力库入口在异步启动请求前绑定。真实 Chromium 用人为延迟的 session 请求验证能力库仍可打开，控制台浏览器文件 14 passed。全量回归 `1438 passed / 7 skipped / 0 failed`（246.13 秒，`FACAI_TEST_CHROMIUM=/usr/bin/chromium .venv/bin/python -m pytest -q -rs`）；跳过项为 deploy-now 不在树中、真实模型密钥未配置、vendor authority manifests 本机缺失。此结果不表示 Production 验收通过。

`dist/FACAI-resume.bundle` 不再纳入 Git 跟踪，以避免 bundle 内包自身造成指数增长；`scripts/checkpoint.py` 本地生成和校验 bundle，远端 Git 分支保留完整已提交历史。下一 Agent 先 `git fetch origin`、核对 `main` HEAD，再按注册表继续开发，勿用旧 ZIP 覆盖新源码。

后续 Skill 审查增量 `aad17c0`：`/api/v1/registries/skills/{key}/source` 要求精确 version + SHA256、按 Owner 查阅已存正文（含软删历史）、响应 `Cache-Control: no-store`；控制台以 `textContent` 呈现，避免把指令当 HTML 执行。Skill 生命周期与控制台完整两组测试 `55 passed`（真实 Chromium）；全量回归 `1439 passed / 7 skipped / 0 failed`（207.90 秒，真实 Chromium，跳过项同上一段）。该接口只取回指令正文，不还原历史 frontmatter 元数据；回滚仍未实现，不能把此功能标为回滚。

2026-10-19 增量：受控恢复旧 Skill 指令正文，要求旧版版本+SHA 和当前版本+SHA，单个 SQLite 事务内发布到未用过的新版本；Owner 隔离、软删拒绝、版本冲突拒绝、旧正文保留。控制台有确认操作，历史 frontmatter 未存档，故沿用当前描述/治理状态，不宣称完整元数据回滚。相关 Skill 与真实 Chromium 控制台测试 `57 passed`；全量测试 `1441 passed / 7 skipped / 0 failed`（210.91 秒，真实 Chromium，跳过项同上）。
