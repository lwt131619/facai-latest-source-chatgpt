"""Local development API for Facai Mission Control."""

import asyncio
import json
import logging
import math
import os
import re
import threading
import time
from contextvars import ContextVar
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               StreamingResponse, Response)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import API_KEY_PREFIX, AuthManager, CSRF_HEADER, SAFE_METHODS, SESSION_COOKIE, gates_off
from .billing import PRICING, budget_gate, effective_monthly_limit, estimated_cost
from .channels import ChannelRegistry, InboundMessage, OutboundReply
from .channels.wechat import WeChatProvider
from .channels.whatsapp import (WhatsAppBridgeError, WhatsAppHostBridge,
                                normalize_pairing_phone)
from . import authority_gate
from .boundary import BoundaryError, BoundaryGovernor
from .checkpoint import diff as git_diff
from .conversion import (AGENT_OVERRIDDEN_EVENT, ASSIGNMENT_EVENT,
                         ConsoleError, ConversionConsole,
                         analyze_environment, assignment_event_payload,
                         build_assignment)
from .execution import execute_mission_task, mission_attribution, opencode_model_ref
from .evidence import build_proof
from .artex import ArtexAdapter
from .gateway import KeyRotation, complete, key_usage
from .n8n import CALLBACK_STATUSES, N8nIntegration, N8nRelay
from .notify import NOTIFY_CHANNELS, ChannelNotifier, owner_bot
from . import secretbox
from .secretbox import SecretBoxError
from .goose import GooseAdapter
from .opencode import OpenCodeAdapter
from .prompting import (complete_with_bound_model, convert_message,
                        deterministic_plan, plan_with_bound_model)
from .registries import Registries, public_registry_value, _validate_skill_metadata
from .scheduler import Scheduler
from .skills import (bound_skill, build_skill_metadata, dispatchable_skill, enabled_only,
                     explicitly_requested_skill,
                     parse_skill_md)
from .store import Store, SkillUpdateConflict, assist_context, DEFAULT_APPROVAL_TTL_SECONDS

logger = logging.getLogger(__name__)
_request_owner = ContextVar("facai_request_owner", default="admin")

# 渠内审批（Grant×渠道相乘点）的固定文案与命令词。命令是精确匹配（strip 后
# 整句相等）：审批是对既有任务的裁决，不是可以误触发的普通文本；不带活动
# 审批行的「批准」照旧走任务转换，行为可预期。TTL 与审批行共用一个常量，
# 提示里的「15 分钟内有效」对行和 grant 同时成立。
APPROVAL_TTL_SECONDS = DEFAULT_APPROVAL_TTL_SECONDS
APPROVE_COMMAND = "批准"
CANCEL_COMMAND = "取消"

# Browser isolation defaults for every response the app produces, including the
# redirects and 401s the auth middleware returns before a handler runs. The app
# is the single owner of these three: the nginx config deliberately omits them,
# because a second copy of X-Frame-Options is undefined for browsers.
SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "same-origin"),
)


class MissionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    original_input: str = Field(min_length=1)
    steps: list[str | dict] = Field(min_length=1)


class AutoMissionCreate(BaseModel):
    original_input: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=200)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class SkillImport(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    source_url: str | None = Field(default=None, max_length=500)


class SkillVersionUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    expected_version: str = Field(max_length=64)
    expected_body_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")


class SkillSourceRestore(BaseModel):
    source_version: str = Field(max_length=64)
    source_body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    new_version: str = Field(min_length=1, max_length=64)
    expected_version: str = Field(max_length=64)
    expected_body_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")


class SkillEnabledToggle(BaseModel):
    """SKILL-001 ②③: 禁用/启用开关的请求体。"""
    enabled: bool
    note: str = Field(default="", max_length=500)


class BoundaryIntake(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    source_url: str | None = Field(default=None, max_length=500)


class CaseIngestRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=50)


class ModelRouteWeight(BaseModel):
    weight: float = Field(ge=0, le=100)


class ModelRoutePause(BaseModel):
    paused: bool


class ModelRouteLimits(BaseModel):
    """水位线编辑（MODEL-001 ④）：分钟窗与并发水位写回 models 注册表条目。"""
    rate_limit_per_min: int = Field(ge=1, le=100000)
    max_concurrency: int = Field(ge=1, le=1000)


class BoundaryNote(BaseModel):
    note: str = Field(default="", max_length=500)


class BoundaryConfigUpdate(BaseModel):
    modes: dict[str, str] | None = None
    extra: dict[str, list[dict]] | None = None


class ConversionText(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


class GateOpen(BaseModel):
    stage_label: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1, max_length=2000)
    kind: str = Field(default="direction")


class GateResolve(BaseModel):
    choice: int = Field(ge=0, le=15)
    custom_answer: str = Field(default="", max_length=4000)


class Binding(BaseModel):
    agent_id: str | None = None
    model_id: str | None = None
    skill_id: str | None = None
    runtime_id: str | None = None


class StepUpdate(BaseModel):
    status: str


class PromptUpdate(BaseModel):
    professional_prompt: str | None = None


class EventCreate(BaseModel):
    type: str = Field(min_length=1)
    payload: dict = Field(default_factory=dict)
    step_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None


class ModelRequest(BaseModel):
    messages: list[dict]
    step_id: str | None = None


class OpenCodeRun(BaseModel):
    prompt: str = Field(min_length=1)
    step_id: str | None = None
    model_ref: str | None = None
    timeout: int = Field(default=300, ge=1, le=3600)


class JobCreate(BaseModel):
    prompt: str = Field(min_length=1)
    step_id: str | None = None
    model_ref: str | None = None
    priority: int = 0
    max_attempts: int = Field(default=1, ge=1, le=10)
    timeout: int = Field(default=300, ge=1, le=3600)


class LoginRequest(BaseModel):
    username: str = "admin"
    password: str = Field(min_length=1, max_length=256)


class ChangePassword(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=256)


class ChannelSend(BaseModel):
    recipient: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=4096)
    reply_to: str | None = None


class UserModelCreate(BaseModel):
    """A client-supplied model: their endpoint, their key, their missions."""
    name: str = Field(min_length=1, max_length=64)
    endpoint: str = Field(min_length=8, max_length=2048)
    model: str = Field(min_length=1, max_length=128)
    api_key: str = Field(min_length=1, max_length=4096)


class ChannelBindingCreate(BaseModel):
    """A customer's own channel bot: their token, the chat that receives progress."""
    bot_token: str = Field(min_length=1, max_length=256)
    chat_id: str = Field(min_length=1, max_length=64)


class LineBindingCreate(BaseModel):
    """A customer's own LINE channel: push token, signing secret, target user.

    The channel secret is stored alongside the access token (encrypted) even
    though push does not need it: it is what future webhook verification for
    the owner's own LINE channel will require, and asking for it once here
    spares a re-bind later.
    """
    access_token: str = Field(min_length=1, max_length=512)
    channel_secret: str = Field(min_length=1, max_length=256)
    user_id: str = Field(min_length=1, max_length=64)


class ChannelReply(BaseModel):
    text: str = Field(min_length=1, max_length=4096)


class WeChatVerifyCode(BaseModel):
    code: str = Field(min_length=4, max_length=8, pattern=r"^[0-9]+$")


class WhatsAppPairRequest(BaseModel):
    # The customer's own WhatsApp number for pairing-code linking. Loose here
    # (so a malformed value reaches OUR validator and its business wording);
    # the strict 7-15 digit check lives in WhatsAppHostBridge.normalize_phone.
    phone: str = Field(default="", max_length=32)


PUBLIC_PATHS = frozenset({
    "/login",
    "/api/v1/auth/login",
    "/api/v1/auth/register",
    "/api/v1/auth/logout",
    "/api/v1/auth/session",
    "/api/v1/health",
    "/health",
    "/setup",
    "/api/v1/setup/bootstrap",
    "/api/v1/setup/recover",
    "/api/v1/n8n/callback",
    "/favicon.ico",
})

# An account flagged ``must_change_password`` (weak password handed out by the
# operator) may keep reading on every credential path -- console session, HTTP
# Basic, client API key -- but can only write to these paths until the password
# is rotated; everything else is refused so the flag is enforced server-side
# and not merely displayed.
MUST_CHANGE_ALLOWED_PATHS = frozenset({
    "/api/v1/auth/change-password",
    "/api/v1/auth/logout",
})


def basic_readonly():
    """Whether HTTP Basic is refused for non-safe methods (FACAI_BASIC_READONLY).

    A browser replays cached Basic credentials on every request to the origin,
    including a cross-site form post, so a deployment without script clients
    that write over Basic can serve Basic as a read-only credential. Read per
    request like FACAI_COOKIE_SECURE, so the switch flips without a code change.
    Default off: the gate is hardening, not a silent API break.
    """
    setting = (os.getenv("FACAI_BASIC_READONLY", "0") or "").strip().lower()
    return setting in {"1", "true", "yes", "on"}


# Warn once per account when a Basic write goes through the open gate: such a
# request is indistinguishable from a browser replay, so the operator should
# know the read-only switch exists. Bounded by the five-account cap.
_basic_write_warned = set()


def warn_basic_write(owner, path):
    if owner in _basic_write_warned:
        return
    _basic_write_warned.add(owner)
    logger.warning("HTTP Basic authenticated a write (%s, %s); set FACAI_BASIC_READONLY=1 "
                   "to require a session with a CSRF token for writes", owner, path)


# A client API key is confined to the client's own surface: the missions it
# owns (create, read, stream, jobs, artifacts), job control, the artifacts
# center and its own billing snapshot. Everything else -- channel configs,
# registries, recharge review, ops data and key management itself -- still
# requires an interactive console session.
API_KEY_PATH_PREFIXES = ("/api/v1/missions", "/api/v1/jobs", "/api/v1/artifacts")
API_KEY_EXACT_PATHS = frozenset({"/api/v1/billing/me"})
# Enough keys for a client's own automation (staging, production, a laptop)
# without letting one account grow the table without bound.
MAX_API_KEYS_PER_OWNER = 20


# 逆向克隆防护（2026-10-06）：存储层为执行/取证保留服务器文件系统路径
# （missions.workspace、artifacts.path、WORKTREE_* 事件 payload.path、
# run 结果 workspace/worktree.path），这些字段一旦原样出现在 HTTP 响应里，
# 就把部署布局（/opt/facai-cloud/data/workspaces/...）白送给做逆向克隆的
# 客户。前端零处消费这些字段（facai/static 无 workspace/payload.path 引用），
# 因此在 API 边界统一剥离；存储层原值不动。tests/test_source_leak.py 钉住。
def public_artifact(artifact):
    """Wire shape of an artifact row: the server-side path never leaves."""
    return {key: value for key, value in artifact.items() if key != "path"}


def public_mission(record):
    """Wire shape of a mission record: platform filesystem layout never leaves."""
    clean = {key: value for key, value in record.items() if key != "workspace"}
    clean["artifacts"] = [public_artifact(artifact)
                          for artifact in record.get("artifacts") or ()]
    return clean


def public_event(event):
    """Wire shape of a mission event: payload never carries a server path."""
    payload = event.get("payload")
    if isinstance(payload, dict) and "path" in payload:
        return {**event, "payload": {key: value for key, value in payload.items()
                                     if key != "path"}}
    return event


def public_run_result(result):
    """Wire shape of a synchronous run result: no workspace, no worktree path."""
    clean = {key: value for key, value in result.items()
             if key not in ("workspace", "worktree")}
    worktree = result.get("worktree")
    if isinstance(worktree, dict):
        clean["worktree"] = {key: value for key, value in worktree.items()
                             if key != "path"}
    return clean

# Customer channel bindings ("每客户通道默认持久"): the channels a tenant may
# bind with their OWN bot credential. The token shape check mirrors the station
# adapter's env validation (facai/channels/telegram.py), so a token that would
# be rejected there never gets stored here; a chat is a numeric id (groups are
# negative) or an @public-channel username the bot can post to.
CUSTOMER_BINDABLE_CHANNELS = ("telegram", "line", "discord", "slack")
# 绑定渠道「测试推送」的固定文案：绑定完成后一键自证链路（真发一条到客户自己的
# bot + 接收会话）。它复用 NOTIFY 循环的同一条信任路径（notify.owner_bot：行内
# secretbox 解密 → 出站专用 provider），DELIVERED 只能来自上游 2xx 且回显具体
# 消息 id；失败透出 adapter 的稳定错误码，凭据永不进响应。
TEST_PUSH_TEXT = "FACAI 测试推送：链路正常 ✅"
# Hosted channels ("云端 7×24 宿主", docs/CHANNEL_HOST_DESIGN.md): the personal
# channels a customer hosts ON the cloud (per-owner credential namespace under
# data/providers/hosting/<owner_id>/), as opposed to the token bindings above
# where the customer's own bot pushes to a chat. The per-customer/per-channel
# quota of 1 is the channel_host_bindings primary key itself; the plan-based
# ceiling documented there is NOT invented here because no plan model exists.
HOST_CHANNELS = ("wechat", "whatsapp")
# Named statement of the structural quota (PRIMARY KEY (owner_id, channel) in
# channel_host_bindings). Kept as a constant so the design doc and the code
# cannot drift apart silently.
HOST_BINDING_QUOTA_PER_CHANNEL = 1
# Per-owner hosted-WeChat monitor (CHANNEL_HOST_DESIGN.md wave 2): a binding
# whose upstream rejects every poll this many times IN A ROW is treated as
# dead credentials and the monitor stops, with an audited event -- instead of
# hammering a dead token forever. A rejection is the only result that proves
# the credential invalid; long-poll gateway timeouts and transport blips are
# retryable noise, and an OK answer resets the streak too.
HOST_WECHAT_CREDENTIAL_FAILURE_LIMIT = 3
TELEGRAM_BOT_TOKEN_PATTERN = r"[0-9]+:[A-Za-z0-9_-]+"
TELEGRAM_CHAT_ID_PATTERN = r"-?[0-9]+|@[A-Za-z][A-Za-z0-9_]{3,64}"
# LINE: the channel access token is a long single-line bearer string (the
# platform promises no fixed shape, so the gate is charset + length); the
# channel secret is the signing secret; a push target is a LINE user id
# (``U`` + 32 hex) -- the same value the webhook reports as ``source.userId``.
LINE_ACCESS_TOKEN_PATTERN = r"[A-Za-z0-9_\-+/=.]{16,512}"
LINE_CHANNEL_SECRET_PATTERN = r"[A-Za-z0-9+/=_-]{16,256}"
LINE_USER_ID_PATTERN = r"U[0-9a-fA-F]{32}"
# Discord: a bot token is three dot-separated base64url segments (client id in
# the middle); a push target is a channel (or thread) snowflake id.
DISCORD_BOT_TOKEN_PATTERN = r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}"
DISCORD_CHANNEL_ID_PATTERN = r"[0-9]{10,25}"
# Slack: bot tokens carry the xoxb- marker; a push target is the platform's
# opaque channel id (historically ``C``-prefixed), charset-gated only.
SLACK_BOT_TOKEN_PATTERN = r"xoxb-[A-Za-z0-9-]{20,}"
SLACK_CHANNEL_ID_PATTERN = r"[A-Za-z0-9]{4,32}"

# 凭证预检（签发权在 Owner 核）：客户在聊天里批准自己的任务，但绑定通道凭证
# 前必须先向平台验证——格式合法不等于 token 有效，落库前真实调一次上游只读
# 接口（Telegram getMe / LINE bot info / Discord @me / Slack auth.test）。
# 5 秒超时；2xx 且上游确认有效才绑定，否则 422「token 无效（<平台> 返回 <原因>）」；
# 连不通（传输层异常）同样 422「无法验证 token（网络原因）」——验证不了绝不
# 当成验证过。FACAI_SKIP_CREDENTIAL_PREFLIGHT=1 可整体跳过预检：仅供离线测试
# 与网络被管制的极端环境使用；跳过后的绑定不再保证凭证有效，问题会推迟到首次
# 真实推送才暴露，因此生产环境不要设置。
CREDENTIAL_PREFLIGHT_TIMEOUT = 5.0
SKIP_CREDENTIAL_PREFLIGHT_ENV = "FACAI_SKIP_CREDENTIAL_PREFLIGHT"


async def preflight_channel_credential(channel, credential):
    """真实预检客户通道凭证：通过上游验证才允许绑定（语义见上方注释块）。

    Telegram 用 getMe（要求 200 且 ok=true），LINE 用 /v2/bot/info（要求 200
    的 JSON 对象），Discord 用 /users/@me（要求 200 且回显 bot 自身 id），
    Slack 用 auth.test（POST，要求 200 且 ok=true）；channel_secret 无法独立
    验证，LINE 只预检 access_token。任何非成功应答都按无效处理并附上游原因
    （Telegram 取 description，LINE/Discord 取 message，Slack 取 error，缺省
    退回 HTTP 状态码）；超时/断网等 httpx 传输层异常单独归为网络原因。凭证
    永不出现在任何错误信息里，且 facai/channels/telegram.py 在 import 时给
    httpx 日志装了 _RedactBotURL 过滤器，getMe 的 URL 日志同样只会留下打码
    形态。
    """
    if os.environ.get(SKIP_CREDENTIAL_PREFLIGHT_ENV, "").strip() == "1":
        return
    if channel == "telegram":
        url = f"https://api.telegram.org/bot{credential}/getMe"
        headers, method = {}, "GET"
        label, reason_field = "Telegram", "description"
    elif channel == "line":
        url = "https://api.line.me/v2/bot/info"
        headers, method = {"Authorization": f"Bearer {credential}"}, "GET"
        label, reason_field = "LINE", "message"
    elif channel == "discord":
        url = "https://discord.com/api/v10/users/@me"
        headers, method = {"Authorization": f"Bot {credential}"}, "GET"
        label, reason_field = "Discord", "message"
    elif channel == "slack":
        url = "https://slack.com/api/auth.test"
        headers, method = {"Authorization": f"Bearer {credential}"}, "POST"
        label, reason_field = "Slack", "error"
    else:
        raise ValueError(f"no credential preflight for channel: {channel}")
    try:
        async with httpx.AsyncClient(timeout=CREDENTIAL_PREFLIGHT_TIMEOUT,
                                     follow_redirects=False) as client:
            response = await client.request(method, url, headers=headers)
    except (httpx.TimeoutException, httpx.RequestError):
        raise HTTPException(422, "无法验证 token（网络原因）") from None
    try:
        body = response.json()
    except ValueError:
        body = None
    if channel in ("telegram", "slack"):
        platform_ok = isinstance(body, dict) and body.get("ok") is True
    elif channel == "discord":
        platform_ok = (isinstance(body, dict) and isinstance(body.get("id"), str)
                       and bool(body["id"]))
    else:  # line: any well-formed JSON object on a 2xx confirms the token
        platform_ok = isinstance(body, dict)
    if platform_ok and 200 <= response.status_code < 300:
        return
    reason = body.get(reason_field) if isinstance(body, dict) else None
    if not (isinstance(reason, str) and reason.strip()):
        reason = f"HTTP {response.status_code}"
    raise HTTPException(422, f"token 无效（{label} 返回 {reason}）")


def bearer_api_key(request):
    """The ``facai_`` bearer token on ``request``, or None.

    ``Bearer`` is case-insensitive per RFC 7235, and only a token carrying the
    client-key marker counts: any other bearer token belongs to someone else's
    scheme and must fall through to the session / Basic handling instead of
    being rejected as an invalid key.
    """
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token if token.startswith(API_KEY_PREFIX) else None


def api_key_allowed(path):
    """True when a client API key may call ``path``.

    Prefixes match a whole path segment only, so ``/api/v1/missions-secret`` is
    not admitted by the ``/api/v1/missions`` entry. A trailing slash is
    cosmetic on these routes and admits exactly like the bare path.
    """
    trimmed = path.rstrip("/")
    if trimmed in API_KEY_EXACT_PATHS:
        return True
    return any(trimmed == prefix or trimmed.startswith(prefix + "/")
               for prefix in API_KEY_PATH_PREFIXES)


def is_public_path(path):
    """Webhook callbacks come from the messaging provider, not a browser session.

    They are public at the transport layer and authenticate themselves with the
    provider's verify token or HMAC signature inside the endpoint.
    """
    if path in PUBLIC_PATHS:
        return True
    if path.startswith("/api/v1/media/"):
        # Digest videos: the filename itself carries an unguessable random
        # token, which is the capability credential for the link in the push.
        return True
    if path == "/api/v1/channels/relay/send":
        # The bridge client (AutoClaw) authenticates with the relay HMAC
        # signature inside the endpoint, never with an interactive session.
        return True
    if path == "/manifest.webmanifest" or path == "/sw.js" or path.startswith("/static/"):
        # PWA install assets: the browser fetches the manifest and registers the
        # service worker outside any session (install prompts, lock-screen
        # refresh). They carry no data — static shell and icons only.
        return True
    if path == "/scan" or path.startswith("/api/v1/quick-scans"):
        # Login-free quick scan (Owner directive 2026-10-28): paste a URL, get
        # a bounded read-only report. No session credential is involved, so
        # there is nothing to CSRF; abuse is bounded by per-IP/per-host rate
        # gates and the scan concurrency cap inside the service.
        return True
    return path.startswith("/api/v1/channels/") and path.endswith("/webhook")


# A minimal, harmless prompt for the manual connectivity probe. It exists only to
# prove the configured credential can complete a request; it is never sent on page
# load and its response body is discarded.
MODEL_TEST_PROMPT = "Reply with the single word: ok"

# A single authenticated user may run at most one connectivity probe per window.
# This is a server-side guard so repeated direct calls cannot drain upstream
# tokens; it is not a promise of high-precision pacing.
MODEL_TEST_COOLDOWN_SECONDS = 15.0

# Self-healing pipeline: every dispatched job may be re-run automatically when
# it fails for a TRANSIENT reason (timeout / rate limit / provider resource /
# transport hiccup). 1 would mean "one strike and a human must intervene",
# which is wrong for a 24/7 station; the cap keeps a permanently broken step
# from burning the model budget in a retry loop.
JOB_MAX_ATTEMPTS = max(1, min(5, int(os.getenv("FACAI_JOB_MAX_ATTEMPTS", "3") or 3)))

# USDT recharge policy: one cold-wallet address, $100 minimum, 5-minute window.
RECHARGE_MIN_USD = 100.0
RECHARGE_WINDOW_SECONDS = 300

# Owner-facing "today's development progress". The facts live in a versioned JSON
# file so a human can audit and update them without touching code; percentages are
# always derived from the completed milestones in that file, so the API can never
# invent progress and can never report a provider as active when its call failed.
DAILY_PROGRESS_FILE = (Path(__file__).resolve().parent.parent
                       / "docs" / "DAILY_PROGRESS_2026-10-02.json")

DAILY_STATUS_LABELS = {
    "verified_local": "本地已验证",
    "in_progress": "进行中",
    "blocked": "受阻",
    "deployed": "已部署",
    "stopped": "已停止",
    "not_executing": "未执行",
}


def _daily_progress_path():
    override = os.getenv("FACAI_DAILY_PROGRESS_FILE", "").strip()
    return Path(override).resolve() if override else DAILY_PROGRESS_FILE


