"""Egress control: no request leaves the host without a rotated proxy.

Owner rule (2026-10-25, standing): the local real IP is burned at the targets.
Every outbound request must carry a disguised exit; when no exit is available
the caller waits rather than connecting directly.

Design decisions, made once:
  * The pool lives in-process. A separate proxy daemon would add a port, an
    auth hop and a restart failure mode for no benefit — net_client is already
    the single choke point for all outbound traffic.
  * Rotation is per scan, not per request. Boolean and time-based injection
    compare responses across several requests; changing IP mid-comparison
    corrupts the oracle and produces false confirmations.
  * Health is tracked per exit. An exit that fails is parked for a cooldown
    instead of being retried in a loop, which is what gets pools banned.
  * The SSRF guard stays local: the target hostname is resolved and validated
    here before the proxy is asked to reach it, so the proxy is never used as
    a blind hop into a private network.
"""
import os
import random
import threading
import time

DEFAULT_COOLDOWN = 120.0
# Domains we operate ourselves: allowed direct so health checks and the
# console keep working even when the pool is empty.
SELF_HOSTS = ('whstapp.lol', 'www.whstapp.lol', 'localhost', '127.0.0.1', 'openingcode.top',
              'www.openingcode.top')


class EgressError(RuntimeError):
    """No usable exit and the host is not self-owned: refuse to send."""


def _split_proxies(raw):
    proxies = []
    for item in raw.replace('\n', ',').split(','):
        item = item.strip()
        if item and not item.startswith('#'):
            proxies.append(item)
    return proxies


def load_pool():
    """Read the pool from the environment, then from a file, in that order.

    Format is a comma or newline separated list of proxy URLs, e.g.
    http://user:pass@isp.example.com:10001
    """
    pool = _split_proxies(os.getenv('FACAI_EGRESS_PROXIES', ''))
    if pool:
        return pool
    for env_key in ('FACAI_EGRESS_FILE',):
        path = os.getenv(env_key)
        if path and os.path.isfile(path):
            with open(path, encoding='utf-8') as handle:
                return _split_proxies(handle.read())
    return []


class Egress:
    """Round-robin proxy pool with per-exit health and cooldown."""

    def __init__(self, proxies=None, cooldown=DEFAULT_COOLDOWN, allow_direct_for=SELF_HOSTS):
        self.proxies = list(proxies) if proxies is not None else load_pool()
        self.cooldown = cooldown
        self.allow_direct_for = tuple(allow_direct_for)
        self._state = {proxy: {'fails': 0, 'parked_until': 0.0, 'ok': 0} for proxy in self.proxies}
        self._lock = threading.Lock()
        self._cursor = 0
        self._stats = {'picked': 0, 'refused': 0, 'failover': 0}

    # ---------------------------------------------------------------- picking
    def available(self):
        now = time.time()
        with self._lock:
            return [proxy for proxy, state in self._state.items()
                    if state['parked_until'] <= now]

    def pick(self, exclude=()):
        """Next healthy exit, rotating so load spreads across the pool."""
        now = time.time()
        with self._lock:
            healthy = [proxy for proxy, state in self._state.items()
                       if state['parked_until'] <= now and proxy not in exclude]
            if not healthy:
                self._stats['refused'] += 1
                return None
            self._cursor = (self._cursor + 1) % len(healthy)
            self._stats['picked'] += 1
            return healthy[self._cursor]

    def report(self, proxy, ok):
        with self._lock:
            state = self._state.get(proxy)
            if state is None:
                return
            if ok:
                state['ok'] += 1
                state['fails'] = 0
            else:
                state['fails'] += 1
                if state['fails'] >= 2:
                    # Two consecutive failures is enough to park an exit; a
                    # single blip is usually the target, not the proxy.
                    state['parked_until'] = time.time() + self.cooldown
                    state['fails'] = 0
                    self._stats['failover'] += 1

    # ----------------------------------------------------------------- policy
    def is_self_host(self, host):
        host = (host or '').lower()
        return any(host == allowed or host.endswith('.' + allowed)
                   for allowed in self.allow_direct_for)

    def require(self, host, exclude=()):
        """Return the proxy to use, or None for an allowed direct call.

        Raises EgressError when the pool is empty and the host is not ours:
        that is the standing no-bare-connection rule, enforced in code.
        """
        if self.is_self_host(host):
            return None
        proxy = self.pick(exclude=exclude)
        if proxy is None:
            raise EgressError(
                f'no usable egress exit for {host}; refusing to connect directly. '
                'Set FACAI_EGRESS_PROXIES or FACAI_EGRESS_FILE.')
        return proxy

    def snapshot(self):
        now = time.time()
        with self._lock:
            return {'pool_size': len(self.proxies),
                    'healthy': sum(1 for state in self._state.values()
                                   if state['parked_until'] <= now),
                    'parked': sum(1 for state in self._state.values()
                                  if state['parked_until'] > now),
                    'stats': dict(self._stats),
                    'exits': [{'host': proxy.split('@')[-1], 'ok': state['ok'],
                               'parked': state['parked_until'] > now}
                              for proxy, state in self._state.items()][:24]}


