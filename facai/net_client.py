"""Pinned-IP, proxy-mandated HTTP transport shared by recon and the attack agent.

Two non-negotiables, both enforced here rather than at call sites:

1. **No bare connections.** Every request resolves the host locally, validates
   that *all* addresses are public, then sends through a rotated egress proxy.
   Only self-owned hosts (health checks, the console) may go direct. With an
   empty pool, `EgressError` is raised instead of leaking the local IP.

2. **Real browser fingerprints.** Cloudflare and friends reject the stdlib TLS
   stack outright. curl_cffi with `impersonate='chrome131'` is used when
   available; the stdlib path stays as a fallback for self-owned hosts, and it
   never pretends to pass a browser check.

The SSRF guard runs *before* the proxy is involved: the hostname is resolved and
validated locally, so the proxy is never asked to reach a private address.

Redirects are never followed automatically: the caller sees the 3xx and decides.
"""
import http.client
import ipaddress
import socket
import ssl
import threading
import time

from . import egress

try:  # complete Mozilla CA bundle; distro stores omit intermediates
    import certifi
except ImportError:  # pragma: no cover
    certifi = None

try:  # browser-grade TLS/JA3 fingerprint
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover
    curl_requests = None

DEFAULT_TIMEOUT = 8
DEFAULT_MAX_BYTES = 65536
IMPERSONATE = 'chrome131'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')

_address_cache = {}
_cache_lock = threading.Lock()


class TransportError(ValueError):
    pass


def default_context():
    """Verifying TLS context using the most complete CA bundle available.

    Using the platform store alone produced false 'untrusted certificate'
    findings on sites whose chain relies on intermediates the local store omits.
    """
    if certifi is not None:
        try:
            return ssl.create_default_context(cafile=certifi.where())
        except (OSError, ssl.SSLError):
            pass
    return ssl.create_default_context()


def resolve_public(host, ttl=300):
    """Resolve once, cache briefly, and refuse any private/mixed answer."""
    now = time.time()
    with _cache_lock:
        cached = _address_cache.get(host)
        if cached and now - cached[0] < ttl:
            return cached[1]
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise TransportError(f'DNS resolution failed for {host}') from exc
    addresses = []
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if not address.is_global:
            raise TransportError(f'{host} resolves to a non-public address ({address}); refusing')
        if str(address) not in addresses:
            addresses.append(str(address))
    if not addresses:
        raise TransportError(f'{host} resolved to no usable public address')
    addresses.sort(key=lambda value: (':' in value, value))
    with _cache_lock:
        _address_cache[host] = (now, addresses)
    return addresses


# --------------------------------------------------------------- proxy path

def _via_curl_cffi(host, path, proxy, *, method, headers, body, timeout, max_bytes):
    """One request through the rotated proxy with a real Chrome fingerprint."""
    url = f'https://{host}{path}'
    proxies = {'http': proxy, 'https': proxy}
    started = time.perf_counter()
    response = curl_requests.request(
        method, url, headers=headers, data=body, proxies=proxies, timeout=timeout,
        impersonate=IMPERSONATE, verify=True, allow_redirects=False)
    raw = response.content[:max_bytes]
    header_list = [(key, value) for key, value in response.headers.items()]
    return {'status': response.status_code, 'reason': '',
            'headers': dict(header_list), 'header_list': header_list,
            'body': raw, 'body_truncated': len(response.content) > max_bytes,
            'elapsed_ms': round((time.perf_counter() - started) * 1000, 1),
            'address': None, 'via': 'proxy', 'fingerprint': IMPERSONATE,
            'cert_verify_failed': None, 'url': url}


# --------------------------------------------------------------- direct path

