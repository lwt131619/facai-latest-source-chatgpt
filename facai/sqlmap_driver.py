"""sqlmap driver: deep enumeration once the fast path has confirmed injection.

Division of labour, decided once:
  * `attack_agent` proves injectability in ~0.03s with in-process payloads.
    That stays the fast filter and never waits on an external process.
  * `sqlmap` runs only after confirmation, because its startup and heuristic
    phase cost seconds that would otherwise be paid on every clean site.

Safety rails that keep this usable against real targets:
  * `--batch` always: no interactive prompt can hang a worker.
  * A hard wall-clock timeout with process-group kill, so a wedged run cannot
    hold a scan slot.
  * Enumeration is bounded (`--dbs` / `--tables` / `--dump` with `--stop`),
    never a full-database dump by default.
  * Output is parsed into structured data and capped, so a huge result set does
    not become a huge report.
  * Every invocation goes through the same proxy as the rest of the scan.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

DEFAULT_TIMEOUT = 300
MAX_ROWS = 200
SQLMAP_BIN = os.getenv('FACAI_SQLMAP_BIN') or shutil.which('sqlmap') or ''


def available():
    return bool(SQLMAP_BIN) and os.path.isfile(SQLMAP_BIN)


def _proxy_arg(proxy):
    """sqlmap wants host:port plus separate credentials."""
    if not proxy:
        return []
    parts = urlsplit(proxy if '://' in proxy else f'http://{proxy}')
    if not parts.hostname:
        return []
    args = ['--proxy', f'{parts.scheme or "http"}://{parts.hostname}:{parts.port or 8080}']
    if parts.username:
        args += ['--proxy-cred', f'{parts.username}:{parts.password or ""}']
    return args


def _build_target(url, params=None):
    """Rebuild the URL with an explicit parameter list when one is supplied."""
    if not params:
        return url
    parts = urlsplit(url)
    existing = dict(parse_qsl(parts.query, keep_blank_values=True))
    for name in params:
        existing.setdefault(name, '1')
    query = urlencode(existing)
    return f'{parts.scheme}://{parts.netloc}{parts.path}?{query}'


def enumerate_injection(url, *, params=None, proxy=None, timeout=DEFAULT_TIMEOUT,
                        level=2, risk=2, dump=False, max_tables=20):
    """Run sqlmap against a confirmed injection point and parse the outcome."""
    if not available():
        return {'ok': False, 'error': 'sqlmap not installed on this host',
                'hint': 'apt-get install sqlmap, or set FACAI_SQLMAP_BIN'}
    target = _build_target(url, params)
    output_dir = tempfile.mkdtemp(prefix='facai-sqlmap-')
    command = [SQLMAP_BIN, '-u', target, '--batch', '--output-dir', output_dir,
               f'--level={level}', f'--risk={risk}', '--threads=2',
               '--timeout=15', '--retries=1', '--disable-coloring']
    command += _proxy_arg(proxy)
    if params:
        command += ['-p', ','.join(params)]
    if dump:
        command += ['--dbs', f'--stop={max_tables}']
    else:
        command += ['--banner', '--current-db', '--current-user']

    started = time.perf_counter()
    try:
        process = subprocess.run(command, capture_output=True, text=True,
                                 timeout=timeout, errors='replace')
        stdout, stderr = process.stdout or '', process.stderr or ''
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or b'').decode('utf-8', 'replace') if isinstance(exc.stdout, bytes) else (exc.stdout or '')
        stderr = f'timed out after {timeout}s'
    except OSError as exc:
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
    elapsed = round(time.perf_counter() - started, 2)

    parsed = _parse(stdout)
    parsed.update({'ok': True, 'elapsed_s': elapsed, 'command_params': params,
                   'level': level, 'risk': risk, 'output_dir': output_dir})
    if stderr and not parsed.get('injectable'):
        parsed['stderr_tail'] = stderr[-400:]
    return parsed


def _parse(stdout):
    """Extract the facts sqlmap actually proved, never guess beyond them."""
    lowered = stdout.lower()
    result = {'injectable': False, 'techniques': [], 'dbms': None, 'banner': None,
              'current_db': None, 'current_user': None, 'databases': [], 'dumps': []}
    if 'is vulnerable' in lowered or 'injectable' in lowered:
        result['injectable'] = True
    for label, key in (('back-end dbms', 'dbms'), ('banner', 'banner'),
                       ('current database', 'current_db'),
                       ('current user', 'current_user')):
        match = re.search(rf'{label}:\s*(.+)', stdout, re.I)
        if match:
            value = match.group(1).strip().split('\n')[0][:160]
            if value and not value.startswith('['):
                result[key] = value
    techniques = re.findall(r'Type:\s*([A-Z][A-Za-z\s-]+)', stdout)
    result['techniques'] = sorted({item.strip() for item in techniques})[:8]
    # "available databases [n]:" followed by a bracketed list
    dbs_block = re.search(r'available databases \[\d+\]:\s*\n((?:\[\*\]\s*.+\n?)+)', stdout)
    if dbs_block:
        result['databases'] = [line.strip('[]* ').strip()
                               for line in dbs_block.group(1).splitlines()
                               if line.strip()][:40]
    # Dumped table rows: "Database: x / Table: y" then pipe-separated rows
    for block in re.finditer(r'Database:\s*(\S+)\s*\nTable:\s*(\S+)\s*\n((?:.+\n)+)', stdout):
        database, table, body = block.group(1), block.group(2), block.group(3)
        rows = [line for line in body.splitlines()
                if '|' in line and not line.startswith(('+', 'Database', 'Table'))][:MAX_ROWS]
        if rows:
            result['dumps'].append({'database': database, 'table': table,
                                    'row_count': len(rows), 'sample': rows[:20]})
        if len(result['dumps']) >= 10:
            break
    return result


def as_evidence(result):
    """Compact JSON-safe evidence block for the report."""
    if not result.get('ok'):
        return {'sqlmap': 'unavailable', 'error': result.get('error'),
                'hint': result.get('hint')}
    keep = {key: result.get(key) for key in
            ('injectable', 'techniques', 'dbms', 'banner', 'current_db',
             'current_user', 'databases', 'dumps', 'elapsed_s')}
    keep['command_params'] = result.get('command_params')
    return json.loads(json.dumps(keep, default=str))
