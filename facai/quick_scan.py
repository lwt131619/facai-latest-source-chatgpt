"""Login-free, bounded quick risk scan for a pasted URL.

Owner directive 2026-10-28: paste a URL, get the fastest honest evidence-backed
risk report, no account and no verification file. This shares the SSRF guards
of the claimed assessment (public-address pinning, HTTPS-only, byte budgets)
while widening the analyzer pipeline: TLS/transport, security headers, cookie
flags, passive technology fingerprint and two high-value read-only exposure
probes (/.git/HEAD, /.env). Every finding carries the observation it rests on;
fingerprint findings are INFERENCE and never claim a proven CVE.
"""
import hashlib
import json
import secrets
import socket
import ssl
import threading
import time
from datetime import datetime, timezone

from .site_assessment import SiteAssessmentError, normalize_site, _fetch, _public_address

# Hard outbound budget for one scan: 8 HTTP requests + 1 TLS handshake,
# each with its own timeout enforced in _fetch / _tls_probe.
# kind: root feeds header/fingerprint analyzers; the rest feed dedicated ones.
PROBES = (
    ('/', 16384, 'root'),
    ('/.git/HEAD', 2048, 'git'),
    ('/.env', 2048, 'env'),
    ('/robots.txt', 4096, 'robots'),
    ('/?facai_probe=fcq7k2p%27%22%3E%3C%7B%7D', 16384, 'reflection'),
    ('/?id=fcq7k2p%27', 16384, 'sqlerror'),
    ('/admin', 2048, 'adminpanel'),
    ('/phpmyadmin', 2048, 'adminpanel'),
)
SQL_ERROR_MARKERS = (
    'sql syntax', 'mysql_fetch', 'warning: mysql', 'sqlite3::query', 'sqlite error',
    'ora-', 'psqlexception', 'pg_query', 'microsoft sql', 'unclosed quotation',
    'sqlstate', 'syntax error in query', 'database error',
)
HSTS_MIN_AGE = 15552000  # 180 days, the common baseline recommendation
SCAN_CONCURRENCY = 4
IP_COOLDOWN_SECONDS = 60
HOST_COOLDOWN_SECONDS = 600
_scan_slots = threading.BoundedSemaphore(SCAN_CONCURRENCY)

SEVERITY_WEIGHT = {'CRITICAL': 10, 'HIGH': 6, 'MEDIUM': 3, 'LOW': 1, 'INFO': 0}


def _finding(fid, category, title, severity, certainty, evidence, recommendation, observed=None, tags=()):
    return {'id': fid, 'category': category, 'title': title, 'severity': severity,
            'certainty': certainty, 'evidence': evidence, 'observed': observed,
            'recommendation': recommendation, 'tags': list(tags)}


def _tls_probe(host):
    """One handshake to the pinned public address; returns TLS + certificate facts."""
    address = _public_address(host)
    context = ssl.create_default_context()
    try:
        raw = socket.create_connection((address, 443), timeout=5)
        wrapped = context.wrap_socket(raw, server_hostname=host)
        try:
            cert = wrapped.getpeercert()
            version = wrapped.version()
        finally:
            wrapped.close()
    except (OSError, ssl.SSLError, ValueError) as exc:
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
    summary = {'ok': True, 'tls_version': version, 'certificate': {}}
    if cert:
        def rdn_value(rdn_sequence, wanted):
            for rdn in rdn_sequence or ():
                for key, value in rdn:
                    if key == wanted:
                        return value
            return None

        not_after = cert.get('notAfter')
        not_before = cert.get('notBefore')
        issuer_name = rdn_value(cert.get('issuer'), 'organizationName') or rdn_value(cert.get('issuer'), 'commonName')
        subject_name = rdn_value(cert.get('subject'), 'commonName')
        summary['certificate'] = {
            'subject_alt_names': [value for kind, value in cert.get('subjectAltName', ())][:8],
            'not_before': not_before, 'not_after': not_after,
            'issuer': issuer_name,
            'self_signed': bool(issuer_name and issuer_name == subject_name)}
        if not_after:
            for stamp_format in ('%b %d %H:%M:%S %Y %Z', '%b %d %H:%M:%S %Y GMT'):
                try:
                    expiry = datetime.strptime(not_after, stamp_format).replace(tzinfo=timezone.utc)
                    summary['certificate']['seconds_remaining'] = round(
                        (expiry - datetime.now(timezone.utc)).total_seconds())
                    break
                except ValueError:
                    continue
    return summary


