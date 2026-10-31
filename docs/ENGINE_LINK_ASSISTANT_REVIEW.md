# link-assistant/agent 来源评审与 FACAI 接入映射

核对日期：2026-10-31。只读浅克隆
[`link-assistant/agent`](https://github.com/link-assistant/agent) 于
`dc8f9ead0ce98b7645cd69f1578cfc3057037f45`；仓库 `LICENSE` 为 Unlicense，
`js/package.json` 标 `@link-assistant/agent` 版本 `0.27.0`，Bun >=1.0.0。
README 称 JavaScript/Bun 实现为生产就绪，Rust 实现仍在开发；这属于上游声明，
FACAI 未做兼容或性能验收。本轮未复制其源码、未安装 Bun、未执行外部 Agent。

## 与 FACAI 产品最相关的能力

| 上游可核对的接口/行为 | FACAI 对应落点 | 接入前必须验证 |
| --- | --- | --- |
| OpenCode `run --format json` 兼容的事件流，含 `tool_use`、`step_start`、`step_finish`、`error` | `facai/opencode.py`、`execution.py`、Mission Timeline | 真实样本事件的字段、顺序、终态与错误映射；不能仅凭 README 的“兼容”断言即插即用 |
| 每次运行一次 `model_resolved`，在首次模型请求前给出实际 provider/model 与 `matchesRequest` | FACAI 模型路由 Trace、配额/预算归因 | 与 FACAI 指定模型一致时才记为匹配；不一致如实记录，不能把上游声明当作真实路由证明 |
| 宿主通过 `LINK_ASSISTANT_AGENT_CONFIG_DIR`、`..._CONFIG`、`..._CONFIG_CONTENT` 注入配置 | FACAI Model Gateway/工作区配置边界 | 不改全局 `XDG_CONFIG_HOME`；配置及凭据不进入事件、Artifact、Git 或命令行日志 |
| 会话续接默认派生新 session ID；可选择保持原 session | FACAI Mission/Execution/Trace | 分清“恢复旧会话”和“新会话继承上下文”，记录父子关系与重复执行风险 |
| 识别上游 `retry-after` 并做指数退避，README 声称可长时间重试 | FACAI Scheduler 租约、失败重试、模型池 | 上游重试必须受任务期限和 FACAI 预算控制；不可让单个进程无限占住 Worker 或掩盖失败 |
| 模型提供方与 MCP 扩展 | `gateway.py`、`registries.py`、Skill/Tool Registry | 逐提供方验证认证、实际成本和输出；MCP 工具不取得 FACAI Owner 身份 |

上游 README 顶部强调默认全自动、无沙箱；同一 README 及
`docs/permissions.md` 又明确提供**可选**的 `auto/plan/readonly/ask` 权限模式。
因此“无权限系统”不再是这个固定版本的准确描述。它也不能使底层模型在推理阶段
必然接受某项请求，FACAI 不应把模型拒绝或上游失败改写成执行成功。

## 引擎定位与下一断点

把它登记为**候选附属执行引擎**，不是替换 FACAI Core、Owner、Mission、
模型池或 Evidence/Proof 的总控。下一 Agent 若做适配，先取固定版本的无副作用
任务事件样本，比较 `facai/opencode.py` 的解析合同；再检查模型选路声明、超时、
成本/Token、会话恢复与中断重试是否可追溯。只有这些合同通过，再评估独立运行环境、
依赖安装和产品入口。来源审阅状态 `SOURCE_VERIFIED`；适配器、VPS 安装、线上任务
和多模型实测状态均为 `NOT_VERIFIED`。
