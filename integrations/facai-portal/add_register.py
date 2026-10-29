#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""add_register.py — 给 facai-portal 加「自助注册」：注册即得中转站 key（自动写入 relay 的 members.json + tiers.json）
用户路径：https://openingcode.top/portal/register  ->  填邮箱+密码  ->  直接拿 sk-oc-... key + 调用示例
"""
import ast, shutil, time

APP = "/opt/facai-portal/app.py"
IDX = "/opt/facai-portal/static/index.html"
ts = time.strftime("%Y%m%d-%H%M%S")
s = open(APP, encoding="utf-8").read()

# ── 1) 顶部加 import（去重） ──
if "import hashlib as _hl" not in s:
    anchor = s.split("\n", 1)[0]
    s = s.replace(anchor, anchor + "\nimport hashlib as _hl\nimport fcntl as _fcntl", 1)

# ── 2) 在登录路由前插入注册模块 ──
ANCHOR = "# ────────────────────────── 登录进程 ──────────────────────────"
assert s.count(ANCHOR) == 1
REG = '''# ────────────────────────── 自助注册（2026-10-26 Owner 令） ──────────────────────────
MEMBERS_P = Path("/opt/uncensor-relay/members.json")
TIERS_P = Path("/opt/uncensor-relay/tiers.json")
REG_LOCKF = "/opt/facai-portal/.reg.lock"
REG_RATE: dict = {}
EMAIL_RE = r"^[^@\\s]{1,64}@[^@\\s]{1,120}\\.[A-Za-z]{2,12}$"


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


'''
s = s.replace(ANCHOR, REG + ANCHOR, 1)

# ── 3) 登录：先查自助账号 ──
OLD_LOGIN = """    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.post(
                NEWAPI + "/api/user/login","""
NEW_LOGIN = """    # 2026-10-26: 先查自助注册账号（members.json），命中即建 member 会话，不走 New API
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
                NEWAPI + "/api/user/login","""
assert s.count(OLD_LOGIN) == 1, "登录锚点不唯一"
s = s.replace(OLD_LOGIN, NEW_LOGIN, 1)

ast.parse(s)
shutil.copy2(APP, APP + ".bak.reg." + ts)
open(APP, "w", encoding="utf-8").write(s)
print("app.py: 注册/登录/会员页 已加，语法 OK")

# ── 4) 登录页文案跳转 ──
h = open(IDX, encoding="utf-8").read()
h2 = h.replace('<a href="/register">立即注册</a>', '<a href="/portal/register">立即注册</a>')
h2 = h2.replace("else { $('#p').value=''; await boot(); }",
                "else { $('#p').value=''; if(j.data && j.data.kind==='member'){ location.href='/portal/member'; return; } await boot(); }")
if h2 != h:
    shutil.copy2(IDX, IDX + ".bak.reg." + ts)
    open(IDX, "w", encoding="utf-8").write(h2)
    print("index.html: 注册链接 + member 跳转已改")
else:
    print("index.html: 无需改（链接已正确）")
