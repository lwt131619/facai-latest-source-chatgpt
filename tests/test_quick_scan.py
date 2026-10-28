"""Tests for the fast-path scan: recon fan-out, attack agent, auto-dispatch."""
import pytest
from fastapi.testclient import TestClient

from facai import attack_agent, recon, venom_agent
from facai.api import create_app
from facai.quick_scan import (QuickScans, analyze_cookies, analyze_dns, analyze_exposure,
                              analyze_firewall, analyze_headers, analyze_injection_surface,
                              analyze_redirects, analyze_tech, analyze_tls, attack_targets,
                              derive_findings, grade_report)
from facai.site_assessment import SiteAssessmentError



class _StubPool:
    """Minimal pinned pool so scans run without a real proxy in tests."""

    proxy = 'http://stub:8080'

    def require(self, host, exclude=()):
        return self.proxy

    def pick(self, exclude=()):
        return self.proxy

    def report(self, proxy, ok=True):
        pass

    def is_self_host(self, host):
        return False

    def snapshot(self):
        return {'mode': 'pinned', 'exit': 'stub:8080', 'pool_size': 1, 'healthy': True,
                'stats': {}}

    def healthy(self):
        return True


@pytest.fixture(autouse=True)
def stub_egress(monkeypatch):
    """Every scan in this module runs with a pinned stub exit and a cold
    arsenal: the host may really have sqlmap installed (sec-toolkit), and a
    CONFIRMED verdict in a test must never launch a real external tool."""
    import facai.egress as eg
    from facai import arsenal as ar
    monkeypatch.setattr(eg, 'for_scan', lambda host=None: _StubPool())
    eg.reset_shared()
    registry = ar.Arsenal()
    monkeypatch.setattr(ar, 'shared', lambda: registry)
    monkeypatch.setattr('facai.quick_scan.arsenal.shared', lambda: registry)
    yield
    eg.reset_shared()
    ar.reset_shared()

# ------------------------------------------------------------------ analyzers

def test_tls_findings():
    dead = analyze_tls({'ok': False, 'error': 'SSLError: verify failed'})
    assert dead[0]['severity'] == 'HIGH' and dead[0]['tags']
    obsolete = analyze_tls({'ok': True, 'tls_version': 'TLSv1', 'obsolete_protocol': True,
                            'cipher': 'RC4-SHA', 'weak_cipher': True,
                            'seconds_remaining': 5 * 86400, 'not_after': 'x',
                            'subject_cn': 'a.example', 'san': ['b.example']})
    ids = {item['id'] for item in obsolete}
    assert {'tls-obsolete', 'tls-weak-cipher', 'cert-expiring', 'cert-san-mismatch'} <= ids
    healthy = analyze_tls({'ok': True, 'tls_version': 'TLSv1.3', 'obsolete_protocol': False,
                           'weak_cipher': False, 'seconds_remaining': 200 * 86400,
                           'subject_cn': 'a.example', 'san': ['a.example']})
    assert healthy == []


def test_header_and_cookie_findings():
    headers = {
        'HSTS': {'present': False}, 'CSP': {'present': True, 'value': "default-src 'self' 'unsafe-inline' 'unsafe-eval'"},
        'X-Content-Type-Options': {'present': True}, 'Referrer-Policy': {'present': False},
        'Permissions-Policy': {'present': False}, 'COOP': {'present': False},
        'CORP': {'present': False}, 'X-Frame-Options': {'present': False},
    }
    ids = {item['id'] for item in analyze_headers(headers)}
    assert {'hsts-missing', 'referrer-missing', 'framing-unset', 'coop-missing'} <= ids
    assert 'csp-unsafe-inline' in ids and 'csp-unsafe-eval' in ids

    cookies = analyze_cookies({'cookies': [{'name': 'PHPSESSID', 'secure': False,
                                            'httponly': False, 'samesite': None}]})
    cookie_ids = {item['id'] for item in cookies}
    assert {'cookie-no-secure', 'cookie-no-httponly', 'cookie-no-samesite'} <= cookie_ids


