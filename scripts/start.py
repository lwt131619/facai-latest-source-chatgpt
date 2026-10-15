"""Loopback-only appliance launcher; keeps the existing FACAI application intact."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
import venv
import webbrowser

ROOT = Path(__file__).resolve().parents[1]


def credentials(data_dir):
    """Use explicit credentials first; create a private local operator config once."""
    if os.environ.get('FACAI_ADMIN_PASSWORD') or os.environ.get('FACAI_USERS_JSON'):
        return {}
    path = data_dir / 'local-owner.json'
    if path.exists():
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value.get('password'), str) or len(value['password']) < 16:
            raise ValueError('Invalid local-owner.json; restore it or supply FACAI_ADMIN_PASSWORD')
        if os.name != 'nt' and path.stat().st_mode & 0o077:
            raise ValueError('local-owner.json must be private: chmod 600 '+str(path))
    else:
        value = {'password': secrets.token_urlsafe(24)}
        with open(path, 'x', encoding='utf-8', opener=lambda p, f: os.open(p, f, 0o600)) as handle:
            json.dump(value, handle)
        print('First start: operator username is admin.', flush=True)
    print('Local login password is stored in: '+str(path), flush=True)
    print('Open this private file to retrieve it; do not share it.', flush=True)
    return {'FACAI_ADMIN_PASSWORD': value['password']}


def open_when_ready(url, process):
    deadline = time.monotonic() + 60
    # Never send loopback health checks through an external proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while process.poll() is None and time.monotonic() < deadline:
        try:
            with opener.open(url+'/api/v1/health', timeout=1) as response:
                if response.status == 200 and json.load(response).get('auth_required') is True:
                    webbrowser.open(url)
                    return
        except (OSError, ValueError):
            pass
        time.sleep(.25)


def main():
    parser = argparse.ArgumentParser(description='Start existing FACAI on localhost')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    if sys.version_info < (3, 10):
        parser.error('Python 3.10 or newer is required')
    environment = ROOT / '.venv'
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.exists():
        print('Preparing Python environment (first start requires Internet)...', flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    import hashlib
    digest = hashlib.sha256((ROOT/'requirements.txt').read_bytes()).hexdigest()
    marker = environment / '.facai-requirements'
    if not marker.exists() or marker.read_text() != digest:
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(ROOT/'requirements.txt')], check=True)
        marker.write_text(digest)
    data = Path(os.environ.get('FACAI_DATA_DIR', str(ROOT/'data'))).expanduser().resolve()
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = dict(os.environ)
    env.update(credentials(data))
    env['FACAI_DATA_DIR'] = str(data)
    url = 'http://127.0.0.1:'+str(args.port)
    print('FACAI: '+url+' — Ctrl+C stops the server. Model/runtime configuration is still required.', flush=True)
    process = subprocess.Popen([str(python), '-m', 'uvicorn', 'facai.api:app', '--host', '127.0.0.1', '--port', str(args.port)], cwd=ROOT, env=env)
    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(url, process), daemon=True).start()
    try:
        return process.wait()
    except KeyboardInterrupt:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return 130


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print('FACAI startup failed: '+str(exc), file=sys.stderr)
        sys.exit(1)
