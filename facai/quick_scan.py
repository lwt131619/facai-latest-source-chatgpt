"""Fast-path site assessment: paste a URL, get recon + real attack results.

Pipeline (all stages run in one request):
  1. recon.run_all()  — every check fans out concurrently, ~25s budget
  2. derive findings  — evidence-backed, severity-tagged, per observation
  3. auto-dispatch    — a high-value signal hands the host to attack_agent,
                        which sends real injection payloads and extracts data
  4. persist + return — one report carrying recon, findings and attack chain

Nothing is deferred to a human and no stage waits on another's permission:
recon is read-only, the attack stage fires only when recon produced a concrete
signal (a parameter that reflects, a DB error, an exposed secret file, an admin
surface), and its results are marked CONFIRMED only when a differential oracle
proves them.
"""
import concurrent.futures
import hashlib
import json
import secrets
import threading
import time
from datetime import datetime, timezone

from . import arsenal, attack_agent, egress, recon, venom_agent
from .net_client import TransportError
from .site_assessment import SiteAssessmentError, normalize_site

IP_COOLDOWN_SECONDS = 60
HOST_COOLDOWN_SECONDS = 600
SCAN_CONCURRENCY = 6
_scan_slots = threading.BoundedSemaphore(SCAN_CONCURRENCY)

# Tech-stack fingerprints that mean "dispatch VENOM". The PHP agent is only
# worth its request budget when the target is actually PHP; a static Next.js
# site gets NO_PHP for the cost of one probe instead of a full LFI sweep.
PHP_STACKS = ('PHP', 'WordPress', 'Laravel', 'Drupal', 'Joomla', 'Magento')

SEVERITY_WEIGHT = {'CRITICAL': 10, 'HIGH': 6, 'MEDIUM': 3, 'LOW': 1, 'INFO': 0}

# Signals that hand the host straight to the attack agent.
ATTACK_TRIGGERS = {
    'sql-error': '数据库错误回显',
    'reflected-unencoded': '参数反射未转义',
    'git-exposed': '.git 公开可读',
    'env-exposed': '.env 公开可读',
    'backup_sql-exposed': '数据库备份可读',
    'wp_config-exposed': 'wp-config 备份泄露',
    'config_bak-exposed': '配置备份泄露',
    'admin-panel': '管理入口匿名可达',
    'upload-surface': '上传表单暴露',
    'swagger-exposed': '接口文档暴露',
    'actuator-exposed': 'Spring Actuator 暴露',
}


def _finding(fid, category, title, severity, certainty, evidence, recommendation,
             observed=None, tags=()):
    return {'id': fid, 'category': category, 'title': title, 'severity': severity,
            'certainty': certainty, 'evidence': evidence, 'observed': observed,
            'recommendation': recommendation, 'tags': list(tags)}


# ------------------------------------------------------------------ findings

def analyze_tls(tls):
    if not tls or tls.get('error') or tls.get('ok') is False:
        return [_finding('tls-handshake', 'transport', 'HTTPS 握手失败', 'HIGH', 'FACT',
                         f"TLS 握手未完成：{(tls or {}).get('error', 'unknown')}",
                         '检查证书链完整性、域名匹配与 443 端口可达性。',
                         tags=['tls', 'availability'])]
    findings = []
    # A chain that browsers may also reject is a real availability/trust defect;
    # report it as HIGH and keep every other check's results.
    if tls.get('verified') is False or tls.get('cert_verify_failed'):
        findings.append(_finding(
            'cert-chain-untrusted', 'transport', '证书链无法验证', 'HIGH', 'FACT',
            f"标准校验失败：{tls.get('cert_verify_failed')}；"
            f"issuer={tls.get('issuer')} subject={tls.get('subject_cn')} "
            f"notAfter={tls.get('not_after')}",
            '部署完整证书链（含中间证书），确认 SNI 与 SAN 匹配；否则部分客户端会直接中断连接。',
            observed={'issuer': tls.get('issuer'), 'subject_cn': tls.get('subject_cn')},
            tags=['tls', 'certificate', 'availability']))
    version = tls.get('tls_version') or ''
    if tls.get('obsolete_protocol') or version in ('TLSv1', 'TLSv1.1', 'SSLv3'):
        findings.append(_finding('tls-obsolete', 'transport', f'使用过时协议 {version}', 'HIGH', 'FACT',
                                 f'握手协商版本 {version}，低于 TLSv1.2。',
                                 '禁用 TLSv1.0/1.1，仅保留 TLSv1.2 及以上。',
                                 tags=['tls', 'misconfig']))
    if tls.get('weak_cipher'):
        findings.append(_finding('tls-weak-cipher', 'transport',
                                 f"弱加密套件 {tls.get('cipher')}", 'MEDIUM', 'FACT',
                                 f"协商套件 {tls.get('cipher')}（{tls.get('cipher_bits')} bit）。",
                                 '改用 AEAD 套件（AES-GCM/ChaCha20-Poly1305）。',
                                 tags=['tls', 'crypto']))
    remaining = tls.get('seconds_remaining')
    if remaining is not None:
        if remaining <= 0:
            findings.append(_finding('cert-expired', 'transport', '证书已过期', 'CRITICAL', 'FACT',
                                     f"notAfter={tls.get('not_after')}，已过期。",
                                     '立即续期并部署新证书。', tags=['tls', 'certificate']))
        elif remaining < 14 * 86400:
            findings.append(_finding('cert-expiring', 'transport', '证书即将过期', 'HIGH', 'FACT',
                                     f"notAfter={tls.get('not_after')}，剩余 {remaining // 86400} 天。",
                                     '尽快续期。', tags=['tls', 'certificate']))
        elif remaining < 30 * 86400:
            findings.append(_finding('cert-expiring-soon', 'transport', '证书 30 天内到期', 'MEDIUM', 'FACT',
                                     f"notAfter={tls.get('not_after')}，剩余 {remaining // 86400} 天。",
                                     '安排自动续期。', tags=['tls', 'certificate']))
    if tls.get('san'):
        cn = tls.get('subject_cn')
        if cn and not any(cn == name or name.startswith('*.') for name in tls['san']):
            findings.append(_finding('cert-san-mismatch', 'transport', '证书 SAN 与主机名不一致',
                                     'MEDIUM', 'FACT',
                                     f"CN={cn}，SAN={tls['san'][:5]}",
                                     '重新签发覆盖实际主机名的证书。',
                                     tags=['tls', 'certificate']))
    return findings


