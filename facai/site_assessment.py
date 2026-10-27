"""Owner-claimed, bounded HTTPS configuration assessment.

This intentionally does not probe vulnerabilities, crawl paths, or infer PHP
source defects from HTTP headers. Network I/O is pinned to a validated public
address so a DNS change cannot redirect a request into a private network.
"""
import hashlib
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
from datetime import datetime, timezone
from urllib.parse import urlsplit


class SiteAssessmentError(ValueError):
    pass


def normalize_site(url):
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').encode('idna').decode('ascii').lower()
        port = parts.port
    except (ValueError, UnicodeError) as exc:
        raise SiteAssessmentError('Invalid HTTPS domain URL') from exc
    if (parts.scheme != 'https' or not host or port not in (None, 443)
            or parts.username or parts.password or parts.path not in ('', '/')
            or parts.query or parts.fragment or len(host) > 253):
        raise SiteAssessmentError('Use an HTTPS domain root URL without credentials, port, query or fragment')
    if host == 'localhost' or '.' not in host or host.endswith(('.local', '.internal', '.localhost')):
        raise SiteAssessmentError('A public domain is required')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise SiteAssessmentError('IP literals are not accepted')
    return host


def _public_address(host):
    try:
        addresses = {ipaddress.ip_address(item[4][0]) for item in
                     socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise SiteAssessmentError('Domain resolution failed') from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise SiteAssessmentError('Domain must resolve only to public addresses')
    return str(sorted(addresses, key=lambda value: (value.version, int(value)))[0])


def _fetch(host, path, *, max_bytes=8192):
    address = _public_address(host)
    connection = http.client.HTTPSConnection(host, timeout=5, context=ssl.create_default_context())
    # HTTPSConnection keeps `host` for Host and TLS SNI/certificate validation;
    # only the TCP destination is replaced with the validated address.
    connection._create_connection = lambda _dest, timeout, source_address=None: socket.create_connection(
        (address, 443), timeout, source_address)
    try:
        connection.request('GET', path, headers={'User-Agent': 'FACAI-SiteAssessment/1.0',
                                                  'Accept': 'text/plain, text/html'})
        response = connection.getresponse()
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes and path != '/':
            raise SiteAssessmentError('Response exceeds inspection limit')
        return {'status': response.status, 'headers': dict(response.getheaders()),
                'body': body[:max_bytes], 'body_truncated': len(body) > max_bytes}
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise SiteAssessmentError('HTTPS request failed') from exc
    finally:
        connection.close()


class SiteAssessments:
    def __init__(self, store, fetch=None):
        self.store = store
        self.fetch = fetch or _fetch
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS site_assessments (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, host TEXT NOT NULL,
                challenge_hash TEXT NOT NULL, verified_at TEXT,
                report_json TEXT, created_at TEXT NOT NULL)''')

    def create(self, owner, url):
        host = normalize_site(url)
        token = secrets.token_urlsafe(32)
        claim_id = secrets.token_hex(16)
        stamp = datetime.now(timezone.utc).isoformat()
        with self.store.connect() as db:
            db.execute('INSERT INTO site_assessments VALUES (?,?,?,?,?,?,?)',
                       (claim_id, owner, host, hashlib.sha256(token.encode()).hexdigest(), None, None, stamp))
        return {'id': claim_id, 'host': host, 'challenge_path': '/.well-known/facai-site-verification.txt',
                'challenge_value': token, 'status': 'PENDING_VERIFICATION'}

    def _row(self, owner, claim_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM site_assessments WHERE id=? AND owner_id=?',
                             (claim_id, owner)).fetchone()
        if row is None:
            raise SiteAssessmentError('Site claim not found')
        return row

    def verify(self, owner, claim_id):
        row = self._row(owner, claim_id)
        response = self.fetch(row['host'], '/.well-known/facai-site-verification.txt')
        if (response['status'] != 200 or
                not secrets.compare_digest(hashlib.sha256(response['body'].strip()).hexdigest(),
                                           row['challenge_hash'])):
            raise SiteAssessmentError('Verification file does not match the claim')
        stamp = datetime.now(timezone.utc).isoformat()
        with self.store.connect() as db:
            db.execute('UPDATE site_assessments SET verified_at=? WHERE id=? AND owner_id=?',
                       (stamp, claim_id, owner))
        return {'id': claim_id, 'status': 'VERIFIED', 'verified_at': stamp}

    def assess(self, owner, claim_id):
        row = self._row(owner, claim_id)
        if not row['verified_at']:
            raise SiteAssessmentError('Site control verification is required')
        # A historical claim alone is insufficient after the site changes hands.
        challenge = self.fetch(row['host'], '/.well-known/facai-site-verification.txt')
        if (challenge['status'] != 200 or
                not secrets.compare_digest(hashlib.sha256(challenge['body'].strip()).hexdigest(),
                                           row['challenge_hash'])):
            raise SiteAssessmentError('Site control verification has expired')
        response = self.fetch(row['host'], '/')
        headers = {key.lower(): value for key, value in response['headers'].items()}
        checks = [
            ('hsts', 'Strict-Transport-Security', 'strict-transport-security'),
            ('csp', 'Content-Security-Policy', 'content-security-policy'),
            ('nosniff', 'X-Content-Type-Options', 'x-content-type-options'),
        ]
        findings = [{'rule': rule, 'header': label, 'observed': headers.get(key),
                     'result': 'PRESENT' if headers.get(key) else 'MISSING_ON_ROOT_RESPONSE',
                     'certainty': 'FACT', 'source_url': 'https://' + row['host'] + '/',
                     'recommendation': 'Review this response header configuration.'}
                    for rule, label, key in checks]
        report = {'claim_id': claim_id, 'host': row['host'], 'observed_at': datetime.now(timezone.utc).isoformat(),
                  'http_status': response['status'], 'findings': findings,
                  'scope': 'single public HTTPS root response; no crawling or vulnerability probes',
                  'php_vulnerabilities': 'UNKNOWN', 'verification': 'site control challenge passed',
                  'evidence_sha256': hashlib.sha256(json.dumps({
                      'status': response['status'], 'headers': headers},
                      sort_keys=True).encode()).hexdigest()}
        with self.store.connect() as db:
            db.execute('UPDATE site_assessments SET report_json=? WHERE id=? AND owner_id=?',
                       (json.dumps(report, ensure_ascii=False), claim_id, owner))
        return report

    def get(self, owner, claim_id):
        row = self._row(owner, claim_id)
        return {'id': claim_id, 'host': row['host'], 'status': 'VERIFIED' if row['verified_at'] else 'PENDING_VERIFICATION',
                'report': json.loads(row['report_json']) if row['report_json'] else None}
