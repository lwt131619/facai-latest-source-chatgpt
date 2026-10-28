import pytest
from fastapi.testclient import TestClient

from facai.api import create_app
from facai.quick_scan import (QuickScans, analyze_exposure, analyze_fingerprint,
                              analyze_headers, analyze_injection_surface, analyze_tls,
                              grade_report)
from facai.site_assessment import SiteAssessmentError


def test_tls_analyzer_levels():
    dead = analyze_tls({'ok': False, 'error': 'SSLError: certificate verify failed'})
    assert dead[0]['severity'] == 'HIGH' and dead[0]['certainty'] == 'FACT'
    obsolete = analyze_tls({'ok': True, 'tls_version': 'TLSv1',
                            'certificate': {'seconds_remaining': 10 * 86400, 'not_after': 'x'}})
    ids = [item['id'] for item in obsolete]
    assert 'tls-obsolete' in ids and 'cert-expiring' in ids
    expired = analyze_tls({'ok': True, 'tls_version': 'TLSv1.3',
                           'certificate': {'seconds_remaining': -5, 'not_after': 'yesterday'}})
    assert expired[0]['id'] == 'cert-expired' and expired[0]['severity'] == 'CRITICAL'
    healthy = analyze_tls({'ok': True, 'tls_version': 'TLSv1.3',
                           'certificate': {'seconds_remaining': 90 * 86400, 'not_after': 'far'}})
    assert healthy == []


def test_headers_cookies_and_grading():
    findings = analyze_headers(200, {
        'Content-Type': 'text/html', 'Server': 'nginx/1.18.0', 'X-Powered-By': 'PHP/7.4',
        'Set-Cookie': 'PHPSESSID=abc123; path=/'})
    by_id = {item['id']: item for item in findings}
    assert by_id['hsts-missing']['severity'] == 'MEDIUM'
    assert by_id['csp-missing']['severity'] == 'MEDIUM'
    assert by_id['framing-unset']['severity'] == 'MEDIUM'
    assert by_id['cookie-no-secure']['severity'] == 'HIGH'
    assert by_id['cookie-no-httponly']['severity'] == 'MEDIUM'
    assert by_id['cookie-no-samesite']['severity'] == 'LOW'
    assert by_id['server-version-exposed']['observed'] == 'nginx/1.18.0'
    hardening = analyze_headers(200, {
        'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
        'Content-Security-Policy': "default-src 'self'; frame-ancestors 'none'",
        'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
        'Referrer-Policy': 'no-referrer', 'Permissions-Policy': 'camera=()'})
    assert hardening == []
    summary = grade_report(findings)
    assert summary['severity_counts']['HIGH'] == 1 and summary['grade'] in 'CDEF'


def test_fingerprint_is_inference_only():
    body = '<html><meta name="generator" content="WordPress 6.2"><script src="/wp-includes/js/jquery.min.js">'
    findings = analyze_fingerprint({'Server': 'Apache'}, body)
    assert len(findings) == 1
    finding = findings[0]
    assert finding['certainty'] == 'INFERENCE' and finding['severity'] == 'INFO'
    names = finding['observed']['stack']
    assert 'WordPress' in names and 'jQuery' in names and 'PHP' not in names
    assert analyze_fingerprint({'Server': 'Apache'}, '<html>plain</html>') == []


def test_exposure_and_injection_probes():
    exposure = analyze_exposure({
        'git': {'path': '/.git/HEAD', 'status': 200, 'body_text': 'ref: refs/heads/main\n'},
        'env': {'path': '/.env', 'status': 404, 'body_text': ''}})
    assert exposure[0]['id'] == 'git-exposed' and exposure[0]['severity'] == 'CRITICAL'
    assert any(item['id'] == 'exposure-clear' for item in exposure)

    env_leak = analyze_exposure({
        'env': {'path': '/.env', 'status': 200, 'body_text': 'APP_KEY=base64:abc\nDB_PASSWORD=s3cret'}})
    assert env_leak[0]['id'] == 'env-exposed' and env_leak[0]['severity'] == 'CRITICAL'

    injection = analyze_injection_surface({
        'reflection': {'path': '/?facai_probe=x', 'status': 200,
                       'body_text': 'hello fcq7k2p\'"><{}{} world'},
        'sqlerror': {'path': '/?id=x', 'status': 200,
                     'body_text': 'You have an error in your SQL syntax near fcq7k2p'},
        'adminpanel': [{'path': '/admin', 'status': 200, 'body_text': 'login'},
                       {'path': '/phpmyadmin', 'status': 404, 'body_text': ''}]})
    by_id = {item['id']: item for item in injection}
    assert by_id['reflected-unencoded']['severity'] == 'HIGH'
    assert 'xss' in by_id['reflected-unencoded']['tags']
    assert by_id['sql-error-echo']['severity'] == 'HIGH'
    assert by_id['admin-panel-reachable']['observed'] == {'paths': ['/admin']}

    clean = analyze_injection_surface({
        'reflection': {'path': '/?facai_probe=x', 'status': 200,
                       'body_text': 'hello fcq7k2p&#39;&quot;&gt;&lt; world'},
        'sqlerror': {'path': '/?id=x', 'status': 200, 'body_text': 'normal page'},
        'adminpanel': [{'path': '/admin', 'status': 302, 'body_text': ''},
                       {'path': '/phpmyadmin', 'status': 404, 'body_text': ''}]})
    assert [item['id'] for item in clean] == ['reflected-encoded']


