# 目标聚焦与取证缺口（2026-11-01）

## Owner 原话的工程化表达

> 以 Owner 已确认的目标、范围、约束和验收标准为唯一任务边界。先选择能最快产出可验证结果的直接路径，执行后立即用真实产物/证据核对。不要因为模型联想而自动扩大目标、追加工具或启动无关子任务。直接路径受阻时，先记录阻塞事实、影响与已尝试路径，再选择一个仍在原范围内、成本较低且可验证的替代路径；每次分流都必须能回到原任务。若替代路径也无法取得证据，标记 UNKNOWN/未完成，并向 Owner 明确说明缺口，不补造结果。

这里的“发散”更准确地说是**受约束的替代路径探索**。它由实际阻塞触发，不能成为默认工作模式。是否授权执行仍由现有 Owner Control Plane/Runtime 决定；任务理解本身不授予权限。

已确认的新 Mission 将该执行方法单独写入不可变 Mission Specification 的
`execution_policy`，并写入专业提示。共享执行入口还会在每次派发前补入该方法，
覆盖手工提示和直接入队路径；旧 Mission 无规格或规格中尚无该策略时保持原有提示行为。
`OPENCODE_STARTED` 记录实际传给适配器的提示 SHA-256 与是否应用此规则。
这些记录只证明指令被交给运行时，不证明模型遵循或任务已完成；真正阻塞仍须
由运行时/Owner 明确记录取证缺口。

## 来源不能自动采集时

资料站点可能通过 `robots.txt`、访问控制、登录、限流或网络故障阻止自动采集。不能把 URL、搜索摘要、一次失败请求或模型记忆当成已取得原文证据，也不能通过伪造身份、绕过登录或规避访问限制补齐空白。

现有 `POST /api/v1/missions/{id}/trust/collection-gaps` 接受来源说明、阻塞原因、下一步和备注，在当前 Mission 的 SQLite 事务中留痕；`GET .../trust` 返回 `collection_gaps`，Proof 快照与 ZIP 的 `collection_gaps.json` 保留该缺口。它不是 `trust_evidence`，不能充当 Claim 的证据。记录缺口不会自动访问目标，也不证明目标内容的真实性。
Owner 也可在 `/evidence-center` 的“取证受阻记录”表单提交并查看这些记录。

可选下一步仅为 `OWNER_UPLOAD`（Owner提供原件）、`OFFICIAL_API`（有权限的正式接口）、`HUMAN_REVIEW`（人工核对）、`NONE`（暂无可用路径）。取得材料后再通过现有证据入口提交来源、内容和校验；只有实际导入的证据才能关联 Claim。当前实现仅记录缺口和下一步，**不自动执行**上传/API/人工工作流。

Owner 提交材料为现有 `OWNER_NOTE` 证据后，可通过
`POST .../trust/collection-gaps/{gap_id}/links` 或 Evidence Center 的“关联后续证据”
按钮显式连接缺口和证据。链接自身追加记录、进入 Proof ZIP 的
`collection_gap_links.json`，并使旧 Proof 成为历史快照；原缺口不删除。
链接状态是 `EVIDENCE_SUBMITTED_NOT_VERIFIED`，不等于外部来源已验证，
也不会自动改变 Claim 的 UNKNOWN/FACT 分类。