def test_exposure_and_injection_surface_findings():
    exposure = analyze_exposure({'git': {'path': '/.git/HEAD', 'status': 200,
                                         'excerpt': 'ref: refs/heads/main'},
                                 'env': {'path': '/.env', 'status': 404}})
    assert exposure[0]['id'] == 'git-exposed' and exposure[0]['severity'] == 'CRITICAL'
    # A CRITICAL exposure suppresses the "nothing found" info line.
    assert not any(item['id'] == 'exposure-clear' for item in exposure)
    clean = analyze_exposure({'git': {'path': '/.git/HEAD', 'status': 404},
                              'env': {'path': '/.env', 'status': 404}})
    assert any(item['id'] == 'exposure-clear' for item in clean)

    surface = analyze_injection_surface({
        'reflection': {'status': 200, 'reflected': True, 'unescaped': True,
                       'context': "'\"><{}", 'excerpt': 'echo fcq7k2p\'"><{}'},
        'sql_error': {'status': 200, 'marker': 'sql syntax', 'excerpt': 'error in your SQL syntax'},
        'admin': [{'path': '/admin', 'status': 200}],
        'upload': [{'path': '/upload', 'status': 200}]})
    ids = {item['id'] for item in surface}
    assert {'reflected-unencoded', 'sql-error', 'admin-panel', 'upload-surface'} <= ids
    # These are exactly the signals that must arm the attack stage.
    assert set(attack_targets(surface)) == {'reflected-unencoded', 'sql-error',
                                            'admin-panel', 'upload-surface'}


def test_fingerprint_and_firewall_are_inference():
    tech = analyze_tech({'detected': {'WordPress': ['wp-content'], 'PHP': ['phpsessid']},
                         'generator': 'WordPress 6.2', 'version_hints': ['6.2'],
                         'server': 'nginx/1.18.0', 'powered_by': 'PHP/7.4'})
    stack = next(item for item in tech if item['id'] == 'tech-stack')
    assert stack['certainty'] == 'INFERENCE' and stack['severity'] == 'INFO'
    assert 'CVE' in stack['recommendation'] or '版本' in stack['recommendation']
    waf = analyze_firewall({'vendors': ['Cloudflare'], 'server': 'cloudflare'})
    assert waf[0]['certainty'] == 'INFERENCE'


def test_dns_and_redirect_findings():
    dns = analyze_dns({'MX': ['10 mail.example.org'], 'TXT': ['v=spf1 -all']},
                      {'signed': False, 'nameserver': 'ns1.example.org'},
                      {'registrar': 'Example Registrar', 'expires': '2030-01-01'})
    ids = {item['id'] for item in dns}
    assert 'dnssec-off' in ids and 'dmarc-missing' in ids and 'caa-missing' in ids
    assert 'spf-missing' not in ids
    redirects = analyze_redirects([{'url': 'https://a/', 'status': 301,
                                    'location': 'http://a/home'},
                                   {'url': 'https://a/home', 'status': 200}])
    assert any(item['id'] == 'redirect-downgrade' for item in redirects)


def test_grade_scales_with_severity():
    assert grade_report([])['grade'] == 'A+'
    assert grade_report([{'severity': 'LOW'}])['grade'] == 'A'
    assert grade_report([{'severity': 'CRITICAL'}])['grade'] == 'D'
    assert grade_report([{'severity': 'CRITICAL'}] * 2)['grade'] == 'F'


# ------------------------------------------------------------- attack agent

def test_boolean_oracle_requires_reproducible_difference(monkeypatch):
    """A random token that changes every request must not be read as an oracle."""
    calls = {'n': 0}

    def fake_request(host, path, **kwargs):
        calls['n'] += 1
        return {'status': 200, 'body': f'page {calls["n"]}'.encode(), 'headers': {},
                'header_list': []}

    monkeypatch.setattr(attack_agent.net_client, 'request', fake_request)
    assert attack_agent.test_boolean_based('h', '/', ['id']) == []

    from urllib.parse import unquote_plus

    def stable_request(host, path, **kwargs):
        decoded = unquote_plus(path)
        # A real boolean oracle: every false payload carries a =2 predicate,
        # every true payload carries =1, so the split is deterministic.
        if any(token in decoded for token in ("'2", '"2', '=2')):
            body = b'no rows here at all'
        else:
            body = b'row found'
        return {'status': 200, 'body': body, 'headers': {}, 'header_list': []}

    monkeypatch.setattr(attack_agent.net_client, 'request', stable_request)
    hits = attack_agent.test_boolean_based('h', '/', ['id'])
    assert hits and hits[0]['technique'] == 'boolean'
    assert hits[0]['param'] == 'id'


