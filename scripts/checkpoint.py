"""Commit explicit files, create a portable Git backup and verify a normal push."""
import argparse
from datetime import datetime, timezone
import json
import hashlib
import os
from urllib.parse import quote, urlsplit
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SECRET = re.compile(rb'github_pat_[A-Za-z0-9_]{30,}|gh[pousr]_[A-Za-z0-9]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')


def git(*args, check=True):
    return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, check=check,
                          timeout=60, env=dict(os.environ, GIT_TERMINAL_PROMPT='0'))


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
    # A Git bundle contains repository history. Tracking it in the same
    # repository makes each new bundle contain the previous bundle's blob and
    # grows without bound. Keep the portable backup local; GitHub refs remain
    # the durable, verified shared handoff channel.
    tracked = git('ls-files', '--error-unmatch', 'dist/FACAI-resume.bundle', check=False)
    if tracked.returncode == 0:
        raise ValueError('Untrack dist/FACAI-resume.bundle before checkpointing to avoid recursive growth')
    git('bundle', 'create', str(bundle), '--all')
    git('bundle', 'verify', str(bundle))
    bundle_sha256 = hashlib.sha256(bundle.read_bytes()).hexdigest()
    bundle.with_suffix('.bundle.sha256').write_text(bundle_sha256+'  '+bundle.name+'\n')
    status = {'updated_at': datetime.now(timezone.utc).isoformat(), 'branch': branch,
              'head': head, 'validation': validation,
              'dirty': bool(git('status', '--porcelain').stdout),
              'bundle': bundle.name, 'bundle_verified': True, 'bundle_sha256': bundle_sha256,
              'remote_verified': False, 'push_attempted': push}
    if push:
        settings_file = ROOT/'github-backup.json'
        settings = json.loads(settings_file.read_text()) if settings_file.exists() else None
        token = os.environ.get('GH_TOKEN', '')
        if settings:
            repository = settings['repository']
            target = settings['target_branch']
            if repository not in {'lwt131619/facai-latest-source-chatgpt', 'lwt131619/facai-cloud-public'}:
                raise ValueError('Unapproved backup destination')
            if not re.fullmatch(r'[A-Za-z0-9._/-]+', target):
                raise ValueError('Invalid backup branch')
            origin = 'https://github.com/'+repository+'.git'
        else:
            origin = git('remote', 'get-url', 'origin').stdout.decode().strip()
            target = branch
        parts = urlsplit(origin)
        if (parts.scheme != 'https' or parts.hostname != 'github.com'
                or parts.path.rstrip('/') not in {'/lwt131619/facai-cloud-public.git', '/lwt131619/facai-latest-source-chatgpt.git'}):
            raise ValueError('Credentials are restricted to the configured FACAI repositories')
        state_refs = [f'refs/heads/{target}']
        if target != branch:
            state_refs.append(f'refs/heads/{branch}')
        status['repository'] = parts.path.strip('/').removesuffix('.git')
        status['target_branch'] = target
        # Official integration credentials may be injected by the environment;
        # GH_TOKEN can be a proxy placeholder. Try the working native channel
        # first instead of replacing it merely because that variable exists.
        status['credential_source'] = 'INHERITED_GIT'
        refspecs = [f'HEAD:{ref}' for ref in state_refs]
        result = git('push', origin, *refspecs, check=False)
        if result.returncode and token:
            destination = 'https://x-access-token:'+quote(token, safe='')+'@github.com'+parts.path
            result = git('push', destination, *refspecs, check=False)
            status['credential_source'] = 'GH_TOKEN_PROCESS_ONLY_FALLBACK'
        status['push_exit_code'] = result.returncode
        # An App may have already synchronized this exact commit while shell
        # write credentials expired. Verify actual refs even if push failed;
        # a failed push alone never establishes that a backup is missing.
        remote = git('ls-remote', origin, *state_refs, check=False)
        actual = {parts[1]: parts[0] for line in remote.stdout.decode().splitlines()
                  if len(parts := line.split()) == 2}
        status['remote_verified'] = remote.returncode == 0 and all(actual.get(ref) == head for ref in state_refs)
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
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print('Checkpoint failed: '+type(exc).__name__, file=sys.stderr)
        sys.exit(1)