RESPONSES = {
    '/': {'status': 200, 'headers': {'Content-Type': 'text/html; charset=utf-8',
                                     'Server': 'nginx',
                                     'Set-Cookie': 'laravel_session=xyz; HttpOnly; SameSite=Lax'},
          'body': b'<html><body>ok wp-content here</body></html>'},
    '/.git/HEAD': {'status': 404, 'headers': {}, 'body': b'not found'},
    '/.env': {'status': 404, 'headers': {}, 'body': b'not found'},
    '/robots.txt': {'status': 200, 'headers': {}, 'body': b'User-agent: *\nDisallow: /admin'},
    '/?facai_probe=fcq7k2p%27%22%3E%3C%7B%7D': {'status': 200, 'headers': {},
                                                'body': b'search: fcq7k2p&#39;&quot; encoded'},
    '/?id=fcq7k2p%27': {'status': 200, 'headers': {}, 'body': b'no error here'},
    '/admin': {'status': 200, 'headers': {}, 'body': b'<form>login</form>'},
    '/phpmyadmin': {'status': 404, 'headers': {}, 'body': b''},
}


def _app_with_mocks(tmp_path, monkeypatch, fetch, tls_probe):
    monkeypatch.setenv('FACAI_ADMIN_PASSWORD', 'mock-admin-pass')
    monkeypatch.setenv('FACAI_SCHEDULER_ENABLED', '0')
    monkeypatch.setenv('FACAI_SKIP_CREDENTIAL_PREFLIGHT', '1')
    original_init = QuickScans.__init__

    def patched_init(self, store):
        original_init(self, store, fetch=fetch, tls_probe=tls_probe)

    monkeypatch.setattr('facai.quick_scan.QuickScans.__init__', patched_init)
    return create_app(tmp_path)


def test_public_scan_flow_no_login(tmp_path, monkeypatch):
    def fetch(host, path, max_bytes=8192):
        return RESPONSES[path]

    def tls_probe(host):
        return {'ok': True, 'tls_version': 'TLSv1.3',
                'certificate': {'seconds_remaining': 100 * 86400, 'not_after': 'later',
                                'self_signed': False, 'issuer': "Let's Encrypt"}}

    app = _app_with_mocks(tmp_path, monkeypatch, fetch, tls_probe)
    client = TestClient(app)

    # Page and endpoint are reachable with no session at all.
    assert client.get('/scan').status_code == 200
    assert '免登录' in client.get('/scan').text

    result = client.post('/api/v1/quick-scans', json={'url': 'https://example.org/'})
    assert result.status_code == 200, result.text
    report = result.json()['report']
    assert result.json()['cached'] is False and result.json()['id'].startswith('qs_')
    assert report['host'] == 'example.org'
    assert report['request_count'] == 9  # 8 GET probes + 1 TLS handshake
    ids = {item['id'] for item in report['findings']}
    assert 'cookie-no-secure' in ids and 'admin-panel-reachable' in ids
    assert 'tech-stack' in ids  # wp-content fingerprint from body
    assert 'reflected-encoded' in ids
    assert report['summary']['grade'] in 'BCDEF'
    assert report['evidence_sha256']

    # Same host again within the cooldown returns the cached report, not 429.
    again = client.post('/api/v1/quick-scans', json={'url': 'https://example.org/'})
    assert again.status_code == 200 and again.json()['cached'] is True
    assert again.json()['report']['evidence_sha256'] == report['evidence_sha256']

    # Persisted report is retrievable by id; an unknown id is a 404.
    fetched = client.get(f"/api/v1/quick-scans/{result.json()['id']}")
    assert fetched.status_code == 200 and fetched.json()['report']['host'] == 'example.org'
    assert client.get('/api/v1/quick-scans/qs_unknown').status_code == 404

    # Same IP, different host within the minute: blocked by the per-IP gate.
    rate = client.post('/api/v1/quick-scans', json={'url': 'https://other-site.org/'})
    assert rate.status_code == 429


def test_scan_rejects_private_and_unsafe_urls_without_login(tmp_path, monkeypatch):
    def fetch(host, path, max_bytes=8192):
        return RESPONSES.get(path, {'status': 200, 'headers': {}, 'body': b'x'})

    def tls_probe(host):
        return {'ok': True, 'tls_version': 'TLSv1.3', 'certificate': {}}

    app = _app_with_mocks(tmp_path, monkeypatch, fetch, tls_probe)
    client = TestClient(app)
    for url in ('http://example.org', 'https://127.0.0.1/', 'https://localhost/'):
        rejected = client.post('/api/v1/quick-scans', json={'url': url})
        assert rejected.status_code == 400


def test_unreachable_site_consumes_host_cooldown(tmp_path, monkeypatch):
    def fetch(host, path, max_bytes=8192):
        raise SiteAssessmentError('HTTPS request failed')

    def tls_probe(host):
        return {'ok': False, 'error': 'timeout'}

    app = _app_with_mocks(tmp_path, monkeypatch, fetch, tls_probe)
    client = TestClient(app)
    first = client.post('/api/v1/quick-scans', json={'url': 'https://down-example.org/'})
    assert first.status_code == 400
    second = client.post('/api/v1/quick-scans', json={'url': 'https://down-example.org/'})
    assert second.status_code == 429  # failed host is cooling down, not rehammered
