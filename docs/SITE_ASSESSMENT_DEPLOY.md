# 单站点 HTTPS 配置体检：上线操作

本功能只观察站点首页一次 HTTPS 响应的 HSTS、CSP、nosniff 配置；不判断 PHP 源码漏洞，
不爬取网站、不登录、不提交表单。每次运行先重新验证站点控制权。报告保存在 FACAI
现有 SQLite 数据库，按登录 Owner 隔离，含采集时间、来源 URL、HTTP 状态、规则结果和
响应元数据摘要。完整报告仅已登录的认领者可读；当前没有公开预览、下载 ZIP、可视化页面。

## 部署和验收

如果 VPS 现有 `whstapp.lol` 已在服务，不能把 `FACAI_DOMAIN` 改成新域名后直接
运行旧 `deploy.sh`：它的 Nginx 模板固定旧域名。先在 VPS 更新代码并备份，确认
`opencodemarketing.top` 和 `www` 的权威 A 记录都指向 VPS，随后在部署源码目录运行：

```bash
sudo FACAI_EMAIL=你的证书通知邮箱 FACAI_SERVER_IP=77.221.148.118 bash deploy/add-marketing-domain.sh
```

脚本单独安装新域名 Nginx 站点并签发证书，保留旧站点；DNS 未一致时停止。
当前执行环境没有 VPS 通道，**此命令尚未在 VPS 上运行**；上线时先核实真实公网 IP、
服务状态和备份。之后分别验收新旧两个域名的 HTTPS、登录、任务流和健康检查。

1. 按仓库 `README.md` / `docs/VPS_24X7_WORKSTATION.md` 启动现有 FACAI 服务；
   不需要新容器或新数据库。生产环境保持已有 HTTPS、账号登录与 CSRF 配置。
   登录后打开 `/site-assessment` 可在页面完成认领、验证、运行、查看报告。
2. 登录 FACAI 控制台取得会话及 `X-CSRF-Token`。调用
   `POST /api/v1/site-assessments`，JSON 为 `{"url":"https://example.org/"}`。
3. 把响应的 `challenge_value` 原样放在该域名的
   `/.well-known/facai-site-verification.txt`，确保公开 HTTPS GET 返回 200，
   内容只有该值。不要在聊天、日志或仓库公开该值。
4. 调用 `POST /api/v1/site-assessments/{id}/verify`，再调用
   `POST /api/v1/site-assessments/{id}/run`。用
   `GET /api/v1/site-assessments/{id}` 读取持久报告。请求必须保留同一登录会话；
   写请求带 CSRF 头。API key 不能冒充交互式 Owner 完成这些操作。
5. 若域名解析到私网/本机地址、HTTPS 证书不匹配、验证文件失效或站点不可达，
   系统拒绝检查。上线前在你控制的域名上走一遍，并从另一个账号确认报告返回 404。

运行限制：同一 Owner 每分钟最多一次检查，多个服务进程共用 SQLite 限额。
当前范围是小规模受控早期功能，**不能宣传为“检测所有 PHP 漏洞”**。开放公众
“输入任意 URL”入口、自动批量爬取和完整风险评分需要后续单独实施与验收。
