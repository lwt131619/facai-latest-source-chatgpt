"""Parallel recon fan-out for the fast path.

Design rules learned from Web-Check's 44 checks, minus the ones that only cost
latency: no traceroute, no full port sweep, no screenshot, no third-party
intel API round trips. Everything here is a bounded, same-host, read-only
observation that either returns fast or is cut off by its own timeout.

All checks run concurrently; a slow one cannot delay the rest.
"""
import concurrent.futures
import json
import re
import socket
import ssl
import time
from datetime import datetime, timezone

from . import net_client
from .net_client import TransportError

DNS_TIMEOUT = 4
MAX_WORKERS = 12


def _safe(fn, *args, **kwargs):
    """Run one check; never let a single failure abort the fan-out."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - recon must survive any single check
        return {'error': f'{type(exc).__name__}: {exc}'}


def _with_pool(fn, host, pool):
    """Bind the scan's pinned exit to this worker thread, then run the check.

    Checks keep their single-argument signature; the active exit lives in a
    thread-local so every nested net_client call inherits it without threading
    a parameter through seventeen functions.
    """
    from . import egress as _egress
    _egress.set_current(pool)
    try:
        return _safe(fn, host)
    finally:
        _egress.set_current(None)


# ---------------------------------------------------------------- DNS / domain

def check_dns(host):
    try:
        import dns.resolver
    except ImportError:
        return {'skipped': 'dnspython not installed'}
    resolver = dns.resolver.Resolver()
    resolver.timeout = DNS_TIMEOUT
    resolver.lifetime = DNS_TIMEOUT
    result = {}
    for record_type in ('A', 'AAAA', 'MX', 'NS', 'TXT', 'CNAME', 'SOA', 'CAA'):
        try:
            answers = resolver.resolve(host, record_type, raise_on_no_answer=False)
            values = [str(item).strip('"') for item in answers] if answers.rrset else []
            if values:
                result[record_type] = values[:20]
        except Exception:  # noqa: BLE001 - missing record types are normal
            continue
    return result


def check_dnssec(host):
    try:
        import dns.message
        import dns.query
        import dns.rdatatype
        import dns.resolver
    except ImportError:
        return {'skipped': 'dnspython not installed'}
    try:
        query = dns.message.make_query(host, dns.rdatatype.A, want_dnssec=True)
        nameservers = [str(item).strip('.') for item in
                       (dns.resolver.Resolver().resolve(host, 'NS').rrset or [])][:2] \
            if _has_ns(host) else []
        for nameserver in nameservers:
            try:
                response = dns.query.udp(query, nameserver, timeout=DNS_TIMEOUT)
            except Exception:  # noqa: BLE001
                continue
            for rrset in response.answer:
                if rrset.rdtype == dns.rdatatype.RRSIG:
                    return {'signed': True, 'nameserver': nameserver}
            return {'signed': False, 'nameserver': nameserver}
    except Exception as exc:  # noqa: BLE001
        return {'error': f'{type(exc).__name__}: {exc}'}
    return {'signed': None, 'note': 'no authoritative nameserver answered over UDP'}


def _has_ns(host):
    try:
        import dns.resolver
        return bool(dns.resolver.Resolver().resolve(host, 'NS').rrset)
    except Exception:  # noqa: BLE001
        return False


def check_whois(host):
    """Minimal WHOIS via port 43; registrar/created/expires only."""
    import socket as _socket
    tld = host.rsplit('.', 1)[-1].lower()
    servers = {'com': 'whois.verisign-grs.com', 'net': 'whois.verisign-grs.com',
               'org': 'whois.pir.org', 'io': 'whois.nic.io', 'top': 'whois.nic.top',
               'lol': 'whois.nic.lol', 'xyz': 'whois.nic.xyz', 'cn': 'whois.cnnic.cn'}
    server = servers.get(tld)
    if not server:
        return {'skipped': f'no WHOIS server mapping for .{tld}'}
    try:
        with _socket.create_connection((server, 43), timeout=DNS_TIMEOUT) as sock:
            sock.sendall((host + '\r\n').encode())
            chunks = []
            while len(b''.join(chunks)) < 20000:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
    except OSError as exc:
        return {'error': f'{type(exc).__name__}: {exc}'}
    raw = b''.join(chunks).decode('utf-8', 'replace')
    extracted = {}
    patterns = {
        'registrar': r'(?im)^\s*Registrar:\s*(.+)$',
        'created': r'(?im)^\s*(?:Creation Date|Created On|Registration Time):\s*(.+)$',
        'expires': r'(?im)^\s*(?:Registry Expiry Date|Expiration Date|Expiry Date):\s*(.+)$',
        'updated': r'(?im)^\s*(?:Updated Date|Last Updated On):\s*(.+)$',
        'status': r'(?im)^\s*(?:Domain Status|Status):\s*(.+)$',
        'registrant_country': r'(?im)^\s*Registrant Country:\s*(.+)$',
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, raw)
        if match:
            extracted[key] = match.group(1).strip()[:120]
    return extracted or {'raw_excerpt': raw[:300]}


# ---------------------------------------------------------------- TLS / cert

def check_tls(host, port=443):
    address = net_client.resolve_public(host)[0]
    summary = {'ok': True, 'tls_version': None, 'certificate': {}}
    # Handshake with verification on: the failure reason is itself a finding.
    try:
        context = net_client.default_context()
        raw = socket.create_connection((address, port), timeout=6)
        wrapped = context.wrap_socket(raw, server_hostname=host)
        try:
            cert = wrapped.getpeercert()
            version = wrapped.version()
            cipher = wrapped.cipher()
        finally:
            wrapped.close()
        verified = True
    except ssl.SSLCertVerificationError as exc:
        verified = False
        verify_error = f'{type(exc).__name__}: {exc}'
        # Retry without verification so we can still report protocol/cipher/cert.
        try:
            context = net_client.default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            raw = socket.create_connection((address, port), timeout=6)
            wrapped = context.wrap_socket(raw, server_hostname=host)
            try:
                cert = wrapped.getpeercert()
                version = wrapped.version()
                cipher = wrapped.cipher()
            finally:
                wrapped.close()
        except (OSError, ssl.SSLError, ValueError) as exc2:
            return {'ok': False, 'error': f'{type(exc2).__name__}: {exc2}',
                    'cert_verify_failed': verify_error}
    except (OSError, ssl.SSLError, ValueError) as exc:
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}

    def rdn(sequence, wanted):
        for entry in sequence or ():
            for key, value in entry:
                if key == wanted:
                    return value
        return None

    summary.update({
        'ok': True, 'tls_version': version, 'verified': verified,
        'cert_verify_failed': None if verified else verify_error,
        'cipher': cipher[0] if cipher else None,
        'cipher_bits': cipher[2] if cipher and len(cipher) > 2 else None,
        'subject_cn': rdn(cert.get('subject'), 'commonName'),
        'issuer': rdn(cert.get('issuer'), 'organizationName') or rdn(cert.get('issuer'), 'commonName'),
        'san': [value for kind, value in cert.get('subjectAltName', ())][:20],
        'not_before': cert.get('notBefore'), 'not_after': cert.get('notAfter')})
    for fmt in ('%b %d %H:%M:%S %Y %Z', '%b %d %H:%M:%S %Y GMT'):
        try:
            expiry = datetime.strptime(cert.get('notAfter', ''), fmt).replace(tzinfo=timezone.utc)
            summary['seconds_remaining'] = round((expiry - datetime.now(timezone.utc)).total_seconds())
            break
        except ValueError:
            continue
    summary['obsolete_protocol'] = version in ('TLSv1', 'TLSv1.1', 'SSLv3')
    summary['weak_cipher'] = bool(cipher and ('RC4' in cipher[0] or '3DES' in cipher[0]
                                              or 'MD5' in cipher[0]))
    return summary


# ---------------------------------------------------------------- HTTP surface

def check_root(host):
    response = net_client.request(host, '/', max_bytes=131072)
    if response.get('error'):
        return response
    return {'status': response['status'], 'headers': response['headers'],
            'header_list': response['header_list'], 'elapsed_ms': response['elapsed_ms'],
            'body_len': len(response['body']), 'body_head': net_client.text_of(response, 200000)}


def check_redirect_chain(host, max_hops=5):
    """Follow same-registered-domain redirects only; report the chain."""
    chain = []
    current = host
    path = '/'
    for _ in range(max_hops):
        response = net_client.request(current, path, max_bytes=4096)
        if response.get('error'):
            chain.append({'url': response.get('url'), 'error': response['error']})
            break
        location = net_client.header(response, 'Location')
        chain.append({'url': response.get('url'), 'status': response['status'],
                      'location': location})
        if response['status'] not in (301, 302, 303, 307, 308) or not location:
            break
        if location.startswith('/'):
            path = location
            continue
        match = re.match(r'https?://([^/:]+)(.*)', location)
        if not match:
            break
        next_host = match.group(1).lower()
        # Only keep following inside the same registrable domain; a hop to a
        # different site is reported but not fetched.
        if _registrable(next_host) != _registrable(host):
            chain.append({'note': f'cross-domain redirect to {next_host} not followed'})
            break
        current, path = next_host, match.group(2) or '/'
    return chain


def _registrable(host):
    parts = host.split('.')
    return '.'.join(parts[-2:]) if len(parts) >= 2 else host


def check_robots(host):
    response = net_client.request(host, '/robots.txt', max_bytes=32768)
    if response.get('error'):
        return response
    text = net_client.text_of(response)
    disallow = [line.split(':', 1)[1].strip() for line in text.splitlines()
                if line.lower().startswith(('disallow:', 'allow:')) and ':' in line]
    return {'status': response['status'], 'disallow': disallow[:60],
            'sitemaps': [line.split(':', 1)[1].strip() for line in text.splitlines()
                         if line.lower().startswith('sitemap:')][:10],
            'raw_len': len(text)}


def check_sitemap(host):
    candidates = ['/sitemap.xml', '/sitemap_index.xml', '/sitemap.xml.gz']
    for path in candidates:
        response = net_client.request(host, path, max_bytes=65536)
        if response.get('status') == 200 and response.get('body'):
            text = net_client.text_of(response)
            urls = re.findall(r'<loc>\s*([^<\s]+)\s*</loc>', text)
            return {'path': path, 'url_count': len(urls), 'sample': urls[:10]}
    return {'found': False, 'tried': candidates}


def check_security_txt(host):
    response = net_client.request(host, '/.well-known/security.txt', max_bytes=16384)
    if response.get('status') == 200:
        text = net_client.text_of(response)
        contact = [line.split(':', 1)[1].strip() for line in text.splitlines()
                   if line.lower().startswith('contact:')]
        return {'found': True, 'contact': contact[:5], 'expires': next(
            (line.split(':', 1)[1].strip() for line in text.splitlines()
             if line.lower().startswith('expires:')), None)}
    return {'found': False, 'status': response.get('status')}


SECURITY_HEADER_SPECS = (
    ('strict-transport-security', 'HSTS', 'max-age'),
    ('content-security-policy', 'CSP', None),
    ('x-content-type-options', 'X-Content-Type-Options', 'nosniff'),
    ('x-frame-options', 'X-Frame-Options', None),
    ('referrer-policy', 'Referrer-Policy', None),
    ('permissions-policy', 'Permissions-Policy', None),
    ('cross-origin-opener-policy', 'COOP', None),
    ('cross-origin-embedder-policy', 'COEP', None),
    ('cross-origin-resource-policy', 'CORP', None),
    ('x-xss-protection', 'X-XSS-Protection', None),
)


def check_security_headers(host, root=None):
    root = root or check_root(host)
    if root.get('error'):
        return root
    headers = root.get('headers') or {}
    lowered = {key.lower(): value for key, value in headers.items()}
    return {name: {'present': lowered.get(key) is not None, 'value': lowered.get(key)}
            for key, name, _ in SECURITY_HEADER_SPECS}


def check_cookies(host, root=None):
    root = root or check_root(host)
    if root.get('error'):
        return root
    cookies = net_client.headers_multi(root, 'Set-Cookie')
    parsed = []
    for cookie in cookies[:20]:
        name = cookie.split('=', 1)[0].strip()
        parts = [part.strip().lower() for part in cookie.split(';')]
        parsed.append({'name': name, 'secure': 'secure' in parts,
                       'httponly': 'httponly' in parts,
                       'samesite': next((part.split('=', 1)[1] for part in parts
                                         if part.startswith('samesite=')), None),
                       'domain': next((part.split('=', 1)[1] for part in parts
                                       if part.startswith('domain=')), None),
                       'path': next((part.split('=', 1)[1] for part in parts
                                     if part.startswith('path=')), None)})
    return {'count': len(parsed), 'cookies': parsed}


def check_tech_stack(host, root=None):
    """Passive fingerprint; version strings are reported, never mapped to CVEs here."""
    root = root or check_root(host)
    if root.get('error'):
        return root
    headers = {key.lower(): value for key, value in (root.get('headers') or {}).items()}
    body = (root.get('body_head') or '').lower()
    haystack = ' '.join(f'{key}:{value}' for key, value in headers.items()).lower() + ' ' + body
    rules = {
        'WordPress': ('wp-content', 'wp-includes'), 'Drupal': ('drupal.js', 'sites/all/'),
        'Joomla': ('/media/jui/', 'joomla!'), 'Magento': ('mage/cookies', '/static/version'),
        'Shopify': ('cdn.shopify.com', 'shopify'), 'Ghost': ('ghost/api', 'content="ghost'),
        'PHP': ('phpsessid', 'x-powered-by: php'), 'ASP.NET': ('__viewstate', 'asp.net'),
        'Java': ('jsessionid',), 'Nginx': ('server:nginx',), 'Apache': ('server:apache',),
        'IIS': ('server:microsoft-iis',), 'Cloudflare': ('cf-ray', 'cloudflare'),
        'Next.js': ('__next_data__', '_next/static/'), 'Nuxt': ('__nuxt', '_nuxt/'),
        'React': ('data-reactroot', '__react'), 'Vue.js': ('data-v-app', 'vue.runtime'),
        'Angular': ('ng-version', 'angular.js'), 'Laravel': ('laravel_session', 'xsrftoken'),
        'Django': ('csrftoken', 'django'), 'Rails': ('_rails', 'csrf-token'),
        'jQuery': ('jquery.min.js', 'jquery.js'), 'Bootstrap': ('bootstrap.min.css',),
        'Google Analytics': ('gtag(', 'googletagmanager'), 'Cloudflare Turnstile': ('turnstile',),
    }
    detected = {name: [marker for marker in markers if marker in haystack]
                for name, markers in rules.items()}
    detected = {name: hits for name, hits in detected.items() if hits}
    generator = None
    match = re.search(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', body)
    if match:
        generator = match.group(1)[:80]
    version_hints = re.findall(r'(?:wordpress|jquery|bootstrap|php)[/\s-]?(\d+\.\d+(?:\.\d+)?)',
                               haystack)[:6]
    return {'detected': detected, 'generator': generator, 'version_hints': version_hints,
            'server': headers.get('server'), 'powered_by': headers.get('x-powered-by')}


def check_firewall(host):
    """WAF/CDN presence from response headers and a single benign probe."""
    response = net_client.request(host, '/', max_bytes=4096)
    if response.get('error'):
        return response
    headers = {key.lower(): value for key, value in (response.get('headers') or {}).items()}
    vendors = []
    signatures = {
        'Cloudflare': ('cf-ray', 'cf-cache-status', 'server:cloudflare'),
        'AWS CloudFront': ('x-amz-cf-id', 'cloudfront'),
        'Akamai': ('akamai', 'x-akamai'),
        'Fastly': ('x-fastly', 'fastly'),
        'Sucuri': ('x-sucuri', 'sucuri'),
        'Imperva/Incapsula': ('incap_ses', 'visid_incap', 'x-iinfo'),
        'F5 BIG-IP': ('bigip', 'x-wa-info'),
        'Barracuda': ('barra',),
        'Azure Front Door': ('x-azure-ref', 'azurefd'),
    }
    haystack = ' '.join(f'{key}:{value}' for key, value in headers.items()).lower()
    for vendor, markers in signatures.items():
        if any(marker in haystack for marker in markers):
            vendors.append(vendor)
    return {'vendors': vendors, 'status': response['status'],
            'server': headers.get('server'),
            'note': 'header-based inference only; absence does not prove no WAF'}


def check_http_methods(host):
    """OPTIONS is enough to see advertised methods; no state-changing verbs sent."""
    response = net_client.request(host, '/', method='OPTIONS', max_bytes=2048)
    if response.get('error'):
        return response
    allow = net_client.header(response, 'Allow') or net_client.header(response, 'Access-Control-Allow-Methods')
    dangerous = [method for method in ('PUT', 'DELETE', 'TRACE', 'PATCH', 'CONNECT')
                 if allow and method in allow.upper()]
    return {'status': response['status'], 'allow': allow, 'dangerous_advertised': dangerous}


def check_path_exposure(host):
    """High-value file probes, all read-only, all bounded."""
    probes = {
        '/.git/HEAD': ('git', lambda text: text.startswith('ref:') or text.startswith('0000000')),
        '/.env': ('env', lambda text: '=' in text and any(
            token in text.upper() for token in ('KEY', 'PASSWORD', 'SECRET', 'TOKEN', 'DB_'))),
        '/.svn/entries': ('svn', lambda text: text.strip().isdigit() or 'dir' in text[:40]),
        '/.DS_Store': ('ds_store', lambda text: text.startswith('\x00\x00\x00\x01Bud1') or 'Bud1' in text[:16]),
        '/backup.sql': ('backup_sql', lambda text: 'CREATE TABLE' in text.upper() or 'INSERT INTO' in text.upper()),
        '/wp-config.php.bak': ('wp_config', lambda text: 'DB_PASSWORD' in text or 'DB_NAME' in text),
        '/config.php.bak': ('config_bak', lambda text: 'password' in text.lower() or 'db_' in text.lower()),
        '/server-status': ('server_status', lambda text: 'Server Status' in text or 'Total Accesses' in text),
        '/actuator/env': ('actuator', lambda text: '"propertySources"' in text or '"activeProfiles"' in text),
        '/swagger-ui.html': ('swagger', lambda text: 'swagger' in text.lower()),
        '/phpinfo.php': ('phpinfo', lambda text: 'PHP Version' in text),
        '/.well-known/security.txt': ('security_txt', lambda text: 'Contact:' in text),
    }
    hits = {}

    def probe_one(item):
        path, (kind, detector) = item
        try:
            response = net_client.request(host, path, max_bytes=16384)
            if response.get('status') == 200:
                text = net_client.text_of(response)
                if detector(text):
                    return kind, {'path': path, 'status': 200, 'excerpt': text[:200]}
                return kind, {'path': path, 'status': 200, 'excerpt': text[:80], 'unconfirmed': True}
            return kind, {'path': path, 'status': response.get('status') or 0}
        except Exception as exc:  # noqa: BLE001 - one probe must not sink the sweep
            return kind, {'path': path, 'status': 0, 'error': f'{type(exc).__name__}: {exc}'}

    # Concurrent: through a proxy each hop costs ~1s, so twelve sequential
    # probes used to dominate the whole scan (13.9s vs 2.1s direct).
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for kind, entry in pool.map(probe_one, probes.items()):
            hits[kind] = entry
    return hits



# Well-known hosting/CDN ranges, checked locally so no external API can stall a scan.
KNOWN_RANGES = (
    ('Cloudflare', ('104.16.', '104.17.', '104.18.', '104.19.', '104.20.', '104.21.',
                    '104.22.', '104.23.', '104.24.', '104.25.', '104.26.', '104.27.',
                    '172.64.', '172.65.', '172.66.', '172.67.', '162.158.', '188.114.')),
    ('AWS', ('3.', '13.', '18.', '34.', '35.', '52.', '54.', '99.84.')),
    ('Google Cloud', ('34.', '35.', '104.196.', '104.197.', '104.198.')),
    ('Azure', ('13.64.', '20.', '40.', '51.', '52.', '104.40.')),
    ('Akamai', ('23.', '104.64.', '184.24.', '2.16.')),
    ('DigitalOcean', ('104.131.', '138.68.', '159.65.', '167.71.', '174.138.')),
    ('Hetzner', ('5.9.', '78.46.', '88.99.', '116.202.', '136.243.', '159.69.')),
    ('OVH', ('51.38.', '51.68.', '51.75.', '51.83.', '137.74.', '145.239.')),
    ('Alibaba Cloud', ('47.52.', '47.74.', '47.88.', '47.91.', '47.96.', '8.209.')),
    ('Tencent Cloud', ('43.128.', '43.129.', '43.132.', '43.134.', '119.28.', '129.226.')),
)


def check_ip_info(host):
    """Local IP ownership inference: resolution plus a static range table.

    Deliberately no external geo API: a 5s third-party timeout used to gate the
    whole report, and hosting ownership is already visible from the ranges.
    """
    addresses = net_client.resolve_public(host)
    primary = addresses[0]
    owners = sorted({owner for owner, prefixes in KNOWN_RANGES
                     for address in addresses
                     if any(address.startswith(prefix) for prefix in prefixes)})
    return {'address': primary, 'all_addresses': addresses,
            'inferred_owner': owners or None,
            'family': 'IPv6' if ':' in primary else 'IPv4',
            'note': 'ownership inferred from a local range table, not a live API'}


CANARY = 'fcq7k2p'

ADMIN_PATHS = ('/admin', '/administrator', '/wp-admin/', '/manage', '/console',
               '/phpmyadmin', '/adminer.php', '/actuator')

UPLOAD_MARKERS = ('type="file"', 'enctype="multipart/form-data"', 'name="upload"')


def check_injection_surface(host):
    """Cheap canary probes that decide whether the attack agent is worth firing.

    Three observations, all single harmless requests:
      * reflection  — does a query parameter come back unencoded?
      * sql_error   — does a quote in a parameter surface a driver error?
      * admin/upload — is a management or upload surface anonymously reachable?
    """
    result = {}
    # Reflection: unique marker with quote/angle/brace characters.
    marker = f"{CANARY}'\"<>{{}}"
    response = net_client.request(host, f'/?facai_probe={marker}', max_bytes=131072)
    if not response.get('error'):
        text = net_client.text_of(response)
        position = text.find(CANARY)
        if position >= 0:
            window = text[position + len(CANARY):position + len(CANARY) + 24]
            result['reflection'] = {
                'status': response['status'], 'reflected': True,
                'unescaped': any(char in window for char in ("'", '"', '<', '>', '{', '}')),
                'context': window,
                'excerpt': text[max(0, position - 40):position + 60]}
        else:
            result['reflection'] = {'status': response['status'], 'reflected': False}
    else:
        result['reflection'] = {'error': response['error']}
    # SQL error echo: a single quote in a numeric-looking parameter.
    response = net_client.request(host, f'/?id={CANARY}%27', max_bytes=131072)
    if not response.get('error'):
        text = net_client.text_of(response)
        lowered = text.lower()
        markers = ('sql syntax', 'mysql_fetch', 'warning: mysql', 'sqlite3::query',
                   'sqlite error', 'ora-0', 'psqlexception', 'pg_query',
                   'unclosed quotation', 'sqlstate', 'syntax error at or near',
                   'you have an error in your sql syntax', 'microsoft sql')
        hit = next((item for item in markers if item in lowered), None)
        if hit:
            position = lowered.find(hit)
            result['sql_error'] = {'status': response['status'], 'marker': hit,
                                   'excerpt': text[max(0, position - 60):position + 120]}
        else:
            result['sql_error'] = {'status': response['status'], 'marker': None}
    else:
        result['sql_error'] = {'error': response['error']}
    # Admin / upload surfaces, probed concurrently for the same reason as the
    # exposure sweep: each proxied hop is ~1s and eight sequential hops would
    # add 8s to every scan.
    admin_hits = []
    upload_hits = []

    def probe_admin(path):
        try:
            probe = net_client.request(host, path, max_bytes=16384)
        except Exception as exc:  # noqa: BLE001
            return path, 0, f'{type(exc).__name__}: {exc}', False
        status = probe.get('status') or 0
        text = net_client.text_of(probe).lower() if status == 200 else ''
        return path, status, text, bool(text and any(m in text for m in UPLOAD_MARKERS))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as admin_pool:
        for path, status, text, is_upload in admin_pool.map(probe_admin, ADMIN_PATHS):
            if status in (200, 401, 403):
                admin_hits.append({'path': path, 'status': status})
            if is_upload:
                upload_hits.append({'path': path, 'status': status})
    result['admin'] = admin_hits
    result['upload'] = upload_hits
    return result


CHECKS = (
    ('dns', check_dns), ('dnssec', check_dnssec), ('whois', check_whois),
    ('tls', check_tls), ('root', check_root), ('redirects', check_redirect_chain),
    ('robots', check_robots), ('sitemap', check_sitemap), ('security_txt', check_security_txt),
    ('security_headers', check_security_headers), ('cookies', check_cookies),
    ('tech_stack', check_tech_stack), ('firewall', check_firewall),
    ('http_methods', check_http_methods), ('path_exposure', check_path_exposure),
    ('ip_info', check_ip_info),
    ('injection_surface', check_injection_surface),
)


def run_all(host, budget_seconds=25, egress_pool=None):
    """Fan out every check concurrently and collect whatever finishes in budget.

    The pinned exit is forwarded to every check so the whole scan leaves
    through one address.
    """
    started = time.perf_counter()
    results = {}
    deadline = started + budget_seconds
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_with_pool, fn, host, egress_pool): name for name, fn in CHECKS}
        for future in concurrent.futures.as_completed(futures, timeout=budget_seconds + 5):
            name = futures[future]
            remaining = deadline - time.perf_counter()
            try:
                results[name] = future.result(timeout=max(0.1, remaining))
            except concurrent.futures.TimeoutError:
                results[name] = {'error': 'check exceeded scan budget'}
            except Exception as exc:  # noqa: BLE001
                results[name] = {'error': f'{type(exc).__name__}: {exc}'}
    for name, _ in CHECKS:
        results.setdefault(name, {'error': 'not completed within budget'})
    results['_meta'] = {'elapsed_ms': round((time.perf_counter() - started) * 1000, 1),
                        'checks': len(CHECKS), 'budget_seconds': budget_seconds}
    return results