def _daily_progress_percent(milestones):
    """Whole 0-100 integer from completed milestones only.

    Floor division matches the mission progress convention: no milestone is 0,
    every milestone is 100, and a partial list rounds down so a half-finished
    task is never shown as complete or with a fractional value.
    """
    total = len(milestones)
    if total <= 0:
        return 0
    done = sum(1 for milestone in milestones if milestone.get("done") is True)
    return max(0, min(100, (100 * done) // total))


def daily_progress_payload():
    """Build the owner-facing progress payload from the versioned facts file."""
    raw = json.loads(_daily_progress_path().read_text(encoding="utf-8"))
    providers = []
    for entry in raw.get("providers", []):
        milestones = entry.get("milestones") or []
        status = entry.get("status") or "not_executing"
        providers.append({
            "id": entry.get("id") or entry.get("provider"),
            "provider": entry.get("provider"),
            "work": entry.get("work", ""),
            "status": status,
            "status_label": DAILY_STATUS_LABELS.get(status, status),
            "local_verified": bool(entry.get("local_verified")),
            "production_deployed": bool(entry.get("production_deployed")),
            "percent": _daily_progress_percent(milestones),
            "summary": entry.get("summary", ""),
            "milestones": milestones,
            "evidence": entry.get("evidence") or [],
            "blockers": entry.get("blockers") or [],
        })
    return {
        "date": raw.get("date"),
        "title": raw.get("title", "今日开发进度"),
        "note": raw.get("note", ""),
        "providers": providers,
    }


def _reported_token_usage(usage):
    """Normalize only the token counts the upstream actually reported.

    Arbitrary upstream payload keys are dropped so a connectivity probe can never
    echo a provider error body or a credential back through the ``usage`` field.
    """
    from .prompting import normalized_token_usage
    return normalized_token_usage(usage)


def _env_seconds(name, default):
    """Read a non-negative second interval from the environment."""
    try:
        return max(0.0, float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


def _env_data_dir(override=None, name="FACAI_DATA_DIR", default="data"):
    """Resolve the runtime data directory, treating a blank value as unset.

    A blank ``FACAI_DATA_DIR`` in the systemd ``EnvironmentFile`` means "leave it
    at the default" (the shipped template itself blanks other settings). Treating
    ``""`` as a real path would silently resolve to the process working directory,
    which under the hardened unit (``ProtectSystem=strict`` with only ``data/``
    writable) makes the service fail to open its database on startup.
    """
    value = override if override is not None else os.getenv(name)
    value = "" if value is None else str(value).strip()
    return Path(value or default).resolve()


def create_app(data_dir=None, opencode_adapter=None, goose_adapter=None):
    root = _env_data_dir(data_dir)
    store = Store(root)
    # Grant persistence: the gate survives restarts — issuance is written
    # through to the store, reload at startup restores non-revoked grants
    # (revoked ones were deleted at revocation time and never resurrect).
    authority_gate.attach_store(store)
    registries = Registries(store)
    # 边界治理（自进化收录闸门）与转换操作台：状态各自落地
    # （boundary.json / 进程内流水线表），不进 store schema。
    boundary_governor = BoundaryGovernor(root)
    console = ConversionConsole()
    auth = AuthManager(store)
    channels = ChannelRegistry(state_dir=root / "providers")

    def _relay_outbox(reply):
        """Durable relay outbox: replies wait here for the AutoClaw poll."""
        return store.record_channel_message(
            "relay", "OUTBOUND", "QUEUED", recipient=reply.recipient,
            text=reply.text,
            payload={"reply_to": reply.reply_to, **(reply.metadata or {})},
            owner_id="admin")

    # Registry stand-ins in tests may expose providers as a plain mapping or
    # lack lookup entirely; a missing relay just means no outbox wiring.
    try:
        _relay_provider = channels.get("relay")
    except AttributeError:
        _relay_provider = None
    if _relay_provider is not None and hasattr(_relay_provider, "bind_outbox"):
        _relay_provider.bind_outbox(_relay_outbox)
    opencode = opencode_adapter or OpenCodeAdapter()
    adapters = {"opencode": opencode, "goose": goose_adapter or GooseAdapter(),
                # ENGINES-001 首个附属引擎：ARTEX 自主渗透 agent（HTTP API 派发，
                # 未配 ARTEX_BASE_URL 时 execute 诚实失败而非静默跳过）。
                "artex": ArtexAdapter()}

    def resolve_adapter(mission):
        """The executable adapter for a mission's bound agent, or None when
        the registry names an adapter we cannot run (fails the job honestly
        instead of silently rerouting to OpenCode)."""
        entry = registries.resolve("agents", mission.get("agent_id"),
                                   owner_id=mission.get("owner_id", "admin"))
        if entry is None:
            return None
        return adapters.get(entry.get("adapter"))

    scheduler = Scheduler(store, opencode, registries, root, adapter_resolver=resolve_adapter)
    n8n = N8nIntegration()
    n8n_relay = N8nRelay(store, n8n)
    n8n_relay.initialize_cursor()
    wechat_poll_interval = _env_seconds("FACAI_WECHAT_POLL_INTERVAL", 0.2)
    wechat_error_backoff = _env_seconds("FACAI_WECHAT_ERROR_BACKOFF", 3.0)
    # Per-owner hosted-WeChat monitors poll on their own (slower) cadence: a
    # customer binding is long-lived 7x24 background work, not the operator's
    # interactive console flow.
    host_wechat_poll_interval = _env_seconds("FACAI_HOST_WECHAT_POLL_INTERVAL", 5.0)
    # Far longer than the bounded conversion model timeout, so a live worker is
    # never robbed of its claim while a crashed one is recovered promptly.
    conversion_lease_seconds = _env_seconds("FACAI_CONVERSION_LEASE_SECONDS", 300.0)
    notify_enabled = os.getenv("FACAI_CHANNEL_NOTIFY_ENABLED", "1") != "0"
    notifier = ChannelNotifier(store, channels)

    async def _supervise(name, factory, restart_delay=5.0):
        """Keep a long-running monitor alive for the whole process lifetime.

        The chat monitors are the persistent link to the customer's messaging
        channel: a 24/7 workstation must never leave them down because one loop
        returned or blew up. Restart it (after a short pause) forever; only an
        actual shutdown (CancelledError) is allowed to end the supervisor.
        """
        while True:
            try:
                await factory()
                logger.warning("%s monitor loop returned; restarting", name)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("%s monitor crashed; restarting", name)
            await asyncio.sleep(restart_delay)

    # --- per-owner hosted channels (CHANNEL_HOST_DESIGN.md wave 2) -------------
    # Every ACTIVE host binding gets its OWN WeChatProvider instance (private
    # env mapping, credential file under providers/hosting/<owner_id>/) and its
    # OWN asyncio poll task. The station's wechat instance and monitor above
    # run untouched beside them: a customer's QR, credential file, iLink cursor
    # and message stream never touch the operator's, and vice versa.
    host_wechat_providers = {}   # owner_id -> WeChatProvider (hosting/<owner>)
    host_monitor_tasks = {}      # owner_id -> asyncio.Task

    def _host_wechat_provider(owner):
        """Get-or-build the owner's isolated provider instance.

        Construction loads the owner's stored credential file when present, so
        the QR-confirm path and the restart recovery scan share one code path.
        """
        provider = host_wechat_providers.get(owner)
        if provider is None:
            provider = WeChatProvider.for_owner_state(root / "providers" / "hosting" / owner)
            host_wechat_providers[owner] = provider
        return provider

    async def host_wechat_monitor(owner):
        """One owner's 7x24 poll loop over their own hosted WeChat instance.

        Inbound messages run through the SAME audited pipeline as the station
        monitor (record -> convert -> link -> dispatch) attributed to THIS
        binding's owner, never to admin. The loop ends only when the binding is
        no longer ACTIVE (stop wins over everything) or the credential is
        provably dead (HOST_WECHAT_CREDENTIAL_FAILURE_LIMIT rejections in a
        row, audited below); cancellation is stop/shutdown, not an error.
        """
        provider = host_wechat_providers[owner]
        credential_failures = 0
        while True:
            binding = store.host_binding(owner, "wechat")
            if binding is None or binding["state"] != "ACTIVE":
                # Stop condition 1: the row moved to UNBOUND (or vanished) --
                # the stop endpoint already audited HOST_BINDING_STOPPED.
                return
            try:
                if not provider.configured():
                    # An ACTIVE row without a usable credential is a dead
                    # binding (file deleted, env cleared): counts as a failure.
                    credential_failures += 1
                    if credential_failures >= HOST_WECHAT_CREDENTIAL_FAILURE_LIMIT:
                        break
                else:
                    # Replay durable pending messages of THIS owner only, so a
                    # crash never loses an already-stored inbound message.
                    for pending in store.pending_inbound_messages("wechat", owner_id=owner):
                        await run_in_threadpool(
                            _process_wechat_message, _wechat_message_from_row(pending), owner)
                    result = await provider.fetch_updates()
                    state = result.get("state")
                    if state == "OK":
                        credential_failures = 0
                        for message in result.get("messages", []):
                            processed = await run_in_threadpool(
                                _process_wechat_message, message, owner)
                            if not processed.get("durable", processed.get("status") != "RETRY"):
                                # Keep the iLink cursor so the next poll replays
                                # a batch that failed before durable storage.
                                break
                        else:
                            acknowledge = getattr(provider, "acknowledge_updates", None)
                            if callable(acknowledge):
                                acknowledge()
                    elif state == "FAILED" and result.get("error") == "UPSTREAM_REJECTED":
                        # Stop condition 2 accumulating: the upstream rejected
                        # the request, which with a stored token means the
                        # credential is no longer valid.
                        credential_failures += 1
                        if credential_failures >= HOST_WECHAT_CREDENTIAL_FAILURE_LIMIT:
                            break
                    else:
                        # RETRY and the other FAILED shapes (long-poll gateway
                        # timeouts, transport blips) are retryable noise: they
                        # neither prove nor disprove the credential, so the
                        # consecutive-rejection streak resets.
                        credential_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("wechat host monitor (%s): round failed; retrying", owner)
            await asyncio.sleep(host_wechat_poll_interval)
        # Dead credentials: stop polling and audit WHY instead of silently
        # leaving an ACTIVE row unmonitored. The row stays ACTIVE so a restart
        # re-runs the bounded 3-strike probe; full revoke stays a later wave.
        store.record_channel_event(
            "wechat", f"host:{owner}", "HOST_MONITOR_STOPPED",
            {"owner_id": owner, "channel": "wechat", "reason": "CREDENTIAL_INVALID",
             "consecutive_failures": credential_failures})

    def start_host_wechat_monitor(owner):
        """Idempotently start (or keep) the owner's monitor task.

        Safe from both the QR-confirm endpoint and the startup scan: a second
        call while the task lives is a no-op, never a second poller racing the
        first for the same iLink cursor. Must run on the event loop thread.
        """
        existing = host_monitor_tasks.get(owner)
        if existing is not None and not existing.done():
            return existing
        _host_wechat_provider(owner)
        task = asyncio.create_task(host_wechat_monitor(owner))
        host_monitor_tasks[owner] = task
        task.add_done_callback(
            lambda done, _owner=owner: host_monitor_tasks.pop(_owner, None)
            if host_monitor_tasks.get(_owner) is done else None)
        return task

    def stop_host_wechat_monitor(owner):
        """Cancel the owner's monitor task, if one is running (loop thread only)."""
        task = host_monitor_tasks.pop(owner, None)
        if task is not None:
            task.cancel()
        return task

    @asynccontextmanager
    async def lifespan(_app):
        # A crashed conversion worker must not strand its inbound message across
        # a restart; expired leases return to the replayable RETRY pool.
        store.recover_stale_channel_conversions()
        wechat_task = asyncio.create_task(_supervise("wechat", wechat_monitor))
        # Restart recovery for per-owner hosted channels (wave 2): every ACTIVE
        # binding gets its own provider instance (loading the owner's stored
        # credential file) and monitor task back, no re-scan needed.
        for binding in store.active_host_bindings("wechat"):
            start_host_wechat_monitor(binding["owner_id"])
        if os.getenv("FACAI_SCHEDULER_ENABLED", "1") != "0":
            scheduler.recover()
            scheduler.start()
        # The relay is a no-op while unconfigured, so starting it never fakes a
        # connection; it only forwards the stage events of new missions.
        relay_enabled = os.getenv("FACAI_N8N_RELAY_ENABLED", "1") != "0"
        if relay_enabled and n8n.configured():
            n8n_relay.start()
        # Progress replies for chat-originated missions; a no-op while no
        # channel is configured, never a faked connection.
        if notify_enabled:
            notifier.start()
        try:
            yield
        finally:
            host_tasks = [stop_host_wechat_monitor(owner) for owner in list(host_monitor_tasks)]
            host_tasks = [task for task in host_tasks if task is not None]
            if host_tasks:
                await asyncio.gather(*host_tasks, return_exceptions=True)
            wechat_task.cancel()
            try:
                await wechat_task
            except asyncio.CancelledError:
                pass
            scheduler.stop()
            n8n_relay.stop()
            notifier.stop()

    # Schema/docs surfacing is deliberately disabled: FastAPI's default
    # /openapi.json, /docs and /redoc would hand every authenticated customer
    # the full endpoint inventory -- including the /api/v1/admin/* routes and
    # every request/response schema -- which is exactly the enumeration
    # surface a tenant must not get. The console is hand-written static HTML
    # and never reads the schema (pinned by tests/test_customer_surface.py).
    app = FastAPI(title="Facai Cloud", version="0.2.0", lifespan=lifespan,
                  openapi_url=None, docs_url=None, redoc_url=None)

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(request, exc):
        # Pydantic's default `input` may be an entire model/login request,
        # including API keys and passwords. Keep useful field errors only.
        return JSONResponse(status_code=422, content={'detail': [
            {key: error[key] for key in ('type', 'loc', 'msg') if key in error}
            for error in exc.errors()]})

    app.state.store = store

    # MODEL-001 跨进程共享冷却：本 app 自持一个挂了 SQLite 后端的 Key 轮换器，

    # 多进程部署读到同一份冷却；进程级默认实例不再被写脏（测试互不串）。

    app.state.key_rotation = KeyRotation(store)
    app.state.registries = registries
    app.state.boundary = boundary_governor
    app.state.conversion = console
    app.state.opencode = opencode
    app.state.scheduler = scheduler
    app.state.auth = auth
    # 模型配额池（2026-10-13 操作员指令：预防式限速 + 可视化支配路由）。
    # 注册表变化即同步（replace_models 幂等，计数存活）。
    from .quota import QuotaPools
    # Read-only seed: registry_list hits the store WITHOUT Registries.list's
    # defaults-persistence side effect — create_app must not be the writer
    # that resurrects a cleared models catalog (deferred-dispatch tests pin
    # this ordering).
    quota_pools = QuotaPools(store.registry_list("models", owner_id=auth.username))
    app.state.quota_pools = quota_pools
    # MODEL-001（2026-10-16）接线：调度器持有同一份池——派发前 reserve、
    # 终态后 report。池未注册的模型行为完全不变（可选增强，不是新闸门）。
    scheduler.quota_pools = quota_pools
    app.state.channels = channels
    app.state.n8n = n8n
    app.state.n8n_relay = n8n_relay
    app.state.notify = notifier
    # Per-owner hosted-channel runtime, for tests and ops introspection only --
    # never a credential channel (values are provider objects, secrets stay in
    # their private env mappings and on-disk files).
    app.state.host_wechat_providers = host_wechat_providers
    app.state.host_monitor_tasks = host_monitor_tasks

    # Per-application-instance cooldown state for the manual connectivity probe.
    # Keyed by authenticated username, guarded by a lock so concurrent requests
    # cannot both pass the check, and based on monotonic time so wall-clock jumps
    # can neither extend nor bypass the window.
    model_test_lock = threading.Lock()
    model_test_probed_at = {}

    # Anti-proxy-resale throttle (反代转售审计 2026-10-06): a reseller pointing
    # their automation at the platform as an upstream must not be able to run a
    # wholesale business on one account. Two independent, per-owner limits on
    # mission CREATION:
    #   * an in-flight (WAITING/RUNNING) mission ceiling -- exceeded means the
    #     account's station capacity is used up, which is a quota problem: 402,
    #     same class as the budget gate;
    #   * a creation-rate ceiling over a sliding window -- exceeded means "too
    #     fast", never "pay": 429.
    # Both read their ceilings from the environment at call time (deploy can
    # tune without a code change) and the window state lives on THIS app
    # instance: monotonic time plus a lock admits concurrent requests one at a
    # time, and different app instances (tests, workers) never share a window.
    # The window counts admitted ATTEMPTS, not just successes -- a client
    # spraying malformed bodies burns the same slots as one spraying valid
    # ones.
    MISSION_CREATE_WINDOW_SECONDS = 60.0
    MISSION_CREATE_MAX_PER_WINDOW = 6

    def max_active_missions_per_owner():
        try:
            return max(1, int(os.getenv("FACAI_MAX_ACTIVE_MISSIONS_PER_OWNER", "10") or "10"))
        except ValueError:
            return 10

    mission_create_lock = threading.Lock()
    mission_create_times = {}
    # Exposed for tests and ops introspection only -- production code must
    # never reach in and clear the window.
    app.state.mission_create_times = mission_create_times

    def reserve_mission_create(owner):
        """Atomically claim a mission-creation slot or refuse with 402/429.

        The ceiling check comes first (it is the fundamental capacity fact),
        then the sliding window records this attempt. Both verdicts carry
        distinct Chinese copy so a client -- and an operator reading logs --
        can tell "capacity full" from "slow down" at a glance.
        """
        cap = max_active_missions_per_owner()
        active = store.count_active_missions(owner)
        if active >= cap:
            raise HTTPException(
                402, f"活跃任务已达上限（{active}/{cap}）：请等待任务完成，"
                     f"或将不需要的任务步骤取消后再创建")
        now = time.monotonic()
        with mission_create_lock:
            recent = [stamp for stamp in mission_create_times.get(owner, [])
                      if now - stamp < MISSION_CREATE_WINDOW_SECONDS]
            if not gates_off() and len(recent) >= MISSION_CREATE_MAX_PER_WINDOW:
                wait = int(MISSION_CREATE_WINDOW_SECONDS - (now - recent[0])) + 1
                raise HTTPException(
                    429, f"任务创建过于频繁：每分钟最多 "
                         f"{MISSION_CREATE_MAX_PER_WINDOW} 个，请 {wait} 秒后再试")
            recent.append(now)
            mission_create_times[owner] = recent

    def current_owner():
        return _request_owner.get()

    def require_admin_owner():
        """Operator-only guard: self-registered tenants stay out of station
        internals (channel configs, n8n, daily progress, station-wide stats).
        Webhook and relay signature paths are exempt by design."""
        if auth.username != current_owner():
            raise HTTPException(403, "仅管理员可访问")

    def reserve_model_probe(owner):
        """Atomically claim the owner's probe slot.

        Returns ``None`` when the probe may proceed, otherwise the seconds left
        before the owner may probe again. The check-and-set happens under the
        lock so two concurrent requests cannot both be admitted.
        """
        now = time.monotonic()
        with model_test_lock:
            last = model_test_probed_at.get(owner)
            if last is not None:
                remaining = MODEL_TEST_COOLDOWN_SECONDS - (now - last)
                if remaining > 0:
                    return remaining
            model_test_probed_at[owner] = now
        return None

    def wants_html(request):
        return "text/html" in request.headers.get("accept", "")

    @app.middleware("http")
    async def admin_session_auth(request, call_next):
        # Unauthenticated traffic (webhooks, health) attributes to the
        # environment-configured operator account. Iterating the merged user
        # dict would hand webhook missions to whichever self-registered account
        # happens to hash first.
        owner = auth.username
        restricted = auth.enabled and not is_public_path(request.url.path)
        # Client API keys: a paid client's automation carries a revocable,
        # scoped key instead of the whole-account password. The key is refused
        # outside its scope here, before any handler can act on it, and needs no
        # CSRF token -- it is not a browser credential and no cross-site page
        # can attach it to a request. Public endpoints authenticate themselves
        # and ignore the header entirely.
        api_key = bearer_api_key(request) if restricted else None
        if api_key is not None:
            owner, key_id = auth.verify_api_key(api_key)
            if owner is None:
                return JSONResponse({"detail": "API key 无效或已吊销"}, status_code=401)
            if not api_key_allowed(request.url.path):
                return JSONResponse({"detail": "该 API key 无权访问此接口"}, status_code=403)
            # last_used_at means "used", not "presented": stamp only a call the
            # key's scope admitted, so out-of-scope probes leave the console
            # listing untouched.
            auth.store.touch_api_key(key_id)
        elif restricted:
            session = auth.session_from_request(request)
            if session is not None:
                owner = session["username"]
                if request.method not in SAFE_METHODS:
                    supplied = request.headers.get(CSRF_HEADER, "")
                    if not auth.valid_csrf(session, supplied):
                        return JSONResponse({"detail": "CSRF token missing or invalid"}, status_code=403)
            else:
                # The Basic-auth path is an online password oracle; it must
                # honor the same per-IP throttle as /login or it becomes an
                # unthrottled brute-force surface.
                client_ip = request.client.host if request.client else "unknown"
                if auth.is_locked(client_ip):
                    return Response(status_code=429, headers={"WWW-Authenticate": 'Basic realm="Facai Cloud"'})
                basic_owner = auth.basic_username(request)
                if basic_owner:
                    if request.method not in SAFE_METHODS:
                        # A browser replays cached Basic credentials on a
                        # cross-site form post; FACAI_BASIC_READONLY=1 refuses
                        # that class of write and leaves sessions as the only
                        # writer. Script clients are unaffected while it is off.
                        if basic_readonly():
                            return JSONResponse({"detail": "Basic 认证仅允许只读请求，请先登录获取会话"}, status_code=403)
                        warn_basic_write(basic_owner, request.url.path)
                    owner = basic_owner
                    auth.store.clear_login_failures(client_ip)
                else:
                    # Only Basic credentials that name an account and fail
                    # verification count a failure against this address: a
                    # foreign scheme ("Bearer sk-...") names nobody and must
                    # stay a bare 401 instead of locking a shared egress IP.
                    if auth.basic_username_claimed(request):
                        auth.store.record_login_failure(client_ip, auth.failure_window)
                    if request.method in SAFE_METHODS and wants_html(request):
                        target = request.url.path
                        if request.url.query:
                            target = f"{target}?{request.url.query}"
                        return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
                    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Facai Cloud"'})
        # One must-change gate for every credential path: an operator-reset
        # platform account keeps read access but may only write to the rotation
        # endpoints until the password is rotated -- over a console session,
        # HTTP Basic or a client API key alike. Judged on the resolved owner so
        # no credential path routes around the flag; environment-backed
        # operator accounts have no row here and always pass.
        if (restricted and request.method not in SAFE_METHODS
                and request.url.path not in MUST_CHANGE_ALLOWED_PATHS
                and auth.store.platform_user_must_change(owner)):
            return JSONResponse({"detail": "请先修改密码"}, status_code=403)
        token = _request_owner.set(owner)
        try:
            return await call_next(request)
        finally:
            _request_owner.reset(token)

    @app.middleware("http")
    async def security_headers(request, call_next):
        # Added last, so it wraps the auth middleware and also decorates the
        # responses that never reach a route (303 login redirect, 401, 403
        # CSRF, 429 lockout). A handler or a dependency that already picked a
        # value for one of these headers keeps it: overriding here would let a
        # sub-app (e.g. mounted static files) be bypassed silently.
        response = await call_next(request)
        for name, value in SECURITY_HEADERS:
            if name not in response.headers:
                response.headers[name] = value
        return response

    def require_mission(mission_id, allow_assist=True):
        """Resolve the mission for the calling owner, or 404 for anyone else.

        Second-parameter assist bypass (协助模式，可见可审): while the operator
        runs an active assist session on a customer, the operator's OWN session
        -- never a minted customer cookie -- may reach that customer's
        mission-scoped resources. The grant is re-checked against the clock on
        every call, and the assist record is pinned to the request context so
        every event written by this request is stamped ``assist_by`` (see
        store.assist_context): the audit trail always names the actual operator,
        and the customer sees the session on /auth/session and their banner.
        """
        owner = current_owner()
        mission = store.get(mission_id, owner_id=owner)
        if mission is None and allow_assist:
            candidate = store.get(mission_id)
            if candidate is not None:
                assist = store.active_assist_for_owner(candidate["owner_id"])
                if assist is not None and assist["admin_username"] == owner:
                    assist_context.set({"admin": owner, "reason": assist["reason"],
                                        "expires_at": assist["expires_at"]})
                    return candidate
        if mission is None:
            raise HTTPException(404, "Mission not found")
        return mission

    @app.get("/")
    def index():
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/api/v1/admin/audit-report")
    def admin_audit_report():
        """每日查账：充值对账 + 消费对账 + 每客户余额核对（admin-only）。"""
        require_admin_owner()
        from .ops.audit_report import build_reconciliation, to_markdown
        report = build_reconciliation(store)
        return {"report": report, "markdown": to_markdown(report)}

    @app.get("/guide")
    def guide_page():
        return FileResponse(Path(__file__).parent / "static" / "guide.html")

    @app.get("/billing")
    def billing_page():
        return FileResponse(Path(__file__).parent / "static" / "billing.html")

    @app.get("/conversion")
    def conversion_page():
        # 转换操作台：移动端可视化流水线进度中心（会话内访问，非公开页）。
        return FileResponse(Path(__file__).parent / "static" / "conversion.html")

    @app.get("/command-center")
    def command_center_page():
        # ICS 指挥中心（Phase 1）：任务中心 + 实时事件流 + 反馈门裁决 +
        # 产出物。数据面全部走 owner 域端点；页面壳本身不携带任何告诫文案。
        return FileResponse(Path(__file__).parent / "static" / "command-center.html")

    @app.get("/login")
    def login_page():
        return FileResponse(Path(__file__).parent / "static" / "login.html")

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(Path(__file__).parent / "static" / "manifest.webmanifest",
                            media_type="application/manifest+json")

    @app.get("/sw.js")
    def service_worker():
        # Root scope: the worker can control both / and /login.
        return FileResponse(Path(__file__).parent / "static" / "sw.js",
                            media_type="application/javascript")

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"),
              name="app-assets")

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    @app.get("/api/v1/auth/session")
    def auth_session(request: Request):
        # is_admin is a server verdict keyed on the configured operator name
        # (FACAI_ADMIN_USER), so the front end never has to guess one.
        if not auth.enabled:
            return {"auth_required": False, "authenticated": True, "username": auth.username,
                    "is_admin": True}
        session = auth.session_from_request(request)
        if session is None:
            return {"auth_required": True, "authenticated": False, "is_admin": False}
        # 协助模式知情权（硬要求）: the assisted customer sees WHO is inside
        # their account, WHY, and UNTIL WHEN -- a null assist means nobody is.
        assist = store.active_assist_for_owner(session["username"])
        return {"auth_required": True, "authenticated": True,
                "username": session["username"], "csrf_token": session["csrf_token"],
                "expires_at": session["expires_at"],
                "is_admin": session["username"] == auth.username,
                "must_change_password": auth.store.platform_user_must_change(session["username"]),
                "assist": ({"active": True, "admin": assist["admin_username"],
                            "reason": assist["reason"], "expires_at": assist["expires_at"]}
                           if assist else None)}

    @app.post("/api/v1/auth/login")
    def auth_login(body: LoginRequest, request: Request):
        if not auth.enabled:
            return {"authenticated": True, "auth_required": False}
        client_ip = request.client.host if request.client else "unknown"
        session, error = auth.login(body.username, body.password, client_ip)
        if error == "throttled":
            raise HTTPException(429, "Too many failed attempts; try again later")
        if error is not None:
            raise HTTPException(401, "Invalid credentials")
        response = JSONResponse({"authenticated": True, "auth_required": True,
                                 "username": session["username"],
                                 "csrf_token": session["csrf_token"],
                                 "expires_at": session["expires_at"],
                                 "must_change_password": session.get("must_change_password", False)})
        response.set_cookie(SESSION_COOKIE, session["token"], max_age=auth.ttl_seconds,
                            httponly=True, samesite="strict", secure=auth.cookie_secure(request),
                            path="/")
        return response

    @app.post("/api/v1/auth/change-password")
    def auth_change_password(body: ChangePassword):
        """Self-service password rotation for a logged-in registered account.

        The operator account is env-backed (FACAI_ADMIN_PASSWORD) and is
        deliberately excluded: its credential must change at the env file, not
        here, so the two never silently diverge.
        """
        username = current_owner()
        changed, error = auth.change_password(username, body.current_password,
                                              body.new_password)
        if error == "not_a_platform_user":
            raise HTTPException(403, "管理员密码由环境变量管理，请勿在此修改")
        if error == "wrong_current_password":
            raise HTTPException(401, "当前密码不正确")
        if error == "weak_password":
            raise HTTPException(422, "新密码过于简单：至少 8 位，且不能是连续数字、重复字符或常见弱口令")
        return {"changed": True, "username": changed}

    class AdminResetPassword(BaseModel):
        username: str = Field(min_length=1, max_length=64)
        new_password: str = Field(min_length=1, max_length=256)

    @app.post("/api/v1/auth/admin-reset-password")
    def auth_admin_reset_password(body: AdminResetPassword):
        """Operator-forced password reset for a registered account.

        A weak target password is set but flags the account to rotate on next
        login, so a temporary hand-out password never lingers.
        """
        require_admin_owner()
        changed, error = auth.admin_reset_password(body.username, body.new_password)
        if error == "not_a_platform_user":
            raise HTTPException(404, "该账号不是平台注册账号")
        return {"changed": True, "username": changed,
                "must_change_password": auth.store.platform_user_must_change(body.username)}

    # --- visible assist sessions (协助模式) ---------------------------------------
    # The operator may work inside a customer's mission scope for a stated,
    # time-boxed reason -- visibly: the customer's /auth/session carries the
    # assist record, their console shows a persistent gold banner, and every
    # event written during the visit is stamped assist_by with the actual
    # operator. No customer session cookie is ever minted and no credential is
    # touched; the operator always acts through their own session with the
    # require_mission owner check widened for the window's duration only.

    ASSIST_TTL_SECONDS = 1800

    class AssistStart(BaseModel):
        reason: str = Field(min_length=1, max_length=500)

    @app.post("/api/v1/admin/assist/{username}", status_code=201)
    def start_assist(username: str, body: AssistStart):
        require_admin_owner()
        target = username.strip()
        reason = body.reason.strip()
        if not reason:
            # " " satisfies min_length; a blank statement of purpose is still no statement.
            raise HTTPException(422, "事由必填")
        if target == auth.username:
            raise HTTPException(422, "不能对自己的账号发起协助会话")
        if target not in auth.users:
            raise HTTPException(404, "该账号不存在")
        store.expire_assist_sessions()
        if store.active_assist_for_owner(target) is not None:
            raise HTTPException(409, "该客户已有进行中的协助会话，请先结束再发起新会话")
        return store.create_assist_session(auth.username, target, reason, ASSIST_TTL_SECONDS)

    @app.delete("/api/v1/admin/assist/{username}")
    def end_assist(username: str):
        require_admin_owner()
        target = username.strip()
        row = store.active_assist_for_owner(target)
        if row is None or row["admin_username"] != auth.username:
            raise HTTPException(404, "没有进行中的协助会话")
        store.end_assist_session(row["id"])
        return {"ended": True, "username": target}

    @app.post("/api/v1/auth/register", status_code=201)
    def auth_register(body: RegisterRequest, request: Request):
        """Self-service account creation for the paid five-user instance.

        Open at the transport layer like login, throttled by the same per-address
        limiter so a flood of signup attempts cannot enumerate or exhaust the
        account cap. Rejection reasons are fixed strings, never probes.
        """
        if not auth.enabled:
            raise HTTPException(403, "注册已关闭：本实例未启用认证")
        client_ip = request.client.host if request.client else "unknown"
        # Registration is a customer-facing front door: validation failures here
        # are product feedback (weak password, name taken), not brute-force
        # signals, so they never feed the per-IP lockout that guards login —
        # one NAT office testing passwords must not lock every future customer
        # out of signup.
        username, error = auth.register_user(body.username, body.password)
        if error == "user_limit_reached":
            raise HTTPException(403, "注册名额已满")
        if error is not None:
            reasons = {"invalid_username": "用户名需为 3-32 位字母、数字、下划线或连字符",
                       "reserved_username": "该用户名被保留",
                       "weak_password": "密码需至少 8 位，且不得为纯数字、连续或重复字符、常见弱口令",
                       "username_taken": "用户名已被使用"}
            raise HTTPException(409 if error == "username_taken" else 422,
                                reasons.get(error, "注册信息无效"))
        session, error = auth.login(username, body.password, client_ip)
        if error is not None or session is None:
            return {"registered": True, "username": username}
        response = JSONResponse({"registered": True, "authenticated": True,
                                 "username": session["username"],
                                 "csrf_token": session["csrf_token"],
                                 "expires_at": session["expires_at"]}, status_code=201)
        response.set_cookie(SESSION_COOKIE, session["token"], max_age=auth.ttl_seconds,
                            httponly=True, samesite="strict", secure=auth.cookie_secure(request),
                            path="/")
        return response

    @app.post("/api/v1/auth/logout")
    def auth_logout(request: Request):
        auth.logout(request)
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/v1/health")
    @app.get("/health")
    def health():
        return {"status": "ready", "phase": 2, "auth_required": auth.enabled}

    @app.get("/api/v1/daily-progress")
    def daily_progress():
        require_admin_owner()
        """Owner-only view of what each AI provider changed today and its evidence.

        Protected by the same session/Basic middleware as the dashboard. The
        payload is read from a versioned JSON file and every percentage is
        computed from that file's completed milestones, so no activity is
        invented and ``local_verified`` never implies ``production_deployed``.
        """
        try:
            return daily_progress_payload()
        except FileNotFoundError:
            raise HTTPException(404, "Daily progress data not found") from None
        except (OSError, ValueError) as exc:
            # Path detail is never surfaced; the file may not exist in a partial
            # deployment, which is a configuration state, not a provider failure.
            logger.warning("daily progress data unreadable: %s", type(exc).__name__)
            raise HTTPException(500, "Daily progress data unreadable") from None

    @app.get("/api/v1/missions")
    def missions():
        return [public_mission(record) for record in store.list(owner_id=current_owner())]

    @app.post("/api/v1/missions", status_code=201)
    def create_mission(body: MissionCreate):
        # Budget gate (per-user monthly limit): a spent owner cannot start a
        # NEW mission. 402 = payment required before more work is dispatched.
        gate = budget_gate(current_owner(), store)
        if not gate["allowed"]:
            raise HTTPException(402, gate["reason"])
        # Anti-resale throttle (反代转售审计): per-owner in-flight ceiling and
        # creation-rate window, each with its own 402/429 verdict.
        reserve_mission_create(current_owner())
        # A per-step model binding is optional, but when present it must name a
        # registered model so dispatch can resolve its OpenCode model ref.
        models = None
        for step in body.steps:
            if isinstance(step, dict) and step.get("model_id") is not None:
                if models is None:
                    models = registries.list("models", owner_id=current_owner())
                if step["model_id"] not in models:
                    raise HTTPException(422, f"Unknown model: {step['model_id']}")
        try:
            return store.create(body.title, body.original_input, body.steps, owner_id=current_owner())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    def dispatch_mission_jobs(mission_id):
        """Bind a valid default when missing, then enqueue one job per workflow step.

        The scheduler's dependency gate holds every job until its step's
        dependencies complete, so enqueueing the whole DAG up front is safe and
        independent branches start as soon as they are ready. Each job prompt
        scopes the shared professional prompt to one stage.
        """
        mission = store.get(mission_id)
        if mission is None:
            return {"status": "MISSING"}
        if store.jobs(mission_id):
            return {"status": "QUEUED", "already_dispatched": True}
        owner = mission["owner_id"]
        # Budget gate (per-user monthly limit): never enqueue the job loop once
        # the owner's month-to-date USD spend has reached their limit.
        gate = budget_gate(owner, store)
        if not gate["allowed"]:
            store.append(mission_id, "DISPATCH_DEFERRED",
                         {"reason": "BUDGET_EXCEEDED", "detail": gate["reason"]})
            return {"status": "DEFERRED", "reason": "BUDGET_EXCEEDED"}
        if not mission["agent_id"]:
            binding = default_binding(owner_id=owner)
            if binding is None:
                store.append(mission_id, "DISPATCH_DEFERRED",
                             {"reason": "NO_VALID_BINDING",
                              "detail": "缺少可用的 Agent / Model / Runtime 绑定"})
                return {"status": "DEFERRED", "reason": "NO_VALID_BINDING"}
            agent_id, model_id, runtime_id = binding
            store.bind(mission_id, agent_id=agent_id, model_id=model_id,
                       runtime_id=runtime_id, changed={"agent_id", "model_id", "runtime_id"},
                       reason="auto-default")
            mission = store.get(mission_id)
        agent = registries.resolve("agents", mission["agent_id"], owner_id=owner)
        model = registries.resolve("models", mission["model_id"], owner_id=owner)
        runtime = registries.resolve("runtimes", mission["runtime_id"], owner_id=owner)
        if not agent or not model or not runtime or agent.get("adapter") != "opencode":
            store.append(mission_id, "DISPATCH_DEFERRED",
                         {"reason": "INVALID_BINDING", "agent_id": mission["agent_id"],
                          "model_id": mission["model_id"], "runtime_id": mission["runtime_id"]})
            return {"status": "DEFERRED", "reason": "INVALID_BINDING"}
        base_prompt = mission["professional_prompt"] or mission["original_input"]
        job_ids = []
        for step in mission["steps"]:
            prompt = f"{base_prompt}\n\n本次只执行阶段：{step['label']}。"
            # A step-level model binding overrides the mission default for this
            # job's OpenCode --model flag; the scheduler keeps attribution honest.
            step_model_id = step.get("model_id")
            step_model_ref = None
            if step_model_id:
                step_model_ref = opencode_model_ref(
                    registries.resolve("models", step_model_id, owner_id=owner))
                if step_model_ref:
                    prompt += f"\n本阶段由模型 {step_model_id} 执行。"
            job = store.enqueue_job(
                mission_id, prompt,
                step_id=step["id"], model_ref=step_model_ref, priority=0, max_attempts=JOB_MAX_ATTEMPTS, timeout=300,
                dispatch={"agent_id": mission["agent_id"], "model_id": mission["model_id"],
                          "runtime_id": mission["runtime_id"], "step_id": step["id"]})
            job_ids.append(job["id"])
        return {"status": "QUEUED", "job_ids": job_ids}

    @app.post("/api/v1/missions/auto", status_code=201)
    async def create_auto_mission(body: AutoMissionCreate):
        """One plain-language sentence in, a dispatched step DAG out.

        The original wording is stored byte-for-byte. A bound model proposes the
        stage DAG and is strictly validated (labels unique, dependencies only
        reference earlier stages); any failure falls back to the deterministic
        five-phase linear plan, so creation never depends on the model.
        """
        owner = current_owner()
        # Budget gate (per-user monthly limit): check before any planning or
        # store.create so a spent owner never reaches the (paid) plan model.
        gate = budget_gate(owner, store)
        if not gate["allowed"]:
            raise HTTPException(402, gate["reason"])
        # Anti-resale throttle (反代转售审计): likewise before planning -- the
        # rate verdict must not burn a paid conversion call the owner is not
        # allowed to spend.
        reserve_mission_create(owner)
        plan = None
        model_id, model = bound_conversion_model(owner_id=owner)
        if model is not None:
            try:
                plan = await run_in_threadpool(
                    plan_with_bound_model, body.original_input, "web", model_id, model, complete)
            except Exception as exc:
                logger.warning("auto plan fell back to deterministic: %s", type(exc).__name__)
        if plan is None:
            plan = deterministic_plan(body.original_input)
        title = (body.title or plan.title or "自动任务")[:200]
        try:
            mission = store.create(title, body.original_input, list(plan.steps), owner_id=owner)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        mission_id = mission["id"]
        store.set_prompt(mission_id, plan.professional)
        conversion_payload = {
            "channel": "web", "original_input": body.original_input,
            "professional_prompt": plan.professional, "basis": list(plan.basis),
            "plan_source": plan.source,
            "plan_steps": [{"label": step["label"], "depends_on": step["depends_on"]}
                           for step in plan.steps],
        }
        if plan.model_id:
            conversion_payload["model_id"] = plan.model_id
        if plan.usage:
            conversion_payload["usage"] = plan.usage
        store.append(mission_id, "PROMPT_CONVERTED", conversion_payload)
        dispatch = dispatch_mission_jobs(mission_id)
        return {**public_mission(store.get(mission_id)), "dispatch": dispatch}

    @app.get("/api/v1/pricing")
    def pricing():
        """The operator's frozen price basis, used by the console's billing panel.

        The rates come from the same ``billing.PRICING`` table that prices the
        budget gate's estimate and /billing/me, so two screens can never quote
        different numbers. The env override moves only this displayed output
        rate; the gate's estimate keeps the frozen table.
        """
        default_rate = PRICING["output_per_mtok_usd"]
        try:
            rate = float(os.getenv("FACAI_PRICE_OUTPUT_USD_PER_MTOK", str(default_rate))
                         or default_rate)
        except ValueError:
            rate = default_rate
        # Budget (billing section): surface the current owner's effective
        # monthly limit next to the token rate; 0 means unlimited.
        return {"currency": "USD",
                "input_per_mtok_usd": PRICING["input_per_mtok_usd"],
                "output_usd_per_mtok": max(0.0, rate),
                "monthly_limit_usd": effective_monthly_limit(store, current_owner()),
                "basis": f"按运营方定价：输入 ${PRICING['input_per_mtok_usd']:g} / "
                         f"输出 ${max(0.0, rate):g} 每 100 万 tokens"}

    # --- per-user monthly budget (billing) --------------------------------------
    # NEW SECTION (budget enforcement): self-service monthly USD limits with
    # hard blocking of NEW dispatch. The gate itself lives in ``facai.billing``
    # and only compares spend already recorded in the ``costs`` table — no
    # price is ever estimated here.

    class BudgetUpdate(BaseModel):
        monthly_limit_usd: float = Field(ge=0)

    def channel_budget_deferred(channel, message_id, reason):
        """Audit and (best-effort) explain a budget-blocked channel message."""
        store.record_channel_event(channel, message_id, "BUDGET_EXCEEDED",
                                   {"reason": reason, "action": "DEFERRED"})
        provider = channels.get(channel)
        if provider is not None and provider.configured():
            sender = (store.channel_message(message_id) or {}).get("sender")
            if sender:
                try:
                    asyncio.run(provider.send(OutboundReply(
                        channel=channel, recipient=sender,
                        text=f"{reason}，新任务已暂缓。"
                             f"充值到账或提高月度额度后，重新发送消息即可继续。")))
                except Exception:
                    logger.warning("budget reply send failed for %s", channel,
                                   exc_info=True)
        return {"status": "DEFERRED", "reason": "BUDGET_EXCEEDED", "detail": reason,
                "message_id": message_id}

    @app.get("/api/v1/billing/me")
    def billing_me():
        """The current owner's month-to-date spend, limit, verdict and estimate.

        ``estimated_cost`` prices only the tokens the upstream reported, using
        the frozen operator ``PRICING``; it sits next to the recorded ledger and
        never replaces it. ``alert`` carries the once-per-billing-month 80%
        reminder when THIS call is the one that first crossed the threshold.
        """
        gate = budget_gate(current_owner(), store)
        return {"username": current_owner(), "allowed": gate["allowed"],
                "reason": gate["reason"], "monthly_limit_usd": gate["limit"],
                "spend": gate["spend"], "alert": gate.get("alert"),
                "pricing": {**PRICING, "currency": "USD", "unit": "USD / 1M tokens"},
                "estimated_cost": estimated_cost(gate["spend"])}

    @app.post("/api/v1/billing/check")
    def billing_check():
        """Dry-run of the same gate every dispatch path enforces."""
        return budget_gate(current_owner(), store)

    @app.get("/api/v1/billing/users/{username}/budget")
    def get_user_budget(username: str):
        # Reading a budget row is self-service (or the operator's); a tenant
        # must not learn another tenant's limit.
        if username != current_owner() and current_owner() != auth.username:
            raise HTTPException(403, "只能查看自己的预算")
        row = store.get_user_budget(username)
        return {"username": username,
                "monthly_limit_usd": float(row["monthly_limit_usd"]) if row
                else effective_monthly_limit(store, username),
                "explicit": row is not None}

    @app.put("/api/v1/billing/users/{username}/budget")
    def put_user_budget(username: str, body: BudgetUpdate):
        # The limit doubles as the credited balance (recharge approvals stack
        # onto it). The operator sets it to any value; a tenant may only LOWER
        # their own ceiling (self-restriction) — never raise above what the
        # operator granted or recharges credited, and 0 (unlimited) is
        # operator-only, or the budget gate would be void.
        if username != current_owner() and current_owner() != auth.username:
            raise HTTPException(403, "只能调整自己的预算")
        if current_owner() != auth.username:
            current = effective_monthly_limit(store, username)
            # current <= 0 means "no ceiling yet" — any positive value lowers it.
            if body.monthly_limit_usd <= 0 or (current > 0 and body.monthly_limit_usd > current):
                raise HTTPException(403, "自助预算只能下调（且必须大于 0）")
        try:
            row = store.set_user_budget(username, body.monthly_limit_usd)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return row

    # --- admin member console (会员分管面板) --------------------------------------
    # Operator-only roster and pool actions over the self-registered accounts
    # (platform_users). Every action is pure ORCHESTRATION of existing store
    # capabilities — top-up stacks exactly like a recharge approval, freeze is
    # an explicit 0 row (the budget gate then refuses dispatch with 402), and
    # unfreeze writes the operator-chosen amount back. No new money semantics
    # are invented here. The env-backed operator has no platform_users row, so
    # these member-scoped endpoints structurally cannot freeze or top-up the
    # operator themselves. Client API keys never reach /api/v1/admin/* (the
    # middleware scope list excludes it); everything here additionally passes
    # through require_admin_owner().

    class TopUpRequest(BaseModel):
        amount_usd: float = Field(gt=0, allow_inf_nan=False)

    class UnfreezeRequest(BaseModel):
        # 选简 (operator directive): the pre-freeze amount is NOT stored in a
        # frozen_prev column — unfreeze requires the operator to state the
        # restored balance explicitly.
        amount_usd: float = Field(gt=0, allow_inf_nan=False)

    def require_member(username):
        """404 for anything that is not a registered member (never the env operator)."""
        if not store.platform_user_exists(username):
            raise HTTPException(404, "会员不存在")

    @app.get("/api/v1/admin/members")
    def admin_members():
        """全部注册会员：额度、本月消费、绑定渠道、key 数与状态，新会员在前。"""
        require_admin_owner()
        return store.members_overview()

    @app.post("/api/v1/admin/members/{username}/top-up")
    def admin_member_top_up(username: str, body: TopUpRequest):
        """资金池官方入口：把金额叠加进该会员的 user_budgets 行。

        Same stacking semantics as recharge approval
        (Store.approve_recharge_and_credit): the amount is ADDED to the
        explicit row (a member without one starts stacking from 0), never a
        replacement — two top-ups of 5 must read 10, so the console can never
        silently overwrite a balance.
        """
        require_admin_owner()
        require_member(username)
        budget = store.get_user_budget(username)
        current = float(budget["monthly_limit_usd"]) if budget else 0.0
        row = store.set_user_budget(username, current + body.amount_usd)
        return {"username": username, "added_usd": body.amount_usd,
                "monthly_limit_usd": row["monthly_limit_usd"]}

    @app.post("/api/v1/admin/members/{username}/freeze")
    def admin_member_freeze(username: str):
        """冻结：预算行写 0，余额池语义立刻拒绝一切新派发（402）。"""
        require_admin_owner()
        require_member(username)
        row = store.set_user_budget(username, 0.0)
        return {"username": username, "frozen": True,
                "monthly_limit_usd": row["monthly_limit_usd"]}

    @app.post("/api/v1/admin/members/{username}/unfreeze")
    def admin_member_unfreeze(username: str, body: UnfreezeRequest):
        """解冻：操作员显式指定恢复后的额度（必填），只对冻结中的会员有效。"""
        require_admin_owner()
        require_member(username)
        budget = store.get_user_budget(username)
        if budget is None or float(budget["monthly_limit_usd"]) != 0.0:
            raise HTTPException(409, "该会员未处于冻结状态")
        row = store.set_user_budget(username, body.amount_usd)
        return {"username": username, "frozen": False,
                "monthly_limit_usd": row["monthly_limit_usd"]}

    @app.get("/api/v1/missions/{mission_id}")
    def get_mission(mission_id: str):
        return public_mission(require_mission(mission_id))

    @app.patch("/api/v1/missions/{mission_id}/steps/{step_id}")
    def update_step(mission_id: str, step_id: str, body: StepUpdate):
        require_mission(mission_id)
        try:
            return store.step_status(mission_id, step_id, body.status)
        except KeyError:
            raise HTTPException(404, "Step not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/v1/missions/{mission_id}/events")
    def events(mission_id: str, after: int = 0):
        require_mission(mission_id)
        return [public_event(event) for event in store.events(mission_id, after)]

    @app.get("/api/v1/missions/{mission_id}/proof")
    def mission_proof(mission_id: str):
        # 执行证据链：只从持久层（事件流 + 表）组装，绝不读进程内存里的
        # 授权引擎状态；owner 校验与本 mission 的其他只读端点完全一致。
        require_mission(mission_id)
        proof = build_proof(store, mission_id)
        proof["artifacts"] = [public_artifact(artifact)
                              for artifact in proof.get("artifacts") or ()]
        return proof

    @app.post("/api/v1/missions/{mission_id}/events", status_code=201)
    def add_event(mission_id: str, body: EventCreate):
        require_mission(mission_id)
        try:
            event_id = store.append(mission_id, body.type, body.payload,
                                    body.step_id, body.session_id, body.agent_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"id": event_id}

    @app.get("/api/v1/missions/{mission_id}/stream")
    async def stream(mission_id: str, after: int = 0):
        require_mission(mission_id)

        async def generate():
            cursor = after
            while True:
                for event in store.events(mission_id, cursor):
                    cursor = event["sequence"]
                    yield f"id: {cursor}\nevent: mission\ndata: {json.dumps(public_event(event), ensure_ascii=False)}\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(generate(), media_type="text/event-stream")

    # --- ICS 事件总线（2026-10-13 平台化指令）：全端订阅同一条 owner 级流 ----
    # 多端同步的地基：手机/电脑/IM 都读这一条，不各自轮询单任务。

    @app.get("/api/v1/events")
    def eventbus_events(after: int = 0):
        """Durable replay: every event of every mission this owner owns."""
        return {"events": [public_event(event)
                           for event in store.events_for_owner(current_owner(), after)]}

    @app.get("/api/v1/events/stream")
    async def eventbus_stream(after: int = 0):
        """Live subscription: the same global sequence as /api/v1/events,
        pushed as SSE with the per-mission stream's cursor contract."""
        owner = current_owner()

        async def generate():
            cursor = after
            while True:
                for event in store.events_for_owner(owner, cursor):
                    cursor = event["sequence"]
                    yield (f"id: {cursor}\nevent: mission\ndata: "
                           f"{json.dumps(public_event(event), ensure_ascii=False)}\n\n")
                await asyncio.sleep(1)

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/api/v1/registries/{kind}")
    def list_registry(kind: str):
        if kind not in registries.KINDS:
            raise HTTPException(404, "Registry not found")
        # Credentials are never returned to the console.
        return {key: public_registry_value(value)
                for key, value in registries.list(kind, owner_id=current_owner()).items()}

    # --- client-supplied models (my models) ---------------------------------
    # Clients register their own endpoint + API key. The key is encrypted at
    # rest (secretbox), decrypted only in memory at use time, never returned
    # (masked tail only), never logged and never placed on a command line.

    @app.post("/api/v1/my/models", status_code=201)
    def register_user_model(body: UserModelCreate):
        owner = current_owner()
        try:
            endpoint = secretbox.validate_public_endpoint(body.endpoint)
            encrypted = secretbox.encrypt(root, body.api_key)
        except SecretBoxError as exc:
            raise HTTPException(422, str(exc)) from None
        existing = [m for m in store.user_models(owner) if m["name"] == body.name]
        if len(store.user_models(owner)) >= 20 and not existing:
            raise HTTPException(409, "每个账号最多登记 20 个自有模型")
        model_id = store.register_user_model(owner, body.name, endpoint, body.model, encrypted)
        registries.register("models", f"user-{model_id}", {
            "name": body.name, "endpoint": endpoint, "model": body.model,
            "provider": "facai-user", "user_model_id": model_id,
            "opencode_model": f"facai-user/{body.model}", "source": "client",
        }, owner_id=owner)
        return {"id": model_id, "name": body.name, "endpoint": endpoint,
                "model": body.model, "api_key_masked": secretbox.mask(body.api_key)}

    @app.get("/api/v1/my/models")
    def list_user_models():
        owner = current_owner()
        rows = []
        for m in store.user_models(owner):
            try:
                masked = secretbox.mask(secretbox.decrypt(root, m["api_key_encrypted"]))
            except SecretBoxError:
                masked = "****"
            rows.append({"id": m["id"], "name": m["name"], "endpoint": m["endpoint"],
                         "model": m["model"], "created_at": m["created_at"],
                         "api_key_masked": masked})
        return rows

    @app.delete("/api/v1/my/models/{model_id}")
    def delete_user_model(model_id: str):
        owner = current_owner()
        if not store.delete_user_model(owner, model_id):
            raise HTTPException(404, "Model not found")
        registries.delete("models", f"user-{model_id}", owner_id=owner)
        return {"deleted": model_id}

    @app.post("/api/v1/my/models/{model_id}/test")
    async def test_user_model(model_id: str):
        """Probe one client-supplied model with its stored credential."""
        owner = current_owner()
        row = store.user_model(owner, model_id)
        if row is None:
            raise HTTPException(404, "Model not found")
        token = store.user_model_encrypted_key(owner, model_id)
        try:
            api_key = secretbox.decrypt(root, token)
        except SecretBoxError as exc:
            raise HTTPException(422, str(exc)) from None
        model = {"name": row["name"], "endpoint": row["endpoint"], "model": row["model"],
                 "api_key": api_key}
        cooldown = reserve_model_probe(owner)
        if cooldown is not None:
            raise HTTPException(429, f"连接测试冷却中，请 {int(cooldown)+1} 秒后重试")
        result = await complete(model, [{"role": "user", "content": MODEL_TEST_PROMPT}],
                                rotation=app.state.key_rotation)
        return {"status": result.get("status"), "label": result.get("label"),
                "http_status": result.get("http_status"),
                "usage": result.get("usage"), "model": row["name"]}

    # --- client API keys (my keys) --------------------------------------------
    # A client mints keys for their own automation. The plaintext is returned
    # once, at creation; the console and every listing show only the stored
    # prefix. Keys are managed with a console session (a key may not mint or
    # revoke keys), scoped by the middleware and revoked individually -- never
    # by rotating the account password.

    @app.post("/api/v1/my/keys", status_code=201)
    def create_api_key():
        owner = current_owner()
        live = [k for k in store.api_keys_for(owner) if not k["revoked_at"]]
        if len(live) >= MAX_API_KEYS_PER_OWNER:
            raise HTTPException(409, f"每个账号最多 {MAX_API_KEYS_PER_OWNER} 个有效 API key")
        return auth.issue_api_key(owner)

    @app.get("/api/v1/my/keys")
    def list_api_keys():
        return store.api_keys_for(current_owner())

    @app.delete("/api/v1/my/keys/{key_id}")
    def revoke_api_key(key_id: str):
        if not store.revoke_api_key(current_owner(), key_id):
            raise HTTPException(404, "API key not found")
        return {"revoked": key_id}

    # --- customer channel bindings (my channels) ------------------------------
    # 每客户通道默认持久：a customer binds the Telegram bot THEY created (their
    # own BotFather bot, never the station's) plus the chat that receives
    # progress. The token is encrypted at rest with the same secretbox as user
    # model keys, decrypted only in memory when the NOTIFY loop sends that
    # owner's progress, and never returned by any API. Unbinding falls back to
    # the station-level channel, whose behavior is unchanged. Binding first
    # runs the real credential preflight (preflight_channel_credential): a
    # well-formed token is not a working one, so the upstream must confirm it
    # before anything is stored.

    @app.put("/api/v1/my/channels/telegram")
    async def bind_my_telegram(body: ChannelBindingCreate):
        owner = current_owner()
        token = body.bot_token.strip()
        chat_id = body.chat_id.strip()
        if not re.fullmatch(TELEGRAM_BOT_TOKEN_PATTERN, token):
            raise HTTPException(
                422, "bot_token 格式不合法（形如 123456:ABC-DEF…，请从 BotFather 复制完整 token）")
        if not re.fullmatch(TELEGRAM_CHAT_ID_PATTERN, chat_id):
            raise HTTPException(422, "chat_id 必须为数字（群聊为负数）或 @频道用户名")
        # 落库前真实预检：getMe 验证通过才绑定（格式合法 ≠ token 有效，签发权在 Owner 核）。
        await preflight_channel_credential("telegram", token)
        try:
            encrypted = secretbox.encrypt(root, token)
        except SecretBoxError as exc:
            raise HTTPException(422, str(exc)) from None
        binding = store.upsert_channel_binding(owner, "telegram", encrypted, chat_id)
        return {"channel": "telegram", "bound": True, "chat_id": binding["chat_id"],
                "created_at": binding["created_at"]}

    @app.get("/api/v1/my/channels")
    def list_my_channels():
        owner = current_owner()
        channels = {}
        for channel in CUSTOMER_BINDABLE_CHANNELS:
            binding = store.channel_binding(owner, channel)
            channels[channel] = {"bound": binding is not None,
                                 "chat_id": binding["chat_id"] if binding else None,
                                 "created_at": binding["created_at"] if binding else None}
        return channels

    @app.delete("/api/v1/my/channels/telegram")
    def unbind_my_telegram():
        owner = current_owner()
        if not store.delete_channel_binding(owner, "telegram"):
            raise HTTPException(404, "Telegram 未绑定")
        return {"channel": "telegram", "bound": False}

    # LINE binding: the access token and channel secret are sealed together as
    # ONE secretbox ciphertext (JSON) -- push needs only the access token, the
    # secret rides along for future webhook verification. The binding row's
    # chat_id column carries the LINE userId (its per-channel meaning, exactly
    # as it carries telegram's chat id); the NOTIFY loop decrypts at send time
    # and falls back to the station channel for any owner without a binding.
    @app.put("/api/v1/my/channels/line")
    async def bind_my_line(body: LineBindingCreate):
        owner = current_owner()
        access_token = body.access_token.strip()
        channel_secret = body.channel_secret.strip()
        user_id = body.user_id.strip()
        if not re.fullmatch(LINE_ACCESS_TOKEN_PATTERN, access_token):
            raise HTTPException(
                422, "access_token 格式不合法（请从 LINE Developers 控制台复制完整的 Channel access token）")
        if not re.fullmatch(LINE_CHANNEL_SECRET_PATTERN, channel_secret):
            raise HTTPException(
                422, "channel_secret 格式不合法（Channel secret 用于将来 webhook 验签）")
        if not re.fullmatch(LINE_USER_ID_PATTERN, user_id):
            raise HTTPException(
                422, "user_id 必须是 LINE 用户 ID（U 加 32 位十六进制，即 webhook 里的 source.userId）")
        # 落库前真实预检：/v2/bot/info 验证 access_token（与 telegram 同构的失败
        # 语义；channel_secret 只随 token 一起密封，无法独立向上游验证）。
        await preflight_channel_credential("line", access_token)
        try:
            sealed = secretbox.encrypt(root, json.dumps(
                {"access_token": access_token, "channel_secret": channel_secret}))
        except SecretBoxError as exc:
            raise HTTPException(422, str(exc)) from None
        binding = store.upsert_channel_binding(owner, "line", sealed, user_id)
        return {"channel": "line", "bound": True, "chat_id": binding["chat_id"],
                "created_at": binding["created_at"]}

    @app.delete("/api/v1/my/channels/line")
    def unbind_my_line():
        owner = current_owner()
        if not store.delete_channel_binding(owner, "line"):
            raise HTTPException(404, "LINE 未绑定")
        return {"channel": "line", "bound": False}

    # Discord/Slack bindings reuse the telegram shape: the customer's own bot
    # token plus the push target. The binding row's chat_id column carries that
    # per-channel target -- a Discord channel snowflake, a Slack channel id --
    # exactly as it carries telegram's chat id and LINE's userId.
    @app.put("/api/v1/my/channels/discord")
    async def bind_my_discord(body: ChannelBindingCreate):
        owner = current_owner()
        token = body.bot_token.strip()
        channel_id = body.chat_id.strip()
        if not re.fullmatch(DISCORD_BOT_TOKEN_PATTERN, token):
            raise HTTPException(
                422, "bot_token 格式不合法（请从 Discord Developer Portal → Bot 页复制完整 Bot Token）")
        if not re.fullmatch(DISCORD_CHANNEL_ID_PATTERN, channel_id):
            raise HTTPException(
                422, "chat_id 必须是 Discord 频道 ID（开发者模式 → 右键频道 → 复制频道 ID）")
        # 落库前真实预检：/users/@me 验证 bot token（格式合法 ≠ token 有效）。
        await preflight_channel_credential("discord", token)
        try:
            encrypted = secretbox.encrypt(root, token)
        except SecretBoxError as exc:
            raise HTTPException(422, str(exc)) from None
        binding = store.upsert_channel_binding(owner, "discord", encrypted, channel_id)
        return {"channel": "discord", "bound": True, "chat_id": binding["chat_id"],
                "created_at": binding["created_at"]}

    @app.delete("/api/v1/my/channels/discord")
    def unbind_my_discord():
        owner = current_owner()
        if not store.delete_channel_binding(owner, "discord"):
            raise HTTPException(404, "Discord 未绑定")
        return {"channel": "discord", "bound": False}

    @app.put("/api/v1/my/channels/slack")
    async def bind_my_slack(body: ChannelBindingCreate):
        owner = current_owner()
        token = body.bot_token.strip()
        channel_id = body.chat_id.strip()
        if not re.fullmatch(SLACK_BOT_TOKEN_PATTERN, token):
            raise HTTPException(
                422, "bot_token 格式不合法（请从 Slack api.apps.com → OAuth & Permissions 复制以 xoxb- 开头的 Bot Token）")
        if not re.fullmatch(SLACK_CHANNEL_ID_PATTERN, channel_id):
            raise HTTPException(
                422, "chat_id 必须是 Slack 频道 ID（频道详情里以 C 开头的 ID，或成员 ID DM 推送也接受）")
        # 落库前真实预检：auth.test 验证 bot token（Slack 的鉴权失败是 200 +
        # ok:false，预检读的是响应体，不是状态码）。
        await preflight_channel_credential("slack", token)
        try:
            encrypted = secretbox.encrypt(root, token)
        except SecretBoxError as exc:
            raise HTTPException(422, str(exc)) from None
        binding = store.upsert_channel_binding(owner, "slack", encrypted, channel_id)
        return {"channel": "slack", "bound": True, "chat_id": binding["chat_id"],
                "created_at": binding["created_at"]}

    @app.delete("/api/v1/my/channels/slack")
    def unbind_my_slack():
        owner = current_owner()
        if not store.delete_channel_binding(owner, "slack"):
            raise HTTPException(404, "Slack 未绑定")
        return {"channel": "slack", "bound": False}

    # 绑定渠道「测试推送」：客户验证自己绑定的 bot 链路是否真的通。与 NOTIFY
    # 进度推送走完全相同的信任路径（notify.owner_bot 原地解密 → 出站专用
    # provider），只是把结果如实返给客户：adapter 的稳定错误码（AUTH_FAILED /
    # RATE_LIMITED / TEXT_TOO_LONG / TIMEOUT / TRANSPORT_ERROR / …）原样透出，
    # 凭据与异常文本（可能携带带 token 的上游 URL）绝不进响应。诚实口径：
    # DELIVERED 只能来自 adapter 确认的上游 2xx + 具体消息 id；审计与其它
    # 出站聊天行同轨（text only，never a credential）。
    @app.post("/api/v1/my/channels/{channel}/test")
    async def test_my_channel(channel: str):
        owner = current_owner()
        if channel not in CUSTOMER_BINDABLE_CHANNELS:
            raise HTTPException(404, "Channel not bindable")
        if store.channel_binding(owner, channel) is None:
            raise HTTPException(409, "该渠道尚未绑定，请先完成绑定")
        bot = owner_bot(store, channel, owner)
        if bot is None:
            # 绑定行在、凭证已不可用（解不开/载荷损坏）：绝不假装已送达，
            # 用稳定码如实报告——该码与 adapter 错误词汇表一样不携带秘密。
            return {"channel": channel, "status": "FAILED",
                    "error": "BINDING_UNUSABLE"}
        provider, recipient = bot
        try:
            result = await provider.send(OutboundReply(
                channel=channel, recipient=recipient, text=TEST_PUSH_TEXT))
        except Exception:
            logger.warning("channel test push crashed for %s/%s",
                           owner, channel, exc_info=True)
            result = None
        if not isinstance(result, dict) or not result.get("status"):
            return {"channel": channel, "status": "FAILED",
                    "error": "SEND_FAILED"}
        # 与 NOTIFY 出站行同轨审计：text only，never a credential；best-effort
        # ——审计失败不影响本次测试结果本身。
        try:
            if not getattr(provider, "records_outbound_itself", False):
                store.record_channel_message(
                    channel, "OUTBOUND", result.get("status", "FAILED"),
                    external_id=result.get("external_id"), recipient=recipient,
                    text=TEST_PUSH_TEXT, owner_id=owner)
        except Exception:
            logger.warning("channel test push audit failed for %s/%s",
                           owner, channel, exc_info=True)
        return {"channel": channel, "status": result["status"],
                "error": result.get("error")}

    # --- channel hosting (my channels, hosted personal channels) ---------------
    # docs/CHANNEL_HOST_DESIGN.md: wave 1 shipped the schema + query layer +
    # self-service endpoints; wave 2 (this file's per-owner runtime section)
    # added the isolated per-owner WeChatProvider instances, the QR-confirm
    # (BINDING→ACTIVE) poll endpoints and one monitor task per ACTIVE binding.
    # Every endpoint below operates ONLY on current_owner()'s own
    # channel_host_bindings row and the owner's own credential namespace --
    # no admin guard needed because nothing here touches station state.

    def _host_namespace(owner, channel):
        """The owner's credential namespace, RELATIVE to the data root.

        The store keeps the relative path so the database never pins down where
        the data directory lives on disk; owner usernames are [A-Za-z0-9_-]
        (facai/auth.py), so the path needs no escaping.
        """
        return f"providers/hosting/{owner}/{channel}"

    def _require_host_channel(channel):
        if channel not in HOST_CHANNELS:
            raise HTTPException(404, "Channel not hostable")

    @app.post("/api/v1/my/channels/wechat/host/start")
    async def start_my_wechat_host():
        owner = current_owner()
        _require_host_channel("wechat")
        existing = store.host_binding(owner, "wechat")
        if existing and existing["state"] == "ACTIVE":
            # One active hosting per channel: stop first, then rebind.
            raise HTTPException(409, "微信宿主已激活，请先解绑再重新绑定")
        # Wave 2: the QR is served by the OWNER'S OWN provider instance
        # (credential file under providers/hosting/<owner>/), not the station
        # singleton -- a customer starting a scan no longer supersedes the
        # operator's in-flight QR, and the later confirmation writes the
        # owner's credential file, never the station's providers/wechat.env
        # (CHANNEL_HOST_DESIGN.md §3 boundary eliminated).
        provider = _host_wechat_provider(owner)
        result = await provider.start_login()
        if result.get("state") == "FAILED":
            # A failed QR start must not leave a BINDING row claiming a session
            # that does not exist.
            raise HTTPException(502, result.get("error", "WECHAT_LOGIN_FAILED"))
        binding = store.transition_host_binding(
            owner, "wechat", "BINDING", session_dir=_host_namespace(owner, "wechat"),
            display_name="个人微信")
        # Defensive: a monitor from a previous ACTIVE life of this row must not
        # outlive the rebind (the stop endpoint normally cancelled it already).
        stop_host_wechat_monitor(owner)
        if existing is None or existing["state"] == "UNBOUND":
            store.record_channel_event("wechat", f"host:{owner}", "HOST_BINDING_STARTED",
                                       {"owner_id": owner, "channel": "wechat",
                                        "state": binding["state"]})
        # The QR dict is returned exactly as the provider built it (state +
        # data:image/png base64) -- no reshaping that could quietly drop fields.
        return {"channel": "wechat", "state": binding["state"],
                "session_dir": binding["session_dir"],
                "created_at": binding["created_at"], "qr": result}

    @app.get("/api/v1/my/channels/wechat/host/login")
    async def poll_my_wechat_host_login():
        """Poll the owner's own QR login; on confirmation bind it ACTIVE.

        Confirmation writes the credential ONLY into the owner's namespace
        (providers/hosting/<owner>/wechat.env via the provider instance), moves
        the row BINDING -> ACTIVE, audits HOST_BINDING_ACTIVATED and starts the
        owner's monitor task. A missing or non-BINDING row is a 409: there is
        no in-flight scan to poll, and nothing may be activated out of order.
        """
        owner = current_owner()
        _require_host_channel("wechat")
        binding = store.host_binding(owner, "wechat")
        if binding is None or binding["state"] != "BINDING":
            raise HTTPException(409, "当前没有进行中的微信扫码绑定")
        provider = _host_wechat_provider(owner)
        result = await provider.poll_login()
        if result.get("state") == "AUTHORIZED":
            row = store.transition_host_binding(owner, "wechat", "ACTIVE")
            store.record_channel_event(
                "wechat", f"host:{owner}", "HOST_BINDING_ACTIVATED",
                {"owner_id": owner, "channel": "wechat",
                 "account": result.get("account"), "method": "QR"})
            start_host_wechat_monitor(owner)
            return {"channel": "wechat", "state": row["state"],
                    "session_dir": row["session_dir"], "login": result}
        return {"channel": "wechat", "state": binding["state"], "login": result}

    @app.post("/api/v1/my/channels/wechat/host/login/verify")
    def verify_my_wechat_host_login(body: WeChatVerifyCode):
        """Submit the verify code for the owner's own in-flight QR (need_verifycode)."""
        owner = current_owner()
        _require_host_channel("wechat")
        binding = store.host_binding(owner, "wechat")
        if binding is None or binding["state"] != "BINDING":
            raise HTTPException(409, "当前没有进行中的微信扫码绑定")
        provider = _host_wechat_provider(owner)
        try:
            return provider.submit_verify_code(body.code)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    # --- whatsapp pairing-code host (免扫码绑定) -------------------------------
    # CHANNEL_HOST_DESIGN.md wave 3: the customer submits their own WhatsApp
    # number; the OWNER'S OWN Baileys session on the VPS bridge
    # (bridge/whatsapp-relay) mints a single-use 8-digit pairing code which the
    # customer types on their phone (设置 -> 已链接的设备 -> 用手机号链接). The
    # bridge signs its admin API with the SAME RELAY_SHARED_SECRET the relay
    # channel already shares, so no second secret exists; the bridge URL is
    # plain deployment config. The client is built lazily from the process env
    # and cached on app.state for tests and ops introspection.
    app.state.host_whatsapp_bridge = None

    def _whatsapp_host_bridge():
        bridge = getattr(app.state, "host_whatsapp_bridge", None)
        if bridge is None:
            try:
                relay = channels.get("relay")
                relay_secret = relay.setting("RELAY_SHARED_SECRET") if relay else ""
            except AttributeError:  # stub registries in tests may lack get()
                relay_secret = ""
            bridge = WhatsAppHostBridge(env={"RELAY_SHARED_SECRET": relay_secret},
                                        base_url=os.getenv("FACAI_WHATSAPP_BRIDGE_URL", ""))
            app.state.host_whatsapp_bridge = bridge
        return bridge

    @app.post("/api/v1/my/channels/whatsapp/host/start")
    async def start_my_whatsapp_host(body: WhatsAppPairRequest):
        """Mint a pairing code in the owner's own bridge session; row -> BINDING.

        Fail-closed like the wechat QR start: a bridge failure raises 502 and
        leaves NO row behind (a BINDING row would claim an in-flight pairing
        that does not exist). The code answer is passed through VERBATIM and
        never cached — Baileys pairing codes are single-use and short-lived.
        ACTIVE is never reached here: only the login poll may confirm a link.
        """
        owner = current_owner()
        _require_host_channel("whatsapp")
        try:
            phone = normalize_pairing_phone(body.phone)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        bridge = _whatsapp_host_bridge()
        if not bridge.configured():
            raise HTTPException(503, "WhatsApp 配对服务未配置，请联系管理员")
        existing = store.host_binding(owner, "whatsapp")
        if existing and existing["state"] == "ACTIVE":
            # One active hosting per channel: stop first, then rebind.
            raise HTTPException(409, "WhatsApp 宿主已激活，请先解绑再重新绑定")
        try:
            result = await bridge.start_pairing(owner, phone)
        except WhatsAppBridgeError as exc:
            raise HTTPException(502, f"配对服务暂不可用（{exc.reason}）") from None
        pairing_state = result.get("state")
        if pairing_state == "LINKED":
            # The bridge already has this owner live-linked; if it is a
            # DIFFERENT number than the one just submitted, answering with a
            # fresh code (or silently binding) would lie about what got linked.
            linked_phone = str(result.get("phone") or "")
            if linked_phone and linked_phone != phone:
                raise HTTPException(
                    409, "该账号的桥接会话已链接到其他号码，请先解绑再重新配对")
        binding = store.transition_host_binding(
            owner, "whatsapp", "BINDING", session_dir=_host_namespace(owner, "whatsapp"),
            display_name="个人 WhatsApp")
        if existing is None or existing["state"] == "UNBOUND":
            store.record_channel_event("whatsapp", f"host:{owner}", "HOST_BINDING_STARTED",
                                       {"owner_id": owner, "channel": "whatsapp",
                                        "state": binding["state"]})
        return {"channel": "whatsapp", "state": binding["state"],
                "session_dir": binding["session_dir"],
                "created_at": binding["created_at"],
                "pairing": {"state": pairing_state, "code": result.get("code"),
                            "phone": phone}}

    @app.get("/api/v1/my/channels/whatsapp/host/login")
    async def poll_my_whatsapp_host_login():
        """Poll the owner's pairing session; a live LINKED bridge session binds ACTIVE.

        Mirrors the wechat QR path exactly: ACTIVE is only ever reached here,
        on the bridge's own confirmation that the owner's session is linked —
        a pairing code answer never moves the row. A poll failure (bridge
        unreachable) leaves BINDING untouched; there is no in-flight pairing
        to poll without a BINDING row (409).
        """
        owner = current_owner()
        _require_host_channel("whatsapp")
        binding = store.host_binding(owner, "whatsapp")
        if binding is None or binding["state"] != "BINDING":
            raise HTTPException(409, "当前没有进行中的 WhatsApp 配对绑定")
        bridge = _whatsapp_host_bridge()
        if not bridge.configured():
            raise HTTPException(503, "WhatsApp 配对服务未配置，请联系管理员")
        try:
            result = await bridge.session_status(owner)
        except WhatsAppBridgeError as exc:
            raise HTTPException(502, f"配对服务暂不可用（{exc.reason}）") from None
        if result.get("state") == "LINKED":
            row = store.transition_host_binding(owner, "whatsapp", "ACTIVE")
            store.record_channel_event(
                "whatsapp", f"host:{owner}", "HOST_BINDING_ACTIVATED",
                {"owner_id": owner, "channel": "whatsapp",
                 "account": result.get("phone"), "method": "PAIRING_CODE"})
            return {"channel": "whatsapp", "state": row["state"],
                    "session_dir": row["session_dir"], "login": result}
        # PAIRING (code may or may not be fresh) or NONE (session evaporated —
        # the customer can simply start again): the row stays BINDING either way.
        return {"channel": "whatsapp", "state": binding["state"], "login": result}

    @app.get("/api/v1/my/channels/{channel}/host/status")
    def my_host_status(channel: str):
        owner = current_owner()
        _require_host_channel(channel)
        binding = store.host_binding(owner, channel)
        if binding is None:
            return {"channel": channel, "state": "NONE", "session_dir": None,
                    "display_name": None, "created_at": None, "updated_at": None}
        return {"channel": channel, "state": binding["state"],
                "session_dir": binding["session_dir"],
                "display_name": binding["display_name"],
                "created_at": binding["created_at"], "updated_at": binding["updated_at"]}

    @app.post("/api/v1/my/channels/{channel}/host/stop")
    async def stop_my_host(channel: str):
        owner = current_owner()
        _require_host_channel(channel)
        bridge = _whatsapp_host_bridge() if channel == "whatsapp" else None
        binding = store.host_binding(owner, channel)
        if binding is None:
            raise HTTPException(404, "该通道尚未开始宿主绑定")
        bridge_logout = None
        if binding["state"] != "UNBOUND":
            binding = store.transition_host_binding(
                owner, channel, "UNBOUND", session_dir=binding["session_dir"],
                display_name=binding["display_name"])
            # WhatsApp: stopping is a REAL revoke on the bridge — the owner's
            # Baileys session is server-side unpaired and its credentials
            # wiped, so nothing keeps silently relinking the customer's number
            # after they stop hosting. Best-effort and audited: the row still
            # goes UNBOUND even when the bridge is unreachable (Facai's state
            # is honest; the audit names the hygiene outcome).
            if bridge is not None:
                if not bridge.configured():
                    bridge_logout = "NOT_CONFIGURED"
                else:
                    try:
                        await bridge.logout(owner)
                        bridge_logout = "OK"
                    except WhatsAppBridgeError as exc:
                        bridge_logout = f"FAILED:{exc.reason}"
            payload = {"owner_id": owner, "channel": channel,
                       "state": binding["state"]}
            if bridge_logout is not None:
                payload["bridge_logout"] = bridge_logout
            store.record_channel_event(channel, f"host:{owner}", "HOST_BINDING_STOPPED",
                                       payload)
        # Stop condition 1 of the monitor: an UNBOUND row ends the owner's poll
        # loop. Cancellation needs the event loop thread, hence the async
        # endpoint; with no task running this is an honest no-op. The WeChat
        # credential file is NOT deleted here -- file cleanup on revoke is a
        # later wave (docs/CHANNEL_HOST_DESIGN.md §5); the unmonitored file
        # stays inside the owner's own namespace either way. WhatsApp differs
        # by necessity (logged out and wiped on the bridge, above): a dormant
        # Baileys credential would keep a LIVE link to the customer's phone.
        stop_host_wechat_monitor(owner)
        # Idempotent: stopping an already-UNBOUND row returns it unchanged.
        return {"channel": channel, "state": binding["state"],
                "session_dir": binding["session_dir"],
                "created_at": binding["created_at"], "updated_at": binding["updated_at"]}

    # --- USDT (TRC20) recharge ------------------------------------------------
    # 收费以 USDT 为准：单地址冷钱包、单笔最低 $100、支付窗口 5 分钟。
    # 地址只从环境变量读取（FACAI_USDT_TRC20_ADDRESS），永不写进仓库。

    @app.get("/api/v1/billing/recharge/config")
    def recharge_config():
        # Always 200: the console polls this on boot and a 503 would land in
        # the browser error log. `enabled` carries the honest state instead.
        address = os.getenv("FACAI_USDT_TRC20_ADDRESS", "").strip()
        return {"enabled": bool(address), "network": "TRC20", "address": address,
                "min_usd": RECHARGE_MIN_USD, "window_seconds": RECHARGE_WINDOW_SECONDS}

    @app.post("/api/v1/billing/recharge", status_code=201)
    def create_recharge(body: dict):
        owner = current_owner()
        try:
            amount = round(float(body.get("amount_usd")), 2)
            if amount != amount or amount in (float("inf"), float("-inf")):
                raise HTTPException(422, "金额不合法")
        except (TypeError, ValueError):
            raise HTTPException(422, "amount_usd 必须是数字") from None
        if amount < RECHARGE_MIN_USD:
            raise HTTPException(422, f"单笔最低 {RECHARGE_MIN_USD:.0f} USDT")
        if amount > 1_000_000:
            raise HTTPException(422, "单笔金额异常，请联系管理员")
        row = store.create_recharge(owner, amount, RECHARGE_WINDOW_SECONDS)
        return {"id": row["id"], "amount_usd": row["amount_usd"],
                "network": "TRC20",
                "address": os.getenv("FACAI_USDT_TRC20_ADDRESS", "").strip(),
                "created_at": row["created_at"], "expires_at": row["expires_at"]}

    @app.get("/api/v1/billing/deposit-address")
    def billing_deposit_address():
        """The platform's USDT-TRC20 receiving address for recharges.

        Public info for logged-in customers (it is where they send money);
        unset means the operator has not configured chain sync yet, so we say
        so honestly instead of inventing an address.
        """
        address = (os.getenv("FACAI_USDT_TRC20_ADDRESS") or "").strip()
        return {"channel": "USDT-TRC20", "address": address or None,
                "note": "" if address else "充值地址未配置，请联系站长"}

    @app.get("/api/v1/billing/recharges/mine")
    def my_recharges():
        return store.recharges_for(current_owner())

    @app.get("/api/v1/billing/recharges/{recharge_id}/match")
    def recharge_chain_match(recharge_id: str):
        """Real-time recharge timeliness: has the user's txid hit the chain yet?

        Owner-scoped (admin sees any). The console polls this so the payer sees
        「链上已检测到 ✓」 within one sync interval (~30s) instead of waiting
        blind for an admin approval.
        """
        from datetime import datetime, timezone
        row = store.get_recharge(recharge_id)
        if row is None:
            raise HTTPException(404, "Recharge not found")
        if row["username"] != current_owner() and auth.username != current_owner():
            raise HTTPException(403, "仅本人或管理员可查看")
        txid = (row["txid"] or "").strip()
        transfer = store.chain_transfer(txid) if txid else None
        return {"txid_attached": bool(txid), "chain_seen": transfer is not None,
                "chain_status": transfer["status"] if transfer else None,
                "amount_usd": row["amount_usd"],
                "recharge_status": row["status"],
                "checked_at": datetime.now(timezone.utc).isoformat()}

    @app.post("/api/v1/billing/recharges/{recharge_id}/tx")
    def attach_recharge_tx(recharge_id: str, body: dict):
        txid = str(body.get("txid") or "").strip()
        if not (10 <= len(txid) <= 128):
            raise HTTPException(422, "txid 长度不合法")
        try:
            return store.attach_recharge_tx(recharge_id, current_owner(), txid)
        except KeyError:
            raise HTTPException(404, "Recharge not found") from None
        except ValueError as exc:
            # One txid backs at most one recharge (recharges_txid_unique).
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/v1/billing/recharges")
    def all_recharges(status: str = "PENDING"):
        if auth.username != current_owner():
            raise HTTPException(403, "仅管理员可查看全部充值请求")
        if status not in {"PENDING", "APPROVED", "REJECTED", "EXPIRED"}:
            raise HTTPException(422, "未知状态")
        return store.recharges_by_status(status)

    @app.post("/api/v1/billing/recharges/{recharge_id}/approve")
    def approve_recharge(recharge_id: str):
        if auth.username != current_owner():
            raise HTTPException(403, "仅管理员可审核充值")
        try:
            claimed = store.get_recharge(recharge_id)
            if claimed is None:
                raise KeyError(recharge_id)
            # One shared path with the TRON chain-sync daemon: approval and the
            # budget credit happen together, so the two can never drift apart.
            # The user-submitted txid rides along so the matching chain transfer
            # is marked MATCHED and reconciliation does not misreport this
            # approval as unbacked (approved_without_chain).
            row = store.approve_recharge_and_credit(recharge_id, current_owner(),
                                                    txid=claimed["txid"])
        except KeyError:
            raise HTTPException(404, "Recharge not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        updated = store.get_user_budget(row["username"])
        return {"recharge": row, "username": row["username"],
                "monthly_limit_usd": updated["monthly_limit_usd"] if updated else row["amount_usd"]}

    @app.post("/api/v1/billing/recharges/{recharge_id}/reject")
    def reject_recharge(recharge_id: str):
        if auth.username != current_owner():
            raise HTTPException(403, "仅管理员可审核充值")
        try:
            return store.decide_recharge(recharge_id, False, current_owner())
        except KeyError:
            raise HTTPException(404, "Recharge not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/v1/billing/reconciliation")
    def billing_reconciliation():
        # 仅管理员：账本（已核准充值）与链上（已匹配到账）对账，列出所有对不上的行。
        if auth.username != current_owner():
            raise HTTPException(403, "仅管理员可查看对账")
        return store.billing_reconciliation()

    @app.get("/api/v1/ops/brief")
    def ops_brief():
        require_admin_owner()
        """The 24-hour station report: throughput, self-healing, spend, stalls."""
        return store.ops_brief(hours=24)

    @app.post("/api/v1/skills/import", status_code=201)
    def import_skill(body: SkillImport):
        """Import versioned SKILL.md source under FACAI OWNER authority.

        Registry listings expose metadata and a hash, never the source body.
        Pipe-to-shell payloads are refused (product reliability).
        """
        try:
            parsed = parse_skill_md(body.content)
            if body.source_url and not parsed.get("source_url"):
                parsed["source_url"] = body.source_url.strip()
            metadata = build_skill_metadata(parsed)
            metadata["body_sha256"] = store.skill_source_register(
                parsed["name"], metadata.get("version") or "", parsed["_body"],
                owner_id=current_owner())
            registries.register("skills", parsed["name"], metadata, owner_id=current_owner())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"id": parsed["name"], "registered": True,
                "review_status": metadata.get("review_status", "approved"),
                "document_sha256": metadata["document_sha256"],
                "source_commit": metadata.get("source_commit"),
                "warnings": metadata.get("warnings", [])}

    @app.post("/api/v1/registries/skills/{key}/update")
    def update_skill_version(key: str, body: SkillVersionUpdate):
        """Replace an imported version only when the expected revision matches."""
        try:
            parsed = parse_skill_md(body.content)
            if parsed["name"] != key:
                raise ValueError("SKILL.md name must match the Skill being updated")
            metadata = build_skill_metadata(parsed)
            _validate_skill_metadata(key, metadata)
            updated = store.skill_source_update(
                key, metadata, parsed["_body"], body.expected_version,
                body.expected_body_sha256, owner_id=current_owner())
            boundary_governor.journal_lifecycle_event(
                "SKILL_UPDATED", key, actor=auth.username,
                extra={"previous_version": body.expected_version,
                       "previous_body_sha256": body.expected_body_sha256,
                       "version": updated.get("version"),
                       "body_sha256": updated["body_sha256"]})
        except KeyError:
            raise HTTPException(404, "Imported Skill not found") from None
        except SkillUpdateConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"id": key, "version": updated.get("version"),
                "body_sha256": updated["body_sha256"],
                "enabled": updated.get("enabled", True)}

    @app.get("/api/v1/registries/skills/{key}/versions")
    def skill_versions(key: str):
        """List the current owner's imported source revisions, without bodies."""
        owner = current_owner()
        if (registries.resolve("skills", key, owner_id=owner) is None
                and store.registry_tombstone("skills", key, owner_id=owner) is None):
            raise HTTPException(404, "Skill not found")
        return {"id": key, "versions": store.skill_source_versions(key, owner_id=owner)}

    @app.get("/api/v1/registries/skills/{key}/source")
    def skill_source_version(key: str, version: str, body_sha256: str):
        """Read one immutable imported body for the owning user, including tombstones."""
        owner = current_owner()
        if (registries.resolve("skills", key, owner_id=owner) is None
                and store.registry_tombstone("skills", key, owner_id=owner) is None):
            raise HTTPException(404, "Skill not found")
        if len(body_sha256) != 64 or any(c not in "0123456789abcdef" for c in body_sha256):
            raise HTTPException(422, "Invalid source hash")
        source = store.skill_source_get(key, body_sha256, owner_id=owner, version=version)
        if source is None:
            raise HTTPException(404, "Skill source version not found")
        return JSONResponse(
            {"id": key, "version": version, "body_sha256": body_sha256, "body": source},
            headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/registries/skills/{key}/restore")
    def restore_skill_source(key: str, body: SkillSourceRestore):
        """Republish a saved instruction body as a new version; retain current metadata."""
        try:
            updated = store.skill_source_restore(
                key, body.source_version, body.source_body_sha256,
                body.new_version, body.expected_version,
                body.expected_body_sha256, owner_id=current_owner())
            boundary_governor.journal_lifecycle_event(
                "SKILL_SOURCE_RESTORED", key, actor=auth.username,
                extra={"source_version": body.source_version,
                       "source_body_sha256": body.source_body_sha256,
                       "previous_version": body.expected_version,
                       "previous_body_sha256": body.expected_body_sha256,
                       "version": body.new_version,
                       "body_sha256": updated["body_sha256"]})
        except KeyError:
            raise HTTPException(404, "Skill or source version not found") from None
        except SkillUpdateConflict as exc:
            raise HTTPException(409, str(exc)) from None
        return {"id": key, "version": updated["version"],
                "body_sha256": updated["body_sha256"],
                "enabled": updated.get("enabled", True)}

    # --- skill lifecycle: enable / disable / soft remove (SKILL-001 ②③) --------
    # 禁用与移除是站长级的治理动作：走管理员会话闸（require_admin_owner，
    # 客户端 API key 的 scope 也不含 /api/v1/registries，天然进不来）。
    # 每次动作都在 boundary journal 留下 SKILL_DISABLED / SKILL_ENABLED /
    # SKILL_REMOVED 事件——证据纪律：治理决策永远可追溯。

    def _live_skill_or_404(key: str) -> dict:
        entry = registries.resolve("skills", key, owner_id=current_owner())
        if entry is None:
            raise HTTPException(404, "Skill not found")
        return entry

    @app.post("/api/v1/registries/skills/{key}/enabled")
    def set_skill_enabled(key: str, body: SkillEnabledToggle):
        """Flip one skill's enabled bit; disabled skills never participate in
        conversion scoring, and (once the scheduler wiring lands) dispatch."""
        require_admin_owner()
        entry = _live_skill_or_404(key)
        updated = dict(entry)
        updated["enabled"] = body.enabled
        try:
            registries.register("skills", key, updated, owner_id=current_owner())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        boundary_governor.journal_lifecycle_event(
            "SKILL_ENABLED" if body.enabled else "SKILL_DISABLED", key,
            actor=auth.username, note=body.note,
            extra={"enabled": body.enabled})
        return {"id": key, "enabled": body.enabled}

    @app.delete("/api/v1/registries/skills/{key}")
    def delete_skill(key: str):
        """Soft-remove one skill: the registry row stays as a tombstone
        (pre-delete snapshot + snapshot sha256 + deleter + timestamp), lists
        exclude it, and the deletion lands in the boundary journal as
        SKILL_REMOVED. Re-deleting an already-removed skill is idempotent
        (200 with already_deleted:true and the existing tombstone); a live
        row's deletion returns 200 with the tombstone summary. Restoring the
        same key is simply re-registering it (import/PUT)."""
        require_admin_owner()
        _live_skill_or_404(key)
        tombstone = registries.delete("skills", key, owner_id=current_owner(),
                                      deleted_by=auth.username)
        if tombstone is None:
            raise HTTPException(404, "Skill not found")
        boundary_governor.journal_lifecycle_event(
            "SKILL_REMOVED", key, actor=auth.username,
            extra={"deleted_at": tombstone["deleted_at"],
                   "snapshot_sha256": tombstone["snapshot_sha256"]})
        return {"id": key, "deleted": True,
                "deleted_at": tombstone["deleted_at"],
                "deleted_by": tombstone["deleted_by"],
                "snapshot_sha256": tombstone["snapshot_sha256"]}

    @app.get("/api/v1/registries/skills/{key}/tombstone")
    def skill_tombstone(key: str):
        """The audit view of one removed skill: deletion metadata plus the
        pre-delete snapshot. Operator-only — tombstones are evidence."""
        require_admin_owner()
        tombstone = store.registry_tombstone("skills", key,
                                             owner_id=current_owner())
        if tombstone is None:
            raise HTTPException(404, "Skill not deleted or not found")
        return tombstone

    @app.get("/api/v1/registries/skills/{key}/usage")
    def skill_usage(key: str):
        """Owner-scoped outcomes and verified Skill body attachments across versions."""
        owner = current_owner()
        if (registries.resolve("skills", key, owner_id=owner) is None
                and store.registry_tombstone("skills", key, owner_id=owner) is None):
            raise HTTPException(404, "Skill not found")
        return store.skill_usage(key, owner_id=owner)

    # 任务详情上的技能解析走 enabled 闸：禁用技能在详情/执行面解析为未绑定
    # （与 conversion 打分口径一致；scheduler 派发点由主会话接入）。

    # --- 边界治理：自进化收录闸门 + 远程权限切断（2026-10-12 操作员指令） ------
    # 每一件收入进来的技能/产品都穿过四限制类（道德/宪章/法律/授权）闸门；
    # 收录即切断其远程端 API 权限——远程永远小于本地 /owner，恢复只能由
    # 所有者在本地签发。所有边界的裁决权都在所有者手里。

    @app.post("/api/v1/boundary/intake")
    def boundary_intake(body: BoundaryIntake):
        # Same operator-only surface as the rest of /boundary/*: intake writes
        # the station-wide governor state (journal, quarantine) — a tenant must
        # not be able to inject quarantine records into the operator's ledger.
        require_admin_owner()
        try:
            result = boundary_governor.intake(body.content, source_url=body.source_url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if result["decision"] == "admitted":
            registries.register("skills", result["skill"], result["metadata"],
                                owner_id=current_owner())
            result["registered"] = True
        else:
            result["registered"] = False
        return result

    @app.get("/api/v1/boundary/journal")
    def boundary_journal():
        require_admin_owner()
        return {"journal": list(boundary_governor.journal[-200:]),
                "total": len(boundary_governor.journal)}

    @app.get("/api/v1/boundary/ledger")
    def boundary_ledger():
        require_admin_owner()
        return {"ledger": boundary_governor.ledger.list(),
                "invariant": boundary_governor.ledger.below_local_report()}

    @app.post("/api/v1/boundary/ledger/{entry_id}/grant")
    def boundary_ledger_grant(entry_id: str, body: BoundaryNote):
        require_admin_owner()
        try:
            entry = boundary_governor.grant_remote(entry_id, authority_gate.FACAI_OWNER,
                                                   note=body.note)
        except KeyError:
            raise HTTPException(404, "Ledger entry not found") from None
        except BoundaryError as exc:
            raise HTTPException(422, str(exc)) from None
        return entry

    @app.post("/api/v1/boundary/ledger/{entry_id}/revoke")
    def boundary_ledger_revoke(entry_id: str):
        require_admin_owner()
        try:
            return boundary_governor.revoke_remote(entry_id)
        except KeyError:
            raise HTTPException(404, "Ledger entry not found") from None

    @app.get("/api/v1/boundary/quarantine")
    def boundary_quarantine():
        require_admin_owner()
        return {"quarantine": [q for q in boundary_governor.quarantine.values()
                               if q["status"] == "open"]}

    @app.post("/api/v1/boundary/quarantine/{quarantine_id}/release")
    def boundary_quarantine_release(quarantine_id: str, body: BoundaryNote):
        require_admin_owner()
        try:
            result = boundary_governor.release(quarantine_id, authority_gate.FACAI_OWNER,
                                               note=body.note)
        except KeyError:
            raise HTTPException(404, "Quarantine record not found") from None
        except BoundaryError as exc:
            raise HTTPException(422, str(exc)) from None
        registries.register("skills", result["skill"], result["metadata"],
                            owner_id=current_owner())
        result["registered"] = True
        return result

    @app.get("/api/v1/boundary/config")
    def boundary_config():
        require_admin_owner()
        return boundary_governor.config_snapshot()

    @app.put("/api/v1/boundary/config")
    def boundary_config_update(body: BoundaryConfigUpdate):
        require_admin_owner()
        try:
            boundary_governor.set_config(modes=body.modes, extra=body.extra)
        except BoundaryError as exc:
            raise HTTPException(422, str(exc)) from None
        return boundary_governor.config_snapshot()

    @app.get("/api/v1/boundary/invariant")
    def boundary_invariant():
        require_admin_owner()
        return boundary_governor.below_local_report()

    # --- 案例爬取入库（2026-10-13 操作员指令：咨询案例导入 facai） -----------
    # cases 是参考数据不是可执行能力：不进 skills、不参与派发，但外源正文
    # 照样过边界四类筛查——案例库不能变成投毒入口。

    @app.post("/api/v1/cases/ingest")
    def cases_ingest(body: CaseIngestRequest):
        require_admin_owner()
        from .ops.case_ingest import ingest_cases
        return ingest_cases(boundary_governor, registries, body.urls,
                            owner_id=current_owner())

    # --- 转换操作台：大白话进，治理过的流水线出（2026-10-12 操作员指令） -------

    @app.post("/api/v1/conversion/plan")
    def conversion_plan(body: ConversionText):
        """Dry-run: 大白话 → 专业提示词 + 环境分析 + 逐级分配，不落任何任务。

        The pure preview path — the load test hammers this endpoint, so it
        must never touch the store and never burn a paid model call.
        """
        owner = current_owner()
        plan = deterministic_plan(body.text, channel="console")
        agents = registries.list("agents", owner_id=owner)
        skills = enabled_only(registries.list("skills", owner_id=owner))
        return {"title": plan.title, "professional_prompt": plan.professional,
                "basis": list(plan.basis), "plan_source": plan.source,
                "steps": [step["label"] for step in plan.steps],
                "analysis": analyze_environment(f"{body.text}\n{plan.professional}"),
                "assignment_tree": build_assignment(plan.steps, agents, skills,
                                                    professional=plan.professional)}

    @app.post("/api/v1/conversion/console", status_code=201)
    def conversion_console_create(body: ConversionText):
        """One plain sentence in: mission created, pipeline opened, jobs queued."""
        owner = current_owner()
        gate = budget_gate(owner, store)
        if not gate["allowed"]:
            raise HTTPException(402, gate["reason"])
        reserve_mission_create(owner)
        plan = deterministic_plan(body.text, channel="console")
        try:
            mission = store.create(plan.title, body.text, list(plan.steps), owner_id=owner)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        mission_id = mission["id"]
        store.set_prompt(mission_id, plan.professional)
        store.append(mission_id, "PROMPT_CONVERTED", {
            "channel": "console", "original_input": body.text,
            "professional_prompt": plan.professional, "basis": list(plan.basis),
            "plan_source": plan.source,
            "plan_steps": [{"label": step["label"], "depends_on": step["depends_on"]}
                           for step in plan.steps]})
        mission = store.get(mission_id)
        agents = registries.list("agents", owner_id=owner)
        skills = enabled_only(registries.list("skills", owner_id=owner))
        pipeline = console.open_pipeline(plan, mission_id, owner,
                                         mission["steps"], agents, skills,
                                         original_input=body.text)
        # ROUTER-001 ①: 评分分配决策持久化。事件是事实源（可跨重启审计），
        # ConversionConsole._pipelines 仅保留作展示加速。载荷新鲜出炉，不依赖
        # pipeline 里那份（管道打开时的树就是本次决策）。
        store.append(mission_id, ASSIGNMENT_EVENT,
                     assignment_event_payload(pipeline["assignment_tree"], mission_id))
        # 任务指令到达前定人：首阶段最强的 Agent 领走整个任务。绑定总是整套
        # （agent+model+runtime 用默认绑定补齐），否则只绑 agent 会让
        # dispatch_mission_jobs 跳过默认绑定分支、带着空 model 落 DEFERRED。
        # 仅当最强 Agent 是可执行适配器时才换人，分配树仍如实展示。
        binding = default_binding(owner_id=owner)
        if binding is not None:
            agent_id, model_id, runtime_id = binding
            tree = pipeline["assignment_tree"]
            primary = (tree[0]["assignment"]["primary"]
                       if tree and tree[0]["assignment"] else None)
            bind_reason = "auto-default"
            if primary and primary["agent_id"] != agent_id:
                entry = registries.resolve("agents", primary["agent_id"], owner_id=owner)
                if entry and entry.get("adapter") == "opencode":
                    agent_id = primary["agent_id"]
                    bind_reason = "conversion-primary"
                else:
                    # ROUTER-001 ③: 展示与实际不一致必须留痕——goose（或任何
                    # 非 opencode 适配器）评为首选时，派发保持默认绑定的
                    # opencode。行为是有意设计，这里只补事件，不改派发。
                    store.append(mission_id, AGENT_OVERRIDDEN_EVENT, {
                        "displayed_primary": primary["agent_id"],
                        "displayed_adapter": entry.get("adapter") if entry else None,
                        "actual_agent": agent_id,
                        "reason": f"仅 opencode 适配器可执行该阶段"
                                  f"（{primary['agent_id']} 适配器不可执行，保持默认绑定）",
                    })
            selected = explicitly_requested_skill(body.text, skills)
            # An exact Skill ID in the owner's raw request is an unambiguous
            # selection. Confirm its source exists before freezing the version;
            # vague scoring alone never injects instructions into a mission.
            if selected and store.skill_source_get(
                    selected[0], selected[1]["body_sha256"], owner_id=owner,
                    version=selected[1].get("version") or "") is None:
                selected = None
            changes = {"agent_id", "model_id", "runtime_id"}
            if selected:
                changes.add("skill_id")
            store.bind(mission_id, agent_id=agent_id, model_id=model_id,
                       runtime_id=runtime_id,
                       skill_id=selected[0] if selected else None,
                       skill_snapshot=selected[1] if selected else None,
                       changed=changes, reason="explicit-skill" if selected else bind_reason)
        dispatch = dispatch_mission_jobs(mission_id)
        return {**pipeline, "mission": public_mission(store.get(mission_id)),
                "dispatch": dispatch}

    @app.get("/api/v1/conversion/console")
    def conversion_console_list():
        """Home screen: the owner's pipelines as light rows, newest first."""
        return {"pipelines": console.list_for_owner(current_owner())}

    @app.get("/api/v1/conversion/console/{pipeline_id}")
    def conversion_console_snapshot(pipeline_id: str):
        """The mobile progress center: store step truth + console overlay."""
        owner = current_owner()
        pipeline = console.get(pipeline_id)
        if pipeline is None or pipeline["owner_id"] != owner:
            raise HTTPException(404, "Pipeline not found")
        mission = store.get(pipeline["mission_id"], owner_id=owner)
        if mission is None:
            raise HTTPException(404, "Pipeline not found")
        status_by_id = {step["id"]: step["status"] for step in mission["steps"]}
        stages = {}
        for label, stage in pipeline["stages"].items():
            stages[label] = {**stage, "status": status_by_id.get(stage["step_id"])}
        # ROUTER-001 ③: 事实源是 AGENT_OVERRIDDEN 事件（跨重启可查），快照只做
        # 透传，UI 借此如实标注"实际执行者与展示不同"。
        overrides = [event["payload"] for event in store.events(pipeline["mission_id"])
                     if event["type"] == AGENT_OVERRIDDEN_EVENT]
        return {**pipeline, "stages": stages,
                "assignment_override": overrides[-1] if overrides else None,
                "mission_status": mission["status"]}

    @app.get("/api/v1/conversion/console/{pipeline_id}/stream")
    async def conversion_console_stream(pipeline_id: str, after: int = 0):
        """Push the pipeline's mission events as SSE (same cursor contract as
        /missions/{id}/stream): the visual console refetches the snapshot on
        every pushed event instead of fixed-interval polling."""
        owner = current_owner()
        pipeline = console.get(pipeline_id)
        if pipeline is None or pipeline["owner_id"] != owner:
            raise HTTPException(404, "Pipeline not found")
        mission_id = pipeline["mission_id"]

        async def generate():
            cursor = after
            while True:
                for event in store.events(mission_id, cursor):
                    cursor = event["sequence"]
                    yield (f"id: {cursor}\nevent: mission\ndata: "
                           f"{json.dumps(public_event(event), ensure_ascii=False)}\n\n")
                await asyncio.sleep(1)

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.post("/api/v1/conversion/console/{pipeline_id}/gates", status_code=201)
    def conversion_gate_open(pipeline_id: str, body: GateOpen):
        """Agent-side escalation: park a stage, surface plain-language choices."""
        owner = current_owner()
        pipeline = console.get(pipeline_id)
        if pipeline is None or pipeline["owner_id"] != owner:
            raise HTTPException(404, "Pipeline not found")
        try:
            gate = console.open_gate(pipeline_id, body.stage_label, body.kind,
                                     body.question, asked_by=owner)
        except KeyError:
            raise HTTPException(404, "Pipeline not found") from None
        except ConsoleError as exc:
            raise HTTPException(422, str(exc)) from None
        step = next((s for s in (store.get(pipeline["mission_id"],
                                           owner_id=owner) or {}).get("steps", [])
                     if s["label"] == body.stage_label), None)
        store.append(pipeline["mission_id"], "FEEDBACK_GATE_OPENED",
                     {"gate_id": gate["id"], "stage": body.stage_label,
                      "kind": body.kind, "question": gate["question"]},
                     step_id=step["id"] if step else None)
        return gate

    @app.post("/api/v1/conversion/console/{pipeline_id}/gates/{gate_id}/resolve")
    def conversion_gate_resolve(pipeline_id: str, gate_id: str, body: GateResolve):
        """One tap: plain choice in, auto-filled professional answer out,
        stage re-dispatched with the answer riding the prompt."""
        owner = current_owner()
        pipeline = console.get(pipeline_id)
        if pipeline is None or pipeline["owner_id"] != owner:
            raise HTTPException(404, "Pipeline not found")
        try:
            resolved = console.resolve_gate(pipeline_id, gate_id, body.choice,
                                            custom_answer=body.custom_answer)
        except KeyError:
            raise HTTPException(404, "Gate not found") from None
        except ConsoleError as exc:
            raise HTTPException(422, str(exc)) from None
        mission = store.get(pipeline["mission_id"], owner_id=owner)
        if mission is None:
            raise HTTPException(404, "Pipeline not found")
        step = next((s for s in mission["steps"] if s["label"] == resolved["stage"]),
                    None)
        feedback_event_id = store.append(
            mission["id"], "FEEDBACK_RESOLVED",
            {"gate_id": gate_id, "stage": resolved["stage"],
             "choice": resolved["choice_label"], "answer": resolved["answer"]},
            step_id=step["id"] if step else None)
        resolved["resume"] = "NO_STAGE"
        if step is not None and resolved["pipeline_status"] == "RUNNING" \
                and step["status"] != "DONE":
            base_prompt = (mission["professional_prompt"] or mission["original_input"])
            prompt = (f"{base_prompt}\n\n本次只执行阶段：{step['label']}。\n"
                      f"用户补充（大白话裁决：{resolved['choice_label']}）："
                      f"{resolved['answer']}")
            jobs = [job for job in store.jobs(mission["id"])
                    if job.get("step_id") == step["id"]]
            if any(job["status"] == "RUNNING" for job in jobs):
                # A live worker owns this stage; the answer stays recorded on
                # the gate and a later resolve can re-issue it.
                resolved["resume"] = "RESUME_DEFERRED"
            else:
                queued = next((job for job in jobs if job["status"] == "QUEUED"), None)
                if queued is not None:
                    # Supersede the not-yet-run job in place: same identity,
                    # prompt now carries the user's answer — never a fork.
                    store.update_job_prompt(
                        queued["id"], prompt, owner_id=owner,
                        source_event_id=feedback_event_id, gate_id=gate_id)
                    resolved["resume"] = "RESUMED"
                    resolved["job_id"] = queued["id"]
                    resolved["resume_kind"] = "REPROMPTED"
                else:
                    job = store.enqueue_job(
                        mission["id"], prompt, step_id=step["id"], priority=0,
                        max_attempts=JOB_MAX_ATTEMPTS, timeout=300,
                        dispatch={"agent_id": mission["agent_id"],
                                  "model_id": mission["model_id"],
                                  "runtime_id": mission["runtime_id"],
                                  "step_id": step["id"]})
                    resolved["resume"] = "RESUMED"
                    resolved["job_id"] = job["id"]
                    resolved["resume_kind"] = "NEW_JOB"
        return resolved

    @app.put("/api/v1/registries/{kind}/{key}")
    def register(kind: str, key: str, value: dict):
        if kind not in registries.KINDS:
            raise HTTPException(404, "Registry not found")
        if kind == "models" and auth.username != current_owner():
            # A registry model's endpoint receives the api_key_env process-env
            # value as a Bearer token on every complete(): a tenant pointing it
            # at their own server would exfiltrate operator secrets
            # (DEEPSEEK_API_KEY, RELAY_SHARED_SECRET, ...). Admin keeps full
            # control; tenants bring their own key via /api/v1/my/models.
            if value.get("api_key_env") or "api_key_envs" in value:
                raise HTTPException(403, "api_key_env 仅限站长；自有模型请走 /api/v1/my/models")
            endpoint = str(value.get("endpoint") or "")
            if endpoint:
                try:
                    value["endpoint"] = secretbox.validate_public_endpoint(endpoint)
                except SecretBoxError as exc:
                    raise HTTPException(422, str(exc)) from None
        try:
            registries.register(kind, key, value, owner_id=current_owner())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if kind in {"agents", "models", "runtimes"}:
            _retry_deferred_dispatches(("telegram", "whatsapp"))
        return {"id": key, "registered": True}

    @app.post("/api/v1/registries/models/{key}/test")
    async def test_model_connection(key: str):
        """Manually verify one registered model's credentials against its upstream.

        The response is limited to the classified outcome, provider/model
        attribution, latency and the token counts the provider actually reported.
        The upstream response body, the request headers and any credential are
        never returned or logged.
        """
        model = registries.resolve("models", key, owner_id=current_owner())
        if model is None:
            raise HTTPException(404, "Model not found")
        retry_after = reserve_model_probe(current_owner())
        if retry_after is not None:
            seconds = math.ceil(retry_after)
            # The upstream detail is never surfaced here; the operator only
            # learns that a probe is already in flight or too recent.
            raise HTTPException(429, f"Connectivity probe rate limited; retry in {seconds}s",
                                headers={"Retry-After": str(seconds)})
        started = time.perf_counter()
        try:
            result = await complete(model, [{"role": "user", "content": MODEL_TEST_PROMPT}],
                                rotation=app.state.key_rotation)
        except (ValueError, KeyError) as exc:
            # Exception text can carry upstream hosts, headers or credentials, so
            # only a fixed generic message and the exception type are exposed.
            logger.warning("model connectivity probe for %s failed: %s", key, type(exc).__name__)
            raise HTTPException(422, "Model connectivity check failed") from None
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        result = result if isinstance(result, dict) else {}
        status = result.get("status")
        return {"ok": status == "MODEL_COMPLETED",
                "status": status,
                "label": result.get("label"),
                "provider": model.get("provider"),
                "model_id": key,
                "model": model.get("model"),
                "latency_ms": latency_ms,
                "usage": _reported_token_usage(result.get("usage"))}

    @app.put("/api/v1/missions/{mission_id}/binding")
    def bind(mission_id: str, body: Binding):
        require_mission(mission_id)
        provided = body.model_fields_set
        values = body.model_dump(exclude_unset=True)
        registry_for = {"agent_id": "agents", "model_id": "models",
                        "skill_id": "skills", "runtime_id": "runtimes"}
        for field, kind in registry_for.items():
            if field in provided and values[field] is not None:
                if values[field] not in registries.list(kind, owner_id=current_owner()):
                    raise HTTPException(422, f"Unknown {kind[:-1]}: {values[field]}")
        bound = store.bind(mission_id, **values, changed=set(values),
                           reason="owner-manual",
                           skill_snapshot=(registries.resolve("skills", values["skill_id"],
                                                             owner_id=current_owner())
                                           if "skill_id" in values and values["skill_id"] else None))
        if store.messages_for_mission(mission_id):
            ensure_channel_dispatch(mission_id)
        return bound

    @app.put("/api/v1/missions/{mission_id}/prompt")
    def set_prompt(mission_id: str, body: PromptUpdate):
        require_mission(mission_id)
        return store.set_prompt(mission_id, body.professional_prompt)

    @app.post("/api/v1/missions/{mission_id}/opencode/prepare")
    def prepare_opencode(mission_id: str):
        mission = require_mission(mission_id)
        adapter = resolve_adapter(mission)
        if adapter is None:
            raise HTTPException(409, "Mission is not bound to a runnable agent")
        result = adapter.prepare(mission_id, root)
        result["session_id"] = store.start_session(mission_id, mission["agent_id"], mission["model_id"])
        return result

    @app.post("/api/v1/missions/{mission_id}/opencode/run")
    async def opencode_run(mission_id: str, body: OpenCodeRun):
        mission = require_mission(mission_id)
        gate = budget_gate(current_owner(), store)
        if not gate["allowed"]:
            raise HTTPException(402, gate["reason"])
        adapter = resolve_adapter(mission)
        if adapter is None:
            raise HTTPException(409, "Mission is not bound to a runnable agent")
        step = next((s for s in mission["steps"] if s["id"] == body.step_id), None)
        if body.step_id and step is None:
            raise HTTPException(404, "Step not found")
        owner = current_owner()
        model = registries.resolve("models", mission["model_id"], owner_id=owner)
        runtime = registries.resolve("runtimes", mission["runtime_id"], owner_id=owner)
        skill = bound_skill(
            registries.resolve("skills", mission["skill_id"], owner_id=owner), mission)
        try:
            result = await run_in_threadpool(
                execute_mission_task, store, adapter, mission, model, root, body.prompt,
                body.step_id, body.model_ref, body.timeout, runtime, skill,
                agent_id=mission["agent_id"])
            return public_run_result(result)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception:
            raise HTTPException(500, "OpenCode execution failed to start") from None

    @app.post("/api/v1/missions/{mission_id}/jobs", status_code=201)
    def enqueue_job(mission_id: str, body: JobCreate):
        mission = require_mission(mission_id)
        gate = budget_gate(current_owner(), store)
        if not gate["allowed"]:
            raise HTTPException(402, gate["reason"])
        if resolve_adapter(mission) is None:
            raise HTTPException(409, "Mission is not bound to a runnable agent")
        if body.step_id and body.step_id not in {step["id"] for step in mission["steps"]}:
            raise HTTPException(404, "Step not found")
        try:
            return app.state.scheduler.enqueue(
                mission_id, body.prompt, step_id=body.step_id, model_ref=body.model_ref,
                priority=body.priority, max_attempts=body.max_attempts, timeout=body.timeout)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/v1/missions/{mission_id}/jobs")
    def mission_jobs(mission_id: str):
        require_mission(mission_id)
        return store.jobs(mission_id, owner_id=current_owner())

    @app.get("/api/v1/jobs/{job_id}")
    def get_job(job_id: str):
        # A job id is globally unique; scope the lookup through its mission so a
        # guessed id can never reveal another tenant's prompt or status.
        job = store.get_job(job_id, owner_id=current_owner())
        if job is None:
            raise HTTPException(404, "Job not found")
        return job

    @app.post("/api/v1/jobs/{job_id}/{action}")
    def job_action(job_id: str, action: str):
        handlers = {"pause": store.pause_job, "resume": store.resume_job,
                    "cancel": store.cancel_job, "retry": store.retry_job}
        if action not in handlers:
            raise HTTPException(404, "Unknown job action")
        # Resolve the job through its owner first so a missing or foreign job
        # keeps its 404 even for a spent owner.
        job = store.get_job(job_id, owner_id=current_owner())
        if job is None:
            raise HTTPException(404, "Job not found")
        # Budget gate (per-user monthly limit): resume/retry re-enqueue work,
        # so they are NEW dispatches and a spent owner cannot perform them
        # (402). pause/cancel only stop in-flight work and stay available to a
        # spent owner, or a runaway job could never be stopped.
        if action in {"resume", "retry"}:
            gate = budget_gate(current_owner(), store)
            if not gate["allowed"]:
                raise HTTPException(402, gate["reason"])
        try:
            return handlers[action](job_id, owner_id=current_owner())
        except KeyError:
            raise HTTPException(404, "Job not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/v1/scheduler")
    def scheduler_stats():
        # stats() exposes station-wide counters (every tenant's running jobs,
        # worker identity, last error) — same operator-only surface as /run.
        require_admin_owner()
        return app.state.scheduler.stats()

    # --- 模型矩阵（2026-10-13 操作员指令）：可视化支配自动路由 -----------------
    # 一两池太少：models 注册表里每一款（含客户端自带 user_model）各成一个
    # 速率池；矩阵可视化健康度，权重/暂停由所有者手动支配。

    @app.get("/api/v1/modelmatrix")
    def model_matrix():
        require_admin_owner()
        from .quota import (COOLDOWN_BASE, COOLDOWN_MAX, DEFAULT_CONCURRENCY,
                            DEFAULT_MINUTE_LIMIT, UNPENALIZED_WINDOW)
        quota_pools = app.state.quota_pools
        quota_pools.replace_models(registries.list("models", owner_id=current_owner()))
        return {"pools": quota_pools.matrix(),
                "route_order": quota_pools.ranked(),
                # 冷却阶梯等内置常量随快照带出：UI 只展示、不编辑（MODEL-001 ④
                # 的诚实只读说明），编辑面只开放注册表承载的两条水位线。
                "constants": {
                    "cooldown_base_s": COOLDOWN_BASE,
                    "cooldown_max_s": COOLDOWN_MAX,
                    "unpenalized_window_s": UNPENALIZED_WINDOW,
                    "default_minute_limit": DEFAULT_MINUTE_LIMIT,
                    "default_concurrency": DEFAULT_CONCURRENCY,
                }}

    @app.put("/api/v1/modelmatrix/{model_id}/weight")
    def model_matrix_weight(model_id: str, body: ModelRouteWeight):
        require_admin_owner()
        try:
            weight = app.state.quota_pools.set_weight(model_id, body.weight)
        except KeyError:
            raise HTTPException(404, "Unknown model pool") from None
        # 权重同步回注册表条目，重启与矩阵刷新都不丢
        models = registries.list("models", owner_id=current_owner())
        if model_id in models:
            entry = dict(models[model_id]); entry["route_weight"] = weight
            registries.register("models", model_id, entry, owner_id=current_owner())
        return {"model_id": model_id, "weight": weight}

    @app.put("/api/v1/modelmatrix/{model_id}/pause")
    def model_matrix_pause(model_id: str, body: ModelRoutePause):
        require_admin_owner()
        try:
            app.state.quota_pools.set_paused(model_id, body.paused)
        except KeyError:
            raise HTTPException(404, "Unknown model pool") from None
        return {"model_id": model_id, "paused": body.paused}

    @app.put("/api/v1/modelmatrix/{model_id}/limits")
    def model_matrix_limits(model_id: str, body: ModelRouteLimits):
        """水位线编辑：分钟窗/并发上限写回注册表条目（与 weight 同款回写纪律，
        重启与矩阵刷新不丢），并同步池配置。冷却阶梯是代码内置常量，不在此面。"""
        require_admin_owner()
        models = registries.list("models", owner_id=current_owner())
        if model_id not in models:
            raise HTTPException(404, "Unknown model pool") from None
        entry = dict(models[model_id])
        entry["rate_limit_per_min"] = body.rate_limit_per_min
        entry["max_concurrency"] = body.max_concurrency
        try:
            registries.register("models", model_id, entry, owner_id=current_owner())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        # 立即生效：replace_models 幂等且保住实时计数（与启动接线同语义）。
        app.state.quota_pools.replace_models(
            registries.list("models", owner_id=current_owner()))
        return {"model_id": model_id,
                "rate_limit_per_min": body.rate_limit_per_min,
                "max_concurrency": body.max_concurrency}

    @app.get("/api/v1/modelmatrix/key-usage")
    def model_matrix_key_usage():
        """MODEL-001 每Key计量：多Key模型按环境引用名的上游调用计数与tokens。

        只报环境变量名与上游自报计数，密钥值不出现在任何字段；estimated_cost
        仅当注册表带明确单价（price_input_per_1k/price_output_per_1k）才计算，
        无单价返回 null——绝不发明估算依据。计数在服务进程内存里，重启清零，
        跨进程聚合仍是登记册在记的独立缺口。"""
        require_admin_owner()
        report = []
        for model_id, model in registries.list("models", owner_id=current_owner()).items():
            refs = model.get("api_key_envs")
            if not isinstance(refs, list) or not refs:
                continue
            endpoint = str(model.get("endpoint", "")).rstrip("/")
            price_in = model.get("price_input_per_1k")
            price_out = model.get("price_output_per_1k")
            priced = (isinstance(price_in, (int, float)) and not isinstance(price_in, bool)
                      and isinstance(price_out, (int, float)) and not isinstance(price_out, bool))
            keys = {}
            for ref, counters in key_usage(endpoint, refs,
                                           rotation=app.state.key_rotation).items():
                row = dict(counters)
                row["estimated_cost"] = (
                    round(counters["input_tokens"] / 1000.0 * price_in
                          + counters["output_tokens"] / 1000.0 * price_out, 6)
                    if priced else None)
                keys[ref] = row
            report.append({"model_id": model_id, "endpoint": endpoint, "keys": keys,
                           "pricing": {"input_per_1k": price_in if priced else None,
                                       "output_per_1k": price_out if priced else None}})
        return {"models": report,
                "basis": "process-local counters, reset on restart; tokens are upstream-reported"}

    @app.get("/api/v1/fleet/agent-progress")
    def fleet_agent_progress():
        """A10 Agent 级可计量单位：按派发归因的任务账（仅 admin/Owner）。

        分子分母都来自事件/任务表，与 Trace 重放同源（事件一致）；运行中未
        完成的任务只压低百分比，绝不发明进度。无派发归因的排队任务不计入
        任何 Agent——没有分母的 Agent 根本不出现，而不是显示一个假数。"""
        require_admin_owner()
        return {"agents": app.state.store.agent_progress(),
                "basis": "attributed jobs from the durable event log;"
                         " no timer-derived values, station-wide"}

    @app.post("/api/v1/scheduler/run")
    async def scheduler_run():
        # The manual tick pumps EVERY owner's queued jobs; it is an operator
        # lever, not a tenant one (a customer forcing ticks could burn the
        # station's upstream budget on someone else's backlog).
        require_admin_owner()
        executed = await run_in_threadpool(app.state.scheduler.run_pending)
        return {"executed": executed}

    @app.get("/api/v1/missions/{mission_id}/diff")
    def mission_diff(mission_id: str, rev: str | None = None):
        mission = require_mission(mission_id)
        if not mission["checkpoints"]:
            return {"checkpoint": None, "available": False, "diff": "", "truncated": False}
        target = rev or mission["checkpoints"][-1]["commit_hash"]
        # rev is a bare argv element to `git show`: a leading-dash value would
        # be parsed as a git option (e.g. --output=<path> writes files).
        import re as _re
        if not _re.fullmatch(r"[0-9a-fA-F]{6,40}", target):
            raise HTTPException(422, "非法的 commit 标识")
        return {"checkpoint": target, **git_diff(mission["workspace"], target)}

    @app.get("/api/v1/media/{name}")
    def media_file(name: str):
        """Serve a generated digest video by its tokenized filename."""
        if (not name.startswith("digest-") or not name.endswith(".mp4")
                or any(ch not in "digest-.0123456789abcdefmp" for ch in name)):
            raise HTTPException(404, "Not found")
        path = (root / "media" / name).resolve()
        if path.parent != (root / "media").resolve() or not path.is_file():
            raise HTTPException(404, "Not found")
        return FileResponse(path, media_type="video/mp4", filename=name)

    @app.get("/api/v1/artifacts")
    def artifacts_center(limit: int = 100):
        """Newest artifacts across the operator's missions (the artifacts center)."""
        return [public_artifact(row) for row in
                store.recent_artifacts(owner_id=current_owner(),
                                       limit=max(1, min(int(limit), 300)))]

    @app.get("/api/v1/missions/{mission_id}/artifacts")
    def mission_artifacts(mission_id: str):
        """List artifacts for one mission (metadata only — content stays on
        the per-artifact download route)."""
        mission = require_mission(mission_id)
        return [
            {k: a[k] for k in ("id", "kind", "created_at") if k in a}
            for a in (mission.get("artifacts") or [])
        ]

    @app.get("/api/v1/missions/{mission_id}/artifacts/{artifact_id}")
    def artifact_content(mission_id: str, artifact_id: str, limit: int = 20000):
        mission = require_mission(mission_id)
        artifact = store.get_artifact(mission_id, artifact_id)
        if artifact is None:
            raise HTTPException(404, "Artifact not found")
        path = Path(artifact["path"]).resolve()
        workspace = Path(mission["workspace"]).resolve()
        if workspace not in path.parents or not path.is_file():
            raise HTTPException(404, "Artifact file is unavailable")
        content = path.read_text(encoding="utf-8", errors="replace")
        limit = max(1, min(limit, 200000))
        return {**public_artifact(artifact), "size": len(content), "truncated": len(content) > limit,
                "content": content[:limit]}

    @app.post("/api/v1/missions/{mission_id}/model/complete")
    async def model_complete(mission_id: str, body: ModelRequest):
        mission = require_mission(mission_id)
        gate = budget_gate(current_owner(), store)
        if not gate["allowed"]:
            raise HTTPException(402, gate["reason"])
        model_id = mission["model_id"]
        if not model_id:
            raise HTTPException(409, "No model bound to this mission")
        owner = current_owner()
        model = registries.list("models", owner_id=owner).get(model_id)
        if model is None:
            raise HTTPException(409, "Bound model is unavailable")
        if body.step_id and body.step_id not in {step["id"] for step in mission["steps"]}:
            raise HTTPException(404, "Step not found")
        try:
            result = await complete(model, body.messages,
                                    rotation=app.state.key_rotation)
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, str(exc)) from None
        runtime = registries.resolve("runtimes", mission["runtime_id"], owner_id=owner)
        skill = dispatchable_skill(
            registries.resolve("skills", mission["skill_id"], owner_id=owner))
        attribution = mission_attribution(mission, model, runtime, skill)
        # Only the counts the gateway actually reported are kept; the split goes
        # into its own cost columns so a total-only report stays visibly total-only.
        usage = _reported_token_usage(result.get("usage")) or {}
        tokens = usage.get("total_tokens")
        if usage:
            store.add_cost(mission_id, model.get("provider", "custom"), model_id, 0.0, tokens,
                           input_tokens=usage.get("input_tokens"),
                           output_tokens=usage.get("output_tokens"))
        store.append(mission_id, result["status"],
                     {**attribution, "http_status": result["http_status"],
                      "reason": result["reason"], "upstream": result["upstream"],
                      "tokens": tokens},
                     body.step_id, agent_id=mission["agent_id"])
        return {**attribution, **result, "tokens": tokens}

    # --- message channels ------------------------------------------------------

    def require_channel(channel):
        provider = channels.get(channel)
        if provider is None:
            raise HTTPException(404, "Channel not found")
        return provider

    def unconfigured(provider):
        return HTTPException(503, {"detail": f"{provider.name} channel is not configured",
                                   "missing": provider.missing()})

    @app.get("/api/v1/channels")
    def list_channels():
        require_admin_owner()
        return channels.statuses()

    @app.get("/api/v1/channels/{channel}")
    def get_channel(channel: str):
        require_admin_owner()
        return require_channel(channel).status().to_dict()

    @app.get("/api/v1/channels/{channel}/messages")
    def channel_messages(channel: str, limit: int = 100):
        require_admin_owner()
        require_channel(channel)
        return store.channel_messages(channel, limit, owner_id=current_owner())

    def default_binding(owner_id="admin"):
        """Return the first fully-dispatchable default binding, or None.

        Only an agent whose adapter is actually wired (OpenCode) together with a
        resolvable model and runtime counts as valid. Anything less is reported
        as deferred rather than faked.
        """
        agent_id = next((key for key, value in registries.list("agents", owner_id=owner_id).items()
                         if value.get("adapter") == "opencode"), None)
        # A client-supplied model (user_model_id) always wins over the shared
        # defaults: clients run their own models, the operator's are a fallback.
        models = registries.list("models", owner_id=owner_id)
        model_id = next((key for key, value in models.items() if value.get("user_model_id")), None)             or next(iter(models), None)
        runtime_id = next(iter(registries.list("runtimes", owner_id=owner_id)), None)
        if agent_id and model_id and runtime_id:
            return agent_id, model_id, runtime_id
        return None

    def record_dispatch_deferred(mission_id, payload):
        """Append DISPATCH_DEFERRED only when it carries new information.

        The wechat monitor replays every jobless mission each poll round, so an
        unconditional append floods the events table with one row per round for
        a single stuck mission. A deferral is superseded — and a fresh one may
        be recorded — once a job has been enqueued after it; an unchanged
        mission stays silent. The DEFERRED result still travels back to the
        caller either way, so replies and statuses never depend on the audit.
        """
        prior = [event for event in store.events(mission_id)
                 if event["type"] == "DISPATCH_DEFERRED"
                 and event["payload"].get("reason") == payload.get("reason")]
        if prior:
            latest_job = max((job["created_at"] for job in store.jobs(mission_id)),
                             default=None)
            if latest_job is None or latest_job <= prior[-1]["occurred_at"]:
                return
        store.append(mission_id, "DISPATCH_DEFERRED", payload)

    def _dispatch_grant_verdict(mission_id, actor=None):
        """Read-only mirror of the Owner gate's decision, for dispatch branching.

        ``AuthorizationEngine.authorize`` CONSUMES one use from a limited grant
        on every ALLOW, and the approval flow signs exactly ONE use — that use
        belongs to the job's execution. A dispatch-time probe that reused
        ``authorize`` would burn the customer's approval before any work ran,
        so this asks the same question (same subject/key matching, same
        expiry/use accounting, same OWNER_GATE mission-grant refusal, same
        stale-reason precedence) without the side effect. Mirrors
        facai/authority.py ``AuthorizationEngine._matching_grant`` /
        ``_stale_grant_reason`` / ``authorize``; keep the three in step.
        """
        gate = authority_gate.engine()
        action = authority_gate.ActionIR(
            actor=actor or "opencode", backend=authority_gate.GATE_BACKEND,
            action=authority_gate.MISSION_RUN, resource=mission_id,
            mission_id=mission_id)
        if gate.backends.get(action.backend) is None:
            return {"decision": "DENY",
                    "reason": f"backend '{action.backend}' is not registered"}
        allow_mission_grants = gate.mode == authority_gate.MISSION_AUTONOMOUS

        def subject_matches(grant):
            if grant.subject not in (action.actor, "*"):
                return False
            if action.mission_id and grant.action_key == f"mission:{action.mission_id}":
                return True
            if grant.action_key == "*":
                return True
            if grant.action_key.endswith(":"):
                return action.key().startswith(grant.action_key)
            return grant.action_key == action.key()

        def admissible(grant):
            # OWNER_GATE accepts only action-level grants; a bare
            # ``mission:<id>`` grant is precisely what that mode refuses.
            if not allow_mission_grants and action.mission_id and \
                    grant.action_key == f"mission:{action.mission_id}" and \
                    not grant.action_key.endswith(":") and grant.action_key != "*":
                return False
            return subject_matches(grant)

        for grant in gate.grants.values():
            if grant.uses_left is not None and \
                    gate._use_count.get(grant.grant_id, 0) >= grant.uses_left:
                continue
            if grant.expires_at is not None and datetime.now(timezone.utc) >= grant.expires_at:
                continue
            if admissible(grant):
                return {"decision": "ALLOW", "reason": "", "grant_id": grant.grant_id}
        for grant in gate.grants.values():
            # Stale-reason precedence mirrors _stale_grant_reason: the first
            # subject-matching dead grant names WHY the gate refused.
            if not subject_matches(grant):
                continue
            is_mission_only = action.mission_id and \
                grant.action_key == f"mission:{action.mission_id}"
            if is_mission_only and not allow_mission_grants:
                continue
            if grant.expires_at is not None and datetime.now(timezone.utc) >= grant.expires_at:
                return {"decision": "DENY", "reason": authority_gate.GRANT_EXPIRED}
            if grant.uses_left is not None and \
                    gate._use_count.get(grant.grant_id, 0) >= grant.uses_left:
                return {"decision": "DENY", "reason": authority_gate.GRANT_EXHAUSTED}
        return {"decision": "DENY", "reason": authority_gate.GRANT_REQUIRED, "grant_id": None}

    def send_channel_text(channel, owner, recipient, text, mission_id=None):
        """Best-effort outbound chat line with the ack's provider/audit rules.

        A hosted wechat owner is answered through THEIR OWN provider instance
        (never the operator's account); everything else rides the station
        registry provider. Send failures are logged, never raised: an approval
        prompt that cannot reach the chat still leaves its durable row behind.
        """
        if channel not in NOTIFY_CHANNELS or not recipient:
            return None
        provider = None
        if channel == "wechat":
            host_binding = store.host_binding(owner, "wechat")
            if host_binding is not None and host_binding["state"] == "ACTIVE":
                provider = host_wechat_providers.get(owner)
        if provider is None:
            # Registry stand-ins may expose providers as a plain mapping or
            # only require(); a missing lookup just means no send, no crash.
            try:
                provider = channels.get(channel)
            except AttributeError:
                providers = getattr(channels, "providers", None)
                if isinstance(providers, dict):
                    provider = providers.get(channel)
        if provider is None or not provider.configured():
            return None
        try:
            result = asyncio.run(provider.send(
                OutboundReply(channel=channel, recipient=recipient, text=text)))
            # Audit the line like every other outbound chat message (text
            # only) — unless the provider records its own delivery.
            if not getattr(provider, "records_outbound_itself", False):
                try:
                    store.record_channel_message(
                        channel, "OUTBOUND",
                        result.get("status", "SENT") if isinstance(result, dict) else "SENT",
                        external_id=result.get("external_id") if isinstance(result, dict) else None,
                        recipient=recipient, text=text, mission_id=mission_id,
                        owner_id=owner)
                except Exception:
                    logger.warning("channel text audit failed for %s", channel,
                                   exc_info=True)
            return result
        except Exception:
            logger.warning("channel text send failed for %s", channel, exc_info=True)
            return None

    def request_channel_approval(mission):
        """Ask the mission's source channel for a one-shot approval.

        Persists the pending_approvals row (the durable state the customer's
        plain 「批准」 later consumes) and best-effort sends the Chinese prompt
        to the earliest inbound sender of the mission. Idempotent per window:
        a mission whose latest approval row is not CONSUMED — an unanswered
        request, live or lapsed, or a refused one — is never re-prompted, so
        the monitors' replay rounds cannot flood the chat; only a CONSUMED row
        (approved once, but the grant died before dispatch) may be re-asked.
        A mission that never came from a channel has nobody to ask: no row.
        """
        mission_id = mission["id"]
        owner = mission.get("owner_id", "admin")
        source = next((m for m in store.messages_for_mission(mission_id)
                       if m["direction"] == "INBOUND"), None)
        if source is None:
            return None
        latest = store.latest_approval_for_mission(mission_id)
        if latest is not None and latest["status"] != "CONSUMED":
            return None
        row = store.create_pending_approval(
            mission_id, owner, source["channel"], message_id=source["id"],
            ttl_seconds=APPROVAL_TTL_SECONDS)
        sender = store.channel_sender_for_mission(mission_id, source["channel"])
        send_channel_text(
            source["channel"], owner, sender,
            f"任务「{mission['title']}」需要你的授权才能继续："
            f"[回复 批准] 或 [回复 取消]（15 分钟内有效）",
            mission_id=mission_id)
        return row

    def handle_channel_approval(owner, channel, message_id, text, sender=None):
        """Intercept a plain-chat 「批准」/「取消」 against a live approval row.

        Runs BEFORE task conversion: the reply is a verdict on an existing
        mission, never a new one. Owner-scoped by construction — the rows come
        from ``pending_approvals_for_owner(owner)`` — so one customer's
        「批准」 can never sign another customer's mission, whatever text they
        send. The oldest live row wins (FIFO) and exactly one row is consumed
        per reply; text without a live row returns None and keeps the normal
        conversion path untouched.

        The grant itself is signed by the Owner core (``issued_by=FACAI_OWNER``
        is enforced by the engine): the customer's confirmation never becomes
        a second authority root. It is an action-level grant scoped to this
        mission's remaining steps (``facai-native:mission.run:<id>``,
        uses=剩余步骤数, TTL=mission 级日额度),
        so it authorizes in both gate modes.

        Returns the pipeline result dict when the text was consumed as an
        approval command, None otherwise.
        """
        command = (text or "").strip()
        if command not in (APPROVE_COMMAND, CANCEL_COMMAND):
            return None
        store.expire_pending_approvals()
        rows = store.pending_approvals_for_owner(owner)
        if not rows:
            return None
        row = rows[0]
        mission_id = row["mission_id"]
        if command == CANCEL_COMMAND:
            if store.cancel_pending_approval(row["id"], owner) is None:
                return None  # lost a concurrent race; treat as ordinary text
            store.append(mission_id, "APPROVAL_CANCELLED",
                         {"approval_id": row["id"], "channel": channel,
                          "message_id": message_id, "owner_id": owner})
            store.record_channel_event(channel, message_id, "APPROVAL_CANCELLED",
                                       {"approval_id": row["id"],
                                        "mission_id": mission_id})
            try:
                store.set_channel_message_status(message_id, "APPROVAL_CANCELLED")
            except Exception:
                logger.warning("approval reply status update failed for %s",
                               message_id, exc_info=True)
            send_channel_text(channel, owner, sender,
                              f"已取消。任务「{store.get(mission_id)['title']}」将保持暂停，"
                              "需要时可在控制台重新派发。", mission_id=mission_id)
            return {"status": "APPROVAL_CANCELLED", "message_id": message_id,
                    "durable": True, "duplicate": False, "approval": "CANCELLED",
                    "approval_id": row["id"], "mission_id": mission_id}
        if store.consume_pending_approval(row["id"], owner) is None:
            return None  # consumed concurrently (or expired mid-flight)
        # A 方案（操作员拍板）：一次批准管全程——uses 覆盖该任务剩余的全部
        # 步骤（每 job 消费一次），TTL 用 mission 级日额度；多步任务不再中途
        # 反复要批准，grant 仍随任务粒度封顶、不会泄漏成万能钥匙。
        remaining = sum(1 for st in store.get(mission_id).get("steps", [])
                        if st.get("status") in ("WAITING", "RUNNING")) or 1
        grant = authority_gate.engine().issue_grant(
            subject="*",
            action_key=f"{authority_gate.GATE_BACKEND}:{authority_gate.MISSION_RUN}:{mission_id}",
            issued_by=authority_gate.FACAI_OWNER,
            ttl_seconds=authority_gate.mission_ttl(), uses=max(remaining, 1),
            note="渠内客户批准（一次批准管全程，覆盖剩余步骤）")
        store.append(mission_id, "APPROVAL_GRANTED",
                     {"approval_id": row["id"], "grant_id": grant.grant_id,
                      "channel": channel, "message_id": message_id,
                      "owner_id": owner, "uses": max(remaining, 1),
                      "ttl_seconds": authority_gate.mission_ttl()})
        store.record_channel_event(channel, message_id, "APPROVAL_GRANTED",
                                   {"approval_id": row["id"], "mission_id": mission_id,
                                    "grant_id": grant.grant_id})
        try:
            store.set_channel_message_status(message_id, "APPROVAL_GRANTED")
        except Exception:
            logger.warning("approval reply status update failed for %s",
                           message_id, exc_info=True)
        # 重派走 ensure_channel_dispatch 本体 —— 与 _retry_deferred_dispatches
        # 同一落点：绑定解析、断点续派、全 DAG 入队、幂等去重都在里面。
        dispatch = ensure_channel_dispatch(mission_id)
        mission = store.get(mission_id)
        title = mission["title"] if mission else mission_id
        if dispatch.get("status") == "QUEUED":
            send_channel_text(channel, owner, sender,
                              f"✅ 已收到批准，任务「{title}」继续执行。",
                              mission_id=mission_id)
        else:
            send_channel_text(
                channel, owner, sender,
                f"已收到批准。任务「{title}」暂时无法启动"
                f"（{dispatch.get('reason', 'UNKNOWN')}），条件恢复后将自动继续。",
                mission_id=mission_id)
        return {"status": "APPROVAL_GRANTED", "message_id": message_id,
                "durable": True, "duplicate": False, "approval": "GRANTED",
                "approval_id": row["id"], "mission_id": mission_id,
                "grant_id": grant.grant_id, "dispatch": dispatch}

    def ensure_channel_dispatch(mission_id):
        """Bind a valid default and enqueue once; never enqueue without one.

        Idempotent in both directions: a mission whose steps are all covered
        by jobs is left alone, and a mission partially dispatched by the older
        first-stage-only code (stalled at its first checkpoint, ~20%) is
        healed here by enqueueing exactly its missing steps -- never a
        duplicate of a step that already has a job.
        """
        mission = store.get(mission_id)
        if mission is None:
            return {"status": "MISSING"}
        existing_jobs = store.jobs(mission_id)
        covered = {job["step_id"] for job in existing_jobs if job.get("step_id")}
        if existing_jobs and all(step["id"] in covered for step in mission["steps"]):
            return {"status": "QUEUED"}
        owner = mission["owner_id"]
        # Budget gate (per-user monthly limit): never enqueue once the owner's
        # month-to-date USD spend has reached their limit. The caller records the
        # message-level BUDGET_EXCEEDED channel event; the detail travels back in
        # this result so a chat reply can quote the exact spend and limit.
        gate = budget_gate(owner, store)
        if not gate["allowed"]:
            record_dispatch_deferred(mission_id,
                                     {"reason": "BUDGET_EXCEEDED", "detail": gate["reason"]})
            return {"status": "DEFERRED", "reason": "BUDGET_EXCEEDED",
                    "detail": gate["reason"]}
        # Owner-grant gate at dispatch（MISSION_DENIED 前置）. 彻底无审核
        # （2026-10-07 操作员指令）: the gate no longer waits for anyone — a
        # missing/expired/exhausted mission grant is re-signed on the spot by
        # the platform core (a mission still in the store is legitimate) and
        # dispatch continues; the re-signing rides GRANT_AUTO_RENEWED in the
        # audit trail. request_channel_approval/handle_channel_approval remain
        # only for the seeded-row interception path and are never prompted.
        verdict = _dispatch_grant_verdict(mission_id, actor=mission.get("agent_id"))
        if verdict["decision"] == "DENY":
            authority_gate.ensure_mission_grant(mission_id)
            store.append(mission_id, "GRANT_AUTO_RENEWED",
                         {"reason": verdict["reason"],
                          "note": "无审核模式：授权门自动补签，任务继续派发"},
                         None, None, mission.get("agent_id"))
        if not mission["agent_id"]:
            binding = default_binding(owner_id=owner)
            if binding is None:
                record_dispatch_deferred(
                    mission_id, {"reason": "NO_VALID_BINDING",
                                 "detail": "缺少可用的 Agent / Model / Runtime 绑定"})
                return {"status": "DEFERRED", "reason": "NO_VALID_BINDING"}
            agent_id, model_id, runtime_id = binding
            store.bind(mission_id, agent_id=agent_id, model_id=model_id, runtime_id=runtime_id,
                       changed={"agent_id", "model_id", "runtime_id"},
                       reason="auto-default")
            mission = store.get(mission_id)
        agent = registries.resolve("agents", mission["agent_id"], owner_id=owner)
        model = registries.resolve("models", mission["model_id"], owner_id=owner)
        runtime = registries.resolve("runtimes", mission["runtime_id"], owner_id=owner)
        if not agent or not model or not runtime or agent.get("adapter") != "opencode":
            record_dispatch_deferred(
                mission_id, {"reason": "INVALID_BINDING", "agent_id": mission["agent_id"],
                             "model_id": mission["model_id"], "runtime_id": mission["runtime_id"]})
            return {"status": "DEFERRED", "reason": "INVALID_BINDING"}
        step_id = mission["steps"][0]["id"] if mission["steps"] else None
        prompt = mission["professional_prompt"] or mission["original_input"]
        step_model_ref = None
        if step_id:
            # The first step's own model binding, when resolvable, runs this job.
            step_model_id = mission["steps"][0].get("model_id")
            if step_model_id:
                step_model_ref = opencode_model_ref(
                    registries.resolve("models", step_model_id, owner_id=owner))
                if step_model_ref:
                    prompt += f"\n本阶段由模型 {step_model_id} 执行。"
        # Enqueue the whole step DAG: the scheduler's dependency gate holds
        # every job until its step's dependencies complete, so the mission
        # never stalls after its first stage. A step's own model binding, when
        # present, runs that job and is announced in its prompt.
        job_ids = []
        for step in mission["steps"]:
            if step["id"] in covered:
                continue  # already dispatched; healing must not duplicate it
            step_prompt = prompt
            step_model_ref = None
            step_model_id = step.get("model_id")
            if step_model_id:
                step_model = registries.resolve("models", step_model_id, owner_id=owner)
                if step_model:
                    step_model_ref = opencode_model_ref(step_model)
                    step_prompt += f"\n本阶段由模型 {step_model_id} 执行。"
            job = store.enqueue_job(mission_id, step_prompt,
                                    step_id=step["id"], model_ref=step_model_ref,
                                    priority=0, max_attempts=JOB_MAX_ATTEMPTS, timeout=300,
                                    dispatch={"agent_id": mission["agent_id"],
                                              "model_id": mission["model_id"],
                                              "runtime_id": mission["runtime_id"],
                                              "step_id": step["id"]})
            job_ids.append(job["id"])
        # The gate inside ran the 80% soft-threshold check; if it fired here
        # (first crossing happened after the caller's own gate ran), the ack
        # caller picks the alert up from this result and appends it to the
        # reply. Deduped by user_alerts, so it can ride along harmlessly.
        return {"status": "QUEUED", "job_ids": job_ids, "alert": gate.get("alert")}

    def _model_with_key(model, owner_id):
        """Attach the decrypted credential of an owner-registered model, in memory only."""
        ref = (model or {}).get("user_model_id")
        if not ref:
            return model
        token = store.user_model_encrypted_key(owner_id, ref)
        if not token:
            return model
        try:
            return {**model, "api_key": secretbox.decrypt(root, token)}
        except SecretBoxError as exc:
            logger.warning("user model credential unavailable: %s", exc)
            return model

    def bound_conversion_model(owner_id="admin"):
        """Return the default dispatch model, or None when it cannot be called.

        Any registered model is eligible — model choice is OWNER data, not a
        code-level ban (§十一.C.6 wave). An incomplete registry entry is
        skipped so conversion falls back deterministically.
        """
        binding = default_binding(owner_id=owner_id)
        if binding is None:
            return None, None
        _agent_id, model_id, _runtime_id = binding
        model = registries.resolve("models", model_id, owner_id=owner_id)
        if not model or not model.get("endpoint") or not model.get("model"):
            return None, None
        return model_id, _model_with_key(model, owner_id)

    def convert_channel_text(channel, text, owner_id="admin"):
        """Prefer the bound model; return sanitized fallback provenance too."""
        fallback = convert_message(text, channel)
        model_id, model = bound_conversion_model(owner_id=owner_id)
        if model is None:
            return fallback, None
        try:
            return complete_with_bound_model(text, channel, model_id, model, complete), None
        except Exception as exc:
            logger.warning("channel prompt conversion for %s fell back: %s",
                           model_id, type(exc).__name__)
            # Never persist the exception string or upstream response: both
            # can carry credentials or provider-private request content.
            return fallback, {"model_id": model_id,
                              "reason_class": type(exc).__name__,
                              "fallback_source": "deterministic"}

    def conversion_payload_for(channel, message_id, conversion):
        payload = {
            "channel": channel, "message_id": message_id,
            "original_input": conversion.original,
            "professional_prompt": conversion.professional,
            "basis": list(conversion.basis),
            "conversion_status": "CONVERTED",
            "conversion_source": conversion.source,
        }
        if conversion.model_id:
            payload["model_id"] = conversion.model_id
        if conversion.usage:
            payload["usage"] = conversion.usage
        return payload

    def convert_and_link_mission(channel, message_id, recorded, owner_id="admin",
                                 alert=None):
        """Convert one inbound message exactly once, then link and dispatch it.

        A durable per-message lease serializes duplicate webhook deliveries: only
        the claim holder may invoke the conversion model, and every duplicate gets
        a truthful ``IN_PROGRESS`` result without conversion or dispatch. The lease
        is released on failure so a replay retries, and it expires if the holder
        crashes so stale claims are recovered.

        ``alert`` carries a caller's pre-gate budget result (the manual dispatch
        endpoint gates before calling this): when the month's 80% reminder fired
        THERE, this function's own gate reads None because the ``user_alerts``
        row is already claimed, so the caller's alert is passed through and
        appended to the ack reply instead of being silently dropped.
        """
        duplicate = not recorded
        claim = store.claim_channel_conversion(message_id, conversion_lease_seconds)
        if not claim["claimed"]:
            row = claim["row"]
            if claim["reason"] == "ALREADY_LINKED" and row and row["mission_id"]:
                dispatch = ensure_channel_dispatch(row["mission_id"])
                return {"status": dispatch["status"], "message_id": message_id, "durable": True,
                        "mission_id": row["mission_id"], "duplicate": duplicate, **dispatch}
            if claim["reason"] == "IN_PROGRESS":
                return {"status": "IN_PROGRESS", "message_id": message_id, "durable": True,
                        "duplicate": duplicate}
            return {"status": "RETRY", "reason": "CONVERSION_CLAIM_FAILED",
                    "message_id": message_id, "durable": True, "duplicate": duplicate}
        owner = claim["owner"]
        text = claim["row"]["text"]
        # Budget gate (per-user monthly limit): runs inside the claim (so
        # duplicate deliveries cannot each hit it) and before the paid
        # conversion model, so a spent owner's message creates no mission and
        # consumes no operator conversion call. The message is parked at
        # DEFERRED — outside the monitor's RECEIVED/RETRY replay pool, so the
        # poll loop cannot re-defer it every round — and stays verbatim; a
        # re-sent message restarts it once the budget recovers.
        gate = budget_gate(owner_id, store)
        if not gate["allowed"]:
            store.release_channel_conversion(message_id, owner, status="DEFERRED")
            channel_budget_deferred(channel, message_id, gate["reason"])
            return {"status": "DEFERRED", "reason": "BUDGET_EXCEEDED",
                    "detail": gate["reason"], "message_id": message_id,
                    "durable": True, "duplicate": duplicate}
        try:
            conversion, fallback_provenance = convert_channel_text(
                channel, text, owner_id=owner_id)
        except ValueError as exc:
            store.release_channel_conversion(message_id, owner, status="CONVERSION_FAILED")
            if recorded:
                store.record_channel_event(channel, message_id, "CONVERSION_FAILED",
                                           {"error": str(exc), "original_input": text})
            return {"status": "CONVERSION_FAILED", "message_id": message_id, "durable": True,
                    "duplicate": duplicate, "error": str(exc)}
        conversion_payload = conversion_payload_for(channel, message_id, conversion)
        if fallback_provenance:
            conversion_payload["fallback_from_model"] = fallback_provenance["model_id"]
            conversion_payload["fallback_reason_class"] = fallback_provenance["reason_class"]
            conversion_payload["fallback_source"] = fallback_provenance["fallback_source"]
        try:
            mission_id, _created = store.create_mission_from_message(
                message_id, channel, title=conversion.title,
                original_input=conversion.original, labels=list(conversion.steps),
                professional_prompt=conversion.professional, conversion_payload=conversion_payload,
                owner_id=owner_id)
        except Exception:
            # Never strand the lease on an unexpected failure: release it so the
            # next delivery or the monitor can replay this message.
            store.release_channel_conversion(message_id, owner)
            raise
        if mission_id is None:
            store.release_channel_conversion(message_id, owner)
            return {"status": "RETRY", "reason": "MISSION_CREATION_FAILED",
                    "message_id": message_id, "durable": True, "duplicate": duplicate}
        if recorded:
            store.record_channel_event(channel, message_id, "PROMPT_CONVERTED", conversion_payload)
        dispatch = ensure_channel_dispatch(mission_id)
        if not duplicate and dispatch.get("status") == "QUEUED":
            # 80% soft threshold: whichever gate first crossed the threshold
            # (the caller's pre-gate, the pre-conversion one above or the
            # dispatch one inside) holds the month's only alert; attach it to
            # the ack reply.
            send_channel_ack(channel, mission_id,
                             alert=gate.get("alert") or dispatch.get("alert") or alert)
        if not duplicate and dispatch.get("reason") == "BUDGET_EXCEEDED":
            # An inbound message blocked by the budget must not leave the sender
            # in silence: reuse the same audited explain-and-reply helper as the
            # manual dispatch endpoint, so every channel gets one Chinese reply.
            channel_budget_deferred(channel, message_id,
                                    dispatch.get("detail") or "预算已用尽")
        return {"status": dispatch["status"], "message_id": message_id, "durable": True,
                "mission_id": mission_id, "duplicate": duplicate, **dispatch}

    def send_channel_ack(channel, mission_id, alert=None):
        """Best-effort chat acknowledgement for a freshly created mission.

        ``alert`` is the budget gate's 80% soft-threshold reminder for the
        dispatch that created this mission: when present, its fixed line is
        appended to the ack so the sender learns about the low balance in the
        reply they were already getting — no separate message, never a gate.
        """
        if channel not in NOTIFY_CHANNELS:
            return
        mission = store.get(mission_id)
        # A hosted owner's mission is acked through the OWNER'S OWN provider
        # instance -- never the station's account: the sender chats with the
        # customer's personal WeChat, not with the operator's.
        provider = None
        if mission is not None and channel == "wechat":
            owner = mission.get("owner_id", "admin")
            host_binding = store.host_binding(owner, "wechat")
            if host_binding is not None and host_binding["state"] == "ACTIVE":
                provider = host_wechat_providers.get(owner)
        if provider is None:
            # Registry stand-ins may expose providers as a plain mapping or only
            # require(); a missing lookup just means no ack, never a crash.
            try:
                provider = channels.get(channel)
            except AttributeError:
                providers = getattr(channels, "providers", None)
                if isinstance(providers, dict):
                    provider = providers.get(channel)
        if provider is None or not provider.configured() or mission is None:
            return
        sender = store.channel_sender_for_mission(mission_id, channel)
        if not sender:
            return
        text = (f"🚀 已收到需求并创建任务「{mission['title']}」，共 {len(mission['steps'])} 个阶段，"
                f"已开始自动执行。发送新消息可追加任务；查看进度：https://whstapp.lol/?mission={mission_id}")
        if alert and alert.get("text"):
            text += f"\n\n⚠️ {alert['text']}"
        try:
            import asyncio
            from .channels import OutboundReply
            result = asyncio.run(provider.send(OutboundReply(channel=channel, recipient=sender, text=text)))
            # Audit the ack like every other outbound chat line (text only) —
            # unless the provider records its own delivery (relay outbox).
            if not getattr(provider, "records_outbound_itself", False):
                try:
                    store.record_channel_message(
                        channel, "OUTBOUND",
                        result.get("status", "SENT") if isinstance(result, dict) else "SENT",
                        external_id=result.get("external_id") if isinstance(result, dict) else None,
                        recipient=sender, text=text, mission_id=mission_id,
                        owner_id=mission.get("owner_id", "admin"))
                except Exception:
                    logger.warning("channel ack audit failed for %s", channel, exc_info=True)
        except Exception:
            logger.warning("channel ack send failed for %s", channel, exc_info=True)

    def process_channel_message(channel, message, owner_id=None):
        """Persist original channel text, convert it and retry its idempotent dispatch.

        ``owner_id`` pins the attribution for background pipelines: the
        per-owner hosted-WeChat monitor passes its binding's owner, so a
        customer's message becomes the CUSTOMER's mission. Webhook/request
        paths keep the request context (current_owner()).
        """
        payload = dict(message.raw or {})
        context_token = (message.metadata or {}).get("context_token")
        if context_token:
            payload["context_token"] = context_token
        owner = owner_id or current_owner()
        message_id = store.record_channel_message(
            channel, "INBOUND", "RECEIVED", external_id=message.external_id,
            sender=message.sender, recipient=message.recipient, kind=message.kind,
            text=message.text, payload=payload, owner_id=owner)
        recorded = message_id is not None
        if message_id is None:
            existing = store.channel_message_by_external_id(channel, message.external_id,
                                                            owner_id=owner)
            if existing is None:
                return {"status": "RETRY", "reason": "MESSAGE_LOOKUP_FAILED", "durable": False}
            message_id = existing["id"]
        existing = store.channel_message(message_id)
        if existing["mission_id"]:
            dispatch = ensure_channel_dispatch(existing["mission_id"])
            return {"status": dispatch["status"], "message_id": message_id, "durable": True,
                    "mission_id": existing["mission_id"], "duplicate": not recorded, **dispatch}
        # 渠内审批拦截（转任务之前）：一句「批准」/「取消」是对既有任务裁决的
        # 命令，不是新需求。命中活动审批行时在此终结（行被原子消费/作废，消息
        # 状态改写，绝不进入转换池）；未命中（无文本匹配或无活动行）返回 None，
        # 照旧走任务转换。
        approval = handle_channel_approval(owner, channel, message_id, existing["text"],
                                           sender=existing.get("sender"))
        if approval is not None:
            return approval
        return convert_and_link_mission(channel, message_id, recorded, owner_id=owner)

    def process_telegram_message(message):
        return process_channel_message("telegram", message)

    def _wechat_message_from_row(row):
        """Rebuild a normalized inbound message from a durable audit row."""
        payload = row.get("payload") or {}
        return InboundMessage(
            channel="wechat", external_id=row["external_id"], sender=row["sender"] or "",
            text=row["text"], received_at=row["created_at"], recipient=row["recipient"],
            kind=row["kind"], raw=payload,
            metadata={"context_token": payload.get("context_token")})

    def _wechat_message_id(external_id, owner_id="admin"):
        try:
            existing = store.channel_message_by_external_id("wechat", external_id,
                                                            owner_id=owner_id)
        except Exception:
            logger.exception("wechat: could not look up message %s", external_id)
            return None
        return existing["id"] if existing else None

    def _wechat_retry_audit(message_id, event_type, reason, exc=None):
        """Best-effort RETRY audit; a failing write must never stop the task."""
        try:
            store.set_channel_message_status(message_id, "RETRY")
        except Exception:
            logger.exception("wechat: could not mark message %s RETRY", message_id)
        payload = {"reason": reason}
        if exc is not None:
            payload["error"] = str(exc)
            payload["error_type"] = type(exc).__name__
        try:
            store.record_channel_event("wechat", message_id, event_type, payload)
        except Exception:
            logger.exception("wechat: could not record %s for message %s",
                             event_type, message_id)

    def _process_wechat_message(message, owner_id="admin"):
        """Process one inbound message in isolation; never abort the monitor.

        ``owner_id`` is the attribution of the message's account: the station
        monitor's own account is the operator ("admin"), each hosted customer's
        monitor passes its binding owner, so rows and missions always land on
        the account that actually received the message.
        """
        message_id = _wechat_message_id(message.external_id, owner_id)
        try:
            result = process_channel_message("wechat", message, owner_id=owner_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("wechat: processing message %s failed", message.external_id)
            if message_id is None:
                message_id = _wechat_message_id(message.external_id, owner_id)
            if message_id is not None:
                _wechat_retry_audit(message_id, "PROCESSING_RETRY", "PROCESSING_ERROR", exc)
            return {"status": "RETRY", "reason": "PROCESSING_ERROR",
                    "message_id": message_id, "durable": message_id is not None}
        if result.get("status") == "RETRY":
            if message_id is None:
                message_id = _wechat_message_id(message.external_id, owner_id)
            if message_id is not None:
                _wechat_retry_audit(message_id, "MISSION_CREATION_FAILED",
                                    result.get("reason", "MISSION_CREATION_FAILED"))
                result["message_id"] = message_id
                result["durable"] = True
        return result

    def _retry_deferred_dispatches(channels):
        """Retry durable channel missions after an operator fixes their binding."""
        for message in store.deferred_channel_messages(channels):
            mission_id = message["mission_id"]
            mission = store.get(mission_id)
            if mission is None:
                continue
            owner = mission.get("owner_id", "admin")
            if mission.get("agent_id"):
                agent = registries.resolve("agents", mission.get("agent_id"), owner_id=owner)
                model = registries.resolve("models", mission.get("model_id"), owner_id=owner)
                runtime = registries.resolve("runtimes", mission["runtime_id"], owner_id=owner)
                binding_ready = bool(agent and model and runtime
                                     and agent.get("adapter") == "opencode")
            else:
                binding_ready = default_binding(owner_id=owner) is not None
            if binding_ready:
                ensure_channel_dispatch(mission_id)

    async def wechat_monitor():
        """Long-poll personal WeChat and feed messages into the same audited pipeline."""
        provider = channels.require("wechat")
        while True:
            try:
                if not provider.configured():
                    await asyncio.sleep(wechat_error_backoff)
                    continue
                # Return fresh crashed conversion claims to the RETRY pool before
                # replaying, so a message abandoned mid-conversion is retried.
                store.recover_stale_channel_conversions()
                # A previously deferred Mission is already durable and therefore
                # absent from pending_inbound_messages. Retry its queue binding
                # when registry entries become available, without new upstream
                # delivery or repeated deferred events.
                _retry_deferred_dispatches(("wechat",))
                # Replay durable pending messages before polling so a message
                # recorded just before a transient failure is never lost.
                # Scoped to the STATION account ("admin"): hosted customers'
                # rows belong to their own monitors, never to this loop.
                for pending in store.pending_inbound_messages("wechat", owner_id="admin"):
                    await run_in_threadpool(
                        _process_wechat_message, _wechat_message_from_row(pending))
                result = await provider.fetch_updates()
                if result.get("state") != "OK":
                    await asyncio.sleep(wechat_error_backoff)
                    continue
                for message in result.get("messages", []):
                    processed = await run_in_threadpool(_process_wechat_message, message)
                    if not processed.get("durable", processed.get("status") != "RETRY"):
                        # Keep the old iLink cursor so the next poll can replay a
                        # batch that failed before an inbound message was stored.
                        break
                else:
                    acknowledge = getattr(provider, "acknowledge_updates", None)
                    if callable(acknowledge):
                        acknowledge()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("wechat: monitoring round failed; retrying")
                await asyncio.sleep(wechat_error_backoff)
                continue
            await asyncio.sleep(wechat_poll_interval)

    @app.get("/api/v1/inbox")
    def inbox(limit: int = 100):
        # Owner-scoped: tenants see their own channel inbox, the operator sees all.
        return store.channel_messages(limit=limit, owner_id=current_owner())

    @app.get("/api/v1/missions/{mission_id}/channel-messages")
    def mission_channel_messages(mission_id: str):
        require_mission(mission_id)
        return store.messages_for_mission(mission_id)

    @app.get("/api/v1/channels/{channel}/messages/{message_id}/events")
    def channel_message_events(channel: str, message_id: str):
        require_channel(channel)
        if store.channel_message(message_id, owner_id=current_owner()) is None:
            raise HTTPException(404, "Channel message not found")
        return store.channel_events(message_id=message_id, channel=channel)

    @app.post("/api/v1/channels/{channel}/messages/{message_id}/dispatch", status_code=201)
    def dispatch_channel_message(channel: str, message_id: str):
        require_channel(channel)
        message = store.channel_message(message_id, owner_id=current_owner())
        if message is None or message["channel"] != channel or message["direction"] != "INBOUND":
            raise HTTPException(404, "Inbound channel message not found")
        if message["mission_id"]:
            ensure_channel_dispatch(message["mission_id"])
            return public_mission(require_mission(message["mission_id"]))
        # Budget gate (per-user monthly limit): block the conversion of this
        # message into a NEW mission. Unlike the plain API paths (402), the
        # channel path stays asynchronous: audit a BUDGET_EXCEEDED event,
        # best-effort tell the sender why, and defer the message.
        gate = budget_gate(current_owner(), store)
        if not gate["allowed"]:
            return JSONResponse(channel_budget_deferred(channel, message_id, gate["reason"]),
                                status_code=202)
        result = convert_and_link_mission(channel, message_id, recorded=True,
                                          alert=gate.get("alert"))
        if result["status"] == "CONVERSION_FAILED":
            # A failed conversion never drops the message: it stays verbatim and
            # can be dispatched again once its content is understood.
            raise HTTPException(422, f"消息无法转换为专业提示词：{result.get('error', '')}") from None
        if result["status"] == "IN_PROGRESS":
            # Another delivery already holds the conversion lease; report that
            # truthfully instead of calling the model or creating a second task.
            return JSONResponse(result, status_code=202)
        mission_id = result.get("mission_id")
        if not mission_id:
            raise HTTPException(503, "消息暂时无法创建任务，请稍后重试")
        ensure_channel_dispatch(mission_id)
        return public_mission(require_mission(mission_id))

    @app.post("/api/v1/channels/wechat/login")
    async def wechat_login_start():
        require_admin_owner()
        provider = require_channel("wechat")
        if provider.configured():
            return {"state": "AUTHORIZED"}
        result = await provider.start_login()
        if result.get("state") == "FAILED":
            raise HTTPException(502, result.get("error", "WECHAT_LOGIN_FAILED"))
        return result

    @app.post("/api/v1/channels/wechat/messages/{message_id}/reply")
    async def wechat_reply(message_id: str, body: ChannelReply):
        require_admin_owner()
        provider = require_channel("wechat")
        if not provider.configured():
            raise unconfigured(provider)
        source = store.channel_message(message_id, owner_id=current_owner())
        if source is None or source["channel"] != "wechat" or source["direction"] != "INBOUND":
            raise HTTPException(404, "Inbound WeChat message not found")
        metadata = {"context_token": source["payload"].get("context_token")}
        result = await provider.send(OutboundReply(channel="wechat", recipient=source["sender"],
                                                   text=body.text, metadata=metadata))
        store.record_channel_message("wechat", "OUTBOUND", result.get("status", "FAILED"),
            external_id=result.get("external_id"), recipient=source["sender"], kind="text",
            text=body.text, payload={"reply_to_message": message_id,
                                     "http_status": result.get("http_status"),
                                     "error": result.get("error")}, mission_id=source.get("mission_id"),
            owner_id=current_owner())
        return result

    @app.get("/api/v1/channels/wechat/login")
    async def wechat_login_status():
        require_admin_owner()
        provider = require_channel("wechat")
        result = await provider.poll_login()
        if result.get("state") == "AUTHORIZED":
            store.record_channel_event("wechat", result["account"], "ACCOUNT_AUTHORIZED",
                                       {"account": result["account"], "method": "QR"})
        return result

    @app.post("/api/v1/channels/wechat/login/verify")
    def wechat_login_verify(body: WeChatVerifyCode):
        require_admin_owner()
        provider = require_channel("wechat")
        try:
            return provider.submit_verify_code(body.code)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/v1/channels/wechat/unbind")
    def wechat_unbind():
        """Drop the stored iLink authorization so a stale token can re-scan.

        ``POST /login`` short-circuits to ``AUTHORIZED`` while credentials are
        loaded (api.py wechat_login_start), so a revoked or expired token left
        the operator with no way back to the QR flow. This endpoint is its
        inverse: delete the data-side provider credential file, clear the
        in-process env keys and reset any in-flight login. The systemd unit
        also injects ``/etc/facai-cloud/providers/*.env`` at start; that
        operator-managed copy is outside the data directory and outside this
        endpoint's reach, so the response says so instead of pretending the
        unbind survives a restart.
        """
        require_admin_owner()
        provider = require_channel("wechat")
        account = provider.setting("WECHAT_ILINK_USER_ID") or None
        try:
            file_removed = provider.unbind()
        except OSError:
            raise HTTPException(
                503, "解除授权失败：微信凭据文件无法删除，请检查数据目录权限") from None
        store.record_channel_event("wechat", account or "wechat", "CHANNEL_UNBOUND",
                                   {"account": account, "method": "admin-unbind",
                                    "credential_file_removed": file_removed})
        return {
            "state": "UNBOUND",
            "configured": provider.configured(),
            "credential_file_removed": file_removed,
            "note": ("数据侧凭据文件已删除，进程内凭据已清除。"
                     "若部署由 systemd 注入 /etc/facai-cloud/providers/wechat.env 副本，"
                     "需操作员同步删除该副本并重启服务，否则重启后旧凭据会重新加载。"),
        }

    @app.get("/api/v1/channels/{channel}/webhook")
    def channel_webhook_verify(channel: str, request: Request):
        provider = require_channel(channel)
        if channel == "telegram":
            raise HTTPException(405, "Telegram webhooks accept POST only", headers={"Allow": "POST"})
        if not provider.implemented:
            raise HTTPException(501, f"{channel} adapter interface is reserved, not wired")
        if not provider.configured():
            raise unconfigured(provider)
        challenge = provider.verify_challenge(request.query_params)
        if challenge is None:
            raise HTTPException(403, "Webhook verification failed")
        return PlainTextResponse(challenge)

    @app.post("/api/v1/channels/{channel}/webhook")
    async def channel_webhook_receive(channel: str, request: Request):
        provider = require_channel(channel)
        if not provider.implemented:
            raise HTTPException(501, f"{channel} adapter interface is reserved, not wired")
        if not provider.configured():
            raise unconfigured(provider)
        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 1024 * 1024:
                raise HTTPException(413, "Webhook body exceeds 1 MiB")
            chunks.append(chunk)
        body = b"".join(chunks)
        if not provider.verify_signature(body, request.headers):
            raise HTTPException(403, "Webhook signature verification failed")
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            raise HTTPException(400, "Webhook body must be valid JSON") from None
        try:
            messages = provider.parse_inbound(payload)
        except ValueError:
            raise HTTPException(400, "Invalid channel message payload") from None
        except NotImplementedError:
            raise HTTPException(501, f"{channel} inbound parsing is reserved, not wired") from None
        # relay (AutoClaw bridge) walks the exact same pipeline: idempotent dedupe,
        # conversion lease, verbatim original, full-DAG dispatch and chat ack.
        # line dispatches like telegram: same signed-webhook + push-adapter shape.
        # discord/slack join via the same contract (bridge HMAC / Slack native v0
        # HMAC) so a signed delivery starts a mission exactly like the others.
        dispatch_channels = ("telegram", "whatsapp", "relay", "line", "discord", "slack")
        if channel in dispatch_channels:
            # Without a long-poll monitor, the next webhook delivery is the
            # natural recovery point for a claim abandoned by a crashed worker.
            store.recover_stale_channel_conversions()
        received = 0
        dispatch_results = []
        for message in messages:
            if channel in dispatch_channels:
                dispatch_results.append(await run_in_threadpool(
                    process_channel_message, channel, message))
                received += dispatch_results[-1].get("duplicate") is False
                continue
            recorded = store.record_channel_message(
                channel, "INBOUND", "RECEIVED", external_id=message.external_id,
                sender=message.sender, recipient=message.recipient, kind=message.kind,
                text=message.text, payload=message.raw, owner_id=current_owner())
            received += recorded is not None
        if channel in dispatch_channels:
            if not dispatch_results:
                return {"received": 0, "dispatch": "NO_MESSAGES", "conversion_failed": 0,
                        "results": []}
            # A budget-blocked message is durable and accepted — answering 503
            # would make the upstream retry until the budget recovers (days),
            # so it gets a 202. A binding deferral or a RETRY stays 503: the
            # bridge's redelivery is what re-runs the pipeline once the
            # operator registers the missing binding (or the failure clears).
            transient = [result for result in dispatch_results
                         if result["status"] == "RETRY"]
            binding_deferred = [result for result in dispatch_results
                                if result["status"] == "DEFERRED"
                                and result.get("reason") != "BUDGET_EXCEEDED"]
            budget_deferred = [result for result in dispatch_results
                               if result["status"] == "DEFERRED"
                               and result.get("reason") == "BUDGET_EXCEEDED"]
            conversion_failed = sum(result["status"] == "CONVERSION_FAILED" for result in dispatch_results)
            if transient or binding_deferred:
                raise HTTPException(503, {"received": received,
                                          "dispatch": "RETRY" if transient else "DEFERRED",
                                          "results": dispatch_results})
            if budget_deferred:
                return JSONResponse({"received": received, "dispatch": "DEFERRED",
                                     "conversion_failed": conversion_failed,
                                     "results": dispatch_results}, status_code=202)
            return {"received": received, "dispatch": "QUEUED", "conversion_failed": conversion_failed,
                    "results": dispatch_results}
        return {"received": received}

    @app.post("/api/v1/channels/relay/send")
    async def relay_send(request: Request):
        """AutoClaw outbound poll: acknowledge delivered ids, fetch queued replies.

        Signed exactly like the inbound webhook (same shared secret, same
        headers), so the bridge client never holds an interactive session.
        Replies stay QUEUED until their ids are acknowledged (at-least-once);
        an acknowledged id is never returned again.
        """
        provider = channels.get("relay")
        if provider is None or not provider.implemented:
            raise HTTPException(501, "relay adapter interface is reserved, not wired")
        if not provider.configured():
            raise unconfigured(provider)
        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 1024 * 1024:
                raise HTTPException(413, "Relay body exceeds 1 MiB")
            chunks.append(chunk)
        body = b"".join(chunks)
        if not provider.verify_signature(body, request.headers):
            raise HTTPException(403, "Relay signature verification failed")
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            raise HTTPException(400, "Relay body must be valid JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "Relay body must be a JSON object")
        ack_ids = payload.get("ack_ids") or []
        if not isinstance(ack_ids, list) or any(not isinstance(i, str) for i in ack_ids):
            raise HTTPException(400, "ack_ids must be a list of message ids")
        acked = store.mark_channel_outbound_sent("relay", ack_ids[:100])
        messages = store.channel_outbound_pending("relay", limit=100)
        return {"status": "PENDING" if messages else "EMPTY", "acked": acked,
                "messages": messages}

    @app.post("/api/v1/channels/{channel}/send")
    async def channel_send(channel: str, body: ChannelSend):
        # Operator-only: this drives the operator's personal sender identities
        # (Telegram/WeChat/WhatsApp). A tenant with it could message anyone
        # from the station's accounts — never exposed to self-registered users.
        require_admin_owner()
        provider = require_channel(channel)
        if not provider.implemented:
            raise HTTPException(501, f"{channel} adapter interface is reserved, not wired")
        if not provider.configured():
            raise unconfigured(provider)
        reply = OutboundReply(channel=channel, recipient=body.recipient,
                              text=body.text, reply_to=body.reply_to)
        try:
            result = await provider.send(reply)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except NotImplementedError:
            raise HTTPException(501, f"{channel} outbound send is reserved, not wired") from None
        store.record_channel_message(
            channel, "OUTBOUND", result.get("status", "FAILED"),
            external_id=result.get("external_id"), recipient=body.recipient,
            kind="text", text=body.text,
            payload={"http_status": result.get("http_status"), "error": result.get("error")},
            owner_id=current_owner())
        return result

    # --- n8n automation --------------------------------------------------------

    def public_delivery(delivery):
        """Delivery view without the forwarded payload or any secret."""
        return {key: delivery[key] for key in (
            "id", "request_id", "mission_id", "event_id", "event_type", "status",
            "attempts", "max_attempts", "http_status", "error", "retryable",
            "created_at", "updated_at")}

    async def read_limited_body(request, limit=1024 * 1024):
        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, "Request body exceeds 1 MiB")
            chunks.append(chunk)
        return b"".join(chunks)

    @app.get("/api/v1/n8n")
    def n8n_status():
        require_admin_owner()
        return {
            **n8n.status(),
            "summary": store.n8n_delivery_summary(),
            "recent": [public_delivery(d) for d in store.n8n_deliveries(15)],
            "recent_callbacks": store.n8n_callbacks(10),
        }

    @app.post("/api/v1/n8n/relay")
    async def n8n_relay_run():
        # Same operator-lever rule as /api/v1/scheduler/run: the relay pump
        # dispatches the station's outbound delivery queue, so a tenant must
        # not be able to force ticks (its state line is also station posture,
        # not tenant data).
        require_admin_owner()
        return await run_in_threadpool(app.state.n8n_relay.run_pending)

    @app.post("/api/v1/n8n/callback")
    async def n8n_callback(request: Request):
        """Signed, idempotent callback that updates a mission's events/steps.

        n8n authenticates with the same HMAC scheme Facai uses outbound. The
        callback is public at the transport layer (no browser session) but is
        rejected unless the timestamp is fresh and the signature matches.
        """
        if not n8n.callback_ready():
            raise HTTPException(503, {
                "state": n8n.status()["state"],
                "missing": n8n.missing(),
                "detail": "n8n 回调签名密钥未配置"})
        body = await read_limited_body(request)
        if not n8n.verify(body, request.headers):
            raise HTTPException(403, "n8n signature verification failed")
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            raise HTTPException(400, "Callback body must be valid JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "Callback body must be a JSON object")
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise HTTPException(422, "request_id is required")
        mission = store.get(payload.get("mission_id") or "")
        if mission is None:
            raise HTTPException(404, "Mission not found")
        mission_id = mission["id"]
        status = payload.get("status") or "SUCCEEDED"
        if status not in CALLBACK_STATUSES:
            raise HTTPException(422, "Invalid callback status")
        error = payload.get("error") if isinstance(payload.get("error"), dict) else None
        retryable = bool(error.get("retryable")) if error else False
        detail = json.dumps({"status": status, "error": error,
                             "retry": payload.get("retry")}, ensure_ascii=False)
        claimed, existing = store.claim_n8n_callback(request_id, mission_id, status, detail)
        if not claimed:
            return {"duplicate": True, "request_id": request_id, "mission_id": mission_id,
                    "status": existing["status"]}
        applied = {"event_id": None, "step": None}
        try:
            event = payload.get("event")
            if isinstance(event, dict) and event.get("type"):
                applied["event_id"] = store.append(
                    mission_id, str(event["type"]),
                    event.get("payload") if isinstance(event.get("payload"), dict) else {},
                    event.get("step_id"), event.get("session_id"), event.get("agent_id"))
            step = payload.get("step")
            if isinstance(step, dict) and step.get("step_id") and step.get("status"):
                store.step_status(mission_id, step["step_id"], step["status"])
                applied["step"] = {"step_id": step["step_id"], "status": step["status"]}
        except KeyError:
            store.finish_n8n_callback(request_id, status, "UNKNOWN_STEP")
            raise HTTPException(404, "Step not found") from None
        except ValueError as exc:
            store.finish_n8n_callback(request_id, status, f"INVALID_TRANSITION: {exc}")
            raise HTTPException(409, str(exc)) from None
        return {"duplicate": False, "request_id": request_id, "mission_id": mission_id,
                "status": status, "applied": applied, "retryable": retryable}

    # Additive preview/confirmation boundary. Confirmation never issues a grant
    # or enqueues work; the explicit authorization action uses the existing gate.
    from .intent_routes import router_for as intent_router

    def before_intent_mission(owner):
        gate = budget_gate(owner, store)
        if not gate["allowed"]:
            raise HTTPException(402, gate["reason"])
        reserve_mission_create(owner)

    def authorize_intent_mission(mission_id):
        require_admin_owner()
        require_mission(mission_id, allow_assist=False)
        gate = budget_gate(current_owner(), store)
        if not gate["allowed"]:
            raise HTTPException(402, gate["reason"])
        grant = authority_gate.ensure_mission_grant(mission_id)
        store.append(mission_id, "INTENT_EXECUTION_AUTHORIZED", {
            "actor": current_owner(), "grant_id": grant.grant_id,
            "execution": "NOT_DISPATCHED"})
        return {"mission_id": mission_id, "authorization": "GRANTED",
                "execution": "NOT_DISPATCHED", "dispatch_via": "existing_mission_console"}

    app.include_router(intent_router(store, current_owner, before_intent_mission,
                                    authorize_intent_mission))
    from .trust_routes import router_for as trust_router

    def require_human_review(request):
        # A scoped automation key may submit evidence, but it cannot impersonate
        # the Owner's interactive review or acceptance. Keep CSRF in middleware.
        if auth.enabled and (bearer_api_key(request) is not None or auth.session_from_request(request) is None):
            raise HTTPException(403, 'Owner review requires an authenticated console session')

    app.include_router(trust_router(store, current_owner, require_mission, require_human_review))
    from .site_assessment_routes import router_for as site_assessment_router
    app.include_router(site_assessment_router(store, current_owner, require_human_review))
    from .quick_scan_routes import router_for as quick_scan_router
    app.include_router(quick_scan_router(store))
    from .setup_routes import router_for as setup_router
    from .mission_spec import digest as specification_digest

    def setup_model_fingerprint(model_id):
        model = registries.resolve('models', model_id, owner_id=current_owner())
        if model is None:
            return None
        key = None
        if model.get('user_model_id'):
            key = store.user_model_encrypted_key(current_owner(), model['user_model_id'])
        elif model.get('api_key_env'):
            key = os.getenv(model['api_key_env'], '')
        return specification_digest({'configuration': model, 'credential': key})

    async def setup_model_probe(model_id):
        model = registries.resolve('models', model_id, owner_id=current_owner())
        if model is None:
            raise HTTPException(404, 'Model not found')
        if model.get('user_model_id'):
            return await test_user_model(model['user_model_id'])
        return await test_model_connection(model_id)

    app.include_router(setup_router(store, auth, current_owner, require_admin_owner, registries,
                                   setup_model_probe, setup_model_fingerprint))
    return app


app = create_app()
