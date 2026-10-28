"""VENOM 单测：全部打桩 net_client，绝不真连网。

覆盖的核心是证据契约：
  * 只有能自证的内容才算确认（etc/passwd 行、解码后的 <?php、执行标记回显）
  * 只回 200 不算证据
  * 非 PHP 目标直接 NO_PHP，不烧后续探针
  * 发现合并进报告后 severity/certainty 正确
"""
import base64
import json

import pytest

from facai import venom_agent


class FakeResponse(dict):
    pass


def _resp(status=200, text='', body=None, headers=None):
    raw = body if body is not None else text.encode()
    return {'status': status, 'headers': headers or {}, 'header_list': list((headers or {}).items()),
            'body': raw, 'error': None, 'url': 'https://t/'}


def _fail(message='boom'):
    return {'status': 0, 'headers': {}, 'body': b'', 'error': message, 'url': 'https://t/'}


@pytest.fixture
def routes(monkeypatch):
    """Install a route table: path -> response (or callable)."""
    table = {}

    def fake_request(host, path, *, method='GET', headers=None, body=None, timeout=8,
                     max_bytes=65536, port=443, allow_unverified=True, egress_pool=None):
        key = f'{method} {path}'
        for candidate in (key, path, key.split('?')[0], path.split('?')[0]):
            if candidate in table:
                value = table[candidate]
                return value() if callable(value) else value
        return _resp(404, 'not found')

    monkeypatch.setattr(venom_agent.net_client, 'request', fake_request)
    return table


def test_fingerprint_detects_php_from_powered_by(routes):
    routes['/'] = _resp(200, '<html><a href="/a.php">x</a></html>',
                        headers={'X-Powered-By': 'PHP/8.1.2', 'Server': 'nginx'})
    routes['/index.php'] = _resp(200, 'home')
    result = venom_agent.fingerprint('t')
    assert result['php'] is True
    assert result['evidence']['version'] == '8.1.2'
    assert any('X-Powered-By' in item for item in result['evidence']['signals'])


def test_fingerprint_rejects_non_php(routes):
    routes['/'] = _resp(200, '<html>static</html>', headers={'Server': 'Vercel'})
    routes['/index.php'] = _resp(404, 'nope')
    result = venom_agent.fingerprint('t')
    assert result['php'] is False


def test_non_php_target_short_circuits(routes):
    routes['/'] = _resp(200, '<html>static</html>', headers={'Server': 'Vercel'})
    routes['/index.php'] = _resp(404)
    result = venom_agent.attack('t')
    assert result['verdict'] == 'NO_PHP'
    assert result['exposures'] == []


def test_exposure_requires_self_verifying_content(routes):
    # /index.php~ returns 200 but without <?php -- that is not an editor backup.
    routes['/'] = _resp(200, 'php app', headers={'X-Powered-By': 'PHP/8.0'})
    routes['/index.php'] = _resp(200, 'ok')
    routes['/index.php~'] = _resp(200, 'welcome to my site')
    routes['/.env'] = _resp(200, 'APP_KEY=base64:xyz\nDB_HOST=localhost\nDB_PASSWORD=hunter2')
    routes['/phpinfo.php'] = _resp(200, 'PHP Version 8.0.1 php.ini loaded')
    sweep = venom_agent.sweep_exposures('t')
    labels = {item['label'] for item in sweep['hits']}
    assert 'env' in labels
    assert 'phpinfo' in labels
    assert 'editor-backup' not in labels


def test_lfi_reads_passwd_only_on_real_marker(routes):
    routes['/'] = _resp(200, 'php', headers={'X-Powered-By': 'PHP/7.4'})
    routes['/index.php'] = _resp(200, 'home')
    routes['/page.php?id=1'] = _resp(200, 'normal page')

    def lfi(host, path, params, pool=None):
        return [{'param': 'file', 'path': path, 'payload': '../../etc/passwd',
                 'target_file': 'etc/passwd', 'technique': 'lfi-traversal',
                 'status': 200, 'evidence': 'root:x:0:0:root:/root:/bin/bash'}]

    # Direct unit: a 200 without the marker proves nothing.
    assert venom_agent.test_lfi('t', '/page.php', ['id'], pool=None) == []
    routes['/page.php?file=..%2F..%2F..%2F..%2F..%2F..%2Fetc%2Fpasswd'] = _resp(
        200, 'root:x:0:0:root:/root:/bin/bash\ndaemon:x:1:1:')
    hits = venom_agent.test_lfi('t', '/page.php', ['file'], pool=None)
    assert hits and hits[0]['target_file'] == 'etc/passwd'
    assert 'root:x:0:0' in hits[0]['evidence']


