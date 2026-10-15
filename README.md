# Facai Cloud · Mission Control

A live Mission Control for isolated missions: the selected mission's real overall progress,
current stage and pipeline are on the first screen, while mission creation and model
registration live in collapsible secondary sections. This is a new repository; no prior
Facai project was modified.

## 开发接手与方向冻结

任何 Agent 先读 [AGENTS.md](AGENTS.md) → [核心不可变量](docs/engineering/CORE_INVARIANTS.md) → [断点交接](AGENT_HANDOFF.md)。
全部方向、当前状态、源码位置、下一步和验收详见 [27项任务对照](docs/engineering/REQUIREMENTS_TRACEABILITY.md)。
开发ZIP整合现有启动/后端/控制台、测试与方向说明；完整Git历史另见 `FACAI-resume.bundle`。
GitHub上传只有远端SHA核对一致才算成功，当前阻塞状态见检查点状态文件。

## 本地一键启动（新增）

Windows：双击 `start.bat`；macOS/Linux：`./start.sh`。首次需要 Python 3.10+ 和网络。
默认打开 http://127.0.0.1:8080，保留既有数据库和调度器。
登录凭据、Docker 用法和当前交付限制见 [本地交付说明](docs/LOCAL_APPLIANCE.md)。

## Run

```powershell
python -m pip install -r requirements.txt
python -m uvicorn facai.api:app --host 127.0.0.1 --port 8765
```

Open http://127.0.0.1:8765. Runtime data is kept in `data/` (or `FACAI_DATA_DIR`). The server binds to localhost by default.

For a remote installation, set a strong `FACAI_ADMIN_PASSWORD` or configure up to five users with `FACAI_USERS_JSON` in the protected service environment file. Serve the app through HTTPS. Browsers log in at `/login` and receive an `HttpOnly`, `SameSite=Strict` session cookie with CSRF protection; scripts may use HTTP Basic with their own account. Never expose the app over plaintext HTTP. Use `deploy/` and `docs/DEPLOYMENT.md` for the reverse proxy, TLS and verification gate.

## Implemented