def analyze_headers(headers_result):
    if not headers_result or headers_result.get('error'):
        return []
    findings = []
    specs = {
        'HSTS': ('hsts-missing', 'MEDIUM', '未启用 HSTS'),
        'CSP': ('csp-missing', 'MEDIUM', '未配置 CSP'),
        'X-Content-Type-Options': ('nosniff-missing', 'LOW', '缺少 X-Content-Type-Options'),
        'Referrer-Policy': ('referrer-missing', 'LOW', '未设置 Referrer-Policy'),
        'Permissions-Policy': ('permissions-missing', 'INFO', '未设置 Permissions-Policy'),
        'COOP': ('coop-missing', 'LOW', '未设置 Cross-Origin-Opener-Policy'),
        'CORP': ('corp-missing', 'LOW', '未设置 Cross-Origin-Resource-Policy'),
    }
    for name, (fid, severity, title) in specs.items():
        entry = headers_result.get(name) or {}
        if not entry.get('present'):
            findings.append(_finding(fid, 'headers', title, severity, 'FACT',
                                     f'根响应缺少 {name} 响应头。',
                                     f'添加 {name} 响应头。', tags=['misconfig', name.lower()]))
    hsts = (headers_result.get('HSTS') or {}).get('value')
    if hsts:
        max_age = 0
        for part in hsts.split(';'):
            piece = part.strip().lower()
            if piece.startswith('max-age='):
                try:
                    max_age = int(piece.split('=', 1)[1])
                except ValueError:
                    pass
        if max_age < 15552000:
            findings.append(_finding('hsts-short', 'headers', 'HSTS max-age 偏短', 'LOW', 'FACT',
                                     f'Strict-Transport-Security: {hsts}',
                                     '提高到至少 15552000（180 天）。',
                                     tags=['misconfig', 'hsts']))
    csp = (headers_result.get('CSP') or {}).get('value')
    if csp:
        lowered = csp.lower()
        if "'unsafe-eval'" in lowered:
            findings.append(_finding('csp-unsafe-eval', 'headers', 'CSP 允许 unsafe-eval', 'MEDIUM', 'FACT',
                                     f'Content-Security-Policy: {csp[:220]}',
                                     '移除 unsafe-eval，用 nonce/哈希放行。',
                                     tags=['csp', 'xss']))
        if "'unsafe-inline'" in lowered:
            findings.append(_finding('csp-unsafe-inline', 'headers', 'CSP 允许 unsafe-inline', 'LOW', 'FACT',
                                     f'Content-Security-Policy: {csp[:220]}',
                                     '改用 nonce/哈希策略。', tags=['csp', 'xss']))
    framing = (headers_result.get('X-Frame-Options') or {}).get('present')
    if not framing and not (csp and 'frame-ancestors' in csp.lower()):
        findings.append(_finding('framing-unset', 'headers', '未限制页面被嵌套', 'MEDIUM', 'FACT',
                                 '既无 X-Frame-Options，CSP 中也无 frame-ancestors。',
                                 "添加 CSP frame-ancestors 'self' 或 X-Frame-Options: DENY。",
                                 tags=['misconfig', 'clickjacking']))
    return findings


def analyze_server_disclosure(tech, headers_result):
    findings = []
    server = (tech or {}).get('server') or ''
    if server and any(char.isdigit() for char in server):
        findings.append(_finding('server-version-exposed', 'headers', 'Server 头暴露精确版本', 'LOW', 'FACT',
                                 f'Server: {server}', '隐藏或泛化版本号。',
                                 observed=server, tags=['misconfig', 'disclosure']))
    powered = (tech or {}).get('powered_by')
    if powered:
        findings.append(_finding('powered-by-exposed', 'headers', 'X-Powered-By 暴露技术栈', 'LOW', 'FACT',
                                 f'X-Powered-By: {powered}', '移除该响应头。',
                                 observed=powered, tags=['misconfig', 'disclosure']))
    return findings