def test_error_based_detects_driver_message(monkeypatch):
    def fake_request(host, path, **kwargs):
        body = b'You have an error in your SQL syntax near facai1'
        return {'status': 200, 'body': body, 'headers': {}, 'header_list': []}

    monkeypatch.setattr(attack_agent.net_client, 'request', fake_request)
    hits = attack_agent.test_error_based('h', '/', ['id'])
    assert hits and hits[0]['db_marker'] == 'sql syntax' and hits[0]['param'] == 'id'


def test_time_based_requires_real_delay(monkeypatch):
    import time as _time

    def fast_request(host, path, **kwargs):
        return {'status': 200, 'body': b'ok', 'headers': {}, 'header_list': []}

    monkeypatch.setattr(attack_agent.net_client, 'request', fast_request)
    assert attack_agent.test_time_based('h', '/', ['id']) == []

    def slow_request(host, path, **kwargs):
        if 'SLEEP' in path.upper():
            _time.sleep(attack_agent.SLEEP_SECONDS + 0.2)
        return {'status': 200, 'body': b'ok', 'headers': {}, 'header_list': []}

    monkeypatch.setattr(attack_agent.net_client, 'request', slow_request)
    hits = attack_agent.test_time_based('h', '/', ['id'])
    assert hits and hits[0]['technique'] == 'time'


def test_attack_verdict_and_param_discovery(monkeypatch):
    def root_request(host, path, **kwargs):
        body = (b'<a href="/product?id=7">x</a>'
                b'<form action="/search" method="post"><input name="q"><input name="page"></form>')
        return {'status': 200, 'body': body, 'headers': {}, 'header_list': []}

    monkeypatch.setattr(attack_agent.net_client, 'request', root_request)
    targets = attack_agent.discover_params('h')
    paths = {(item['path'], item['method']) for item in targets}
    assert ('/product', 'GET') in paths and ('/search', 'POST') in paths
    assert ['q', 'page'] in [item['params'] for item in targets]

    monkeypatch.setattr(attack_agent, 'discover_params', lambda host, pool=None: [])
    result = attack_agent.attack('h')
    assert result['verdict'] == 'NO_CONFIRMED_INJECTION'


# --------------------------------------------------------- end-to-end wiring