_shared = None
_shared_lock = threading.Lock()

# Thread-local binding: recon fans out over a thread pool, and every nested
# net_client call in a worker must use the same pinned exit without threading
# a parameter through seventeen check functions.
_local = threading.local()


def set_current(pool):
    """Bind the active scan's exit to this thread."""
    _local.pool = pool


def current():
    """The scan's pinned exit for this thread, or the process pool."""
    return getattr(_local, 'pool', None) or shared()


class PinnedEgress:
    """A scan-scoped view that holds one exit for its whole lifetime.

    Injection oracles compare responses across several requests. Rotating the
    exit mid-comparison changes the edge node, the WAF state and sometimes the
    backend shard, which corrupts the differential and produces false results.
    So a scan binds one exit up front and every request in that scan uses it.
    """

    def __init__(self, proxy, parent=None):
        self.proxy = proxy
        self.parent = parent
        self.allow_direct_for = (parent or shared()).allow_direct_for
        self._state = {proxy: {'fails': 0, 'parked_until': 0.0, 'ok': 0}} if proxy else {}
        self._lock = threading.Lock()
        self._stats = {'picked': 0, 'refused': 0, 'failover': 0}

    @property
    def proxies(self):
        return [self.proxy] if self.proxy else []

    def pick(self, exclude=()):
        if self.proxy and self.proxy not in exclude:
            self._stats['picked'] += 1
            return self.proxy
        self._stats['refused'] += 1
        return None

    def report(self, proxy, ok):
        with self._lock:
            state = self._state.get(proxy)
            if state is None:
                return
            if ok:
                state['ok'] += 1
                state['fails'] = 0
            else:
                state['fails'] += 1
                if state['fails'] >= 2:
                    self._stats['failover'] += 1
                    state['fails'] = 0
                    # A pinned exit that keeps failing is reported, not swapped:
                    # the caller decides whether to abort this scan.

    def is_self_host(self, host):
        return any((host or '').lower() == allowed
                   or (host or '').lower().endswith('.' + allowed)
                   for allowed in self.allow_direct_for)

    def require(self, host, exclude=()):
        if self.is_self_host(host):
            return None
        if not self.proxy or self.proxy in exclude:
            raise EgressError(f'no usable egress exit for {host}; refusing to connect directly')
        return self.proxy

    def healthy(self):
        with self._lock:
            state = self._state.get(self.proxy) if self.proxy else None
            return bool(state) and state['fails'] < 2

    def snapshot(self):
        return {'mode': 'pinned', 'exit': (self.proxy or '').split('@')[-1] if self.proxy else None,
                'pool_size': len(self.proxies), 'healthy': self.healthy(),
                'stats': dict(self._stats)}


def for_scan(host=None):
    """Bind one exit for a whole scan, honouring the self-owned direct path."""
    pool = shared()
    if host and pool.is_self_host(host):
        return PinnedEgress(None, parent=pool)
    proxy = pool.pick()
    if proxy is None:
        raise EgressError(
            'no usable egress exit; refusing to scan. Set FACAI_EGRESS_PROXIES '
            'or FACAI_EGRESS_FILE with at least one healthy proxy.')
    return PinnedEgress(proxy, parent=pool)


def shared():
    """Process-wide pool, created on first use so config is read after startup."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = Egress()
        return _shared


def reset_shared():
    """Drop the cached pool; used by tests and after a config change."""
    global _shared
    with _shared_lock:
        _shared = None
