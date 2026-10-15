# FACAI / ICS — 当前可续接断点

先读 AGENTS.md → FACAI_CODEX_HANDOFF.md → docs/engineering/CORE_INVARIANTS.md。
实际 Git 历史、源码和测试优先于聊天或历史线上描述。

## GitHub 与工作目录

- 唯一交接备份库：https://github.com/lwt131619/facai-latest-source-chatgpt
- 公开 main 已包含完整源码及本轮开发；官方 GitHub 集成推送已经恢复。
- origin 已改为上述新库；latest 同样指向新库。旧 facai-cloud-public 没有改动。
- 本地 `/workspace/facai-cloud-public`，工作分支 `facai/production-build`。
- 用户要求每小步骤明确文件 commit + 普通 push origin HEAD:main；WIP可用，必须如实标验证。
- 不强推、不删库、不改可见性。其他 Agent 需各自授权 GitHub 集成。
- checkpoint 默认先使用继承的 Git 认证，失败才考虑 GH_TOKEN 内存临时回退；不持久化凭据。

## 本轮实际实现

1. 原 /workspace Intent 预览、原话不可变修订、幂等确认、独立授权保留。
2. Mission Specification 在确认事务内持久化：目标/约束/原话/修订/真实步骤ID/Trace/SHA。
3. Trust v1：证据、规范化断言、Owner会话审查、上下文冲突、不可变Proof、受控ZIP导出。
4. /evidence-center 已连入桌面、手机和理解工作区。
5. 自动化API Key不能提交Owner审查或验收；所有Trust数据严格按实际Owner隔离。
6. 未执行/缺每步执行证据/未知或争议Claim不能验收；验收绑定快照，后续事件/证据/产物字节变化使其过期。

重要界限：Owner人工审查不是外部来源认证；事件引用只是数据库记录；哈希只验证字节。
Legacy /proof.verified 的原含义保持不变。不能将本功能叫作完整TruthEngine或Production完成。

## 真实验证

- 完整回归：1282 passed / 28 skipped / 1上游弃用警告，114.65秒。
- Trust/Intent/legacy Proof定向：27 passed。
- 实际 Chromium桌面1440×1000/手机390×844：2 passed；登录→Intent→确认→录证→审查→ZIP下载校验→刷新恢复，无JS错误、无横向溢出。
- 浏览器与单元测试使用合成测试数据，没有真实Provider/工具项目分析，也未连接VPS部署。
- 本轮WIP及后续进展实时入口：docs/ACTIVE_DEVELOPMENT.md。

## 立即续接

```sh
git status --short
git remote -v
git log --oneline -8
python -m pip install -r requirements.txt
python scripts/validate_handoff.py
python -m pytest -q
```

当前云环境Python依赖位于 `/workspace/facai-venv`。
可选浏览器验收：安装playwright后设置 FACAI_TEST_CHROMIUM=/usr/bin/chromium，运行 tests/test_trust_browser.py。

下一步按任务登记表推进首次Setup/模型连接、Runtime真实多Agent容量、自动可信采证及知识联动。
不得把默认并发2/上限8/OpenCode串行写成已支持20模型并发，不用随机小数伪装进度。
Rust/Shannon/Kali/BlackArch仍是增量方向，不替换现有Core。

## 备份与恢复

python scripts/checkpoint.py --validation pass 生成并校验完整dist/FACAI-resume.bundle，并普通推送main和工作分支后比对SHA。
python scripts/package.py 重建开发包及文件哈希，仍标DEVELOPMENT_NOT_PRODUCTION。
完整Git历史在bundle，开发ZIP不是Git历史。源码/文档/测试均进Git，产物不含配置秘密。

VPS历史信息在docs/DEPLOYMENT.md，不代表本轮验证或部署成功。不要照历史记录宣布已上线。