def analyze_tls(tls):
    """Transport findings from one observed handshake; handshake failure is a FACT."""
    if not tls.get('ok'):
        return [_finding('tls-handshake', 'transport', 'HTTPS 握手失败', 'HIGH', 'FACT',
                         f'对 443 端口的标准 TLS 握手未完成：{tls.get("error", "unknown")}',
                         '检查证书链是否完整、域名是否匹配以及 443 端口是否对公网开放。')]
    findings = []
    version = tls.get('tls_version') or ''
    if version in ('TLSv1', 'TLSv1.1', 'SSLv3'):
        findings.append(_finding('tls-obsolete', 'transport', f'使用过时协议 {version}', 'HIGH', 'FACT',
                                 f'握手协商版本为 {version}，低于 TLSv1.2。',
                                 '在服务器禁用 TLSv1.0/1.1，仅保留 TLSv1.2 及以上。', tags=['tls', 'misconfig']))
    cert = tls.get('certificate') or {}
    remaining = cert.get('seconds_remaining')
    if remaining is not None:
        if remaining <= 0:
            findings.append(_finding('cert-expired', 'transport', '证书已过期', 'CRITICAL', 'FACT',
                                     f'证书 notAfter={cert.get("not_after")}，已过有效期。',
                                     '立即续期证书并更换全链路使用的证书文件。'))
        elif remaining < 14 * 86400:
            findings.append(_finding('cert-expiring', 'transport', '证书即将过期', 'HIGH', 'FACT',
                                     f'证书 notAfter={cert.get("not_after")}，剩余 {remaining // 86400} 天。',
                                     '尽快续期，避免到期后全站不可达。'))
        elif remaining < 30 * 86400:
            findings.append(_finding('cert-expiring-soon', 'transport', '证书 30 天内到期', 'MEDIUM', 'FACT',
                                     f'证书 notAfter={cert.get("not_after")}，剩余 {remaining // 86400} 天。',
                                     '安排自动续期（ACME）或手动更换。'))
    return findings


def _header(headers, name):
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _parse_cookie_flags(value):
    parts = [part.strip().lower() for part in value.split(';')]
    return {'secure': 'secure' in parts,
            'httponly': 'httponly' in parts,
            'samesite': any(part.startswith('samesite') for part in parts)}


