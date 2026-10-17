# 任何Agent可用的续接步骤

## 入口与事实源

1. 读根AGENTS.md与FACAI_CODEX_HANDOFF.md。
2. 读CORE_INVARIANTS.md、CORE_CONTRACTS.md、task-registry.json。
3. 读AGENT_HANDOFF.md并检查git status/log，不依赖上一段聊天。
4. 定位待做任务ID，检查依赖、source_files、acceptance与next_action。

优先级：用户最新明确要求→适用仓库指令→冻结目标→实际代码/实测状态→历史记录。
目标冻结不是实现完成。证据不足保留未知，不复制历史1278全绿/线上部署等说法。

## 正常GitHub接手

```sh
git clone --branch facai/production-build https://github.com/lwt131619/facai-latest-source-chatgpt.git
cd facai-cloud-public
git status --short
git log -5 --oneline
python -m pip install -r requirements.txt
python -m pytest -q
```

远端尚无该分支或push未成功时，不声称拉到最新；用本机新生成的完整bundle。
新环境应读自己的网络/凭据配置，不假定能访问旧Windows localhost或环境变量。

## GitHub不可写时从本机bundle续接

```sh
git bundle verify FACAI-resume.bundle
git clone --branch facai/production-build FACAI-resume.bundle facai-resumed
cd facai-resumed
git remote set-url origin https://github.com/lwt131619/facai-latest-source-chatgpt.git
```

bundle保留已提交全部历史。`scripts/checkpoint.py`在本机`dist/`生成并验证它；
bundle不再跟踪入同一Git仓库，否则每轮bundle会包含上一轮bundle并无限膨胀。
GitHub上历史旧bundle只是历史快照，不代表最新HEAD。无法联网时需把本机新bundle
作为独立文件交给下一Agent。开发ZIP是启动/交付包，不能代替Git历史。
读取随包checkpoint-status.json，核验head/dirty/bundle_verified/remote_verified。
只有dirty=false的检查点才能保证所有本次修改已在bundle中；有未提交文件先检查归属。

## 进入开发

- 先读对应模块与现有测试；选择一个可运行阶段，记录task_id和文件所有权。
- 保留兼容性，新增结构/路由做增量迁移；不要换核心来绕过困难。
- 执行相称验证；运行真实CLI/model需要真实环境，不用fake声称生产验收。
- 更新任务状态、实际结果与剩余步骤。状态定义见task-registry.json。
- 运行 `python scripts/validate_handoff.py` 检查方向/依赖/文件关系。

## 创建检查点与上传

```sh
python scripts/checkpoint.py --message 'task-id: verified change' --paths file1 file2 --validation pass
```

明确列文件，不自动提交未知工作区文件。检查凭据形状后提交，在本机生成完整bundle并验证，
尝试普通push，再ls-remote比对HEAD。exit2表示本地成功但push失败。
成功只在remote_verified=true时报告；错误时保留外部阻塞并提供bundle。
不能用force push/删库/重建/改公开性代替授权。跨工作区迁移先保留双方历史。

## 每次交接至少写这8项

当前branch/HEAD；当前task_id；完成文件；验证命令与结果；未提交变更；外部阻塞；
下一条具体操作；bundle与GitHub远端核验状态。保持task-registry与交接说明一致。

## 验收与产物

`python scripts/package.py` 整合开发包。必须包含当前方向/核心/续接说明与必要源代码，
按MANIFEST逐文件和ZIP SHA256校验。最后按ACCEPTANCE_MATRIX逐项验收，不用README截图替代运行。

任务登记以task-registry.json为单一状态源。修改后执行：
`python scripts/validate_handoff.py --render`，生成可读对照并校验依赖/源码/入口。
默认不带参数时若Markdown与JSON不同将失败，避免状态在两份说明间漂移。

新库权限和同步事实见docs/GITHUB_AGENT_ACCESS.md；github-backup.json是当前推送目标单一来源。
