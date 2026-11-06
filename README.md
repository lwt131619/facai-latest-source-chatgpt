# FACAI / ICS

FACAI 是以 Owner 控制为核心的任务平台：保留用户原话，先预览理解与确认任务，
再由独立的授权与运行时执行，记录事件、证据、产物和 Trace。现有 Python/FastAPI、
SQLite、Web/手机控制台及本地启动入口持续可用。**本仓库仍在开发中，尚未通过
Production v1.0 全项验收。**

## 快速开始

本地安装需 Python 3.10+。Windows 双击 `start.bat`；Linux/macOS 运行：

```sh
./start.sh
```

打开 `http://127.0.0.1:8080`，首次使用按 `/setup` 初始化 Owner。
登录后直接进入 [FACAI 操作台](http://127.0.0.1:8080/command-center)；
同一服务的手机入口是 `http://127.0.0.1:8080/static/mobile.html`。
本地 `127.0.0.1` 只在运行 FACAI 的那台电脑上有效；其他设备需要按部署说明
配置可访问的 HTTPS 域名，不能把开发容器的地址当成公网入口。
容器和远程部署分别见 [本地交付](docs/LOCAL_APPLIANCE.md) 与
[部署说明](docs/DEPLOYMENT.md)。启动脚本不会替你配置真实模型、外部渠道或 VPS。

## 从哪里读起

| 目的 | 当前入口 |
| --- | --- |
| 新 Agent 接手开发 | [AGENTS.md](AGENTS.md) → [当前断点](AGENT_HANDOFF.md) |
| 产品方向与不可变量 | [接管协议](FACAI_CODEX_HANDOFF.md) · [核心不可变量](docs/engineering/CORE_INVARIANTS.md) |
| 已实现/未完成/验收 | [任务对照](docs/engineering/REQUIREMENTS_TRACEABILITY.md) · [验收矩阵](docs/engineering/ACCEPTANCE_MATRIX.md) |
| 寻找源码和文档 | [仓库地图](docs/engineering/REPOSITORY_MAP.md) · [文档导航](docs/INDEX.md) |
| 最新提交与同步 | `git log -1 --oneline` · `git status --short` · [同步说明](docs/GITHUB_AGENT_ACCESS.md) |

`facai/` 是服务、运行时及静态控制台；`tests/` 是回归测试；`deploy/` 是部署配置；
`packaging/` 是桌面打包源码；`docs/` 是规范、记录与验收；`vendor/` 是候选来源材料，
不代表已安装或可用。`integrations/` 和 `bridge/` 是独立的外围集成。

## 验证与交接

```sh
python -m pip install -r requirements.txt
python scripts/validate_handoff.py
python -m pytest -q
```

测试总数不等于真实 Provider、跨平台安装包或 VPS 验收；逐项结论看验收矩阵。
`dist/` 中跟踪的 ZIP、bundle、JSON 是**历史快照**，不可当作当前 HEAD 的构建物。
如需当前开发包，先在干净检出的源码运行 `python scripts/package.py` 并核对其
`MANIFEST.json` 的 `source_commit`，再运行
`python scripts/verify_package.py --require-current-head`；旧包会明确报
`matches_current_checkout: false` 并退出 2。需要完整历史时直接克隆本仓库。
每次成功的 [GitHub Actions 构建](https://github.com/lwt131619/facai-latest-source-chatgpt/actions/workflows/production-build.yml)
会上传与该次提交对应的 `FACAI-development-package`；下载时核对工作流提交 SHA。
数据库恢复见 [本地恢复](docs/LOCAL_RECOVERY.md)，只覆盖数据库，不包含密钥与工作区。

早期详细说明已保留在 [历史 README](docs/archive/README_pre_2026-11-01.md)，
其中部署日期和测试数是当时记录，不代表当前线上状态。
