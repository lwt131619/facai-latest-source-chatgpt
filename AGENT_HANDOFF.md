# FACAI / ICS — 从这里断点续接

先读根 `AGENTS.md`。方向标注已完成；本轮已实际新增Intent预览/修订/确认/独立授权。Setup/Trust仍未实现。
27项任务见 `docs/engineering/task-registry.json`；具体下一步见 DEVELOPMENT_PLAN.md。

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

## 本轮整合断点

- 原开发ZIP111个文件逐一对照当前源码，0缺失、0内容冲突。
- 核心源码沿用上次实测版本；没有把中途未完成Store事务改动混入包。
- 新增统一AGENTS入口、冻结核心/合约/架构决定/27方向/验收/续接runbook和结构校验脚本。
- 开发包扩充测试/必要vendor元数据/工程说明/桌面壳源码，保留启动、Docker与运行数据语义。
- 验证：validate_handoff、ZIP逐文件/哈希、从bundle克隆后入口与任务结构验证。
- 后续先做MISSION-001原子事务与独立确认基础，再接SETUP/INTENT/TRUST，不能直接签授权。
- 当前Git与连接器写入均403；本地bundle为完整已提交断点，真实状态见随附checkpoint-status.json。
- 整合ZIP解压后的方向校验及定向测试：59 passed；全部文件哈希已核验。
- 更新任务后执行 `python scripts/validate_handoff.py --render`，再默认校验，避免可读任务表漂移。

## 最新实际功能进展：Intent工作区

- 新增 `/workspace`，主控制台和手机页有入口。
- `intent_workspace.py`保存原话与不可变修订；确认事务创建一份Mission，默认不grant/不排队。
- `intent_routes.py`复用现有认证/CSRF/Owner，授权为独立Owner动作，控制台仍负责绑定与派发。
- 新增6项测试含并发幂等、回滚清理、预算402、跨账号隔离、确认无副作用。
- 真实Chromium桌面/手机完整操作与刷新恢复通过，无JS错误；QA配置了明确测试额度，未绕生产预算。
- 预览使用保守规则和人工修订，confidence=null；不是完整LLM语义编译器，不假报所有Intent需求完成。
- 下一步：SETUP-001/完整MissionSpec/Evidence；GitHub仍需核验，checkpoint已读取GH_TOKEN临时凭据。
- 本功能最终完整回归：1265 passed / 28 skipped / 1上游警告（90.75秒）；原桌面与手机布局2项真实浏览器验收通过。
- 最新发布结论与缺口：docs/RELEASE_STATUS.md；完整Production仍未完成。

## 用户指定新增备份仓库

用户要求新建“最新facai源代码”并上传完整源代码。GitHub不接受中文仓库名，目标为
`lwt131619/facai-latest-source`，描述“最新 FACAI 源代码”，默认私密。
已准备 `python scripts/publish_latest_repo.py`：实际POST建库→普通push到新库main→核对SHA，
凭据仅在进程内。当前建库接口Forbidden，不能标已创建/已同步。旧仓库和历史不删除、不强推。
该脚本处理已存在的同名私库，不改变可见性，也不覆盖非快进的远端main。