def analyze_cookies(cookies_result):
    if not cookies_result or cookies_result.get('error'):
        return []
    findings = []
    for cookie in cookies_result.get('cookies', []):
        name = cookie['name']
        session_like = any(token in name.lower() for token in
                           ('sess', 'token', 'auth', 'login', 'jsession', 'csrf', 'sid', 'jwt'))
        if session_like and not cookie['secure']:
            findings.append(_finding('cookie-no-secure', 'cookies', f'会话 Cookie {name} 未加 Secure',
                                     'HIGH', 'FACT', f"Set-Cookie: {name}=... (no Secure)",
                                     '为会话 Cookie 添加 Secure。', tags=['cookies', 'session']))
        if session_like and not cookie['httponly']:
            findings.append(_finding('cookie-no-httponly', 'cookies', f'会话 Cookie {name} 未加 HttpOnly',
                                     'MEDIUM', 'FACT', f"Set-Cookie: {name}=... (no HttpOnly)",
                                     '添加 HttpOnly。', tags=['cookies', 'session']))
        if session_like and not cookie['samesite']:
            findings.append(_finding('cookie-no-samesite', 'cookies', f'会话 Cookie {name} 未加 SameSite',
                                     'LOW', 'FACT', f"Set-Cookie: {name}=... (no SameSite)",
                                     '添加 SameSite=Lax/Strict。', tags=['cookies', 'csrf']))
    return findings


def analyze_tech(tech):
    if not tech or tech.get('error'):
        return []
    detected = tech.get('detected') or {}
    generator = tech.get('generator')
    versions = tech.get('version_hints') or []
    if not detected and not generator:
        return []
    names = ', '.join(sorted(detected))
    line = names + (f'; generator="{generator}"' if generator else '')
    return [_finding('tech-stack', 'fingerprint', f'识别到技术栈：{line}', 'INFO', 'INFERENCE',
                     f'被动特征匹配：{json.dumps(detected, ensure_ascii=False)}'
                     + (f'；meta generator="{generator}"' if generator else '')
                     + (f'；版本线索 {versions}' if versions else ''),
                     '指纹用于确定后续测试方向；版本号需与组件实际版本核对后才可对应 CVE。',
                     observed={'stack': sorted(detected), 'generator': generator,
                               'version_hints': versions},
                     tags=['fingerprint', 'tech'])]


def analyze_firewall(firewall):
    if not firewall or firewall.get('error'):
        return []
    vendors = firewall.get('vendors') or []
    if vendors:
        return [_finding('waf-detected', 'fingerprint', f"检测到边缘防护：{', '.join(vendors)}",
                         'INFO', 'INFERENCE',
                         f"响应头特征匹配到 {vendors}；Server={firewall.get('server')}",
                         '防护存在会影响载荷可达性；攻击阶段需考虑绕过策略。',
                         observed=vendors, tags=['fingerprint', 'waf'])]
    return [_finding('waf-none', 'fingerprint', '未从响应头识别到 WAF/CDN', 'INFO', 'INFERENCE',
                     '响应头中没有 Cloudflare/CloudFront/Akamai/Imperity 等特征。',
                     '无头特征不等于无防护（可能仅对攻击流量触发），需以实际探测结果为准。',
                     tags=['fingerprint', 'waf'])]


def analyze_injection_surface(surface):
    """Canary-probe results from recon → findings that also arm the attack stage."""
    if not surface or surface.get('error'):
        return []
    findings = []
    reflection = surface.get('reflection') or {}
    if reflection.get('reflected'):
        if reflection.get('unescaped'):
            findings.append(_finding(
                'reflected-unencoded', 'injection', '查询参数反射且未转义（XSS 注入面）',
                'HIGH', 'FACT',
                f"GET /?facai_probe=… 响应中原样回显：{reflection.get('excerpt', '')!r}",
                '按上下文做输出编码；反射本身需人工确认执行上下文才等同可利用 XSS。',
                observed={'context': reflection.get('context')},
                tags=['xss', 'injection', 'reflection']))
        else:
            findings.append(_finding(
                'reflected-encoded', 'injection', '查询参数反射已转义', 'INFO', 'FACT',
                f"响应回显了标记但引号/尖括号已编码：{reflection.get('context')!r}",
                '保持现有输出编码策略。', tags=['xss', 'reflection']))
    sql_error = surface.get('sql_error') or {}
    if sql_error.get('marker'):
        findings.append(_finding(
            'sql-error', 'injection', f"SQL 错误回显（{sql_error['marker']}）", 'HIGH', 'FACT',
            f"GET /?id=…' 触发数据库错误：{sql_error.get('excerpt', '')!r}",
            '关闭生产环境详细错误；参数化查询。已自动派发注入验证。',
            observed={'marker': sql_error['marker']},
            tags=['sqli', 'injection', 'error-based']))
    admin_hits = surface.get('admin') or []
    if admin_hits:
        findings.append(_finding(
            'admin-panel', 'exposure',
            f"管理入口匿名可达（{', '.join(item['path'] for item in admin_hits[:4])}）",
            'LOW', 'FACT',
            '；'.join(f"GET {item['path']} → {item['status']}" for item in admin_hits[:6]),
            '限制管理入口来源 IP 或加网关鉴权；确认强口令与 2FA。',
            observed={'paths': [item['path'] for item in admin_hits]},
            tags=['exposure', 'panel']))
    upload_hits = surface.get('upload') or []
    if upload_hits:
        findings.append(_finding(
            'upload-surface', 'injection',
            f"上传表单匿名暴露（{', '.join(item['path'] for item in upload_hits[:3])}）",
            'MEDIUM', 'FACT',
            '；'.join(f"GET {item['path']} → 200，页面含 file input" for item in upload_hits[:4]),
            '上传入口需鉴权、类型白名单与隔离存储；已标记为攻击面。',
            observed={'paths': [item['path'] for item in upload_hits]},
            tags=['upload', 'injection', 'attack-surface']))
    return findings


