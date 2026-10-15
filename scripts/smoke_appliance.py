"""POSIX native launcher/restart QA in isolated temporary data (no provider calls)."""
import http.cookiejar
import importlib.util
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    if os.name == 'nt':
        raise SystemExit('This POSIX smoke check does not certify the Windows launcher')
    spec = importlib.util.spec_from_file_location('native_launcher', ROOT/'scripts/start.py')
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    # Child-only isolated installation; never inspect or reuse operator secrets.
    for key in ('FACAI_ADMIN_PASSWORD', 'FACAI_USERS_JSON', 'FACAI_LOCAL_OWNER_FILE',
                'FACAI_BOOTSTRAP_TOKEN', 'FACAI_ADMIN_USER'):
        os.environ.pop(key, None)
    out = ROOT/'dist'; out.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='facai-native-') as folder, open(out/'native-launcher-smoke.log', 'w') as log:
        data = Path(folder)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        url = 'http://127.0.0.1:'+str(port)
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar))

        def request(path, body=None, headers=None):
            payload = None if body is None else json.dumps(body).encode()
            req = urllib.request.Request(url+path, data=payload,
                                         headers={'Content-Type': 'application/json', **(headers or {})})
            with opener.open(req, timeout=3) as response:
                return json.load(response)

        def stop(process):
            if process.poll() is not None:
                return
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.wait()

        def start():
            config = launcher.credentials(data)
            env = dict(os.environ, **config, FACAI_DATA_DIR=str(data),
                       FACAI_SCHEDULER_ENABLED='0', FACAI_CHANNEL_NOTIFY_ENABLED='0')
            process = subprocess.Popen([sys.executable, str(ROOT/'scripts/start.py'), '--no-browser', '--port', str(port)],
                                       cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic()+55
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    if request('/health')['auth_required']:
                        return process, config
                except OSError:
                    time.sleep(.2)
            stop(process)
            raise RuntimeError('Native launcher did not become healthy; inspect the smoke log')

        password = secrets.token_urlsafe(24)
        process, config = start()
        try:
            result = request('/api/v1/setup/bootstrap', {'token': config['FACAI_BOOTSTRAP_TOKEN'],
                             'username': 'native-owner', 'password': password}, {'Origin': url})
            assert result['initialized']
            login = request('/api/v1/auth/login', {'username': 'native-owner', 'password': password})
            intent = request('/api/v1/intents', {'raw_request': '分析这个项目'}, {'X-CSRF-Token': login['csrf_token']})
            assert intent['status'] == 'NEED_CONTEXT'
            assert 'password' not in json.loads((data/'local-owner.json').read_text())
        finally:
            stop(process)
        process, config = start()
        try:
            assert 'FACAI_BOOTSTRAP_TOKEN' not in config
            assert request('/api/v1/setup/status')['checks']['authentication']['status'] == 'ENABLED'
            assert any(item['id'] == intent['id'] for item in request('/api/v1/intents'))
            assert not request('/api/v1/setup/bootstrap')['bootstrap_pending']
        finally:
            stop(process)
    report = {'native_launcher': 'PASS', 'owner_hash_persisted': True,
              'restart_same_session': True, 'intent_history_restored': True,
              'bootstrap_consumed': True, 'live_provider_calls': False}
    (out/'native-launcher-smoke.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
