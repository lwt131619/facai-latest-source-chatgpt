# FACAI / ICS — 当前接手入口

先读AGENTS.md → FACAI_CODEX_HANDOFF.md → docs/engineering/CORE_INVARIANTS.md。
源码、Git历史、任务登记表和实测报告优先于历史聊天及旧VPS上线描述。

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
备份：python scripts/checkpoint.py --validation pass，生成完整dist/FACAI-resume.bundle并验证远端。
ZIP是开发包，不带Git历史；bundle才是完整已提交历史。最终包重建的文档更新不会改动已测runtime/test文件，其哈希可核对。

## 下一阶段优先项

1. RUNTIME-001：先审计缺worktree时回退共享工作区的路径，再完成隔离/租约/配额/容量测试。
   当前默认2/上限8/同任务3，OpenCode默认串行；不能只改线程上限就叫20+Agent。
2. 按实际可测单位实现进度，不能随机或定时伪造0.01%；区分CLI/调度/模型并发。
3. 完整语义Compiler、独立来源信任/自动采证与校准置信度，未知保持未知。
4. Windows/macOS native、真实模型/Runtime、HTTPS跨设备及VPS部署验收。
5. Rust/Shannon/Kali/BlackArch仍按冻结边界增量接入，不替换现有Core。

完整27方向与验收矩阵继续有效；不得因范围大而删减需求或宣布Production v1.0完成。
