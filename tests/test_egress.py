"""Egress control tests: the no-bare-connection rule must hold in code."""
import os

import pytest

from facai import egress, net_client


def test_empty_pool_refuses_non_self_host():
    pool = egress.Egress(proxies=[])
    with pytest.raises(egress.EgressError):
        pool.require('example.org')


def test_empty_pool_allows_self_owned_host():
    pool = egress.Egress(proxies=[])
    assert pool.require('whstapp.lol') is None
    assert pool.require('www.whstapp.lol') is None
    assert pool.require('localhost') is None


def test_pool_loads_from_env(monkeypatch):
    monkeypatch.setenv('FACAI_EGRESS_PROXIES',
                       'http://u:p@a.example:10001,\nhttp://u:p@b.example:10002')
    pool = egress.Egress()
    assert len(pool.proxies) == 2
    assert pool.proxies[0].endswith('10001')


def test_pool_loads_from_file(monkeypatch, tmp_path):
    pool_file = tmp_path / 'proxies.txt'
    pool_file.write_text('# comment\nhttp://u:p@a.example:10001\nhttp://u:p@b.example:10002\n',
                         encoding='utf-8')
    monkeypatch.delenv('FACAI_EGRESS_PROXIES', raising=False)
    monkeypatch.setenv('FACAI_EGRESS_FILE', str(pool_file))
    pool = egress.Egress()
    assert len(pool.proxies) == 2
    assert all(not proxy.startswith('#') for proxy in pool.proxies)


def test_two_failures_park_an_exit_then_it_recovers():
    pool = egress.Egress(proxies=['http://a:1', 'http://b:2'], cooldown=0.01)
    first = pool.proxies[0]
    pool.report(first, ok=False)
    assert first in pool.available()          # one blip is usually the target
    pool.report(first, ok=False)
    assert first not in pool.available()      # two in a row parks it
    import time
    time.sleep(0.02)
    assert first in pool.available()          # cooldown expires


def test_pinning_holds_one_exit_for_the_whole_scan():
    pool = egress.PinnedEgress('http://u:p@pinned:10001')
    assert pool.require('example.org') == 'http://u:p@pinned:10001'
    assert pool.require('other.org') == 'http://u:p@pinned:10001'
    # Rotation across picks must not happen inside a scan.
    assert pool.pick() == pool.pick() == 'http://u:p@pinned:10001'


def test_pinned_pool_without_exit_refuses():
    pool = egress.PinnedEgress(None)
    with pytest.raises(egress.EgressError):
        pool.require('example.org')
    assert pool.require('whstapp.lol') is None


def test_for_scan_binds_a_pool_exit(monkeypatch):
    monkeypatch.setattr(egress, '_shared',
                        egress.Egress(proxies=['http://u:p@x:1', 'http://u:p@y:2']))
    pool = egress.for_scan('example.org')
    assert pool.proxy in ('http://u:p@x:1', 'http://u:p@y:2')
    assert pool.snapshot()['mode'] == 'pinned'


def test_for_scan_refuses_when_pool_empty(monkeypatch):
    monkeypatch.setattr(egress, '_shared', egress.Egress(proxies=[]))
    with pytest.raises(egress.EgressError):
        egress.for_scan('example.org')
    # Self-owned hosts still scan, so health checks and the console keep working.
    assert egress.for_scan('whstapp.lol').proxy is None


def test_thread_local_pool_isolates_scans():
    pool_a = egress.PinnedEgress('http://a:1')
    pool_b = egress.PinnedEgress('http://b:2')
    results = {}

    def worker(name, pool):
        egress.set_current(pool)
        try:
            results[name] = egress.current().proxy
        finally:
            egress.set_current(None)

    import threading
    threads = [threading.Thread(target=worker, args=('a', pool_a)),
               threading.Thread(target=worker, args=('b', pool_b))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == {'a': 'http://a:1', 'b': 'http://b:2'}


def test_request_raises_instead_of_leaking_local_ip(monkeypatch):
    """The core death-order rule: no exit means no packet leaves the box."""
    monkeypatch.setattr(egress, '_shared', egress.Egress(proxies=[]))
    egress.set_current(None)
    monkeypatch.setattr(net_client, 'resolve_public', lambda host, ttl=300: ['93.184.215.14'])
    with pytest.raises(egress.EgressError):
        net_client.request('example.org', '/')


def test_ssrf_guard_runs_before_proxy_selection(monkeypatch):
    """A private resolution must fail even with a healthy proxy configured."""
    monkeypatch.setattr(egress, '_shared', egress.Egress(proxies=['http://u:p@good:1']))
    egress.set_current(None)
    net_client._address_cache.clear()
    monkeypatch.setattr(net_client.socket, 'getaddrinfo', lambda *a, **k: [
        (2, 1, 6, '', ('10.1.2.3', 443))])
    with pytest.raises(net_client.TransportError):
        net_client.request('internal.example', '/')


def test_failed_exit_fails_over_to_the_next(monkeypatch):
    pool = egress.Egress(proxies=['http://dead:1', 'http://alive:2'])
    calls = []

    def fake_curl(host, path, proxy, **kwargs):
        calls.append(proxy)
        if 'dead' in proxy:
            raise OSError('connection refused')
        return {'status': 200, 'headers': {}, 'header_list': [], 'body': b'ok',
                'body_truncated': False, 'elapsed_ms': 1.0, 'via': 'proxy', 'url': path}

    monkeypatch.setattr(net_client, 'resolve_public', lambda host, ttl=300: ['93.184.215.14'])
    monkeypatch.setattr(net_client, '_via_curl_cffi', fake_curl)
    monkeypatch.setattr(net_client, 'curl_requests', object())  # pretend it is installed
    egress.set_current(pool)
    try:
        response = net_client.request('example.org', '/')
    finally:
        egress.set_current(None)
    assert response['status'] == 200
    # Failover happened: a dead exit was tried first, then a healthy one served.
    assert calls, 'the proxy path must actually be exercised'
    assert 'alive' in calls[-1]