def analyze_headers(root_status, headers):
    """Security-header findings; only headers actually present or absent on the
    observed root response are claimed, and cookie findings only when the site
    itself set a cookie."""
    findings = []
    hsts = _header(headers, 'Strict-Transport-Security')
    if not hsts:
        findings.append(_finding('hsts-missing', 'headers', '未启用 HSTS', 'MEDIUM', 'FACT',
                                 f'根响应（HTTP {root_status}）没有 Strict-Transport-Security 头。',
                                 '添加 Strict-Transport-Security: max-age=31536000; includeSubDomains。', tags=['misconfig', 'hsts']))
    else:
        max_age = 0
        for part in hsts.split(';'):
            piece = part.strip().lower()
            if piece.startswith('max-age='):
                try:
                    max_age = int(piece.split('=', 1)[1])
                except ValueError:
                    pass
        if max_age < HSTS_MIN_AGE:
            findings.append(_finding('hsts-short', 'headers', 'HSTS max-age 偏短', 'LOW', 'FACT',
                                     f'Strict-Transport-Security: {hsts}，max-age={max_age} 低于 {HSTS_MIN_AGE}。',
                                     '将 max-age 提高到至少 15552000（180 天）。', tags=['misconfig', 'hsts']))
    csp = _header(headers, 'Content-Security-Policy')
    if not csp:
        findings.append(_finding('csp-missing', 'headers', '未配置 CSP', 'MEDIUM', 'FACT',
                                 f'根响应（HTTP {root_status}）没有 Content-Security-Policy 头。',
                                 '配置最小化 CSP，从 default-src \'self\' 起步并按需放行。',
                                 tags=['misconfig', 'csp']))
    else:
        lowered = csp.lower()
        if "'unsafe-eval'" in lowered:
            findings.append(_finding('csp-unsafe-eval', 'headers', 'CSP 允许 unsafe-eval', 'MEDIUM', 'FACT',
                                     f'Content-Security-Policy 中含 unsafe-eval：{csp[:200]}',
                                     '移除 unsafe-eval，改用 nonce 或哈希放行可信脚本。', tags=['csp', 'xss']))
        if "'unsafe-inline'" in lowered:
            findings.append(_finding('csp-unsafe-inline', 'headers', 'CSP 允许 unsafe-inline', 'LOW', 'FACT',
                                     f'Content-Security-Policy 中含 unsafe-inline：{csp[:200]}',
                                     '改用 nonce/哈希策略，缩小 XSS 注入面。', tags=['csp', 'xss']))
    if not _header(headers, 'X-Content-Type-Options'):
        findings.append(_finding('nosniff-missing', 'headers', '缺少 X-Content-Type-Options', 'LOW', 'FACT',
                                 '根响应没有 X-Content-Type-Options: nosniff。',
                                 '添加该头，阻止浏览器 MIME 嗅探歧义。', tags=['misconfig', 'nosniff']))
    xfo = _header(headers, 'X-Frame-Options')
    frame_ancestors = csp and 'frame-ancestors' in csp.lower()
    if not xfo and not frame_ancestors:
        findings.append(_finding('framing-unset', 'headers', '未限制页面被嵌套（点击劫持面）', 'MEDIUM', 'FACT',
                                 '根响应既无 X-Frame-Options，CSP 中也无 frame-ancestors。',
                                 '添加 CSP frame-ancestors \'self\' 或 X-Frame-Options: DENY。',
                                 tags=['misconfig', 'clickjacking']))
    if not _header(headers, 'Referrer-Policy'):
        findings.append(_finding('referrer-unset', 'headers', '未设置 Referrer-Policy', 'LOW', 'FACT',
                                 '根响应没有 Referrer-Policy 头。',
                                 '添加 Referrer-Policy: strict-origin-when-cross-origin。', tags=['misconfig', 'referrer']))
    if not _header(headers, 'Permissions-Policy'):
        findings.append(_finding('permissions-unset', 'headers', '未设置 Permissions-Policy', 'INFO', 'FACT',
                                 '根响应没有 Permissions-Policy 头。',
                                 '按需禁用摄像头/麦克风/地理位置等敏感能力。', tags=['misconfig', 'permissions']))
    server = _header(headers, 'Server')
    if server and any(character.isdigit() for character in server):
        findings.append(_finding('server-version-exposed', 'headers', 'Server 头暴露精确版本', 'LOW', 'FACT',
                                 f'Server: {server}',
                                 '隐藏或泛化版本号（如 ServerTokens Prod），缩小已知漏洞匹配面。',
                                 observed=server, tags=['misconfig', 'disclosure']))
    powered = _header(headers, 'X-Powered-By')
    if powered:
        findings.append(_finding('powered-by-exposed', 'headers', 'X-Powered-By 暴露技术栈', 'LOW', 'FACT',
                                 f'X-Powered-By: {powered}',
                                 '移除 X-Powered-By 头。',
                                 observed=powered, tags=['misconfig', 'disclosure']))
    cookies = [value for key, value in headers.items() if key.lower() == 'set-cookie']
    for cookie in cookies:
        name = cookie.split('=', 1)[0].strip()
        flags = _parse_cookie_flags(cookie)
        session_like = any(token in name.lower() for token in
                           ('sess', 'token', 'auth', 'login', 'jsession', 'csrf', 'sid'))
        if session_like and not flags['secure']:
            findings.append(_finding('cookie-no-secure', 'cookies', f'会话 Cookie {name} 未加 Secure', 'HIGH', 'FACT',
                                     f'Set-Cookie: {cookie[:200]}',
                                     '为所有会话类 Cookie 添加 Secure 标记，防止经明文信道泄露。', tags=['cookies', 'session']))
        if session_like and not flags['httponly']:
            findings.append(_finding('cookie-no-httponly', 'cookies', f'会话 Cookie {name} 未加 HttpOnly', 'MEDIUM', 'FACT',
                                     f'Set-Cookie: {cookie[:200]}',
                                     '为会话 Cookie 添加 HttpOnly，阻止脚本读取。', tags=['cookies', 'session']))
        if session_like and not flags['samesite']:
            findings.append(_finding('cookie-no-samesite', 'cookies', f'会话 Cookie {name} 未加 SameSite', 'LOW', 'FACT',
                                     f'Set-Cookie: {cookie[:200]}',
                                     '添加 SameSite=Lax/Strict，降低 CSRF 与跨站携带面。', tags=['cookies', 'csrf']))
    return findings


