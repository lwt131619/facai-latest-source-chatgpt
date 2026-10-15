# GitHub 与下一 Agent 接入

主库：https://github.com/lwt131619/facai-latest-source-chatgpt

官方 GitHub App 授权后，原生 Git 推送已真实恢复，main 已包含源码和本轮开发。
此前403/远端只有README的记录是历史状态，不能继续据此阻止开发。

```sh
git clone https://github.com/lwt131619/facai-latest-source-chatgpt.git
cd facai-latest-source-chatgpt
git switch main
git pull --ff-only origin main
```

按 AGENTS.md 和 AGENT_HANDOFF.md 续接。公开读取无需凭据；写入要求当前Agent自己的GitHub授权。
GitHub账号层权限不等于所有会话都会继承权限，不能让用户在聊天中传密码或PAT共享授权。
本次官方集成无需把令牌嵌入remote URL。checkpoint优先继承Git凭据。

用户授权小步骤WIP备份：明确文件提交，git push origin HEAD:main，核对远端SHA。
不强推、不改可见性、不删除或重建仓库，不修改其他项目。
checkpoint另外验证工作分支，dist/checkpoint-status.json仅在远端SHA匹配时标remote_verified。
若环境写入失败，保存完整Git bundle并报告真实状态；不要声称源码丢失或已上传。

GitHub集成不提供VPS登录。VPS仍需独立SSH授权和当前网络允许，历史配置见docs/DEPLOYMENT.md。