def test_filter_chain_requires_decoded_php_open_tag(routes):
    plain = base64.b64encode(b'just some text, no code here').decode()
    routes['/v.php?page=php%3A%2F%2Ffilter%2Fconvert.base64-encode%2Fresource%3Dindex'] = _resp(
        200, plain)
    assert venom_agent.test_filter_chain('t', '/v.php', ['page'], pool=None) is None
    source = base64.b64encode(b'<?php echo "hi"; ?>' + b'A' * 120).decode()
    routes['/v.php?page=php%3A%2F%2Ffilter%2Fconvert.base64-encode%2Fresource%3Dindex'] = _resp(
        200, source)
    hit = venom_agent.test_filter_chain('t', '/v.php', ['page'], pool=None)
    assert hit and hit['technique'] == 'php-filter-chain'
    assert hit['evidence'].startswith('<?php')


def test_phpunit_rce_requires_marker_echo(routes):
    path = '/vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php'
    routes['POST ' + path] = _resp(200, 'silence')
    assert venom_agent.test_phpunit_rce('t') is None
    routes['POST ' + path] = _resp(200, venom_agent.EXEC_MARKER)
    hit = venom_agent.test_phpunit_rce('t')
    assert hit and hit['marker'] == venom_agent.EXEC_MARKER


def test_wp_user_enum_requires_json_users(routes):
    routes['/wp-json/wp/v2/users?per_page=5'] = _resp(200, json.dumps(
        [{'id': 1, 'slug': 'admin', 'name': 'Admin'}]))
    hit = venom_agent.test_wp_users('t')
    assert hit and hit['users'][0]['slug'] == 'admin'
    routes['/wp-json/wp/v2/users?per_page=5'] = _resp(200, '{"code":"rest_no_route"}')
    assert venom_agent.test_wp_users('t') is None


def test_attack_verdict_priority_rce_over_lfi(routes):
    routes['/'] = _resp(200, 'php', headers={'X-Powered-By': 'PHP/8.2'})
    routes['/index.php'] = _resp(200, 'home')
    routes['POST /vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php'] = _resp(
        200, venom_agent.EXEC_MARKER)
    result = venom_agent.attack('t')
    assert result['verdict'] == 'CONFIRMED_PHP_RCE'
    assert result['agent']['name'] == 'VENOM'


def test_attack_budget_and_no_evidence(routes):
    routes['/'] = _resp(200, 'php', headers={'X-Powered-By': 'PHP/8.2'})
    routes['/index.php'] = _resp(200, 'home')
    result = venom_agent.attack('t')
    assert result['verdict'] == 'NO_CONFIRMED_PHP'
    assert result['fingerprint']['php'] is True


def test_agent_identity_is_stable():
    assert venom_agent.AGENT['id'] == 'venom'
    assert venom_agent.AGENT['name'] == 'VENOM'
    assert 'PHP' in venom_agent.AGENT['title']


def test_php_hint_overrides_strict_fingerprint(routes):
    """Hardened WordPress hides PHP headers; recon's stack evidence must win."""
    routes['/'] = _resp(200, '<html>wp</html>', headers={'Server': 'nginx'})
    routes['/index.php'] = _resp(404)
    assert venom_agent.attack('t')['verdict'] == 'NO_PHP'
    result = venom_agent.attack('t', php_hint=True)
    assert result['fingerprint']['php'] is True
    assert result['fingerprint']['evidence']['hint'] == 'recon: PHP 栈指纹'
    assert result['verdict'] != 'NO_PHP'
    assert result['chain'][0]['step'] == 'php-hint'


def test_form_action_is_normalized_to_a_path(routes):
    """An absolute form action must not become https://host/https://host/..."""
    routes['/'] = _resp(200, '<form action="https://t/submit.php" method="post">'
                            '<input name="q"></form>')
    routes['/index.php'] = _resp(404)
    targets = venom_agent.discover_targets('t')
    assert targets, targets
    assert all(item['path'].startswith('/') for item in targets), targets
    assert any(item['path'] == '/submit.php' for item in targets), targets