FINGERPRINT_RULES = (
    ('WordPress', ('wp-content', 'wp-includes')),
    ('Drupal', ('drupal.js', 'sites/all/', 'drupal.settings')),
    ('Joomla', ('/media/jui/', 'joomla!')),
    ('PHP', ('phpsessid',)),
    ('ASP.NET', ('asp.net', '__viewstate')),
    ('Next.js', ('__next_data__', '_next/static/')),
    ('Nuxt', ('__nuxt', '_nuxt/')),
    ('React', ('data-reactroot', 'react-dom', '__react')),
    ('Vue.js', ('data-v-app', 'vue.runtime', 'vue.global')),
    ('Angular', ('ng-version', 'angular.js')),
    ('Laravel', ('laravel_session', 'xsrf-token', 'laravel_session')),
    ('jQuery', ('jquery.js', 'jquery.min.js', 'jquery-migrate')),
)


def analyze_fingerprint(headers, body_text):
    """Passive technology fingerprint. Every hit is INFERENCE with the exact
    bytes/hdr it matched; no CVE is claimed from a version string."""
    haystack_headers = ' '.join(f'{key}:{value}' for key, value in headers.items()).lower()
    haystack = haystack_headers + ' ' + body_text.lower()
    detected = []
    for name, markers in FINGERPRINT_RULES:
        matched = [marker for marker in markers if marker in haystack]
        if matched:
            detected.append({'name': name, 'matched_markers': matched[:3],
                             'source': 'response headers + first bytes of body'})
    generator = None
    for marker in ('generator',):
        lowered = body_text.lower()
        position = lowered.find(f'<meta name="{marker}"')
        if position >= 0:
            snippet = body_text[position:position + 160]
            generator = snippet.split('content="', 1)[-1].split('"', 1)[0] if 'content="' in snippet else snippet
            break
    findings = []
    stack_line = ', '.join(item['name'] for item in detected) + (f'; meta generator="{generator}"' if generator else '')
    if detected or generator:
        findings.append(_finding('tech-stack', 'fingerprint', f'识别到技术栈：{stack_line}', 'INFO', 'INFERENCE',
                                 f'基于响应头与正文的被动特征匹配：{json.dumps(detected, ensure_ascii=False)}'
                                 + (f'；正文 meta generator="{generator}"' if generator else ''),
                                 '该指纹仅为识别结果，不代表已证实漏洞；如需组件×漏洞库比对，请上传源码或 SBOM 走离线审计线。',
                                 observed={'stack': [item['name'] for item in detected], 'generator': generator}))
    return findings


def _looks_like_git_head(body_text):
    return body_text.startswith('ref:') or body_text.startswith('0000000')


def _looks_like_env(body_text):
    return '=' in body_text and any(token in body_text.upper() for token in
                                    ('KEY', 'PASSWORD', 'SECRET', 'TOKEN', 'DB_', 'API_'))


CANARY = 'fcq7k2p'


