"""Arsenal registry tests: dispatch honesty and the confirmation gate."""
from facai import arsenal


class _Probe:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {'ok': True, 'injectable': False}


def test_snapshot_reports_installation_truth():
    registry = arsenal.Arsenal()
    registry.register(arsenal.Tool(
        'demo', 'demo 工具', lambda: False, lambda *a, **k: {},
        category='recon', requires_confirmation=False))
    snapshot = registry.snapshot()
    assert snapshot[0]['installed'] is False
    assert snapshot[0]['requires_confirmation'] is False


def test_dispatch_refuses_uninstalled_tool():
    registry = arsenal.Arsenal()
    registry.register(arsenal.Tool(
        'demo', 'demo 工具', lambda: False, lambda *a, **k: {'ok': True}))
    result = registry.dispatch('demo')
    assert result['ok'] is False and 'not installed' in result['error']


def test_dispatch_unknown_tool():
    assert arsenal.Arsenal().dispatch('nope')['ok'] is False


def test_dispatch_runs_and_tracks_stats():
    registry = arsenal.Arsenal()
    probe = _Probe()
    registry.register(arsenal.Tool('demo', 'demo 工具', lambda: True, probe))
    result = registry.dispatch('demo', 'https://h/item', params=['id'])
    assert result['ok'] is True and result['tool'] == 'demo'
    assert probe.calls == [(('https://h/item',), {'params': ['id']})]
    snapshot = registry.snapshot()[0]
    assert snapshot['runs'] == 1 and snapshot['failures'] == 0
    assert snapshot['last_run']['ok'] is True


def test_dispatch_survives_tool_exception():
    registry = arsenal.Arsenal()

    def boom(*args, **kwargs):
        raise RuntimeError('tool crashed')

    registry.register(arsenal.Tool('demo', 'demo 工具', lambda: True, boom))
    result = registry.dispatch('demo')
    assert result['ok'] is False and 'RuntimeError' in result['error']
    assert registry.snapshot()[0]['failures'] == 1


def test_default_registry_has_sqlmap_with_confirmation_gate():
    registry = arsenal.shared()
    tool = registry.get('sqlmap')
    assert tool is not None
    assert tool.requires_confirmation is True  # heavy tools wait for a confirmed injection
    snapshot = {item['id']: item for item in registry.snapshot()}
    assert 'sqlmap' in snapshot and 'nmap' in snapshot