def _fake_recon(host):
    return {
        'tls': {'ok': True, 'tls_version': 'TLSv1.3', 'obsolete_protocol': False,
                'weak_cipher': False, 'seconds_remaining': 90 * 86400, 'not_after': 'later',
                'subject_cn': host, 'san': [host], 'cipher': 'TLS_AES_256_GCM_SHA384'},
        'security_headers': {
            'HSTS': {'present': True, 'value': 'max-age=31536000'},
            'CSP': {'present': True, 'value': "default-src 'self'; frame-ancestors 'none'"},
            'X-Content-Type-Options': {'present': True, 'value': 'nosniff'},
            'X-Frame-Options': {'present': True, 'value': 'DENY'},
            'Referrer-Policy': {'present': True, 'value': 'no-referrer'},
            'Permissions-Policy': {'present': True, 'value': 'camera=()'},
            'COOP': {'present': True, 'value': 'same-origin'},
            'CORP': {'present': True, 'value': 'same-origin'},
        },
        'cookies': {'count': 1, 'cookies': [{'name': 'sid', 'secure': True,
                                             'httponly': True, 'samesite': 'lax'}]},
        'tech_stack': {'detected': {'Nginx': ['server:nginx']}, 'generator': None,
                       'version_hints': [], 'server': 'nginx', 'powered_by': None},
        'firewall': {'vendors': [], 'server': 'nginx'},
        'path_exposure': {'git': {'path': '/.git/HEAD', 'status': 404},
                          'env': {'path': '/.env', 'status': 404},
                          'backup_sql': {'path': '/backup.sql', 'status': 404}},
        'dns': {'A': ['93.184.215.14'], 'MX': [], 'TXT': ['v=spf1 -all']},
        'dnssec': {'signed': True},
        'whois': {'registrar': 'Test Registrar', 'expires': '2031-01-01'},
        'robots': {'status': 200, 'disallow': ['/private/'], 'sitemaps': []},
        'sitemap': {'found': False},
        'security_txt': {'found': False},
        'http_methods': {'status': 200, 'allow': 'GET, HEAD', 'dangerous_advertised': []},
        'redirects': [{'url': f'https://{host}/', 'status': 200}],
        'subdomains': {'count': 2, 'sample': [host, 'www.' + host]},
        'ip_info': {'address': '93.184.215.14', 'org': 'Test'},
        'injection_surface': {
            'reflection': {'status': 200, 'reflected': False},
            'sql_error': {'status': 200, 'marker': 'sql syntax', 'excerpt': 'SQL syntax error'},
            'admin': [], 'upload': []},
        '_meta': {'elapsed_ms': 1200, 'checks': 18, 'budget_seconds': 25},
    }


def test_scan_auto_dispatches_attack_and_reports_confirmed(tmp_path, monkeypatch):
    monkeypatch.setenv('FACAI_ADMIN_PASSWORD', 'mock-admin-pass')
    monkeypatch.setenv('FACAI_SCHEDULER_ENABLED', '0')
    monkeypatch.setenv('FACAI_SKIP_CREDENTIAL_PREFLIGHT', '1')
    attacked = {'hosts': []}

    def fake_attack(host, egress_pool=None):
        attacked['hosts'].append(host)
        return {'host': host, 'verdict': 'CONFIRMED_INJECTION', 'elapsed_ms': 900,
                'targets': [{'path': '/item', 'params': ['id']}],
                'chain': [{'path': '/item', 'step': 'error-based', 'hits': 1}],
                'confirmed': [{'kind': 'SQLi', 'technique': 'error-based', 'path': '/item',
                               'param': 'id', 'db_marker': 'sql syntax',
                               'evidence': 'error in your SQL syntax'}]}

    original_init = QuickScans.__init__

    def patched_init(self, store):
        original_init(self, store, recon_runner=_fake_recon, attack_runner=fake_attack)

    monkeypatch.setattr('facai.quick_scan.QuickScans.__init__', patched_init)
    app = create_app(tmp_path)
    client = TestClient(app)

    assert client.get('/scan').status_code == 200
    result = client.post('/api/v1/quick-scans', json={'url': 'https://example.org/'})
    assert result.status_code == 200, result.text
    report = result.json()['report']
    # The SQL error signal must have fired the attack agent in the same request.
    assert attacked['hosts'] == ['example.org']
    assert 'sql-error' in report['attack_triggers']
    ids = {item['id'] for item in report['findings']}
    assert 'sqli-confirmed' in ids
    sqli = next(item for item in report['findings'] if item['id'] == 'sqli-confirmed')
    assert sqli['severity'] == 'CRITICAL' and sqli['certainty'] == 'FACT'
    assert report['attack']['verdict'] == 'CONFIRMED_INJECTION'
    assert report['summary']['grade'] == 'F'
    assert report['timing']['attack_ms'] == 900
    assert report['evidence_sha256']