def analyze_injection_surface(probe_results):
    """Harmless-payload injection-surface probes, benchmarked against the
    first layer of open-source scanners (sqlmap error-based echo, reflected
    XSS reflection check): a unique canary is injected via GET parameters and
    judged only on what the response actually echoes back. No time-based
    blind payloads, no writes, no destructive syntax — one request each.
    """
    findings = []
    reflection = probe_results.get('reflection')
    if reflection and reflection['status'] == 200:
        text = reflection['body_text']
        position = text.find(CANARY)
        if position >= 0:
            window = text[position + len(CANARY):position + len(CANARY) + 24]
            unescaped = any(marker in window for marker in ("'", '"', '<', '>', '{', '}'))
            if unescaped:
                findings.append(_finding(
                    'reflected-unencoded', 'injection', '查询参数反射且未转义（XSS 注入面）', 'HIGH', 'FACT',
                    f'GET /?facai_probe={CANARY}\'"><{{}} 的响应在 HTML 中原样回显 payload：'
                    f'…{text[max(0, position - 16):position + 40]!r}…',
                    '对全部输出点按上下文做 HTML/属性/JS 编码；反射本身不等于可利用 XSS，需人工确认执行上下文。',
                    observed={'canary': CANARY, 'echo_context': window},
                    tags=['xss', 'injection', 'reflection']))
            else:
                findings.append(_finding(
                    'reflected-encoded', 'injection', '查询参数反射已转义', 'INFO', 'FACT',
                    f'GET /?facai_probe=… 的响应回显了标记 {CANARY}，但紧跟的引号/尖括号已被编码：{window!r}',
                    '保持现有输出编码；新增参数时延续同一转义策略。',
                    tags=['xss', 'reflection']))
    sql = probe_results.get('sqlerror')
    if sql and sql['status'] == 200:
        lowered = sql['body_text'].lower()
        hit = next((marker for marker in SQL_ERROR_MARKERS if marker in lowered), None)
        if hit:
            position = lowered.find(hit)
            findings.append(_finding(
                'sql-error-echo', 'injection', f'SQL 错误信息回显（特征：{hit}）', 'HIGH', 'FACT',
                f'GET /?id={CANARY}\' 使响应回显数据库错误：…{sql["body_text"][max(0, position - 40):position + 80]!r}…',
                '错误回显给攻击者提供了数据库指纹与注入 oracle；关闭生产环境详细错误输出，参数化查询，'
                '并用该响应在授权下做进一步注入验证。',
                observed={'marker': hit},
                tags=['sqli', 'injection', 'error-based']))
    admin_hits = []
    for entry in probe_results.get('adminpanel', []):
        if entry['status'] == 200:
            admin_hits.append(entry['path'])
    if admin_hits:
        findings.append(_finding(
            'admin-panel-reachable', 'exposure', f'管理入口匿名可达（{"、".join(admin_hits)}）', 'LOW', 'FACT',
            '；'.join(f'GET {path} 返回 200' for path in admin_hits)
            + '。公开可及不等于弱口令，但它是爆破与漏洞匹配的首选入口。',
            '限制管理入口来源 IP 或加网关鉴权，确认强口令与 2FA。',
            observed={'paths': admin_hits},
            tags=['exposure', 'misconfig', 'panel']))
    return findings


def analyze_exposure(probe_results):
    """The two highest-value read-only probes, judged only on direct observation."""
    findings = []
    git = probe_results.get('git')
    if git and git['status'] == 200 and _looks_like_git_head(git['body_text']):
        findings.append(_finding('git-exposed', 'exposure', '.git 仓库公开可读', 'CRITICAL', 'FACT',
                                 f'GET /.git/HEAD 返回 200，内容以 git 引用开头：{git["body_text"][:80]!r}',
                                 '在 Web 服务器拒绝 /.git/ 路径（deny 规则），并将仓库移出站点根目录。',
                                 tags=['exposure', 'git', 'source-disclosure']))
    env = probe_results.get('env')
    if env and env['status'] == 200 and _looks_like_env(env['body_text']):
        findings.append(_finding('env-exposed', 'exposure', '.env 凭据文件公开可读', 'CRITICAL', 'FACT',
                                 f'GET /.env 返回 200，内容含 KEY/PASSWORD/SECRET 类赋值：{env["body_text"][:80]!r}',
                                 '立即从站点根移除该文件并轮换其中全部凭据。',
                                 tags=['exposure', 'credentials', 'config']))
    checked_pass = []
    for kind, path in (('git', '/.git/HEAD'), ('env', '/.env')):
        result = probe_results.get(kind)
        if result and result['status'] != 200:
            checked_pass.append(f'{path} → HTTP {result["status"]}')
    if checked_pass:
        findings.append(_finding('exposure-clear', 'exposure', '高危暴露点探测未命中', 'INFO', 'FACT',
                                 '；'.join(checked_pass),
                                 '当前配置未直接暴露这两项，保持观察。',
                                 tags=['exposure']))
    return findings


