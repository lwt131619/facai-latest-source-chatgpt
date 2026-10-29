#!/usr/bin/env python3
import hashlib as _hl
import fcntl as _fcntl
"""雾核 · 会员详情面板 (BFF over New API)

登录进程:  账号/密码经 New API `POST /api/user/login` 校验 -> 服务端会话 + HttpOnly Cookie
详情面板:  /portal/api/{me,usage,tokens,logs}  只读聚合 New API 真源库
安全边界:  数据库 mode=ro 打开; 不下发 users.password / access_token;
           tokens.key 仅本人点击"显示"时经鉴权接口单条返回; 不复用/回显上游 API_KEY
"""
import os
import re
import time
import json
import sqlite3
import secrets
from contextlib import closing
import logging
from pathlib import Path
from datetime import datetime

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse, RedirectResponse

BASE = Path(__file__).resolve().parent
DB_PATH = os.environ.get("NEWAPI_DB", "/opt/new-api/data/one-api.db")
NEWAPI = os.environ.get("NEWAPI_BASE", "http://127.0.0.1:3000")
QUOTA_PER_USD = float(os.environ.get("QUOTA_PER_USD", "500000"))
SESSION_TTL = int(os.environ.get("PORTAL_SESSION_TTL", str(8 * 3600)))
COOKIE = "facai_portal"
COOKIE_PATH = "/portal"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("portal")


def _load_secret() -> bytes:
    p = BASE / "secret.key"
    if not p.exists():
        p.write_bytes(secrets.token_hex(32).encode())
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    return p.read_bytes().strip()


SECRET = _load_secret()

TIERS = {
    "default": {"name": "按量体验", "ratio": "1.0×", "price": "—", "desc": "未绑定月卡 · 余额按量计费"},
    "vip699": {"name": "雾核·基础月卡", "ratio": "0.5×", "price": "3,999 USDT/月", "desc": "含 1,000 会员 IP · 每月 1 日重置额度"},
    "vip999": {"name": "雾核·进阶月卡", "ratio": "0.35×", "price": "6,999 USDT/月", "desc": "含 2,500 会员 IP · 每月 1 日重置额度"},
    "vip1999": {"name": "雾核·旗舰月卡", "ratio": "0.15×", "price": "11,999 USDT/月", "desc": "含 6,000 会员 IP · 每月 1 日重置额度"},
    "svip": {"name": "雾核·无限月卡", "ratio": "0×", "price": "19,999 USDT/月", "desc": "会员 IP 不限 · 单月调用无上限"},
}

LEDGER_PATH = BASE / "owner_ledger.json"


def load_ledger() -> dict:
    """主体账号商务账本(可热更新, 每次请求读取)"""
    try:
        return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

app = FastAPI(title="facai-portal", docs_url=None, redoc_url=None, openapi_url=None)
sessions: dict = {}
fails: dict = {}


# ────────────────────────── 基础工具 ──────────────────────────
def db():
    conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=6)
    conn.row_factory = sqlite3.Row
    return conn


def client_ip(req: Request) -> str:
    for h in ("x-real-ip", "x-forwarded-for"):
        v = req.headers.get(h)
        if v:
            return v.split(",")[0].strip()
    return req.client.host if req.client else "-"


def usd(quota) -> float:
    try:
        return round(float(quota or 0) / QUOTA_PER_USD, 2)
    except (TypeError, ValueError):
        return 0.0


def mask_key(key: str) -> str:
    if not key:
        return "—"
    k = key if key.startswith("sk-") else "sk-" + key
    if len(k) <= 11:
        return k[:5] + "…"
    return k[:7] + "…" + k[-4:]


def norm_key(key: str) -> str:
    return key if (key or "").startswith("sk-") else "sk-" + (key or "")


def rate_limited(ip: str) -> bool:
    now = time.time()
    arr = [t for t in fails.get(ip, []) if now - t < 600]
    fails[ip] = arr
    return len(arr) >= 8


def note_fail(ip: str) -> None:
    fails.setdefault(ip, []).append(time.time())


def current(req: Request):
    sid = req.cookies.get(COOKIE)
    if not sid:
        return None
    s = sessions.get(sid)
    if not s:
        return None
    if s["exp"] < time.time():
        sessions.pop(sid, None)
        return None
    s["exp"] = time.time() + SESSION_TTL
    return s


