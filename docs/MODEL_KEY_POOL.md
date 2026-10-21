# 单模型多 Key 环境引用（开发增量）

管理员可在模型注册信息里设置 `api_key_envs`，值为 1–16 个互不重复的环境变量名；实际 Key 只在运行环境中提供，不写进注册表、日志或交付包。原有 `api_key_env` 单 Key 配置保持兼容。租户不能在共享模型注册接口指定这两个字段；自有模型仍走 `/api/v1/my/models` 的加密存储路径。

示例注册信息（由管理员提交到 `PUT /api/v1/registries/models/example`）：

```json
{
  "name": "Example",
  "provider": "OpenAI-compatible",
  "endpoint": "https://api.example.com/v1",
  "model": "example-model",
  "api_key_envs": ["FACAI_EXAMPLE_KEY_A", "FACAI_EXAMPLE_KEY_B"]
}
```

网关只挑选已设置的环境变量，按调用轮换；某 Key 收到上游 429 后冷却 10 秒，收到 401/403 后冷却 60 秒，并在该次请求内尝试下一把。全部不可用时明确返回认证失败或限流结果，不伪造成功；模型拒绝、工具错误与网络超时不触发 Key 轮换。冷却状态在服务进程内存中，重启后重置。

这一步完成网关请求的多 Key 故障转移，不等于全平台 Key 级配额池：CLI Agent 自己发出的上游请求、跨进程共享冷却、每 Key 配额与成本统计仍需单独接线与验收。真实 Provider 调用也仍待外部凭据和额度支持。
