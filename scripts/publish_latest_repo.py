"""Create the requested private latest-source repository and verify a normal push."""
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
OWNER = 'lwt131619'
NAME = 'facai-latest-source'
REPO = OWNER+'/'+NAME
URL = 'https://github.com/'+REPO+'.git'


def command(args, input=None):
    return subprocess.run(args, cwd=ROOT, input=input, text=True, capture_output=True,
                          timeout=60, env=dict(os.environ, GIT_TERMINAL_PROMPT='0'))


def main():
    head = command(['git', 'rev-parse', 'HEAD']).stdout.strip()
    state = {'repository': REPO, 'local_head': head, 'created': False,
             'push_attempted': False, 'remote_verified': False}
    payload = json.dumps({'name': NAME, 'description': '最新 FACAI 源代码',
                          'private': True, 'auto_init': False})
    result = command(['gh', 'api', '--method', 'POST', 'user/repos', '--input', '-'], payload)
    if result.returncode != 0:
        # An existing repository can be used only when it is the same private
        # destination. Do not change visibility or overwrite its history.
        existing = command(['gh', 'api', 'repos/'+REPO]) if '422' in result.stderr else None
        if existing is None or existing.returncode != 0:
            state['status'] = 'CREATE_FORBIDDEN' if 'Forbidden' in result.stderr or '403' in result.stderr else 'CREATE_FAILED'
            return save(state)
        result = existing
    else:
        state['created'] = True
    repo = json.loads(result.stdout)
    if repo.get('full_name') != REPO or repo.get('private') is not True:
        state['status'] = 'DESTINATION_REQUIRES_REVIEW'
        return save(state)
    destination = URL
    token = os.environ.get('GH_TOKEN', '')
    if token:
        destination = 'https://x-access-token:'+quote(token, safe='')+'@github.com/'+REPO+'.git'
    state['push_attempted'] = True
    result = command(['git', 'push', destination, 'HEAD:refs/heads/main'])
    if result.returncode == 0:
        verified = command(['git', 'ls-remote', URL, 'refs/heads/main'])
        state['remote_verified'] = verified.returncode == 0 and verified.stdout.split()[:1] == [head]
    state['status'] = 'SYNCED' if state['remote_verified'] else 'PUSH_FAILED'
    return save(state)


def save(state):
    out = ROOT/'dist'; out.mkdir(exist_ok=True)
    (out/'latest-repo-upload-status.json').write_text(json.dumps(state, indent=2)+'\n')
    print(json.dumps(state, indent=2))
    return 0 if state['remote_verified'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