def need_auth(req: Request):
    s = current(req)
    if not s:
        return None, JSONResponse({"success": False, "message": "未登录或会话已过期"}, status_code=401)
    return s, None


def set_session_cookie(resp: Response, sid: str) -> None:
    resp.set_cookie(
        COOKIE, sid, max_age=SESSION_TTL, path=COOKIE_PATH, httponly=True, secure=True, samesite="lax"
    )


# ────────────────────────── 页面 ──────────────────────────
@app.get("/portal/")
def index():
    return FileResponse(BASE / "static" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/portal")
def index_redirect():
    return RedirectResponse("/portal/", status_code=308)


@app.get("/healthz")
def healthz():
    return {"ok": True, "ts": int(time.time())}


# ────────────────────────── 自助注册（2026-10-26 Owner 令） ──────────────────────────
MEMBERS_P = Path("/opt/uncensor-relay/members.json")
TIERS_P = Path("/opt/uncensor-relay/tiers.json")
REG_LOCKF = "/opt/facai-portal/.reg.lock"
REG_RATE: dict = {}
EMAIL_RE = r"^[^@\s]{1,64}@[^@\s]{1,120}\.[A-Za-z]{2,12}$"


def _mask(k: str) -> str:
    return (k[:7] + "…" + k[-4:]) if len(k or "") > 12 else (k or "")


def _jload(p, dflt):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return dflt


def _jdump(p, obj):
    tmp = str(p) + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def _pw_hash(salt: str, pw: str) -> str:
    return _hl.sha256((salt + pw).encode("utf-8")).hexdigest()[:16]


def _pw_ok(salt: str, pw: str, want: str) -> bool:
    cands = {_pw_hash(salt, pw),
             _hl.sha256((pw + salt).encode("utf-8")).hexdigest()[:16],
             _hl.sha256(pw.encode("utf-8")).hexdigest()[:16],
             _hl.sha256(pw.encode("utf-8")).hexdigest()}
    return (want or "") in cands


def _reg_limited(ip: str) -> bool:
    now = time.time()
    arr = [t for t in REG_RATE.get(ip, []) if now - t < 3600]
    REG_RATE[ip] = arr
    return len(arr) >= 5


# ────────────── 图形验证码（2026-10-27 Owner 令：注册必须能验证） ──────────────
CAPTCHA: dict = {}
CAP_TTL = 300
CAP_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _cap_new() -> str:
    import random as _rnd
    return "".join(_rnd.choice(CAP_CHARS) for _ in range(5))


def _cap_svg(code: str) -> str:
    import random as _rnd
    w, h = 132, 44
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">' % (w, h, w, h),
             '<rect width="100%%" height="100%%" fill="#0b0d13"/>']
    for _ in range(6):
        parts.append('<path d="M%d %d Q%d %d %d %d" stroke="#2a3040" stroke-width="1" fill="none"/>'
                     % (_rnd.randint(0, w), _rnd.randint(0, h), _rnd.randint(0, w),
                        _rnd.randint(0, h), _rnd.randint(0, w), _rnd.randint(0, h)))
    x = 12
    for ch in code:
        col = _rnd.choice(["#d4af37", "#8fb8ff", "#3ddc84", "#e8ecf4", "#ffb86b"])
        rot = _rnd.randint(-18, 18)
        dy = _rnd.randint(-4, 4)
        parts.append('<text x="%d" y="%d" font-family="monospace" font-size="26" font-weight="700" '
                     'fill="%s" transform="rotate(%d %d %d)">%s</text>'
                     % (x, 31 + dy, col, rot, x + 9, 31 + dy, ch))
        x += 24
    parts.append('</svg>')
    return "".join(parts)


@app.get("/portal/api/captcha")
def captcha(req: Request):
    now = time.time()
    for k in [k for k, v in list(CAPTCHA.items()) if v[1] < now]:
        CAPTCHA.pop(k, None)
    code = _cap_new()
    cid = secrets.token_urlsafe(16)
    CAPTCHA[cid] = [code.upper(), now + CAP_TTL, 0]
    return JSONResponse({"success": True, "data": {"id": cid, "svg": _cap_svg(code),
                                                   "expires_in": CAP_TTL}},
                        headers={"Cache-Control": "no-store"})


