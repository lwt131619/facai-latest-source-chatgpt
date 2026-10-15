# FACAI / ICS — 所有开发 Agent 的统一入口

适用于整个仓库。用户当前明确指令优先于本文件。此项目已经有实际 ICS 能力，
接手目标是完成生产产品，不是重新定义产品或做聊天演示。

## 必须先读

1. `FACAI_CODEX_HANDOFF.md`：产品定位与冻结核心。
2. `docs/engineering/CORE_INVARIANTS.md`：不可改变的边界及代码约束。
3. `AGENT_HANDOFF.md`：实际提交、同步状态和续接命令。
4. `DEVELOPMENT_PLAN.md`：阶段顺序。
5. `docs/engineering/REQUIREMENTS_TRACEABILITY.md` 与 `task-registry.json`：逐项范围与验收。
6. 修改模块前查 `docs/engineering/REPOSITORY_MAP.md` 和 `CORE_CONTRACTS.md`。

不要把历史 HANDOFF-NEXT.md / docs/HANDOFF_NOW.md 的线上断言当作本次验证结果。
不要把设计标注 Frozen 理解为实现完成，不要把模块存在理解为全流程可用。

## 开发规则

- 源码优先；先定位、再修改；保留已有有效 API、数据、渠道、计费、模型及运行时。
- 不把产品缩成单 Agent/聊天壳；不靠假进度、假来源、假执行补齐界面。
- 理解、确认、授权、执行、验证、Owner验收必须分开；原始请求不可被改写覆盖。
- 保持 Owner > Runtime > Agent/Model/Skill/Plugin，扩展不取得 Owner 身份。
- 变更采用兼容迁移，不一次性换数据库/框架/语言/核心。Rust是增量目标，当前Python服务须继续工作。
- 允许自主完成已授权的兼容实现、测试和提交；不必为日常实现选择反复询问。
- 若与冻结核心发生真实冲突，先记录冲突、影响和兼容选项；不能擅自删除/弱化核心。
- 并行协作先分配文件所有权；共享工作区不能覆盖他人改动；提交前检查完整 diff。
- 密钥不入源码、文档、日志、测试报告、Git远程URL或交接包。已有聊天令牌不复制进仓库。

## 每个可运行断点

用户最新防丢失要求：每完成一个小步骤立即提交明确文件并推送新主库 main。
允许 WIP 和尚未全绿的测试，但提交说明及 docs/ACTIVE_DEVELOPMENT.md 必须标明验证状态。
收到用量警告先保存提交并推送，再继续；重要代码/文档/配置不能只留在临时目录。
目前 origin 已指向 facai-latest-source-chatgpt，推送前仍需检查目标。

1. 更新 task-registry.json / AGENT_HANDOFF.md：完成、待办、验证范围、阻塞、下一步。
2. 运行与修改相称的真实验证；完整版本验收要执行接受矩阵。
3. `python scripts/checkpoint.py --message '...' --paths <明确文件列表> --validation <状态>`。
4. 核验 `dist/checkpoint-status.json` 的 HEAD 与 remote_verified；失败不能写成已上传。
5. 即使push失败，也必须保留已提交Git历史与经验证的 `dist/FACAI-resume.bundle`。

不得强推、删除仓库、重建仓库、改变公开性或改其他项目来修复同步。
“实时备份”按可运行检查点提交并尝试推送，不承诺无人运行的后台服务会持续执行。

## 完成标准

只有 `docs/engineering/ACCEPTANCE_MATRIX.md` 的用户全流程验收通过，才可标记
FACAI Production v1.0 完成。外部环境阻塞必须如实保留，不能删掉该需求来宣布完成。
