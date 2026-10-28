"""Arsenal: one registry for every external tool the pipeline can call.

Why a registry instead of ad-hoc calls scattered through the pipeline:
  * **One availability truth.** `snapshot()` answers "what can actually run on
    this host right now" — the report shows real capability, not aspiration.
  * **One contract per tool.** A tool declares how it is invoked, its budget,
    and whether it needs the scan's pinned exit. Callers never re-derive this.
  * **No silent degradation.** If a tool is missing, the pipeline says so in
    the report; it never pretends a step ran.

Scheduling rule (Owner directive): heavy tools are gated by a fast-path
confirmation. sqlmap runs only after `attack_agent` proved injectability, so
clean targets never pay its startup cost. Future tools register the same way
and inherit the gate by declaring `requires_confirmation`.
"""
import shutil
import subprocess
import threading
import time

from . import sqlmap_driver


class Tool:
    def __init__(self, tool_id, title, probe, invoke, *, budget_s=300,
                 requires_confirmation=True, uses_egress=True, category='attack'):
        self.id = tool_id
        self.title = title
        self.probe = probe              # () -> bool: installed and runnable
        self.invoke = invoke            # (target, **kwargs) -> dict evidence
        self.budget_s = budget_s
        self.requires_confirmation = requires_confirmation
        self.uses_egress = uses_egress
        self.category = category
        self.last_run = None
        self.total_runs = 0
        self.total_failures = 0


class Arsenal:
    """Registry + dispatcher. Thread-safe; tools register once at import."""

    def __init__(self):
        self._tools = {}
        self._lock = threading.Lock()

    def register(self, tool):
        with self._lock:
            self._tools[tool.id] = tool

    def get(self, tool_id):
        return self._tools.get(tool_id)

    def available(self, tool_id):
        tool = self._tools.get(tool_id)
        return bool(tool and tool.probe())

    def dispatch(self, tool_id, *args, **kwargs):
        """Run one tool, time it, and keep honest run statistics."""
        tool = self._tools.get(tool_id)
        if tool is None:
            return {'ok': False, 'error': f'unknown tool: {tool_id}'}
        if not tool.probe():
            return {'ok': False, 'error': f'{tool.title} is not installed on this host',
                    'hint': 'see the tool registry snapshot for installation status'}
        started = time.perf_counter()
        try:
            result = tool.invoke(*args, **kwargs)
            failures = 0 if result.get('ok', result.get('injectable') is not None) else 1
        except Exception as exc:  # noqa: BLE001 - the pipeline survives any tool
            result = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
            failures = 1
        elapsed = round(time.perf_counter() - started, 2)
        with self._lock:
            tool.last_run = {'at': time.time(), 'elapsed_s': elapsed,
                             'ok': not failures}
            tool.total_runs += 1
            tool.total_failures += failures
        if isinstance(result, dict):
            result.setdefault('tool', tool_id)
            result.setdefault('tool_title', tool.title)
            result['elapsed_s'] = elapsed
        return result

    def snapshot(self):
        """What this host can actually do right now."""
        with self._lock:
            tools = list(self._tools.values())
        return [{'id': tool.id, 'title': tool.title, 'category': tool.category,
                 'installed': bool(tool.probe()),
                 'requires_confirmation': tool.requires_confirmation,
                 'budget_s': tool.budget_s,
                 'runs': tool.total_runs, 'failures': tool.total_failures,
                 'last_run': tool.last_run}
                for tool in tools]


def _sqlmap_probe():
    return sqlmap_driver.available()


def _sqlmap_invoke(url, *, params=None, proxy=None, timeout=300, dump=False, **_):
    result = sqlmap_driver.enumerate_injection(url, params=params, proxy=proxy,
                                               timeout=timeout, dump=dump)
    evidence = sqlmap_driver.as_evidence(result)
    evidence.setdefault('ok', result.get('ok', bool(result.get('injectable'))))
    return evidence


def _nmap_probe():
    return bool(shutil.which('nmap'))


def _nmap_invoke(*args, **_):
    return {'ok': False, 'error': 'not wired: port sweeps were deliberately '
            'excluded from the fast path; register a bounded variant before use'}


_arsenal = None
_arsenal_lock = threading.Lock()


def shared():
    """Process-wide registry, populated on first use."""
    global _arsenal
    with _arsenal_lock:
        if _arsenal is None:
            registry = Arsenal()
            registry.register(Tool(
                'sqlmap', 'sqlmap 深度枚举', _sqlmap_probe, _sqlmap_invoke,
                budget_s=300, requires_confirmation=True, uses_egress=True,
                category='sqli'))
            registry.register(Tool(
                'nmap', 'nmap 端口测绘（预留）', _nmap_probe, _nmap_invoke,
                budget_s=120, requires_confirmation=False, uses_egress=False,
                category='recon'))
            _arsenal = registry
        return _arsenal


def reset_shared():
    global _arsenal
    with _arsenal_lock:
        _arsenal = None
