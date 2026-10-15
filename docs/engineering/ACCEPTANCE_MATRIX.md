# FACAI Production v1.0 最终验收矩阵

这份表定义怎么证明完成，不是已通过报告。每次执行记录：commit、平台、命令/操作、
输入、实际输出、日志/截图/产物位置、失败原因。模拟适配器与真实模型环境分别记录。

| 编号 | 用户/系统验收 | 必要证据 | 当前状态 |
|---|---|---|---|
| A01 | 解压启动，浏览器打开8080，已有数据不丢 | Linux首次/重启与Docker启动实测；Windows/Mac另测 | PARTIAL |
| A02 | 首次向导真实系统检测、Owner设置、控制/恢复密钥 | 初始化前后鉴权、bootstrap失效、重启不再初始化 | NOT_IMPLEMENTED |
| A03 | OpenAI/DeepSeek/Ollama/Custom模型连接 | 自有配置真实连接；缺Key/额度/网络明确区分 | PARTIAL |
| A04 | 原话→action/target/purpose/scope/constraint理解 | 原话逐字保存，缺上下文不假定 | PARTIAL |
| A05 | Preview/修改理解/补信息/取消 | 修订不可变，Owner隔离，确认显示当前版本 | NOT_VERIFIED |
| A06 | 确认产生一个Mission且不立即执行 | 重复/并发确认幂等；旧revision拒绝；无grant/jobs副作用 | PARTIAL |
| A07 | 独立授权后再能力/预算/配额/Runtime检查 | 拒绝、撤销、过期、跨租户、重启授权一致 | PARTIAL |
| A08 | 规划DAG、Agent/Skill/Tool分配可查看 | 真正任务/依赖/选路原因，来源于实际记录 | PARTIAL |
| A09 | 20+Agent状态与协作执行 | 独立任务/同任务/CLI/模型并发分别实测，非20张假卡片 | NOT_VERIFIED |
| A10 | 总/阶段/Agent实时进度到0.01% | 可测基数/单位/事件一致，不可测显示未知 | NOT_VERIFIED |
| A11 | 工具调用、等待、重试、失败实时可见 | Timeline/SSE重连重放、真实开始结束/错误/成本 | PARTIAL |
| A12 | Key A耗尽切B、Provider失败转移 | 受控429/配额/冷却测试+真实Provider配置验收分开 | PARTIAL |
| A13 | Skill安装更新禁用移除审查与效果记录 | manifest版本/依赖/历史；禁用后新任务不再调用 | PARTIAL |
| A14 | Evidence/Claim/来源/验证/置信度 | 实际收集记录与FACT/INFERENCE/UNKNOWN/DISPUTED依据 | PARTIAL |
| A15 | 时间/范围/定义/环境冲突归类 | 四类冲突的双方陈述/来源/解释与待审查路径 | PARTIAL |
| A16 | Artifact/Metadata/Evidence/Verification/Trace导出 | 哈希可重算，按任务/Owner隔离且不含秘密 | PARTIAL |
| A17 | Owner接受证明快照 | 接受与语义验证/执行成功分离，变更不复用旧接受 | PARTIAL |
| A18 | 结果保存为可追溯知识资产 | 知识关联证据版本/Trace，未知不自动成为事实 | PARTIAL |
| A19 | 历史、事件、任务重启恢复 | 原生密码保留、Docker同一session跨重启已实测；任务恢复另验 | PARTIAL |
| A20 | 桌面/手机全流程操作 | 1440x900和390x844真实浏览器；HTTPS跨设备及通知/设置 | NOT_VERIFIED |
| A21 | Desktop本地软件可操作后端 | 本地生命周期/端口冲突/缺环境与native安装包实测 | PARTIAL |
| A22 | 更新/迁移失败可回滚 | 更新前备份、故障注入、恢复原数据/代码，跨平台脚本 | PARTIAL |
| A23 | health/status/metrics真实 | Backend/DB/Storage/Runtime/Model分开，不假报ready | PARTIAL |
| A24 | 新Agent随时续接且GitHub备份一致 | 接手runbook实操，bundle verify，push后远端SHA相等 | VERIFIED_LOCAL |
| A25 | 包与本次源码一致，配置秘密不入包 | 明确allowlist、每文件manifest和ZIP SHA256，实际解压启动 | PARTIAL |
| A26 | 总体验从自然语言到可验证结果闭环 | 同一任务完整Trace、真实Agent执行、独立验证、产物/知识与恢复 | NOT_VERIFIED |

## 已有实测的范围

完整回归1259通过/28跳过；Linux原生启动和重启密码保留；Docker非root登录及同一session
跨重启恢复；原开发包111文件与源码逐文件一致。详见 BUILD_VALIDATION_2026-10-15.md。
这些不等于表中所有PARTIAL均已通过，也不证明Windows/Mac或真实20模型并发。

## 最终发布判定

所有必需项有实际证据并解决阻塞后，才把开发包升级为Production v1.0。
用户更改验收范围可修订本表并留下决定；Agent不能自行删除困难项以缩小产品。

## Trust v1增量验证

新功能范围、限制与API见docs/TRUST_WORKFLOW.md。完整回归1282通过/28跳过，另2项真实桌面/手机浏览器通过。A05/A06/A15/A17已有独立路径与对应本地测试；真实Provider/Runtime验收仍保留。A24官方GitHub集成已恢复并真实push核对SHA。
