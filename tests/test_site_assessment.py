import pytest
from fastapi.testclient import TestClient

from facai.api import create_app
from facai.site_assessment import SiteAssessments, SiteAssessmentError, normalize_site, _public_address


@pytest.mark.parametrize('url', [
    'http://example.org', 'https://localhost', 'https://127.0.0.1',
    'https://example.org:8080', 'https://user:pass@example.org',
    'https://example.org/path', 'https://example.org/?x=1',
])
def test_claim_rejects_unsafe_or_out_of_scope_url(url):
    with pytest.raises(SiteAssessmentError):
        normalize_site(url)


def test_mixed_public_private_dns_is_rejected(monkeypatch):
    monkeypatch.setattr('facai.site_assessment.socket.getaddrinfo', lambda *args, **kwargs: [
        (2, 1, 6, '', ('93.184.215.14', 443)), (2, 1, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(SiteAssessmentError, match='public'):
        _public_address('example.org')


def test_claim_verify_assess_owner_isolation_and_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('FACAI_ADMIN_PASSWORD', 'site-admin-pass')
    monkeypatch.setenv('FACAI_SCHEDULER_ENABLED', '0')
    monkeypatch.setenv('FACAI_SKIP_CREDENTIAL_PREFLIGHT', '1')
    challenge = {'token': ''}

    def fetch(host, path):
        assert host == 'example.org'
        if path.startswith('/.well-known/'):
            return {'status': 200, 'headers': {}, 'body': challenge['token'].encode()}
        assert path == '/'
        return {'status': 200, 'headers': {'Strict-Transport-Security': 'max-age=31536000'},
                'body': b'public homepage'}

    monkeypatch.setattr('facai.site_assessment._fetch', fetch)
    app = create_app(tmp_path)
    owner = TestClient(app)
    other = TestClient(app)
    result = owner.post('/api/v1/auth/register', json={'username': 'siteowner', 'password': 'siteowner-pass'})
    assert result.status_code == 201
    owner.headers.update({'X-CSRF-Token': result.json()['csrf_token']})
    result = other.post('/api/v1/auth/register', json={'username': 'otherowner', 'password': 'otherowner-pass'})
    assert result.status_code == 201
    other.headers.update({'X-CSRF-Token': result.json()['csrf_token']})
    page = owner.get('/site-assessment')
    assert page.status_code == 200
    assert '生成验证文件' in page.text

    claim = owner.post('/api/v1/site-assessments', json={'url': 'https://example.org/'})
    assert claim.status_code == 201, claim.text
    claim_id = claim.json()['id']
    challenge['token'] = claim.json()['challenge_value']
    assert other.get(f'/api/v1/site-assessments/{claim_id}').status_code == 404
    assert owner.post(f'/api/v1/site-assessments/{claim_id}/run').status_code == 400

    verified = owner.post(f'/api/v1/site-assessments/{claim_id}/verify')
    assert verified.status_code == 200, verified.text
    report = owner.post(f'/api/v1/site-assessments/{claim_id}/run')
    assert report.status_code == 200, report.text
    assert report.json()['php_vulnerabilities'] == 'UNKNOWN'
    assert report.json()['findings'][0]['result'] == 'PRESENT'
    assert report.json()['findings'][1]['result'] == 'MISSING_ON_ROOT_RESPONSE'
    assert owner.post(f'/api/v1/site-assessments/{claim_id}/run').status_code == 429
    assert other.post(f'/api/v1/site-assessments/{claim_id}/run').status_code == 404
    reloaded = SiteAssessments(app.state.store, fetch=fetch)
    assert reloaded.get('siteowner', claim_id)['report']['evidence_sha256'] == report.json()['evidence_sha256']
