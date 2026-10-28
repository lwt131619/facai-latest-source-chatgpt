"""Pinned-IP HTTP transport shared by recon and the attack agent.

Every request resolves the host once, validates that *all* returned addresses
are public, and then pins the TCP destination to one validated address while
keeping the hostname for Host/SNI. This keeps DNS rebinding and redirect-to-
private-network tricks out of both the read-only recon path and the active
attack path.

Redirects are never followed automatically: the caller sees the 3xx and decides,
so a redirect cannot silently move the request off the validated address.
"""
import http.client
import ipaddress
import socket
import ssl
import threading
import time

try:  # A distro Python can ship a CA store without the intermediates that
    import certifi  # most sites chain to; certifi is the complete Mozilla bundle.
except ImportError:  # pragma: no cover - certifi is optional
    certifi = None

DEFAULT_TIMEOUT = 6
DEFAULT_MAX_BYTES = 32768
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
    # Prefer IPv4 for reachability, keep the rest for fallback.
    addresses.sort(key=lambda value: (':' in value, value))
    with _cache_lock:
        _address_cache[host] = (now, addresses)
    return addresses


def request(host, path, *, method='GET', headers=None, body=None,
            timeout=DEFAULT_TIMEOUT, max_bytes=DEFAULT_MAX_BYTES, port=443,
            context=None, allow_unverified=True):
    """One request against the pinned address. Returns a normalised dict.

    Certificate handling: a chain that fails verification (missing intermediate,
    unknown CA, expired leaf) is a *finding*, not a reason to lose the whole
    report. The first attempt verifies normally; on SSLCertVerificationError the
    request is retried without verification and the response is flagged
    `cert_verify_failed`, so header/cookie/exposure checks still run and the
    TLS finding carries the real reason.

    Never raises for HTTP-level failures: transport errors come back as
    {'error': ...} so a scan can report what happened instead of dying.
    """
    addresses = resolve_public(host)
    merged = {'User-Agent': UA, 'Accept': '*/*', 'Accept-Encoding': 'identity',
              'Connection': 'close'}
    if headers:
        merged.update(headers)
    last_error = None
    verify_failed = None
    for address in addresses:
        for unverified in ((False, True) if allow_unverified and port == 443 else (False,)):
            if verify_failed is not None and not unverified:
                continue
            connection = None
            try:
                if port == 443:
                    ctx = context or default_context()
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
                connection.request(method, path, body=body, headers=merged)
                response = connection.getresponse()
                raw = response.read(max_bytes + 1)
                elapsed = round((time.perf_counter() - started) * 1000, 1)
                header_list = response.getheaders()
                return {'status': response.status, 'reason': response.reason,
                        'headers': dict(header_list), 'header_list': header_list,
                        'body': raw[:max_bytes], 'body_truncated': len(raw) > max_bytes,
                        'elapsed_ms': elapsed, 'address': address,
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
    return {'error': last_error or 'no address reachable', 'status': 0,
            'headers': {}, 'header_list': [], 'body': b'',
            'cert_verify_failed': verify_failed, 'url': f'https://{host}{path}'}


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
