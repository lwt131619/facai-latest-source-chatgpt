# VPS 部署 Runbook（SSH 一到手就能执行）

日期：2026-10-16。目标：把 `lwt131619/facai-latest-source-chatgpt` 的 `main`（当前 HEAD `6830132`）部署到 77.221.148.118 的 `/opt/facai-cloud`，域名 whstapp.lol。
依据：`docs/DEPLOYMENT.md`（权威文档）+ `deploy/deploy.sh`（幂等脚本，自带备份/回滚/自拷贝拒绝）。

## 0. 待用户填的开放问题（唯一阻塞）

| # | 问题 | 填写 |
|---|------|------|
| 1 | SSH 用户名和端口？（root 还是 sudo 用户？22 还是自定义？） | ______ |
| 2 | 我的公钥怎么给进去：用户贴进 authorized_keys，还是给我一次性密码自己装？ | ______ |
| 3 | VPS 上 `/opt/facai-cloud` 现在跑的是什么版本？（`deploy.sh status` 或 `cat data/…` 可查） | ______ |
| 4 | VPS 能否直连 GitHub 拉这个仓库？（能→走 A 路线；不能→走 B 路线） | ______ |

我的公钥已生成：`C:\Users\user\.zcode\cli\memories\…\~/.ssh/facai_vps_takeover.pub`
（实际路径 `~/.ssh/facai_vps_takeover.pub`，ed25519，注释 `zcode-facai-vps-takeover`）。

## 1. 预检（在本地，不碰 VPS）

- [ ] `git ls-remote origin refs/heads/main` == 本地 HEAD（今天已验证 6830132）
- [ ] DNS 闸门：`for ns in ns1.dnsowl.com ns2.dnsowl.com ns3.dnsowl.com; do echo "@$ns apex: $(dig +short @$ns whstapp.lol A)"; done` —— 每行都必须是 77.221.148.118（文档记录 apex 曾部分未同步；未全同步就**不宣布上线**，但**不妨碍部署**）
- [ ] 确认不改公开性、不强推、不删仓库（纪律不变）

## 2. 部署（在 VPS 上）

A 路线（VPS 能拉 GitHub）：
```bash
ssh <user>@77.221.148.118
cd /opt/facai-staging   # 或按文档先 git clone 到 staging；deploy.sh 拒绝在 /opt/facai-cloud 自身运行
git remote -v && git fetch origin && git checkout <FULL-SHA-6830132>   # 按 SHA 固定，绝不 --force
sudo /opt/facai-staging/deploy/deploy.sh update
```

B 路线（VPS 拉不动 GitHub，从本地推 bundle）：
```bash
# 本地：
git bundle create facai-6830132.bundle 6830132
scp facai-6830132.bundle <user>@77.221.148.118:/tmp/
# VPS：
git clone /tmp/facai-6830132.bundle /opt/facai-staging || (cd /opt/facai-staging && git fetch /tmp/facai-6830132.bundle main && git checkout FETCH_HEAD)
sudo /opt/facai-staging/deploy/deploy.sh update
```

脚本自身保证（不需要我操心）：scoped rsync 只替换受管代码路径，data/用户文件永不触碰；每次变更前自动打时间戳备份到 `/var/backups/facai-cloud`；secrets 只在 `/etc/facai-cloud/facai.env` 和 `providers/*.env`（0640 root:facai），不进代码/日志/Git。

## 3. 部署后验证（按顺序，任何一步失败就 rollback）

```bash
sudo /opt/facai-staging/deploy/deploy.sh status     # loopback health 期望 200 {"status":"ready","auth_required":true}
curl -s http://127.0.0.1:8080/api/v1/health         # 或 status 输出的端口
# 公网链路（本地 Windows 也行）：
$env:FACAI_ADMIN_PASSWORD='...'; .\deploy\check_public_access.ps1 -Domain whstapp.lol -ServerIp 77.221.148.118
```
- [ ] health 200 且 auth_required=true
- [ ] 浏览器打开 https://whstapp.lol 控制台正常
- [ ] admin 登录成功；建一个冒烟任务（配额允许时，跑 scripts/smoke_local.py 同款路径）
- [ ] 事件流水出现（MISSION_CREATED 起链）

## 4. 回滚（不靠强推）

```bash
sudo /opt/facai-staging/deploy/deploy.sh rollback [archive.tar.gz]   # 或不带参数选最近备份
sudo /opt/facai-staging/deploy/deploy.sh status
```

## 阻塞点

只有一条：**用户没给 SSH 通道**（用户名/端口/密钥安装方式）。其余全部就绪。