def _cap_check(cid: str, code: str) -> tuple:
    ent = CAPTCHA.get(cid or "")
    if not ent:
        return False, "验证码已过期，请点图刷新"
    if ent[1] < time.time():
        CAPTCHA.pop(cid, None)
        return False, "验证码已过期，请点图刷新"
    ent[2] += 1
    if ent[2] > 5:
        CAPTCHA.pop(cid, None)
        return False, "验证码错误次数过多，请刷新"
    if (code or "").strip().upper() != ent[0]:
        return False, "验证码不对"
    CAPTCHA.pop(cid, None)
    return True, ""


@app.get("/portal/register")
def register_page():
    return FileResponse(BASE / "static" / "register.html", headers={"Cache-Control": "no-store"})


@app.get("/register")
def register_alias():
    return RedirectResponse("/portal/register", status_code=308)


@app.get("/login")
def login_alias():
    return RedirectResponse("/portal/", status_code=308)


@app.post("/portal/api/register")
async def register(req: Request):
    ip = client_ip(req)
    if _reg_limited(ip):
        return JSONResponse({"success": False, "message": "注册过于频繁，请 1 小时后再试"}, status_code=429)
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"success": False, "message": "请求格式错误"}, status_code=400)
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    _okc, _whyc = _cap_check(body.get("captcha_id") or "", body.get("captcha_code") or "")
    if not _okc:
        return JSONResponse({"success": False, "message": _whyc}, status_code=400)
    import re as _re
    if not _re.match(EMAIL_RE, email):
        return JSONResponse({"success": False, "message": "邮箱格式不对"}, status_code=400)
    if len(password) < 8:
        return JSONResponse({"success": False, "message": "密码至少 8 位"}, status_code=400)
    if len(password) > 200:
        return JSONResponse({"success": False, "message": "密码超长"}, status_code=400)

    lf = open(REG_LOCKF, "a+")
    try:
        _fcntl.flock(lf, _fcntl.LOCK_EX)
        mem = _jload(MEMBERS_P, {"members": {}})
        members = mem.setdefault("members", {})
        if email in members:
            return JSONResponse({"success": False, "message": "这个邮箱已经注册过，直接登录即可"}, status_code=409)
        salt = secrets.token_hex(8)
        key = "sk-oc-" + secrets.token_hex(12)
        members[email] = {"salt": salt, "pw": _pw_hash(salt, password), "key": key,
                          "created": time.strftime("%Y-%m-%d %H:%M:%S"), "self": "1"}
        _jdump(MEMBERS_P, mem)
        tiers = _jload(TIERS_P, {"tiers": {}, "keys": {}})
        tiers.setdefault("keys", {})[key] = {"tier": "default", "note": "self-reg %s" % email}
        _jdump(TIERS_P, tiers)
    finally:
        try:
            _fcntl.flock(lf, _fcntl.LOCK_UN)
        finally:
            lf.close()

    REG_RATE.setdefault(ip, []).append(time.time())
    sid = secrets.token_urlsafe(32)
    sessions[sid] = {"uid": 0, "username": email, "ip": ip, "exp": time.time() + SESSION_TTL,
                     "born": time.time(), "kind": "member", "mkey": key}
    log.info("register ok ip=%s email=%s", ip, email)
    resp = JSONResponse({"success": True, "data": {
        "email": email, "key": key, "key_masked": _mask(key),
        "endpoint": "https://openingcode.top/v1", "models": ["Facai-DS", "Facai-168"]}})
    set_session_cookie(resp, sid)
    return resp


@app.get("/portal/member")
def member_page():
    return FileResponse(BASE / "static" / "member.html", headers={"Cache-Control": "no-store"})


@app.get("/portal/api/member/me")
def member_me(req: Request):
    s, err = need_auth(req)
    if err:
        return err
    if s.get("kind") != "member":
        return JSONResponse({"success": False, "message": "非自助账号"}, status_code=400)
    key = s.get("mkey") or ""
    led = _jload("/opt/uncensor-relay/usage_ledger.json", {})
    row = led.get(key) or {}
    tinfo = (_jload(TIERS_P, {}).get("tiers", {}) or {}).get(row.get("tier") or "default", {}) or {}
    return JSONResponse({"success": True, "data": {
        "email": s.get("username"), "key": key, "key_masked": _mask(key),
        "created": _jload(MEMBERS_P, {"members": {}})["members"].get(s.get("username"), {}).get("created"),
        "tier": tinfo.get("label") or "default",
        "usage": {"requests": row.get("requests", 0), "tokens_in": int(row.get("tokens_in", 0)),
                  "tokens_out": int(row.get("tokens_out", 0)), "usd": round(float(row.get("usd_total", 0)), 4),
                  "last_used": row.get("last_used")},
        "endpoint": "https://openingcode.top/v1", "models": ["Facai-DS", "Facai-168"]}})