def test_scan_skips_attack_without_signals(tmp_path, monkeypatch):
    monkeypatch.setenv('FACAI_ADMIN_PASSWORD', 'mock-admin-pass')
    monkeypatch.setenv('FACAI_SCHEDULER_ENABLED', '0')
    monkeypatch.setenv('FACAI_SKIP_CREDENTIAL_PREFLIGHT', '1')
    clean = _fake_recon('example.org')
    clean['injection_surface'] = {'reflection': {'status': 200, 'reflected': False},
                                  'sql_error': {'status': 200, 'marker': None},
                                  'admin': [], 'upload': []}
    called = {'n': 0}

    def fake_attack(host, egress_pool=None):
        called['n'] += 1
        return {'verdict': 'NO_CONFIRMED_INJECTION', 'confirmed': [], 'targets': []}

    original_init = QuickScans.__init__

    def patched_init(self, store):
        original_init(self, store, recon_runner=lambda host: clean, attack_runner=fake_attack)

    monkeypatch.setattr('facai.quick_scan.QuickScans.__init__', patched_init)
    app = create_app(tmp_path)
    client = TestClient(app)
    result = client.post('/api/v1/quick-scans', json={'url': 'https://clean-site.org/'})
    assert result.status_code == 200, result.text
    assert called['n'] == 0
    assert result.json()['report']['attack_skipped']
    assert result.json()['report']['attack'] is None


def test_scan_cache_and_rate_limit(tmp_path, monkeypatch):
    monkeypatch.setenv('FACAI_ADMIN_PASSWORD', 'mock-admin-pass')
    monkeypatch.setenv('FACAI_SCHEDULER_ENABLED', '0')
    monkeypatch.setenv('FACAI_SKIP_CREDENTIAL_PREFLIGHT', '1')
    original_init = QuickScans.__init__

    def patched_init(self, store):
        original_init(self, store, recon_runner=_fake_recon,
                      attack_runner=lambda host: {'verdict': 'NO_CONFIRMED_INJECTION',
                                                  'confirmed': [], 'targets': []})

    monkeypatch.setattr('facai.quick_scan.QuickScans.__init__', patched_init)
    app = create_app(tmp_path)
    client = TestClient(app)
    first = client.post('/api/v1/quick-scans', json={'url': 'https://example.org/'})
    assert first.status_code == 200
    again = client.post('/api/v1/quick-scans', json={'url': 'https://example.org/'})
    assert again.status_code == 200 and again.json()['cached'] is True
    assert client.get(f"/api/v1/quick-scans/{first.json()['id']}").status_code == 200
    assert client.get('/api/v1/quick-scans/qs_missing').status_code == 404
    other = client.post('/api/v1/quick-scans', json={'url': 'https://another.org/'})
    assert other.status_code == 429


def test_scan_rejects_unsafe_urls(tmp_path, monkeypatch):
    monkeypatch.setenv('FACAI_ADMIN_PASSWORD', 'mock-admin-pass')
    monkeypatch.setenv('FACAI_SCHEDULER_ENABLED', '0')
    monkeypatch.setenv('FACAI_SKIP_CREDENTIAL_PREFLIGHT', '1')
    original_init = QuickScans.__init__

    def patched_init(self, store):
        original_init(self, store, recon_runner=_fake_recon,
                      attack_runner=lambda host, egress_pool=None: {'confirmed': [], 'targets': []})

    monkeypatch.setattr('facai.quick_scan.QuickScans.__init__', patched_init)
    app = create_app(tmp_path)
    client = TestClient(app)
    for url in ('http://example.org', 'https://127.0.0.1/', 'https://localhost/',
                'https://example.org/path?x=1'):
        assert client.post('/api/v1/quick-scans', json={'url': url}).status_code == 400


def test_recon_fanout_survives_individual_failures(monkeypatch):
    def boom(host):
        raise RuntimeError('check exploded')

    monkeypatch.setattr(recon, 'CHECKS', (('ok', lambda host: {'fine': True}), ('boom', boom)))
    results = recon.run_all('example.org', budget_seconds=5)
    assert results['ok'] == {'fine': True}
    assert 'exploded' in results['boom']['error']
    assert results['_meta']['checks'] == 2


def test_transport_refuses_private_resolution(monkeypatch):
    import socket as _socket

    monkeypatch.setattr(recon.net_client.socket, 'getaddrinfo', lambda *a, **k: [
        (2, 1, 6, '', ('10.0.0.5', 443))])
    recon.net_client._address_cache.clear()
    with pytest.raises(recon.TransportError):
        recon.net_client.resolve_public('internal.example')


