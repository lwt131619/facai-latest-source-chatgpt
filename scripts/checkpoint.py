"""Commit explicit files, create a portable Git backup and verify a normal push."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SECRET = re.compile(rb'github_pat_[A-Za-z0-9_]{30,}|gh[pousr]_[A-Za-z0-9]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')


def git(*args, check=True):
    return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, check=check)


def checkpoint(message=None, paths=(), validation='not_run', push=True):
    branch = git('branch', '--show-current').stdout.decode().strip()
    if not branch or branch in {'main', 'master'}:
        raise ValueError('Use a named development branch; checkpoint never rewrites main/master')
    if message:
        if not paths:
            raise ValueError('Explicit --paths are required when committing')
        # Scan individual source bytes before staging and the complete staged patch.
        for name in paths:
            file = (ROOT/name).resolve()
            if not file.is_relative_to(ROOT) or not file.is_file():
                raise ValueError('Only explicit repository files may be checkpointed')
            if SECRET.search(file.read_bytes()):
                raise ValueError('Credential-shaped content blocks checkpoint')
        git('add', '--', *paths)
        patch = git('diff', '--cached', '--binary').stdout
        if SECRET.search(patch):
            raise ValueError('Credential-shaped content blocks commit; inspect staged files')
        if patch:
            git('-c', 'user.name=Codex', '-c', 'user.email=codex@users.noreply.github.com',
                'commit', '-m', message)
    head = git('rev-parse', 'HEAD').stdout.decode().strip()
    out = ROOT/'dist'; out.mkdir(exist_ok=True)
    bundle = out/'FACAI-resume.bundle'
    git('bundle', 'create', str(bundle), '--all')
    git('bundle', 'verify', str(bundle))
    status = {'updated_at': datetime.now(timezone.utc).isoformat(), 'branch': branch,
              'head': head, 'validation': validation,
              'dirty': bool(git('status', '--porcelain').stdout),
              'bundle': bundle.name, 'bundle_verified': True,
              'remote_verified': False, 'push_attempted': push}
    if push:
        result = git('push', 'origin', f'HEAD:refs/heads/{branch}', check=False)
        status['push_exit_code'] = result.returncode
        if result.returncode == 0:
            remote = git('ls-remote', 'origin', f'refs/heads/{branch}', check=False)
            status['remote_verified'] = remote.returncode == 0 and remote.stdout.decode().split()[:1] == [head]
        # Never persist or echo raw Git stderr, which may contain credential URLs.
        status['sync_status'] = 'SYNCED' if status['remote_verified'] else 'PUSH_FAILED'
    else:
        status['sync_status'] = 'LOCAL_ONLY'
    (out/'checkpoint-status.json').write_text(json.dumps(status, indent=2)+'\n')
    (ROOT/'.git/facai-checkpoint.json').write_text(json.dumps(status, indent=2)+'\n')
    print(json.dumps(status, indent=2))
    return 0 if status['remote_verified'] or not push else 2


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--message')
    parser.add_argument('--paths', nargs='*', default=[])
    parser.add_argument('--validation', choices=['pass', 'partial', 'fail', 'not_run'], default='not_run')
    parser.add_argument('--local-only', action='store_true')
    args = parser.parse_args()
    try:
        sys.exit(checkpoint(args.message, args.paths, args.validation, not args.local_only))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print('Checkpoint failed: '+type(exc).__name__, file=sys.stderr)
        sys.exit(1)
