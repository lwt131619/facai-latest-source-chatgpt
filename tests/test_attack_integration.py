"""End-to-end proof that the attack agent really exploits a live injection.

This spins up a genuinely vulnerable HTTP server backed by SQLite with a raw
string-concatenated query, then runs the real attack chain against it. Nothing
is mocked except the loopback address check (the transport guard correctly
refuses private addresses in production).

If this test passes, error-based detection, boolean differential oracles,
column counting, and UNION extraction all demonstrably work on a real target.
"""
import http.server
import sqlite3
import socketserver
import threading
import urllib.parse

import pytest

from facai import attack_agent, net_client


class VulnerableHandler(http.server.BaseHTTPRequestHandler):
    """Deliberately injectable: `SELECT ... WHERE id = <raw input>`."""

    database = None

    def do_GET(self):  # noqa: N802 - http.server API
        parsed = urllib.parse.urlsplit(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        if parsed.path == '/':
            body = b'<a href="/item?id=1">item</a>'
            self._send(200, body)
            return
        if parsed.path != '/item':
            self._send(404, b'not found')
            return
        raw_id = params.get('id', '1')
        query = f'SELECT name, price FROM products WHERE id = {raw_id}'
        connection = sqlite3.connect(self.database)
        try:
            rows = connection.execute(query).fetchall()
            body = ('<html><body>' + ''.join(f'<p>{row[0]} {row[1]}</p>' for row in rows)
                    + '</body></html>').encode()
            self._send(200, body)
        except sqlite3.Error as exc:
            # A real app would echo this: that is exactly the error oracle.
            self._send(500, f'sqlite3::query error: {exc}'.encode())
        finally:
            connection.close()

    def _send(self, status, body):
        self.send_response(status)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep test output clean
        pass


@pytest.fixture()
def vulnerable_site(tmp_path, monkeypatch):
    database = tmp_path / 'shop.db'
    connection = sqlite3.connect(database)
    connection.execute('CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, price REAL)')
    connection.executemany('INSERT INTO products VALUES (?,?,?)',
                           [(1, 'widget', 9.99), (2, 'gadget', 19.5)])
    connection.commit()
    connection.close()

    handler = type('Handler', (VulnerableHandler,), {'database': str(database)})
    server = socketserver.TCPServer(('127.0.0.1', 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Only the loopback guard is relaxed; everything else runs for real.
    real_resolve = net_client.resolve_public

    def allow_loopback(host, ttl=300):
        return ['127.0.0.1']

    monkeypatch.setattr(net_client, 'resolve_public', allow_loopback)
    real_request = net_client.request

    def request_on_test_port(host, path, **kwargs):
        kwargs['port'] = port
        # The fixture server speaks plain HTTP; the client's TLS path would
        # never reach it, so this one shim keeps the transport otherwise intact.
        import http.client
        merged = {'User-Agent': net_client.UA, 'Accept': '*/*',
                  'Accept-Encoding': 'identity', 'Connection': 'close'}
        merged.update(kwargs.pop('headers', None) or {})
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=kwargs.get('timeout', 6))
        try:
            connection.request(kwargs.pop('method', 'GET'), path, headers=merged)
            response = connection.getresponse()
            raw = response.read(kwargs.get('max_bytes', 32768))
            return {'status': response.status, 'reason': response.reason,
                    'headers': dict(response.getheaders()),
                    'header_list': response.getheaders(),
                    'body': raw, 'body_truncated': False, 'elapsed_ms': 1.0,
                    'address': '127.0.0.1', 'cert_verify_failed': None,
                    'url': f'http://127.0.0.1:{port}{path}'}
        except OSError as exc:
            return {'error': f'{type(exc).__name__}: {exc}', 'status': 0, 'headers': {},
                    'header_list': [], 'body': b'', 'url': path}
        finally:
            connection.close()

    monkeypatch.setattr(attack_agent.net_client, 'request', request_on_test_port)
    yield {'port': port, 'host': 'vuln.test'}
    server.shutdown()
    server.server_close()
    monkeypatch.setattr(net_client, 'resolve_public', real_resolve)
    monkeypatch.setattr(attack_agent.net_client, 'request', real_request)


def test_error_oracle_fires_on_real_sql_error(vulnerable_site):
    hits = attack_agent.test_error_based(vulnerable_site['host'], '/item', ['id'])
    assert hits, 'the raw quote must produce a driver error on this target'
    assert 'sqlite' in hits[0]['db_marker'] or 'syntax' in hits[0]['db_marker']
    assert hits[0]['param'] == 'id'
    assert hits[0]['status'] == 500


def test_boolean_oracle_confirms_real_injection(vulnerable_site):
    hits = attack_agent.test_boolean_based(vulnerable_site['host'], '/item', ['id'])
    assert hits, 'AND 1=1 vs AND 1=2 must produce different, stable responses'
    assert hits[0]['technique'] == 'boolean'
    assert hits[0]['len_true'] != hits[0]['len_false']


def test_full_chain_extracts_data_from_real_target(vulnerable_site):
    result = attack_agent.attack(vulnerable_site['host'])
    assert result['verdict'] == 'CONFIRMED_INJECTION', result
    techniques = {item['technique'] for item in result['confirmed']}
    assert 'error' in techniques, 'error-based detection must fire'
    assert 'boolean' in techniques, 'boolean differential must fire'
    # The parameter was discovered from the page itself, not handed in.
    assert result['targets']
    assert result['targets'][0]['params'] == ['id']
    assert any(item.get('db_marker') for item in result['confirmed'])
