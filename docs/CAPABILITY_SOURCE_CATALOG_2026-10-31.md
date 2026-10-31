# FACAI 能力来源情报批次：2026-10-31

本批次只读核对上游仓库与许可证；**候选不是已安装的 Skill，也没有被 FACAI
运行时调用**。来源仓库的 README 与许可证在所列 commit 检查；没有复制代码到产品。
GitHub API 受本执行环境代理阻断，因此以 Git 浅克隆实证为准，最新发行版和依赖
供应链仍待单独核对。下次更新先核对新 commit，再做差异审查。

| 来源与固定 commit | 上游事实 | FACAI 接入判断 | 当前状态 |
| --- | --- | --- | --- |
| [Agent Skills](https://github.com/agentskills/agentskills) `69ef37e` | `LICENSE` Apache-2.0；README 定义 `SKILL.md`、`scripts/`、`references/`、`assets/` 与渐进披露 | 格式/校验依据，映射 FACAI `skills.py` 与注册表；它本身不是运行时 Skill | SOURCE_VERIFIED，未导入 |
| [OSV-Scanner](https://github.com/google/osv-scanner) `f3b468c` | `LICENSE` Apache-2.0；README 明确支持多语言依赖与 OSV 漏洞数据，含 PHP/Composer | 用户提供的源码、lockfile 或 SBOM 的离线/受控组件比对候选；结果需保留数据库更新时间、匹配版本与 advisory ID | SOURCE_VERIFIED，未安装、未连接 |
| [Trivy](https://github.com/aquasecurity/trivy) `e2c05f7` | `LICENSE` Apache-2.0；README 列出容器、文件系统、Git、虚拟机、Kubernetes 目标，以及依赖、配置、secret、许可证扫描类型 | 用户提供的镜像、源码或 SBOM 资产评估候选；与 OSV 重复结果需以 package/version/advisory 归一去重 | SOURCE_VERIFIED，未安装、未连接 |

这些是不同层：Agent Skills 是**技能格式**，OSV-Scanner 与 Trivy 是**工具候选**，
不能将工具仓库直接标为可调度 Agent。实际接入先确定数据入口、输出 schema、
资源预算、版本锁定、错误语义和证据链，再由 FACAI 权限与运行时调度。

## 最短集成顺序

1. 接受用户上传的 lockfile/SBOM，保存原件 SHA256、文件类型、来源和解析状态。
2. 先接一个组件漏洞数据库适配器，以离线/固定测试样本验证版本匹配与误报；
   再判断是否需要第二个工具做覆盖补充。不得把 CVE 匹配写成已证实可利用。
3. 统一发现项：资产 ID、包坐标、已安装版本、advisory ID、数据源版本、
   证据哈希、置信度、修复建议。与 FACAI Trust/Proof 关联。
4. 用同一份样本比较覆盖、耗时和重复项；通过验收后才更新路由权重和能力状态。

## 后续红队 Skill 收录

收到 Skill 原件后按 `docs/SKILL_INTAKE_TEMPLATE.md` 逐项登记。先固定来源与
哈希，检查许可证、依赖、权限需求和真实测试证据；未审查的条目保持
`CANDIDATE`，不因名称或自述自动取得执行权限。用户原始用途与授权范围
保存在 Mission 中，Skill 元数据不能改变 Owner 指令。
