# FACAI / ICS — 从这里断点续接

本地分支：`facai/production-build`。先读 FACAI_CODEX_HANDOFF.md，再读本文件与
DEVELOPMENT_PLAN.md。以 `git log`、实际源码和复跑测试为准；历史文档的线上部署
断言并不表示本次完成。不要覆盖 `/workspace/123` 的早期预览或用它替代真实 ICS。

## 立即接手

```sh
git status --short
git log -5 --oneline
python -m pip install -r requirements.txt
python -m pytest -q
```

上一完整基线：1259 passed / 28 skipped（bc6ee5a）。本轮新增功能必须单独验证。
已实现：真实仓库审计、原生一键启动、Docker、开发包、测试隔离修复。
待补：首次设置向导、结构化 Intent→确认→授权→执行、Evidence/Proof、并发与全流程验收。
默认调度并发2、上限8、同任务并发3；OpenCode默认串行，不得把调度线程数叫模型并发。

## 每次可运行阶段必须备份

修改后运行相关测试，再执行（路径必须显式列出）：

```sh
python scripts/checkpoint.py --message 'Describe verified change' --paths path/to/file --validation pass
```

该命令提交本地代码、生成并验证完整 `dist/FACAI-resume.bundle`，尝试普通 push，
再通过 `ls-remote` 比对提交。**只有 remote_verified=true 才表示 GitHub 已同步**。
状态文件 `dist/checkpoint-status.json` / `.git/facai-checkpoint.json` 不含凭据。
退出2表示本地提交和 bundle 成功、远端同步失败；不要伪称已上传。
不要使用 force push；不要删除/重建远端库、改公开性或改其他项目。

当前远端权限曾返回403，当前远端main与本地原快照历史也发生变化。保留双方历史，
使用独立开发分支。不得把这当作强推授权。无需读取/打印任何密钥来排错。

## 无 GitHub 写权限仍可完整接手

下载 `FACAI-resume.bundle` 后：

```sh
git clone -b facai/production-build FACAI-resume.bundle facai-resumed
cd facai-resumed
git remote set-url origin https://github.com/lwt131619/facai-cloud-public.git
```

bundle 包含所有已提交历史；未提交文件不在其中，故先检查 dirty=false。
`dist/FACAI-v1.0-development.zip` 是可运行开发包，不代替完整 Git 历史备份。