def grade_report(findings):
    score = sum(SEVERITY_WEIGHT.get(item['severity'], 0) for item in findings)
    if score == 0:
        grade = 'A+'
    elif score <= 2:
        grade = 'A'
    elif score <= 5:
        grade = 'B'
    elif score <= 9:
        grade = 'C'
    elif score <= 15:
        grade = 'D'
    else:
        grade = 'F'
    counts = {level: sum(1 for item in findings if item['severity'] == level)
              for level in ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO')}
    return {'score': score, 'grade': grade, 'severity_counts': counts}


class QuickScans:
    def __init__(self, store, fetch=None, tls_probe=None):
        self.store = store
        self.fetch = fetch or (lambda host, path, max_bytes=8192: _fetch(host, path, max_bytes=max_bytes))
        self.tls_probe = tls_probe or _tls_probe
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS quick_scans (
                id TEXT PRIMARY KEY, host TEXT NOT NULL, source_ip TEXT,
                report_json TEXT NOT NULL, created_at TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS quick_scan_rate (
                rate_key TEXT PRIMARY KEY, last REAL NOT NULL, report_id TEXT)''')

    def _rate_take(self, db, rate_key, cooldown):
        """Cross-worker reservation; returns the cached report id when the same
        key is still cooling down (cache semantics, not an error)."""
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT last, report_id FROM quick_scan_rate WHERE rate_key=?',
                         (rate_key,)).fetchone()
        now = time.time()
        if row is not None and now - row['last'] < cooldown:
            return {'blocked': True, 'retry_after': int(cooldown - (now - row['last'])),
                    'cached_report_id': row['report_id']}
        db.execute('INSERT INTO quick_scan_rate(rate_key,last,report_id) VALUES (?,?,NULL) '
                   'ON CONFLICT(rate_key) DO UPDATE SET last=excluded.last, report_id=NULL',
                   (rate_key, now))
        return {'blocked': False}

    def _release_host_slot(self, rate_key, report_id):
        with self.store.connect() as db:
            db.execute('UPDATE quick_scan_rate SET report_id=? WHERE rate_key=?', (report_id, rate_key))

    def _cached_report(self, report_id):
        if not report_id:
            return None
        with self.store.connect() as db:
            row = db.execute('SELECT report_json FROM quick_scans WHERE id=?', (report_id,)).fetchone()
        return json.loads(row['report_json']) if row else None

    def scan(self, url, source_ip):
        host = normalize_site(url)
        # Host gate first: a cache hit for a recently scanned site returns the
        # stored report instantly without consuming the caller's IP quota.
        cached = None
        with self.store.connect() as db:
            host_gate = self._rate_take(db, f'host:{host}', HOST_COOLDOWN_SECONDS)
            if host_gate['blocked']:
                cached = self._cached_report(host_gate['cached_report_id'])
                if cached is None:
                    # A previous attempt against this host failed and is still
                    # cooling down; do not hammer an unreachable target.
                    return {'status': 'RATE_LIMITED', 'retry_after': host_gate['retry_after']}
        if cached is not None:
            return {'status': 'OK', 'report': cached, 'cached': True}
        with self.store.connect() as db:
            ip_gate = self._rate_take(db, f'ip:{source_ip}', IP_COOLDOWN_SECONDS)
            if ip_gate['blocked']:
                return {'status': 'RATE_LIMITED', 'retry_after': ip_gate['retry_after']}
        if not _scan_slots.acquire(timeout=0.1):
            return {'status': 'BUSY', 'retry_after': 5}
        try:
            report = self._collect_and_analyze(host)
        finally:
            _scan_slots.release()
        scan_id = 'qs_' + secrets.token_urlsafe(18)
        with self.store.connect() as db:
            db.execute('INSERT INTO quick_scans VALUES (?,?,?,?,?)',
                       (scan_id, host, source_ip, json.dumps(report, ensure_ascii=False),
                        datetime.now(timezone.utc).isoformat()))
        self._release_host_slot(f'host:{host}', scan_id)
        return {'status': 'OK', 'report': report, 'id': scan_id, 'cached': False}

    def _collect_and_analyze(self, host):
        collected_at = datetime.now(timezone.utc).isoformat()
        tls = self.tls_probe(host)
        root = None
        probes = {}
        errors = []
        for path, max_bytes, kind in PROBES:
            try:
                response = self.fetch(host, path, max_bytes=max_bytes)
            except SiteAssessmentError as exc:
                errors.append(f'{path}: {exc}')
                continue
            # Keep the full bounded body for reflection/SQL echo matching (the
            # interesting byte can sit past the first screen); evidence strings
            # slice their own short excerpts at render time.
            entry = {'path': path, 'status': response['status'],
                     'body_text': response['body'].decode('utf-8', 'replace')}
            if kind == 'root':
                root = {'status': response['status'], 'headers': response['headers'], 'body': response['body']}
            elif kind == 'adminpanel':
                probes.setdefault('adminpanel', []).append(entry)
            else:
                probes[kind] = entry
        findings = []
        if root is None and not tls.get('ok'):
            raise SiteAssessmentError('站点在预算内不可达：' + '; '.join(errors[:3]))
        if root is not None:
            body_text = root['body'].decode('utf-8', 'replace')
            findings.extend(analyze_headers(root['status'], root['headers']))
            findings.extend(analyze_fingerprint(root['headers'], body_text))
        else:
            findings.append(_finding('root-unreachable', 'transport', '首页 HTTP 不可达', 'HIGH', 'FACT',
                                     '; '.join(errors[:3]), '检查 Web 服务是否监听 443 并允许公网访问。',
                                     tags=['availability']))
        findings.extend(analyze_tls(tls))
        findings.extend(analyze_injection_surface(probes))
        findings.extend(analyze_exposure(probes))
        findings.sort(key=lambda item: -SEVERITY_WEIGHT.get(item['severity'], 0))
        report = {
            'host': host, 'collected_at': collected_at, 'tls': tls,
            'findings': findings, 'summary': grade_report(findings),
            'scope': 'login-free bounded scan: 1 TLS handshake + 8 read-only GET requests '
                     '(root, /.git/HEAD, /.env, /robots.txt, canary reflection, SQL-error echo, '
                     '/admin, /phpmyadmin); no crawling beyond these, no forms, no login, '
                     'no time-based or destructive payloads',
            'limitations': '指纹为被动推断（INFERENCE）；注入面探测为无害单发载荷的回显判定，'
                           '证实可利用性需授权下的深入验证；深层页面与源码级漏洞不在本模式范围。',
            'request_count': 1 + len(PROBES),
            'evidence_sha256': hashlib.sha256(json.dumps(
                {'host': host, 'tls': tls, 'root_status': root and root['status'],
                 'headers': root and {k.lower(): v for k, v in root['headers'].items()},
                 'probes': {path: status for path, _, _ in PROBES
                            for status in [self._probe_status(probes, path)]}},
                sort_keys=True).encode()).hexdigest(),
        }
        return report

    @staticmethod
    def _probe_status(probes, path):
        for value in probes.values():
            entries = value if isinstance(value, list) else [value]
            for entry in entries:
                if entry['path'] == path:
                    return entry['status']
        return 'ERR'

    def get(self, scan_id):
        with self.store.connect() as db:
            row = db.execute('SELECT report_json FROM quick_scans WHERE id=?', (scan_id,)).fetchone()
        return json.loads(row['report_json']) if row else None