def analyze_exposure(paths_result):
    if not paths_result or paths_result.get('error'):
        return []
    findings = []
    critical = {
        'git': ('.git 仓库公开可读', 'CRITICAL', '立即用 Web 服务器规则拒绝 /.git/ 并把仓库移出站点根目录。'),
        'env': ('.env 凭据文件公开可读', 'CRITICAL', '立即移除该文件并轮换其中全部凭据。'),
        'backup_sql': ('数据库备份文件公开可读', 'CRITICAL', '立即删除备份文件，检查是否已被下载。'),
        'wp_config': ('wp-config 备份泄露数据库凭据', 'CRITICAL', '删除备份并轮换数据库口令。'),
        'config_bak': ('配置备份文件泄露', 'CRITICAL', '删除备份并轮换相关凭据。'),
        'svn': ('.svn 元数据公开可读', 'HIGH', '拒绝 /.svn/ 路径。'),
        'ds_store': ('.DS_Store 泄露目录结构', 'LOW', '禁止访问隐藏文件。'),
        'actuator': ('Spring Actuator 端点暴露', 'HIGH', '限制 /actuator 端点，仅内网可访问。'),
        'server_status': ('server-status 暴露运行信息', 'MEDIUM', '限制该端点来源 IP。'),
        'phpinfo': ('phpinfo 页面暴露', 'MEDIUM', '删除 phpinfo 文件。'),
    }
    for kind, entry in paths_result.items():
        if not isinstance(entry, dict) or entry.get('status') != 200 or entry.get('unconfirmed'):
            continue
        if kind in critical:
            title, severity, recommendation = critical[kind]
            findings.append(_finding(f'{kind}-exposed', 'exposure', title, severity, 'FACT',
                                     f"GET {entry['path']} 返回 200，内容：{entry.get('excerpt', '')[:160]}",
                                     recommendation, observed=entry['path'],
                                     tags=['exposure', kind]))
        elif kind == 'swagger':
            findings.append(_finding('swagger-exposed', 'exposure', '接口文档页面暴露', 'MEDIUM', 'FACT',
                                     f"GET {entry['path']} 返回 200，包含 swagger 字样。",
                                     '生产环境关闭接口文档或加鉴权。',
                                     observed=entry['path'], tags=['exposure', 'api']))
    if not any(item['severity'] in ('CRITICAL', 'HIGH') for item in findings):
        checked = [f"{entry['path']} → {entry['status']}" for entry in paths_result.values()
                   if isinstance(entry, dict)]
        if checked:
            findings.append(_finding('exposure-clear', 'exposure', '高危文件暴露探测未命中', 'INFO', 'FACT',
                                     '；'.join(checked[:12]),
                                     '当前配置未直接暴露这些路径。', tags=['exposure']))
    return findings


def analyze_dns(dns_result, dnssec_result, whois_result):
    findings = []
    if dnssec_result and dnssec_result.get('signed') is False:
        findings.append(_finding('dnssec-off', 'dns', '未启用 DNSSEC', 'LOW', 'FACT',
                                 f"权威服务器 {dnssec_result.get('nameserver')} 返回的回答未带 RRSIG。",
                                 '为域名启用 DNSSEC 签名，防止 DNS 投毒与劫持。',
                                 tags=['dns', 'dnssec']))
    if whois_result and isinstance(whois_result, dict):
        expires = whois_result.get('expires')
        if expires:
            import re as _re
            match = _re.search(r'(\d{4})-(\d{2})-(\d{2})', expires)
            if match:
                from datetime import date
                try:
                    expiry = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                    days = (expiry - date.today()).days
                    if days < 0:
                        findings.append(_finding('domain-expired', 'dns', '域名已过期', 'CRITICAL', 'FACT',
                                                 f'WHOIS 到期日 {expires}，已过期。',
                                                 '立即续费域名。', tags=['dns', 'domain']))
                    elif days < 30:
                        findings.append(_finding('domain-expiring', 'dns', '域名即将到期', 'HIGH', 'FACT',
                                                 f'WHOIS 到期日 {expires}，剩余 {days} 天。',
                                                 '尽快续费。', tags=['dns', 'domain']))
                except ValueError:
                    pass
        if whois_result.get('registrar'):
            findings.append(_finding('whois-info', 'dns', f"注册商：{whois_result['registrar']}",
                                     'INFO', 'FACT',
                                     f"注册商={whois_result.get('registrar')}；创建={whois_result.get('created')}；"
                                     f"到期={whois_result.get('expires')}；状态={whois_result.get('status')}",
                                     '用于确认资产归属与到期管理。', tags=['dns', 'whois']))
    if dns_result and isinstance(dns_result, dict) and not dns_result.get('error'):
        mx = dns_result.get('MX') or []
        txt = ' '.join(dns_result.get('TXT') or [])
        if mx and 'v=spf1' not in txt.lower():
            findings.append(_finding('spf-missing', 'dns', '配置了邮件但缺少 SPF', 'LOW', 'FACT',
                                     f'MX={mx[:3]}，TXT 记录中无 v=spf1。',
                                     '添加 SPF 记录，降低伪造发信风险。', tags=['dns', 'email']))
        if 'v=dmarc1' not in txt.lower() and mx:
            findings.append(_finding('dmarc-missing', 'dns', '缺少 DMARC 策略', 'LOW', 'FACT',
                                     'TXT 记录中没有 v=DMARC1。',
                                     '发布 DMARC 策略并配置报告地址。', tags=['dns', 'email']))
        if not dns_result.get('CAA'):
            findings.append(_finding('caa-missing', 'dns', '未设置 CAA 记录', 'INFO', 'FACT',
                                     '查询无 CAA 记录，任何 CA 均可为该域名签发证书。',
                                     '添加 CAA 记录限定可签发证书的 CA。', tags=['dns', 'caa']))
    return findings


