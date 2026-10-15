# 新主仓库与所有Agent的接手权限

用户指定：**lwt131619/facai-latest-source-chatgpt**。
当前GitHub实际可见性为public。匿名git读取已成功，任何Agent可以读取该库实际已有代码。

**注意：当前完整本地代码推送尚返回403；远端目前只有初始化README，不能假称已有最新开发成果。**
当前状态以dist/checkpoint-status.json的head/remote_verified为准。

## 不同权限的实际含义

| 用途 | 必要授权 | 说明 |
|---|---|---|
| 读取公开代码 | 无凭据 | 公开仓库可clone，不代表本地尚未推送的代码也在远端 |
| 提交/推送 | 当前Agent身份对该库Contents读写 | App/PAT仓库范围必须包含新库；旧令牌不一定自动包含 |
| 提交Actions工作流 | Workflows读写 | 本工程有.github/workflows文件 |
| 读取元数据 | Metadata只读 | 令牌通常自动具备 |
| 修改库权限或邀请账号 | GitHub Owner/Administration权限 | 当前会话无可调用的管理权限设置工具，不假报已修改 |

公开仓库没有“任何匿名身份都可以直接写入”的设置。
其他Agent使用自己的GitHub连接/当前环境Secret，必须也有该库对应写权限。
不要把Owner账号密码/PAT写进代码、GitURL配置或交接包来共享权限。

## Agent接手

远端同步核验成功后：

```sh
git clone --branch facai/production-build https://github.com/lwt131619/facai-latest-source-chatgpt.git
cd facai-latest-source-chatgpt
```

先读AGENTS.md→FACAI_CODEX_HANDOFF.md→AGENT_HANDOFF.md→任务登记表。
推送所需GH_TOKEN由各自运行环境私密注入，或使用该Agent已配置的有效Git凭据。

当前仓库目标由github-backup.json固定，checkpoint同时普通推送main与工作分支，逐个核对SHA。
新库初始化提交通过正常合并保留，旧源仓库和全部开发历史保留，不force push。
本地目录名字仍可为facai-cloud-public；目录名不决定推送目标。

## 当前验证结果

- 当前连接账号lwt131619；新库存在、公开，匿名读取成功。
- Git直接推送、进程GH_TOKEN推送均403；连接器建分支返回Resource not accessible by integration。
- 元数据permissions.push=true描述账号层权限，不证明当前令牌/App具备写入scope。
- 当前会话没有管理其他Agent身份/邀请权限的入口，不能擅自授予未指定账号管理权。
- 如果远端尚未有开发分支，用FACAI-resume.bundle克隆完整本地检查点继续工作。