- Mission-specific workspace, event timeline, agent sessions, artifacts, and workflow steps, stored in SQLite.
- Independent mission progress calculated from that mission's steps, with event polling and live Server-Sent Events.
- Separate agent, model, skill, and runtime registries; each mission binds all four independently (binding one never clears another), and bindings are never shared between missions.
- OpenCode adapter discovers the local binary, prepares a mission-specific workspace, and runs a task inside exactly that workspace. Stdout/stderr are captured as mission events and artifacts, and the run's real status (completed/failed/timeout) is recorded without fabricating success.
- Each successful run makes a real Git commit in the mission workspace (checkpoint) and records tokens and upstream cost reported by the model, visible per mission.
- A durable SQLite scheduler queues jobs with priority and leases, runs them with a bounded concurrency, and supports pause/resume/cancel/retry. Queued and running jobs survive process restart and expired leases are recovered.
- Model gateway for configured OpenAI-compatible endpoints and Anthropic Claude Messages API. Outcomes are attributed to the upstream as completed, refused, timeout, rate-limited, authentication failure, transport failure, tool failure, resource unavailable, or a generic upstream failure, with the raw upstream body retained; a model refusal is never represented as a Facai refusal. Task events and the console show the same provider/model/runtime/skill, cost and token fields. Credentials are referenced by environment-variable name and stored per provider, never in the model registry.
- Mission Control console: the selected mission's real overall progress, current stage and live stage pipeline are shown on the first screen (desktop and phone); mission creation, model registration and the other registry controls sit in collapsible secondary sections. Deep-link a mission with `?mission=<id>`; the selection is restored after login. Bind agent/model/skill/runtime independently, preserve the original input verbatim alongside a separately stored professional prompt whose every revision is logged with the original it came from, drive real step transitions, run OpenCode tasks synchronously or through the queue, and inspect per-stage events, artifacts (with content), checkpoints, the latest code diff, attribution and cost on PC or mobile.
- Session authentication for public use: login form, `HttpOnly` `SameSite=Strict` session cookie, hashed tokens, CSRF protection, per-address login throttling and logout. Up to five configured users have isolated Missions, linked messages and mission costs; registries and channel integrations remain instance-wide. HTTP Basic remains available for scripts and health checks.
- Production deployment plane under `deploy/`: nginx HTTPS reverse proxy with SSE support, hardened systemd unit, certificate-aware two-phase install (HTTP ACME bootstrap first, TLS only once the cert exists), scoped non-destructive code sync with pre-deploy backup and rollback, and a DNS/TLS/HTTP verification gate that checks the authoritative NameSilo servers, not just a cached resolver. Credentials are classified by service: app login/runtime config in `/etc/facai-cloud/facai.env` and each model credential in its own `/etc/facai-cloud/providers/<service>.env` (both `0640 root:facai`, loaded by the unit); `install`/`update` seed and migrate them atomically, with a config backup first. See `docs/DEPLOYMENT.md`.
- Message channel adapters (`facai/channels/`): one shared `InboundMessage`/`OutboundReply` contract and a secret-free health/config view. **Implemented:** Telegram text webhooks and WhatsApp Cloud webhooks feed the same inbound pipeline — the original text is preserved verbatim, converted into a professional prompt, turned into a mission, then dispatched to the durable queue — and repeat deliveries are idempotent by platform message id (a duplicate WhatsApp or Telegram delivery reuses the same mission and job, and a delivery deferred because no valid binding existed can be retried after one is registered). Personal WeChat uses iLink QR authorization plus a background long-poll loop that enters that same pipeline once the account is authorized. Outbound send exists for WhatsApp Cloud, Telegram `sendMessage` and WeChat text. **Needs configuration:** real send/receive only works with the user's own platform credentials or authorization (Meta WhatsApp tokens, a Telegram bot token plus webhook secret, or an actual WeChat QR scan); until then each channel reports `UNCONFIGURED` and returns 503 rather than pretending a connection. **Not implemented:** Telegram edits/media/callback queries and WeChat media/files/voice/group chat. Messages are audited in SQLite and shown in Mission Control. See `docs/CHANNELS.md`.
- n8n automation integration (`facai/n8n.py`): mission/stage events are forwarded to a configured n8n webhook as signed JSON (HMAC-SHA256 over `<timestamp>.<body>`, sent with `X-Facai-Timestamp`/`X-Facai-Signature`/`X-Facai-Request-Id`), and a signed callback endpoint applies n8n updates to mission events and steps with an idempotent `request_id`. Credentials come from the classified provider env file `N8N_WEBHOOK_URL`/`N8N_SIGNING_SECRET`; the webhook URL is treated as a secret and never returned or logged. Without both values the relay is a no-op and the integration reports `CONFIG_REQUIRED`. Retryable upstream failures are retried with real attribution, and the console shows the honest config state and recent delivery results. See `docs/N8N.md`.
- Customer-owned persistent channel bindings (`/api/v1/my/channels*`, `facai/notify.py`): a signed-in tenant binds the Telegram bot **they created themselves** (their own BotFather token plus the chat that receives progress) and it persists server-side — 绑一次 = 云端 7×24. The token is stored only as secretbox ciphertext, is decrypted solely in memory at the moment the NOTIFY loop sends that owner's mission progress, and is never returned by any API or written to logs/audit rows. A bound owner's progress rides their own bot even when the station bot is unconfigured; a failed customer-bot send is honestly audited as FAILED without blocking the pipeline; unbinding falls back to the station channel, whose behavior is unchanged; bindings are tenant-isolated (each account sees and clears only its own). See `docs/CLIENT_DELIVERY.md`.
- Private self-hosted n8n Community Edition deployment (official pinned Docker image, loopback-only port, persistent Docker volume and SSH-tunnel administration) is defined in [`deploy/n8n/`](deploy/n8n/). It avoids the n8n Cloud subscription for internal operator automations. n8n's Sustainable Use License restricts offering its workflow builder directly to customers; see [`deploy/n8n/README.md`](deploy/n8n/README.md) before any commercial exposure.
- Non-destructive backup restore drill (`facai.backup drill`) that restores a snapshot into a throwaway directory and compares row counts.
- Layout verified in real Chromium viewports: a 1440x900 desktop first screen shows the progress bar, current stage and pipeline without scrolling, and a 390x844 phone viewport has no horizontal overflow, a vertical pipeline, adequate tap targets and a sticky header. Deep-link and login-restore flows are covered too (`tests/test_console_layout.py`, `tests/test_mobile_ui.py`).
- Open-source architecture review and a staged adoption plan for visual Agent workflows, safe parallel branches, auditable run traces and integration boundaries: [`docs/OPEN_SOURCE_RESEARCH.md`](docs/OPEN_SOURCE_RESEARCH.md).
- Open Agent Skill source catalog, license notes, initial FACAI use cases and review requirements before any skill is imported or enabled: [`docs/OPEN_SOURCE_SKILLS.md`](docs/OPEN_SOURCE_SKILLS.md).

