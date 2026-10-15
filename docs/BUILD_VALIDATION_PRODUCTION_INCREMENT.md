# 本轮工程交付验证

权威机器记录：docs/validation/package-acceptance.json、production-increment.json。

- 完整解压源码包398文件，逐文件SHA与ZIP SHA核对；实际运行全套测试：1319通过/13跳过，0失败。
- 包不带.git，一个依赖checkout上下文的额外跳过是预期；嵌套Git隔离测试已改为自建父仓库，实际测试更准确。
- 所有新代码在解压测试前已提交；最终补充交接文档后重建包，逐项核对runtime/test/config哈希不变。
- 测试包含真实Chromium；Runtime完成记录使用明确合成夹具，不是付费模型或外部CLI成果。
- Native与Docker检查脚本已入Git，可重新执行；Owner密钥/会话仅在临时数据/内存中使用，不入代码与报告。
- 本机映射Docker端口曾超时，容器内部服务与重启验证通过；没有把此限制写成对外上线成功。
- 发现并修正：校验错误可能反射秘密、产物读接口泄漏内部路径、Proof JSON尾换行导致文件SHA不一致、
  手动UI测试与后台调度竞争、测试错误依赖源码目录带Git元数据。

最终判断仍是DEVELOPMENT_NOT_PRODUCTION。完整范围/剩余项见RELEASE_STATUS.md。

## 远端CI发现并补齐的真实nginx路径

GitHub Ubuntu runner安装了nginx，本机未安装，首次CI触发可选真实nginx校验并失败。
以官方nginx镜像/UID10001复现：默认client_temp缓存目录权限不足。
验证wrapper已将各类缓存/日志隔离至临时目录，自定义域名证书路径也只替换为临时测试证书。
真实nginx1.30.6的默认/自定义域名HTTP与TLS均通过，未跳过校验、未更改生产模板或服务器。
源码SHA及实测在docs/validation/nginx-validation.json。已测应用/测试文件保持不变，额外QA变更独立记录。