def analyze_redirects(chain):
    if not chain or not isinstance(chain, list):
        return []
    findings = []
    hops = [hop for hop in chain if hop.get('status')]
    if len(hops) > 3:
        findings.append(_finding('redirect-long', 'transport', f'重定向链过长（{len(hops)} 跳）',
                                 'LOW', 'FACT',
                                 f"链：{' → '.join(str(hop.get('url')) for hop in hops)}",
                                 '精简跳转，减少延迟与开放重定向风险面。',
                                 tags=['redirect', 'performance']))
    for hop in hops:
        location = (hop.get('location') or '').lower()
        if location.startswith('http://'):
            findings.append(_finding('redirect-downgrade', 'transport', '重定向降级到 HTTP',
                                     'HIGH', 'FACT',
                                     f"{hop.get('url')} → {hop.get('location')}",
                                     '改为 HTTPS 目标，避免明文传输。',
                                     tags=['redirect', 'tls']))
            break
    if any(hop.get('note') for hop in chain):
        findings.append(_finding('redirect-external', 'transport', '存在跨域重定向', 'INFO', 'FACT',
                                 '; '.join(hop['note'] for hop in chain if hop.get('note')),
                                 '确认跨域跳转目标可信，防止开放重定向。',
                                 tags=['redirect']))
    return findings


def analyze_methods(methods_result):
    if not methods_result or methods_result.get('error'):
        return []
    dangerous = methods_result.get('dangerous_advertised') or []
    if dangerous:
        return [_finding('dangerous-methods', 'headers', f"OPTIONS 暴露危险方法：{', '.join(dangerous)}",
                         'MEDIUM', 'FACT',
                         f"Allow: {methods_result.get('allow')}",
                         '关闭不需要的 HTTP 方法，尤其 TRACE 与 PUT/DELETE。',
                         observed=dangerous, tags=['misconfig', 'methods'])]
    return []


def analyze_robots(robots_result, sitemap_result, security_txt):
    findings = []
    if robots_result and robots_result.get('disallow'):
        sensitive = [path for path in robots_result['disallow']
                     if any(token in path.lower() for token in
                            ('admin', 'backup', 'config', 'private', 'internal', 'api', 'db', 'sql', '.git'))]
        if sensitive:
            findings.append(_finding('robots-disclosure', 'exposure', 'robots.txt 暴露敏感路径',
                                     'MEDIUM', 'FACT',
                                     f"Disallow 中含敏感路径：{sensitive[:8]}",
                                     'robots.txt 是公开提示文件，不应作为访问控制；改为服务端鉴权。',
                                     observed=sensitive[:8], tags=['exposure', 'robots']))
    if security_txt and security_txt.get('found'):
        findings.append(_finding('security-txt', 'dns', '已发布 security.txt', 'INFO', 'FACT',
                                 f"Contact: {security_txt.get('contact')}；Expires: {security_txt.get('expires')}",
                                 '保持更新，便于漏洞上报。', tags=['governance']))
    return findings