# ────────────────────────── 登录进程 ──────────────────────────
@app.post("/portal/api/login")
async def login(req: Request):
    ip = client_ip(req)
    if rate_limited(ip):
        return JSONResponse({"success": False, "message": "尝试过于频繁，请 10 分钟后再试"}, status_code=429)
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"success": False, "message": "请求格式错误"}, status_code=400)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        return JSONResponse({"success": False, "message": "请输入账号与密码"}, status_code=400)
    if len(username) > 64 or len(password) > 200:
        return JSONResponse({"success": False, "message": "输入超长"}, status_code=400)

    # 2026-10-26: 先查自助注册账号（members.json），命中即建 member 会话，不走 New API
    try:
        _mem = _jload(MEMBERS_P, {"members": {}}).get("members", {})
        _ent = _mem.get(username.lower())
        if _ent and _pw_ok(_ent.get("salt") or "", password, _ent.get("pw") or ""):
            sid = secrets.token_urlsafe(32)
            sessions[sid] = {"uid": 0, "username": username.lower(), "ip": ip, "exp": time.time() + SESSION_TTL,
                             "born": time.time(), "kind": "member", "mkey": _ent.get("key") or ""}
            log.info("login ok(member) ip=%s email=%s", ip, username.lower())
            resp = JSONResponse({"success": True, "data": {"username": username.lower(), "kind": "member"}})
            set_session_cookie(resp, sid)
            return resp
        if _ent:
            note_fail(ip)
            return JSONResponse({"success": False, "message": "账号或密码错误"}, status_code=401)
    except Exception as _e:
        log.warning("member login check failed: %s", _e)

    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.post(
                NEWAPI + "/api/user/login",
                json={"username": username, "password": password},
                headers={"Content-Type": "application/json"},
            )
        data = r.json()
    except Exception as e:  # 认证后端不可达
        log.error("login backend error: %s", e)
        return JSONResponse({"success": False, "message": "认证服务暂时不可用，请稍后再试"}, status_code=502)

    if not data.get("success"):
        note_fail(ip)
        log.info("login denied ip=%s user=%s", ip, username)
        return JSONResponse({"success": False, "message": "账号或密码错误"}, status_code=401)

    with closing(db()) as conn:
        row = conn.execute(
            "select id, username, display_name, role, status, \"group\", quota, used_quota,"
            " request_count, created_at, last_login_at from users where username = ? collate nocase",
            (username,),
        ).fetchone()
    if not row:
        note_fail(ip)
        return JSONResponse({"success": False, "message": "账号不存在"}, status_code=401)
    if int(row["status"] or 0) != 1:
        return JSONResponse({"success": False, "message": "账号已禁用，请联系管理员"}, status_code=403)

    sid = secrets.token_urlsafe(32)
    sessions[sid] = {
        "uid": int(row["id"]),
        "username": row["username"],
        "ip": ip,
        "exp": time.time() + SESSION_TTL,
        "born": time.time(),
    }
    log.info("login ok ip=%s uid=%s user=%s", ip, row["id"], row["username"])
    resp = JSONResponse({"success": True, "data": {"username": row["username"]}})
    set_session_cookie(resp, sid)
    return resp


@app.post("/portal/api/logout")
def logout(req: Request):
    sid = req.cookies.get(COOKIE)
    if sid:
        sessions.pop(sid, None)
    resp = JSONResponse({"success": True})
    resp.delete_cookie(COOKIE, path=COOKIE_PATH)
    return resp


