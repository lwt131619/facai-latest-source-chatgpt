# 原开发成果与方向交接包整合

本轮用户指令：先标清全部大方向和核心，确保任何Agent接手不偏移，并融合前面已开发的包。

## 实际整合依据

整合前对 dist/FACAI-v1.0-development.zip 中 MANIFEST 的111个文件逐一读取，
与当前真实源码SHA256比较：0缺失、0差异。原来一键启动、后端、控制台、Docker、
配置与持久化验证成果都在同一仓库；没有另造一套不相连的项目。

## 新增的交接能力

- 根AGENTS统一入口和FACAI_CODEX_HANDOFF核心概要。
- 10类核心不可变量，目标合约、源码地图、架构决定及真实兼容冲突。
- 27项有ID/依赖/状态/源码/下一步/验收的方向登记与可读对照。
- 最终用户验收矩阵、任意Agent恢复runbook、阶段checkpoint脚本。
- 原开发包扩充测试/必要vendor元数据/工程说明/Electron现有源码；运行时源代码保留。

原有历史HANDOFF文档仍保留，并注明优先读当前统一入口，避免复制旧线上断言。
未完成的Store事务钩子和Setup/Intent/Trust新模块没有混入当前包；这些是接手后的明确任务。

## 产物与验证

- FACAI-v1.0-development.zip：当前整合开发源码与说明，含逐文件MANIFEST哈希。
- FACAI-v1.0-development.zip.sha256：压缩包校验值。
- FACAI-resume.bundle：当前已提交完整Git历史，可直接clone开发分支。
- FACAI-resume.bundle.sha256：Git备份完整性校验。
- checkpoint-status.json：实际branch/HEAD/dirty/bundle/远端核验状态。

MANIFEST中的source_commit/source_tree_dirty说明构建来自哪个提交、是否有未提交文件。
正式交接前提交方向与脚本，然后重建包，使source_tree_dirty=false。
GitHub同步必须remote_verified=true，目前Git与连接器写入403，不能宣称远端已备份。

完整产品目标仍以ACCEPTANCE_MATRIX为准，开发包不得称为已完成Production v1.0。

整合包解压到临时空目录后：方向结构校验通过，原有launcher/intent/evidence/quota/startup
定向测试 **59 passed**（1条上游Starlette/httpx弃用警告）。本轮只增补交接/打包/备份，
没有改动FACAI运行时核心，不把该59项当作重新执行全部1259项回归。