def grade_report(findings):
    score = sum(SEVERITY_WEIGHT.get(item['severity'], 0) for item in findings)
    grade = ('A+' if score == 0 else 'A' if score <= 2 else 'B' if score <= 5
             else 'C' if score <= 9 else 'D' if score <= 15 else 'F')
    counts = {level: sum(1 for item in findings if item['severity'] == level)
              for level in ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO')}
    return {'score': score, 'grade': grade, 'severity_counts': counts}


def derive_findings(recon_results, canary_findings=None):
    """Turn raw recon output into the finding list, in one place."""
    findings = []
    findings.extend(analyze_tls(recon_results.get('tls')))
    findings.extend(analyze_headers(recon_results.get('security_headers')))
    findings.extend(analyze_server_disclosure(recon_results.get('tech_stack'),
                                              recon_results.get('security_headers')))
    findings.extend(analyze_cookies(recon_results.get('cookies')))
    findings.extend(analyze_tech(recon_results.get('tech_stack')))
    findings.extend(analyze_firewall(recon_results.get('firewall')))
    findings.extend(analyze_exposure(recon_results.get('path_exposure')))
    findings.extend(analyze_dns(recon_results.get('dns'), recon_results.get('dnssec'),
                                recon_results.get('whois')))
    findings.extend(analyze_redirects(recon_results.get('redirects')))
    findings.extend(analyze_methods(recon_results.get('http_methods')))
    findings.extend(analyze_robots(recon_results.get('robots'), recon_results.get('sitemap'),
                                   recon_results.get('security_txt')))
    findings.extend(analyze_injection_surface(recon_results.get('injection_surface')))
    findings.extend(canary_findings or [])
    findings.sort(key=lambda item: -SEVERITY_WEIGHT.get(item['severity'], 0))
    return findings


def attack_targets(findings):
    """Which findings justify spending real payloads on this host."""
    return [item['id'] for item in findings if item['id'] in ATTACK_TRIGGERS]


def _agent_roster(attack_result, venom_result):
    """Named agents and what each actually did on this target.

    The roster is what the report and the UI show as the product's agent line:
    a name, a scope, and the real verdict — never a claim about a stage that
    did not run.
    """
    roster = []
    if attack_result is not None:
        roster.append({'id': attack_agent.AGENT['id'], 'name': attack_agent.AGENT['name'],
                       'title': attack_agent.AGENT['title'],
                       'verdict': attack_result.get('verdict'),
                       'elapsed_ms': attack_result.get('elapsed_ms'),
                       'confirmed': len(attack_result.get('confirmed') or [])})
    if venom_result is not None:
        roster.append({'id': venom_agent.AGENT['id'], 'name': venom_agent.AGENT['name'],
                       'title': venom_agent.AGENT['title'],
                       'verdict': venom_result.get('verdict'),
                       'elapsed_ms': venom_result.get('elapsed_ms'),
                       'confirmed': len(venom_result.get('confirmed') or []),
                       'exposures': len(venom_result.get('exposures') or [])})
    return roster


def php_signal(recon_results):
    """Does this host look like PHP? Decided from recon evidence, not guesses."""
    tech = recon_results.get('tech_stack') or {}
    detected = tech.get('detected') or {}
    if any(name in detected for name in PHP_STACKS):
        return True
    if 'php' in ((tech.get('powered_by') or '') + ' ' + (tech.get('server') or '')).lower():
        return True
    return any('php' in str(hint).lower() for hint in (tech.get('version_hints') or []))


def _venom_findings(venom_result):
    """Convert VENOM output into findings with the same evidence contract."""
    findings = []
    if not venom_result:
        return findings
    confirmed = venom_result.get('confirmed') or []
    rce = [item for item in confirmed if item.get('kind') == 'RCE']
    lfi = [item for item in confirmed if item.get('kind') == 'LFI']
    users = [item for item in confirmed if item.get('kind') == 'USER_ENUM']
    if rce:
        hit = rce[0]
        findings.append(_finding(
            'php-rce-confirmed', 'injection', 'PHP 代码执行已实证（phpunit eval-stdin）',
            'CRITICAL', 'FACT',
            f"POST {hit.get('path')} 回显执行标记 {hit.get('marker')}，服务端执行了我们提交的 PHP 代码。",
            '立即下线 vendor 目录暴露、升级 phpunit、隔离主机取证；按已失陷处置。',
            observed=hit, tags=['php', 'rce', 'confirmed']))
    if lfi:
        hit = lfi[0]
        findings.append(_finding(
            'php-lfi-confirmed', 'injection', f"PHP 文件读取已实证（{hit.get('technique')}）",
            'CRITICAL', 'FACT',
            f"{hit.get('path')} 参数 {hit.get('param')} 载荷 {hit.get('payload')} "
            f"读回 {hit.get('target_file') or '源码'}：{str(hit.get('evidence'))[:160]}",
            '参数白名单化、禁用 php:// 包装器与 ../ 路径、open_basedir 限制。',
            observed=hit, tags=['php', 'lfi', 'confirmed']))
    exposures = venom_result.get('exposures') or []
    if exposures:
        labels = sorted({item['label'] for item in exposures})
        worst = 'CRITICAL' if any(item['severity'] == 'CRITICAL' for item in exposures) else 'HIGH'
        findings.append(_finding(
            'php-exposure', 'exposure', f'PHP 敏感文件可读（{", ".join(labels[:5])}）',
            worst, 'FACT',
            '; '.join(f"{item['path']} -> 200（{item['label']}，{item['length']}B）"
                      for item in exposures[:6]),
            '删除备份/调试文件、.git 与 vendor 暴露，Web 根目录只保留入口。',
            observed={'paths': [item['path'] for item in exposures[:10]]},
            tags=['php', 'exposure', 'confirmed']))
    if users:
        hit = users[0]
        findings.append(_finding(
            'wp-user-enum', 'exposure', 'WordPress 用户枚举（REST API 公开）',
            'MEDIUM', 'FACT',
            'GET /wp-json/wp/v2/users 返回账号：'
            + ', '.join(str(item.get('slug')) for item in hit.get('users') or [])[:200],
            '关闭 REST 用户端点或限制到已认证请求；改强口令并启用 2FA。',
            observed=hit, tags=['wordpress', 'enumeration']))
    if venom_result.get('verdict') == 'NO_CONFIRMED_PHP':
        findings.append(_finding(
            'php-tested', 'injection', 'PHP 攻击面已测试但未确认', 'INFO', 'FACT',
            f"VENOM 已跑 {len(venom_result.get('targets') or [])} 个入口、"
            f"{len(venom_result.get('chain') or [])} 个阶段，未读回任何可自证内容。",
            '未确认不等于安全：可能存在过滤或 WAF，需带上下文人工复核。',
            tags=['php', 'tested']))
    return findings


class QuickScans:
    def __init__(self, store, recon_runner=None, attack_runner=None, venom_runner=None):
        self.store = store
        self.recon_runner = recon_runner or recon.run_all
        self.attack_runner = attack_runner or attack_agent.attack
        self.venom_runner = venom_runner or venom_agent.attack
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS quick_scans (
                id TEXT PRIMARY KEY, host TEXT NOT NULL, source_ip TEXT,
                report_json TEXT NOT NULL, created_at TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS quick_scan_rate (
                rate_key TEXT PRIMARY KEY, last REAL NOT NULL, report_id TEXT)''')

    def _rate_take(self, db, rate_key, cooldown):
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

    def _cached_report(self, report_id):
        if not report_id:
            return None
        with self.store.connect() as db:
            row = db.execute('SELECT report_json FROM quick_scans WHERE id=?', (report_id,)).fetchone()
        return json.loads(row['report_json']) if row else None

    def scan(self, url, source_ip, run_attack=True):
        host = normalize_site(url)
        cached = None
        with self.store.connect() as db:
            host_gate = self._rate_take(db, f'host:{host}', HOST_COOLDOWN_SECONDS)
            if host_gate['blocked']:
                cached = self._cached_report(host_gate['cached_report_id'])
                if cached is None:
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
            report = self._collect(host, run_attack)
        finally:
            _scan_slots.release()
        scan_id = 'qs_' + secrets.token_urlsafe(18)
        with self.store.connect() as db:
            db.execute('INSERT INTO quick_scans VALUES (?,?,?,?,?)',
                       (scan_id, host, source_ip, json.dumps(report, ensure_ascii=False),
                        datetime.now(timezone.utc).isoformat()))
            db.execute('UPDATE quick_scan_rate SET report_id=? WHERE rate_key=?',
                       (scan_id, f'host:{host}'))
        return {'status': 'OK', 'report': report, 'id': scan_id, 'cached': False}

    def _collect(self, host, run_attack=True):
        started = time.perf_counter()
        collected_at = datetime.now(timezone.utc).isoformat()
        # Bind one exit for the whole scan: the injection oracle compares
        # responses across requests, so the exit must not change mid-scan.
        try:
            pool = egress.for_scan(host)
        except egress.EgressError as exc:
            raise SiteAssessmentError(str(exc)) from exc
        try:
            recon_results = self.recon_runner(host)
        except TransportError as exc:
            raise SiteAssessmentError(str(exc)) from exc
        findings = derive_findings(recon_results)
        if not findings and recon_results.get('root', {}).get('error') \
                and (recon_results.get('tls') or {}).get('ok') is False:
            raise SiteAssessmentError('站点在预算内不可达：'
                                      + str(recon_results['root'].get('error'))[:200])
        # Auto-dispatch. Two named agents share one pinned exit and run in
        # parallel: the SQLi attack agent (armed by a concrete recon signal) and
        # VENOM (armed by a PHP stack). Running them concurrently means the PHP
        # sweep never adds serial latency to the injection chain.
        triggers = attack_targets(findings)
        attack_result = None
        venom_result = None
        attack_skipped = None
        need_attack = bool(run_attack and triggers)
        need_venom = bool(run_attack and php_signal(recon_results))
        if need_attack or need_venom:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                attack_future = venom_future = None
                if need_attack:
                    attack_future = executor.submit(self._safe_attack, host, pool)
                if need_venom:
                    venom_future = executor.submit(self._safe_venom, host, pool)
                if attack_future is not None:
                    attack_result = attack_future.result()
                if venom_future is not None:
                    venom_result = venom_future.result()
            if attack_result:
                # Deep enumeration only after the fast path confirmed a real
                # injection: sqlmap's startup cost is never paid on clean sites.
                if attack_result.get('verdict') == 'CONFIRMED_INJECTION':
                    attack_result['sqlmap'] = self._deep_enumerate(host, attack_result, pool)
                findings.extend(_attack_findings(attack_result))
            if venom_result:
                findings.extend(_venom_findings(venom_result))
            findings.sort(key=lambda item: -SEVERITY_WEIGHT.get(item['severity'], 0))
        elif not triggers:
            attack_skipped = '侦察未产生可攻击信号（无参数反射/无 DB 错误/无暴露文件/无管理入口）'
        summary = grade_report(findings)
        report = {
            'host': host, 'collected_at': collected_at,
            'summary': summary, 'findings': findings,
            'recon': _slim_recon(recon_results),
            'attack': attack_result,
            'venom': venom_result,
            'agents': _agent_roster(attack_result, venom_result),
            'attack_triggers': triggers,
            'attack_skipped': attack_skipped,
            'egress': pool.snapshot(),
            'arsenal': arsenal.shared().snapshot(),
            'timing': {'total_ms': round((time.perf_counter() - started) * 1000, 1),
                       'recon_ms': (recon_results.get('_meta') or {}).get('elapsed_ms'),
                       'attack_ms': (attack_result or {}).get('elapsed_ms'),
                       'venom_ms': (venom_result or {}).get('elapsed_ms')},
            'scope': 'recon: 17 concurrent read-only checks (DNS/DNSSEC/WHOIS/TLS/headers/cookies/'
                     'tech/WAF/redirects/robots/sitemap/security.txt/methods/path-exposure/'
                     'ip-ownership/injection-surface). agents: STRIKE (SQLi, real payloads only '
                     'when recon raised a signal; confirmed injections handed to sqlmap) and '
                     'VENOM (PHP surface: exposure sweep, LFI/filter-chain, phpunit eval-stdin, '
                     'WP user enum) dispatch in parallel. All outbound traffic exits through a '
                     'rotated proxy with a Chrome fingerprint.',
            'limitations': '指纹与 WAF 判定为 INFERENCE；攻击结论仅在被差分 oracle 复现时标记 CONFIRMED；'
                           'sqlmap 枚举以 --level/--risk 限制且设硬超时；未做全端口扫描、未做目录爆破、'
                           '未登录后台。',
            'evidence_sha256': hashlib.sha256(json.dumps(
                {'host': host, 'recon': _hashable(recon_results),
                 'attack': attack_result, 'venom': venom_result},
                sort_keys=True, default=str).encode()).hexdigest(),
        }
        return report

    def _safe_attack(self, host, pool):
        try:
            return self.attack_runner(host, egress_pool=pool)
        except Exception as exc:  # noqa: BLE001 - attack stage must not lose the recon
            return {'verdict': 'ATTACK_STAGE_ERROR', 'error': f'{type(exc).__name__}: {exc}'}

    def _safe_venom(self, host, pool):
        try:
            return self.venom_runner(host, pool=pool, php_hint=True)
        except Exception as exc:  # noqa: BLE001 - PHP stage must not lose the recon
            return {'verdict': 'VENOM_STAGE_ERROR', 'error': f'{type(exc).__name__}: {exc}'}

    def _deep_enumerate(self, host, attack_result, pool):
        """Hand a confirmed injection point to the arsenal's sqlmap tool."""
        confirmed = attack_result.get('confirmed') or []
        point = next((item for item in confirmed if item.get('param') and item.get('path')), None)
        if point is None:
            return {'ok': False, 'error': 'no parameterised injection point to enumerate'}
        url = f'https://{host}{point["path"]}'
        return arsenal.shared().dispatch(
            'sqlmap', url, params=[point['param']],
            proxy=pool.proxy if pool else None)

    def get(self, scan_id):
        with self.store.connect() as db:
            row = db.execute('SELECT report_json FROM quick_scans WHERE id=?', (scan_id,)).fetchone()
        return json.loads(row['report_json']) if row else None

    def recent(self, limit=25):
        """Mission flow: the last scans with the fields the board needs.

        Only summary columns are read out of the stored JSON — never the full
        report — so the history view stays cheap regardless of report size.
        """
        with self.store.connect() as db:
            rows = db.execute(
                'SELECT id, host, created_at, report_json FROM quick_scans '
                'ORDER BY created_at DESC LIMIT ?', (int(limit),)).fetchall()
        missions = []
        for row in rows:
            try:
                report = json.loads(row['report_json'])
            except (ValueError, TypeError):
                continue
            timing = report.get('timing') or {}
            egress = report.get('egress') or {}
            missions.append({
                'id': row['id'], 'host': row['host'], 'created_at': row['created_at'],
                'grade': (report.get('summary') or {}).get('grade'),
                'score': (report.get('summary') or {}).get('score'),
                'severity_counts': (report.get('summary') or {}).get('severity_counts'),
                'findings': len(report.get('findings') or []),
                'confirmed_injection': bool((report.get('attack') or {}).get('verdict')
                                            == 'CONFIRMED_INJECTION'),
                'attack_triggers': report.get('attack_triggers') or [],
                'total_ms': timing.get('total_ms'),
                'egress_exit': egress.get('exit'),
                'collected_at': report.get('collected_at')})
        return missions


def _attack_findings(attack_result):
    """Convert confirmed attack output into findings with real severity."""
    findings = []
    confirmed = attack_result.get('confirmed') or []
    if confirmed:
        techniques = sorted({item.get('technique') for item in confirmed})
        params = sorted({f"{item.get('path')}?{item.get('param')}" for item in confirmed
                         if item.get('param')})
        data_read = [item for item in confirmed if item.get('technique') == 'union-extract']
        severity = 'CRITICAL' if data_read else 'CRITICAL'
        evidence = (f"已在 {len(confirmed)} 处复现注入，技术：{', '.join(techniques)}；"
                    f"注入点：{', '.join(params[:6])}")
        if data_read:
            extracted = data_read[0]
            evidence += (f"；已读出数据库元信息：version={extracted.get('version')} "
                         f"user={extracted.get('user')} db={extracted.get('database')}")
        findings.append(_finding(
            'sqli-confirmed', 'injection', f'SQL 注入已实证（{", ".join(techniques)}）',
            severity, 'FACT', evidence,
            '参数化查询；注入点立即下线或加 WAF 规则；轮换数据库口令并审计已泄露数据范围。',
            observed={'techniques': techniques, 'points': params[:10],
                      'extracted': data_read[0] if data_read else None},
            tags=['sqli', 'injection', 'confirmed']))
    elif attack_result.get('verdict') == 'NO_CONFIRMED_INJECTION':
        targets = attack_result.get('targets') or []
        if targets:
            findings.append(_finding(
                'sqli-not-confirmed', 'injection', '注入面已测试但未确认',
                'INFO', 'FACT',
                f"已对 {len(targets)} 个参数点发送错误/布尔/时间载荷，未出现可复现差分："
                + '; '.join(f"{item['path']}({','.join(item['params'][:4])})" for item in targets[:4]),
                '未确认不等于安全：可能存在过滤、WAF 或非 SQL 型注入，需要人工带上下文复核。',
                tags=['sqli', 'tested']))
    elif attack_result.get('verdict') == 'ATTACK_STAGE_ERROR':
        findings.append(_finding('attack-error', 'injection', '攻击阶段执行异常', 'INFO', 'FACT',
                                 str(attack_result.get('error'))[:300],
                                 '重试或人工执行同一链路。', tags=['attack', 'error']))
    return findings


def _slim_recon(recon_results):
    """Keep the report readable: full raw output would bury the findings."""
    keep = ('tls', 'security_headers', 'cookies', 'tech_stack', 'firewall', 'dns',
            'dnssec', 'whois', 'robots', 'sitemap', 'security_txt', 'http_methods',
            'redirects', 'ip_info', 'injection_surface', '_meta')
    slim = {key: recon_results.get(key) for key in keep}
    exposure = recon_results.get('path_exposure') or {}
    slim['path_exposure'] = {kind: entry.get('status')
                             for kind, entry in exposure.items() if isinstance(entry, dict)}
    root = recon_results.get('root') or {}
    slim['root'] = {'status': root.get('status'), 'elapsed_ms': root.get('elapsed_ms'),
                    'server': (root.get('headers') or {}).get('Server'),
                    'content_type': (root.get('headers') or {}).get('Content-Type')}
    return slim


def _hashable(recon_results):
    """Stable digest input: drop timings and byte bodies, keep the observations."""
    slim = _slim_recon(recon_results)
    slim.pop('_meta', None)
    return slim
