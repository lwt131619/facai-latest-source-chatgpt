# FACAI Skill / 工具候选收录模板

每个外部 Skill 单独复制此表。原件保留在受控来源；不要把私钥、Token、Cookie、
客户数据或整套第三方仓库直接写入 FACAI 主库。

| 字段 | 填写内容 |
| --- | --- |
| 候选 ID / 名称 |  |
| 类型 | Agent Skill / CLI 工具 / 数据源 / 模型适配器 |
| 来源仓库与文件路径 |  |
| 固定 commit / tag / 原件 SHA256 |  |
| 许可证及来源文件 |  |
| 维护状态与最后核对日期 |  |
| 实际能力与适用技术栈 |  |
| 输入、输出、证据字段 |  |
| 依赖与最低运行环境 |  |
| 网络、文件、命令和凭据权限需求 |  |
| Owner 授权与目标范围 |  |
| 时间、并发、资源预算 |  |
| 失败/超时/部分完成语义 |  |
| 固定样本测试结果和误报记录 |  |
| 审查人、日期、结论 |  |
| FACAI 状态 | CANDIDATE / REVIEWED / INSTALLED_DISABLED / ENABLED / RETIRED |

收录完成仅代表可追溯。`ENABLED` 还需通过 FACAI 现有注册表版本校验、Owner
控制面、依赖检查与定向回归；运行结果是否可信由 Evidence/Proof 单独判定。

2026-10-31 起，`SKILL.md` frontmatter 可带 `source_commit: <完整40位Git SHA>`。
导入响应和注册表均保留 `document_sha256`（整份提交文本的 UTF-8 SHA256），
与已有 `body_sha256`（正文哈希）并列；缺少 commit 的本地原件仍可导入，
但来源提交状态应如实标为未固定。此字段用于核对原件，不证明来源内容可信或
Skill 已按说明执行。`source_commit` 是提交者声明的来源版本；FACAI 当前只校验
其 SHA 形状，尚不自动比对远端仓库中对应文件。