# ────────────────────────── 详情面板数据 ──────────────────────────
@app.get("/portal/api/me")
def me(req: Request):
    s, err = need_auth(req)
    if err:
        return err
    with closing(db()) as conn:
        row = conn.execute(
            "select id, username, display_name, role, status, \"group\", quota, used_quota,"
            " request_count, created_at, last_login_at from users where id = ?",
            (s["uid"],),
        ).fetchone()
        if not row:
            sessions.pop(req.cookies.get(COOKIE), None)
            return JSONResponse({"success": False, "message": "账号不存在"}, status_code=401)
        plans = conn.execute(
            "select title, subtitle, price_amount, currency, duration_value, duration_unit,"
            " upgrade_group, total_amount, quota_reset_period from subscription_plans"
            " where enabled = 1 order by sort_order"
        ).fetchall()
        sub = conn.execute(
            "select s.plan_id, s.status, s.start_time, s.end_time, s.amount_total, s.amount_used,"
            " s.next_reset_time, p.title, p.price_amount, p.duration_value, p.duration_unit"
            " from user_subscriptions s left join subscription_plans p on p.id = s.plan_id"
            " where s.user_id = ? order by s.id desc limit 1",
            (s["uid"],),
        ).fetchone()

    grp = (row["group"] or "default").strip() or "default"
    tier = TIERS.get(grp) or {"name": grp, "ratio": "—", "price": "—", "desc": "自定义分组"}

    sub_out = None
    if sub and sub["end_time"]:
        end = int(sub["end_time"] or 0)
        sub_out = {
            "plan": sub["title"] or ("套餐 #%s" % sub["plan_id"]),
            "status": sub["status"],
            "end_at": end,
            "end_text": datetime.fromtimestamp(end).strftime("%Y-%m-%d"),
            "days_left": max(0, int((end - time.time()) // 86400)),
            "amount_total": usd(sub["amount_total"]),
            "amount_used": usd(sub["amount_used"]),
            "next_reset": datetime.fromtimestamp(int(sub["next_reset_time"])).strftime("%Y-%m-%d")
            if sub["next_reset_time"]
            else None,
        }

    used_raw = int(row["used_quota"] or 0)
    remain_raw = int(row["quota"] or 0)
    total_raw = used_raw + remain_raw

    quota_out = {
        "remain_usd": usd(remain_raw),
        "used_usd": usd(used_raw),
        "total_usd": usd(total_raw),
        "remain_raw": remain_raw,
        "used_raw": used_raw,
        "used_pct": round(used_raw * 100.0 / total_raw, 1) if total_raw else 0.0,
        "currency": "USDT",
        "source": "New API 账本",
        "today_usd": None,
    }

    # ── 主体账号：商务账本覆盖(最高级会员 + 口述额度/用量口径) ──
    owner = None
    lg = load_ledger()
    if lg and (lg.get("username") or "").lower() == (row["username"] or "").lower():
        t = lg.get("tier") or {}
        led = lg.get("ledger") or {}
        if t.get("name"):
            tier = {
                "name": t.get("name"),
                "ratio": t.get("ratio", "—"),
                "price": t.get("price", "—"),
                "desc": t.get("desc", ""),
                "code": "svip",
                "level": t.get("level", ""),
            }
            if t.get("renew_date"):
                try:
                    rd = datetime.strptime(t["renew_date"], "%Y-%m-%d")
                    sub_out = {
                        "plan": t["name"],
                        "status": "active",
                        "end_at": int(rd.timestamp()),
                        "end_text": t["renew_date"],
                        "days_left": max(0, int((rd.timestamp() - time.time()) // 86400)),
                        "amount_total": round(float(led.get("spent_total_usdt") or 0), 2),
                        "amount_used": round(float(led.get("today_usdt") or 0), 2),
                        "next_reset": t["renew_date"],
                    }
                except ValueError:
                    pass
        bal = float(led.get("balance_usdt") or 0)
        spent = float(led.get("spent_total_usdt") or 0)
        tot = bal + spent
        quota_out.update(
            {
                "remain_usd": round(bal, 2),
                "used_usd": round(spent, 2),
                "total_usd": round(tot, 2),
                "used_pct": round(spent * 100.0 / tot, 1) if tot else 0.0,
                "currency": led.get("currency", "USDT"),
                "source": led.get("source", "业主账本"),
                "today_usd": round(float(led.get("today_usdt") or 0), 2),
            }
        )
        owner = {"tier": t, "ledger": led, "ip": lg.get("ip") or {}, "updated_at": lg.get("updated_at")}

    return {
        "success": True,
        "data": {
            "user": {
                "id": row["id"],
                "username": row["username"],
                "display_name": row["display_name"] or row["username"],
                "is_admin": int(row["role"] or 0) >= 10,
                "group": grp,
                "created_at": datetime.fromtimestamp(int(row["created_at"] or 0)).strftime("%Y-%m-%d"),
                "last_login_at": datetime.fromtimestamp(int(row["last_login_at"] or 0)).strftime("%Y-%m-%d %H:%M")
                if row["last_login_at"]
                else "—",
                "request_count": int(row["request_count"] or 0),
            },
            "tier": dict(tier, code=grp),
            "quota": quota_out,
            "sub": sub_out,
            "owner": owner,
            "plans": [
                {
                    "title": p["title"],
                    "subtitle": p["subtitle"],
                    "price": p["price_amount"],
                    "currency": p["currency"],
                    "period": "%s%s" % (p["duration_value"], "月" if p["duration_unit"] == "month" else p["duration_unit"]),
                    "group": p["upgrade_group"],
                }
                for p in plans
            ],
            "session": {"expires_in": max(0, int(s["exp"] - time.time()))},
        },
    }


RELAY_LEDGER = Path("/opt/uncensor-relay/usage_ledger.json")


@app.get("/portal/api/live")
def live_usage(req: Request):
    """中转站实时计费: 直读 relay 的 usage_ledger.json, 每请求落账。"""
    s, err = need_auth(req)
    if err:
        return err
    try:
        data = json.loads(RELAY_LEDGER.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    rows = []
    ti = to = rq_all = 0
    usd_all = 0.0
    for k, e in (data or {}).items():
        if not isinstance(e, dict):
            continue
        a = int(e.get("tokens_in") or 0)
        b = int(e.get("tokens_out") or 0)
        c = int(e.get("requests") or 0)
        u = float(e.get("usd_total") or 0)
        ti += a; to += b; rq_all += c; usd_all += u
        rows.append({
            "key": k,
            "tier": e.get("tier") or "default",
            "label": e.get("tier_label") or "",
            "in": a, "out": b, "req": c,
            "usd": round(u, 6),
            "usd_in": round(float(e.get("usd_in") or 0), 6),
            "usd_out": round(float(e.get("usd_out") or 0), 6),
            "models": list((e.get("models") or {}).keys()),
            "last": e.get("last_used") or "",
        })
    rows.sort(key=lambda r: -r["usd"])
    return {
        "success": True,
        "rate": {"in": 296, "out": 357},
        "total": {"tokens_in": ti, "tokens_out": to, "requests": rq_all, "usd": round(usd_all, 6)},
        "keys": rows,
        "updated": datetime.now().strftime("%H:%M:%S"),
    }


@app.get("/portal/api/usage")
def usage(req: Request, days: int = 14):
    s, err = need_auth(req)
    if err:
        return err
    days = max(1, min(days, 60))
    now = int(time.time())
    uid = s["uid"]
    with closing(db()) as conn:
        def agg(since):
            r = conn.execute(
                "select count(*) n, coalesce(sum(prompt_tokens),0) pin,"
                " coalesce(sum(completion_tokens),0) pout, coalesce(sum(quota),0) q"
                " from logs where user_id = ? and type = 2 and created_at >= ?",
                (uid, since),
            ).fetchone()
            return {
                "requests": int(r["n"] or 0),
                "tokens_in": int(r["pin"] or 0),
                "tokens_out": int(r["pout"] or 0),
                "tokens": int((r["pin"] or 0) + (r["pout"] or 0)),
                "cost_usd": usd(r["q"]),
            }

        today0 = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        out = {
            "today": agg(today0),
            "d7": agg(now - 7 * 86400),
            "d30": agg(now - 30 * 86400),
        }
        rows = conn.execute(
            "select created_at, coalesce(prompt_tokens,0)+coalesce(completion_tokens,0) tk,"
            " coalesce(quota,0) q from logs where user_id = ? and type = 2 and created_at >= ?",
            (uid, now - days * 86400),
        ).fetchall()
        models = conn.execute(
            "select model_name, count(*) n, coalesce(sum(quota),0) q from logs"
            " where user_id = ? and type = 2 and created_at >= ? group by model_name"
            " order by q desc limit 8",
            (uid, now - 30 * 86400),
        ).fetchall()

    buckets = {}
    for i in range(days):
        d = datetime.fromtimestamp(now - i * 86400).strftime("%m-%d")
        buckets[d] = {"date": d, "tokens": 0, "cost_usd": 0.0, "requests": 0}
    for r in rows:
        d = datetime.fromtimestamp(int(r["created_at"])).strftime("%m-%d")
        if d in buckets:
            buckets[d]["tokens"] += int(r["tk"] or 0)
            buckets[d]["cost_usd"] = round(buckets[d]["cost_usd"] + usd(r["q"]), 4)
            buckets[d]["requests"] += 1
    out["series"] = list(reversed(list(buckets.values())))
    out["models"] = [
        {"model": m["model_name"] or "—", "requests": int(m["n"]), "cost_usd": usd(m["q"])} for m in models
    ]
    out["success"] = True
    return out


@app.get("/portal/api/tokens")
def tokens(req: Request):
    s, err = need_auth(req)
    if err:
        return err
    with closing(db()) as conn:
        rows = conn.execute(
            "select id, name, key, status, created_time, accessed_time, expired_time,"
            " remain_quota, unlimited_quota, used_quota, model_limits from tokens"
            " where user_id = ? order by id desc limit 30",
            (s["uid"],),
        ).fetchall()
    out = []
    for r in rows:
        exp = int(r["expired_time"] or 0)
        out.append(
            {
                "id": r["id"],
                "name": r["name"] or ("令牌 #%s" % r["id"]),
                "masked": mask_key(r["key"]),
                "status": "启用" if int(r["status"] or 0) == 1 else "已禁用",
                "created": datetime.fromtimestamp(int(r["created_time"] or 0)).strftime("%Y-%m-%d")
                if r["created_time"]
                else "—",
                "accessed": datetime.fromtimestamp(int(r["accessed_time"] or 0)).strftime("%m-%d %H:%M")
                if r["accessed_time"]
                else "未使用",
                "used_usd": usd(r["used_quota"]),
                "remain": "无限制" if int(r["unlimited_quota"] or 0) == 1 else usd(r["remain_quota"]),
                "expired": "永不过期" if exp in (0, -1) else datetime.fromtimestamp(exp).strftime("%Y-%m-%d"),
                "model_limits": (r["model_limits"] or "") == "" and "全部模型" or r["model_limits"],
            }
        )
    return {"success": True, "data": out}


@app.get("/portal/api/tokens/{tid}/key")
def token_key(tid: int, req: Request):
    s, err = need_auth(req)
    if err:
        return err
    with closing(db()) as conn:
        r = conn.execute("select key from tokens where id = ? and user_id = ?", (tid, s["uid"])).fetchone()
    if not r:
        return JSONResponse({"success": False, "message": "令牌不存在"}, status_code=404)
    return {"success": True, "key": norm_key(r["key"])}


@app.get("/portal/api/logs")
def logs(req: Request, limit: int = 20):
    s, err = need_auth(req)
    if err:
        return err
    limit = max(1, min(limit, 100))
    with closing(db()) as conn:
        rows = conn.execute(
            "select created_at, model_name, prompt_tokens, completion_tokens, quota, use_time,"
            " is_stream, \"group\", channel_name from logs where user_id = ? and type = 2"
            " order by id desc limit ?",
            (s["uid"], limit),
        ).fetchall()
    data = [
        {
            "time": datetime.fromtimestamp(int(r["created_at"] or 0)).strftime("%m-%d %H:%M:%S"),
            "model": r["model_name"] or "—",
            "tokens_in": int(r["prompt_tokens"] or 0),
            "tokens_out": int(r["completion_tokens"] or 0),
            "cost_usd": usd(r["quota"]),
            "ms": int(r["use_time"] or 0),
            "stream": bool(r["is_stream"]),
        }
        for r in rows
    ]
    return {"success": True, "data": data}


@app.get("/portal/api/connect")
def connect(req: Request):
    """一条可直接复制的接入串: base_url | sk-xxx（本人最新启用令牌）"""
    s, err = need_auth(req)
    if err:
        return err
    with closing(db()) as conn:
        r = conn.execute(
            "select key from tokens where user_id = ? and status = 1 order by id desc limit 1", (s["uid"],)
        ).fetchone()
    host = req.headers.get("x-forwarded-host") or req.headers.get("host") or "openingcode.top"
    base = "https://%s/v1" % host.split(",")[0].strip()
    key = norm_key(r["key"]) if r and r["key"] else ""
    return {"success": True, "base_url": base, "key": key, "line": base + (" | " + key if key else ""), "has_key": bool(key)}


@app.get("/portal/api/endpoint")
def endpoint(req: Request):
    s, err = need_auth(req)
    if err:
        return err
    host = req.headers.get("x-forwarded-host") or req.headers.get("host") or "openingcode.top"
    return {
        "success": True,
        "base_url": "https://%s/v1" % host,
        "models": ["Facai-DS", "Facai-168"],
    }
