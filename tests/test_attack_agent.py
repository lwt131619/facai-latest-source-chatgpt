"""Unit pins for STRIKE transport-level retry semantics (no network).

Mid-chain resets (WAF, middlebox, dropped loopback connections) must cost a
bounded retry, never a technique-wide miss, and persistent failure must keep
the honest error-response semantics the oracle relies on.
"""
import facai.attack_agent as attack_agent


class _Flaky:
    def __init__(self, failures, good):
        self.failures = list(failures)
        self.good = good
        self.calls = []

    def __call__(self, host, path, **kw):
        self.calls.append((host, path))
        if self.failures:
            self.failures.pop(0)
            return {'error': 'ConnectionAbortedError: mid-stream reset',
                    'status': 0, 'body': b'', 'headers': {}}
        return dict(self.good)


GOOD = {'error': None, 'status': 200, 'body': b'<html>ok</html>', 'headers': {}}


def test_transient_transport_failure_is_retried(monkeypatch):
    flaky = _Flaky([1], GOOD)
    monkeypatch.setattr(attack_agent.net_client, 'request', flaky)
    response = attack_agent._probe('vuln.test', '/item', {'id': '1'})
    assert response['status'] == 200
    assert len(flaky.calls) == 2


def test_persistent_failure_returns_last_error_after_bounded_attempts(monkeypatch):
    flaky = _Flaky([1, 1, 1, 1], GOOD)
    monkeypatch.setattr(attack_agent.net_client, 'request', flaky)
    response = attack_agent._probe('vuln.test', '/item', {'id': '1'})
    assert response['error']
    assert response['status'] == 0
    assert len(flaky.calls) == 3  # bounded: never an infinite retry loop


def test_discover_params_survives_a_dropped_root_fetch(monkeypatch):
    good_root = dict(GOOD, body=b'<a href="/item?id=42">x</a>')
    flaky = _Flaky([1], good_root)
    monkeypatch.setattr(attack_agent.net_client, 'request', flaky)
    found = attack_agent.discover_params('vuln.test')
    assert found and found[0]['path'] == '/item'
    assert found[0]['params'] == ['id']
