# integrations/ — 同一产品的卫星服务

FACAI 是一个产品，但生产环境里跑着**四个进程**。本目录收拢其中不属于主服务代码树的部分，
避免源码只存在于 VPS 单点。

| 进程 | 目录 | 端口 | 说明 |
|---|---|---|---|
| `facai-cloud` | 仓库根 `facai/`（主服务） | 127.0.0.1:8765 | 核心 API/控制台/扫描/证据链 |
| `facai-tron-sync` | 仓库根 `facai/`（同进程代码） | — | TRON 同步任务 |
| `facai-whatsapp-relay` | `bridge/whatsapp-relay/` | 本机回环 | WhatsApp 协议中继，回连主站 `FACAI_BASE_URL`，签名 `X-Facai-Signature` |
| `facai-portal` | `integrations/facai-portal/` | 127.0.0.1:9910 | 会员登录 + 额度/用量/Tokens 详情面板（BFF over New API） |
| 落地页 | `integrations/facai-landing/` | nginx 静态 | 对外落地页 |

## 与 PHP 漏洞验证能力的关系（澄清）

"验 PHP 漏洞"的能力 **在主服务代码里，不在本目录**：

- `facai/venom_agent.py` — VENOM 命名 Agent，五段链：fingerprint → 21 条暴露面并发探测 →
  phpunit RCE(仅回显固定标记) → LFI 穿越（必须读回 `root:x:0:0` / `[fonts]`）→
  `php://filter` 源码读取（必须解出 `<?php`）。
- `facai/quick_scan.py` — 侦察到 PHP 指纹（X-Powered-By / PHPSESSID / .php 入口 / WordPress /
  Laravel / Drupal / Joomla / Magento / wp-content）时自动派发 VENOM，与 STRIKE(SQLi)
  并发执行，共用同一 pinned 出口；报告里以 `agents` 编队数组呈现。
- `facai/arsenal.py` 注册 `venom` 工具；`facai/static/scan.html` 有独立 VENOM 卡片。

所以：**VENOM 属于主库**；本目录的 portal/landing 只是同一产品的售卖/入口侧，代码独立。

## 来源与部署

本目录文件从生产 VPS 实际运行目录取回（2026-10-29）：
`/opt/facai-portal`、`/var/www/facai-landing`。这些服务**不经** `deploy/deploy.sh update`
部署（该脚本只同步主服务），需单独放置并配 systemd/nginx。

## 密钥策略

以下**不入库**，由运行环境自行持有：

- `secret.key`：portal 首启自动生成，用于会话签名。丢失会使所有已登录会话失效，但不会泄露数据。
  生产位于 `/opt/facai-portal/secret.key`（随 `/etc` 备份策略单独保管）。
- `owner_ledger.json`、`.reg.lock`：运行态数据。
- `bridge/whatsapp-relay/wa-auth/`：WhatsApp 会话凭据（配对后可登录该账号，极高敏感）。