def _via_stdlib(host, path, *, method, headers, body, timeout, max_bytes, port=443):
    """Pinned-address direct request. Self-owned hosts only."""
    addresses = resolve_public(host)
    last_error = None
    verify_failed = None
    for address in addresses:
        for unverified in ((False, True) if port == 443 else (False,)):
            if verify_failed is not None and not unverified:
                continue
            connection = None
            try:
                if port == 443:
                    ctx = default_context()
                    if unverified:
                        ctx = default_context()
                        ctx.check_hostname = False
                        ctx.verify_mode = ssl.CERT_NONE
                    connection = http.client.HTTPSConnection(host, timeout=timeout, context=ctx)
                else:
                    connection = http.client.HTTPConnection(host, timeout=timeout)
                connection._create_connection = (
                    lambda _dest, timeout, source_address=None, _addr=address, _port=port:
                    socket.create_connection((_addr, _port), timeout, source_address))
                started = time.perf_counter()
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                raw = response.read(max_bytes + 1)
                header_list = response.getheaders()
                return {'status': response.status, 'reason': response.reason,
                        'headers': dict(header_list), 'header_list': header_list,
                        'body': raw[:max_bytes], 'body_truncated': len(raw) > max_bytes,
                        'elapsed_ms': round((time.perf_counter() - started) * 1000, 1),
                        'address': address, 'via': 'direct', 'fingerprint': 'stdlib',
                        'cert_verify_failed': verify_failed,
                        'url': f'https://{host}{path}' if port == 443 else f'http://{host}{path}'}
            except ssl.SSLCertVerificationError as exc:
                verify_failed = f'{type(exc).__name__}: {exc}'
                last_error = verify_failed
                continue
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                last_error = f'{type(exc).__name__}: {exc}'
                break
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
    return {'error': last_error or 'no address reachable', 'status': 0, 'headers': {},
            'header_list': [], 'body': b'', 'cert_verify_failed': verify_failed,
            'via': 'direct', 'url': f'https://{host}{path}'}


# ------------------------------------------------------------------ facade

def request(host, path, *, method='GET', headers=None, body=None,
            timeout=DEFAULT_TIMEOUT, max_bytes=DEFAULT_MAX_BYTES, port=443,
            allow_unverified=True, egress_pool=None):
    """Send one request through a rotated exit; never leak the local IP.

    Raises `egress.EgressError` when the pool is empty and the host is not
    self-owned — the caller sees an explicit refusal instead of a silent
    direct connection.
    """
    pool = egress_pool or egress.current()
    merged = {'User-Agent': UA, 'Accept': '*/*', 'Accept-Encoding': 'identity',
              'Connection': 'close'}
    if headers:
        merged.update(headers)

    # The SSRF guard runs first, before any proxy is selected.
    resolve_public(host)
    proxy = pool.require(host)

    if proxy is None:
        return _via_stdlib(host, path, method=method, headers=merged, body=body,
                           timeout=timeout, max_bytes=max_bytes, port=port)

    if curl_requests is None:
        # A proxy exists but no browser-grade client: refuse rather than send a
        # fingerprint that Cloudflare will block anyway.
        raise TransportError('curl_cffi is required for proxied requests; '
                             'install curl_cffi or add this host to the self-owned list')

    tried = set()
    last_error = None
    while True:
        try:
            response = _via_curl_cffi(host, path, proxy, method=method, headers=merged,
                                      body=body, timeout=timeout, max_bytes=max_bytes)
        except Exception as exc:  # noqa: BLE001 - any transport failure triggers failover
            last_error = f'{type(exc).__name__}: {exc}'
            pool.report(proxy, ok=False)
            tried.add(proxy)
            proxy = pool.pick(exclude=tried)
            if proxy is None:
                return {'error': f'all exits failed: {last_error}', 'status': 0,
                        'headers': {}, 'header_list': [], 'body': b'',
                        'cert_verify_failed': None, 'via': 'proxy',
                        'url': f'https://{host}{path}'}
            continue
        # A 5xx from a proxy hop is ambiguous; anything below that proves the hop.
        pool.report(proxy, ok=response['status'] < 500)
        return response


def text_of(response, limit=200000):
    return response.get('body', b'')[:limit].decode('utf-8', 'replace')


def header(response, name):
    lowered = name.lower()
    for key, value in response.get('header_list') or []:
        if key.lower() == lowered:
            return value
    return None


def headers_multi(response, name):
    lowered = name.lower()
    return [value for key, value in (response.get('header_list') or [])
            if key.lower() == lowered]
