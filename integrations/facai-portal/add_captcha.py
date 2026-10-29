#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""add_captcha.py — 2026-10-27 Owner 令: 注册要能"验证"
给 /portal/api/register 加自绘 SVG 图形验证码(不依赖任何第三方/发信服务):
  GET  /portal/api/captcha  -> {"id":..., "svg":"<svg…>"}
  注册时必带 captcha_id + captcha_code(不区分大小写, 5 分钟有效, 一次性)
另外把注册限流收紧到 5 次/小时/IP(已有), 验证码错 5 次自动作废。
"""
import ast, shutil, time

APP = "/opt/facai-portal/app.py"
ts = time.strftime("%Y%m%d-%H%M%S")
s = open(APP, encoding="utf-8").read()

CAP = '''# ────────────── 图形验证码（2026-10-27 Owner 令：注册必须能验证） ──────────────
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


'''

ANCHOR = '@app.get("/portal/register")'
assert s.count(ANCHOR) == 1
s = s.replace(ANCHOR, CAP + ANCHOR, 1)

OLD = """    import re as _re
    if not _re.match(EMAIL_RE, email):"""
NEW = """    _okc, _whyc = _cap_check(body.get("captcha_id") or "", body.get("captcha_code") or "")
    if not _okc:
        return JSONResponse({"success": False, "message": _whyc}, status_code=400)
    import re as _re
    if not _re.match(EMAIL_RE, email):"""
assert s.count(OLD) == 1
s = s.replace(OLD, NEW, 1)

ast.parse(s)
shutil.copy2(APP, APP + ".bak.cap." + ts)
open(APP, "w", encoding="utf-8").write(s)
print("app.py: 图形验证码已接入注册")

# ── 注册页加验证码 ──
RG = "/opt/facai-portal/static/register.html"
h = open(RG, encoding="utf-8").read()
OLD_FORM = """      <label>确认密码</label>
      <input id="pw2" type="password" placeholder="再输一次" required minlength="8">"""
NEW_FORM = """      <label>确认密码</label>
      <input id="pw2" type="password" placeholder="再输一次" required minlength="8">
      <label>验证码（点图可换）</label>
      <div style="display:flex;gap:10px;align-items:center">
        <input id="cap" placeholder="输入图中 5 位" required autocomplete="off"
               style="flex:1;letter-spacing:3px;text-transform:uppercase">
        <span id="capImg" title="点击刷新" style="cursor:pointer;line-height:0"></span>
      </div>"""
assert h.count(OLD_FORM) == 1
h = h.replace(OLD_FORM, NEW_FORM, 1)

OLD_JS = """const $ = id => document.getElementById(id);
let KEY = '';"""
NEW_JS = """const $ = id => document.getElementById(id);
let KEY = '', CAP_ID = '';
async function loadCap(){
  try{
    const r = await fetch('/portal/api/captcha', {cache:'no-store'});
    const j = await r.json();
    CAP_ID = j.data.id;
    $('capImg').innerHTML = j.data.svg;
    $('cap').value = '';
  }catch(_){ $('capImg').textContent = '加载失败，点此重试'; }
}"""
assert h.count(OLD_JS) == 1
h = h.replace(OLD_JS, NEW_JS, 1)

OLD_SUB = """  const email = $('email').value.trim(), pw = $('pw').value, pw2 = $('pw2').value;
  if(pw !== pw2){ m.className='msg err'; m.textContent='两次密码不一致'; return; }"""
NEW_SUB = """  const email = $('email').value.trim(), pw = $('pw').value, pw2 = $('pw2').value;
  const cap = $('cap').value.trim();
  if(pw !== pw2){ m.className='msg err'; m.textContent='两次密码不一致'; return; }
  if(!cap){ m.className='msg err'; m.textContent='请填验证码'; return; }"""
assert h.count(OLD_SUB) == 1
h = h.replace(OLD_SUB, NEW_SUB, 1)

OLD_POST = """headers:{'Content-Type':'application/json'}, body: JSON.stringify({email, password: pw})});"""
NEW_POST = """headers:{'Content-Type':'application/json'}, body: JSON.stringify({email, password: pw, captcha_id: CAP_ID, captcha_code: cap})});"""
assert h.count(OLD_POST) == 1
h = h.replace(OLD_POST, NEW_POST, 1)

OLD_ERR = """    if(!r.ok || !j.success){ m.className='msg err'; m.textContent = j.message || ('注册失败 HTTP '+r.status); return; }"""
NEW_ERR = """    if(!r.ok || !j.success){ m.className='msg err'; m.textContent = j.message || ('注册失败 HTTP '+r.status); loadCap(); return; }"""
assert h.count(OLD_ERR) == 1
h = h.replace(OLD_ERR, NEW_ERR, 1)

OLD_END = """$('copyKey').onclick = () => cp(KEY, 'key');"""
NEW_END = """$('capImg').onclick = loadCap;
loadCap();
$('copyKey').onclick = () => cp(KEY, 'key');"""
assert h.count(OLD_END) == 1
h = h.replace(OLD_END, NEW_END, 1)

shutil.copy2(RG, RG + ".bak.cap." + ts)
open(RG, "w", encoding="utf-8").write(h)
print("register.html: 验证码 UI 已加")