## Verify

```powershell
python -m pytest -q
```

The suite includes real-browser checks (`tests/test_console_layout.py` and
`tests/test_mobile_ui.py`) driven by Playwright. On a 1440x900 desktop viewport the
first screen shows the progress bar, current stage and pipeline before any scrolling;
on a 390x844 phone viewport there is no horizontal overflow, the pipeline stacks
vertically with adequate tap targets, and deep-link plus login-restore work. They
skip automatically if Playwright is absent; to run them:

```powershell
python -m pip install playwright
python -m playwright install chromium
```

## Backup and restore

```powershell
python -m facai.backup create  --data-dir data
python -m facai.backup verify  --dest data\backups\facai-<timestamp>.sqlite3
python -m facai.backup restore --data-dir data --dest data\backups\facai-<timestamp>.sqlite3
python -m facai.backup drill   --data-dir data
```

`drill` takes a verified snapshot, restores it into a throwaway directory and
compares row counts against the live database; the live database is never
modified and only counts are reported.

Backups snapshot the mission database (missions, steps, events, sessions, artifacts, checkpoints, costs, jobs, registries) and store credential references only. Stop the server before restoring; the previous database is preserved beside it. Workspace files are not part of the database backup and must be copied separately.

## Production status

**Last live deployment verified (2026-10-01).** The VPS `77.221.148.118` has the
then-current code under `/opt/facai-cloud`, bound to `127.0.0.1:8765` behind nginx HTTPS
with a Let's Encrypt certificate for `whstapp.lol` and `www.whstapp.lol`. The
mission-first console refactor was deployed and re-verified over real HTTPS:
`check_public_access.sh` exits 0 and a Chromium run against `https://whstapp.lol`
passed 15/15 checks (1440x900 first-screen progress bar/current stage/pipeline,
collapsed creation and registry sections, 390x844 no horizontal overflow, anonymous
redirect to `/login`).
`deploy/check_public_access.sh whstapp.lol 77.221.148.118` exits 0 (authoritative
DNS, TCP 443, TLS, `health 200`, anonymous `401`, authenticated `200`), and the
login flow, mission pipeline and SSE stream were exercised over HTTPS. Deploy with
`sudo ./deploy/deploy.sh update` (then `cert` on first install); verify with
`deploy/check_public_access.*` and never report availability from inspection alone.
See `docs/DEPLOYMENT.md`.

**Live OpenCode execution verified (2026-10-01).** The VPS had no Node/npm, so
`deploy/install-opencode.sh` installs the self-contained, SHA-256-pinned
`opencode` CLI (v1.18.32) to `/usr/local/bin/opencode`; `deploy/deploy.sh opencode`
runs it, and `install` does so automatically when the binary is missing. The hardened
systemd unit now sets `PATH` and a writable `HOME` for the service. A real coding task
was executed online through the adapter in mission `80587244520747ddb11f639e2746bce5`:
`OPENCODE_COMPLETED`, cost and tokens recorded, a stdout artifact captured and a real
Git checkpoint committed, while a second mission stayed fully isolated. Real upstream
`MODEL_AUTH_FAILED` (HTTP 401), `MODEL_TIMEOUT` and `MODEL_TRANSPORT_FAILED` outcomes
were reproduced; no credential was printed. See `docs/ACCEPTANCE_OPENCODE.md`.

Remaining product work is configuration and live verification rather than new
routing: the Telegram, WhatsApp and personal WeChat inbound paths are implemented,
but none has received a real platform message in production — no live Meta or
Telegram credentials are set and no WeChat account has been scanned, so the
channels stay `UNCONFIGURED` and only simulated upstreams have been exercised.
Still to build are the optional GPU-worker adapter and the remaining WeChat
media/file/voice/group-chat paths. The n8n adapter is implemented but
unconfigured, so no event has been delivered to a real workflow. New local commits
must be deployed and verified separately; don't treat this recorded server state
as including later changes.
The data plane moves to PostgreSQL/Redis when the SQLite design stops meeting
concurrency needs.
