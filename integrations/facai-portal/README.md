# facai-portal — 会员详情面板

生产实例：VPS `/opt/facai-portal`，systemd `facai-portal.service`，`uvicorn app:app`
监听 `127.0.0.1:9910`（nginx 反代对外）。

## 它做什么

- **登录**：账号/密码经上游 New API `POST /api/user/login` 校验，成功后发服务端会话 +
  HttpOnly Cookie（`facai_portal`）。
- **详情面板**：`/portal/api/{me,usage,tokens,logs}` 只读聚合 New API 真源库。
- **注册/验证码**：`add_register.py` / `add_captcha.py` 是管理侧脚本，用于补注册流程与
  验证码校验；`owner_ledger.json` 记录 Owner 额度台账（运行态，不入库）。
- 对外售卖的是 `openingcode.top/v1` 上的 `Facai-DS` / `Facai-168` 模型额度。

## 安全边界（代码自身声明，改动时不要放松）

- 数据库以 `mode=ro` 打开，只读。
- 不下发 `users.password` / `access_token`。
- `tokens.key` 仅在本人点击"显示"时经鉴权接口单条返回，不复用/回显上游 `API_KEY`。
- 会话签名密钥 `secret.key` 首启生成，不入库。

## 本地跑

```sh
pip install fastapi uvicorn
# 需要的环境（见 app.py 顶部常量）：上游 New API 地址、数据库路径
uvicorn app:app --host 127.0.0.1 --port 9910
```

## 部署

`facai-portal.service` 为本目录附带的生产 systemd 单元，复制到
`/etc/systemd/system/` 后 `systemctl daemon-reload && systemctl enable --now facai-portal`。
**注意**：`deploy/deploy.sh update` 不会同步本目录，需单独 rsync 到 `/opt/facai-portal`，
且不要覆盖目标机的 `secret.key` 与 `owner_ledger.json`。