# --------------------------------------------------- VENOM (PHP agent) wiring

def test_php_target_dispatches_venom_and_merges_findings(tmp_path, monkeypatch):
    """A PHP stack arms VENOM even with zero SQLi signals, in parallel."""
    monkeypatch.setenv('FACAI_ADMIN_PASSWORD', 'mock-admin-pass')
    monkeypatch.setenv('FACAI_SCHEDULER_ENABLED', '0')
    monkeypatch.setenv('FACAI_SKIP_CREDENTIAL_PREFLIGHT', '1')
    php_recon = _fake_recon('php-site.org')
    php_recon['tech_stack'] = {'detected': {'PHP': ['x-powered-by: php']}, 'generator': None,
                               'version_hints': ['8.1'], 'server': 'nginx',
                               'powered_by': 'PHP/8.1.2'}
    php_recon['injection_surface'] = {'reflection': {'status': 200, 'reflected': False},
                                      'sql_error': {'status': 200, 'marker': None},
                                      'admin': [], 'upload': []}
    called = {'venom': [], 'attack': 0}

    def fake_venom(host, pool=None, php_hint=False):
        called['venom'].append(host)
        return {'agent': venom_agent.AGENT, 'host': host, 'verdict': 'PHP_SURFACE_FOUND',
                'elapsed_ms': 700, 'fingerprint': {'php': True, 'evidence': {}},
                'targets': [{'path': '/index.php', 'params': ['file']}],
                'chain': [{'step': 'exposure-sweep', 'hits': 1}],
                'exposures': [{'path': '/.env', 'label': 'env', 'severity': 'CRITICAL',
                               'status': 200, 'length': 120, 'evidence': 'APP_KEY=x',
                               'self_verifying': False}],
                'confirmed': []}

    def fake_attack(host, egress_pool=None):
        called['attack'] += 1
        return {'verdict': 'NO_CONFIRMED_INJECTION', 'confirmed': [], 'targets': []}

    original_init = QuickScans.__init__

    def patched_init(self, store):
        original_init(self, store, recon_runner=lambda host: php_recon,
                      attack_runner=fake_attack, venom_runner=fake_venom)

    monkeypatch.setattr('facai.quick_scan.QuickScans.__init__', patched_init)
    app = create_app(tmp_path)
    client = TestClient(app)
    result = client.post('/api/v1/quick-scans', json={'url': 'https://php-site.org/'})
    assert result.status_code == 200, result.text
    report = result.json()['report']
    assert called['venom'] == ['php-site.org']
    assert report['venom']['verdict'] == 'PHP_SURFACE_FOUND'
    ids = {item['id'] for item in report['findings']}
    assert 'php-exposure' in ids
    assert any(agent['name'] == 'VENOM' for agent in report['agents'])
    assert report['timing']['venom_ms'] == 700


def test_non_php_target_does_not_dispatch_venom(tmp_path, monkeypatch):
    monkeypatch.setenv('FACAI_ADMIN_PASSWORD', 'mock-admin-pass')
    monkeypatch.setenv('FACAI_SCHEDULER_ENABLED', '0')
    monkeypatch.setenv('FACAI_SKIP_CREDENTIAL_PREFLIGHT', '1')
    called = {'venom': 0}

    def fake_venom(host, pool=None, php_hint=False):
        called['venom'] += 1
        return {'verdict': 'NO_PHP'}

    original_init = QuickScans.__init__

    def patched_init(self, store):
        original_init(self, store, recon_runner=_fake_recon,
                      attack_runner=lambda host, egress_pool=None: {'verdict': 'NO_CONFIRMED_INJECTION',
                                                                    'confirmed': [], 'targets': []},
                      venom_runner=fake_venom)

    monkeypatch.setattr('facai.quick_scan.QuickScans.__init__', patched_init)
    app = create_app(tmp_path)
    client = TestClient(app)
    result = client.post('/api/v1/quick-scans', json={'url': 'https://example.org/'})
    assert result.status_code == 200, result.text
    assert called['venom'] == 0
    assert result.json()['report']['venom'] is None
