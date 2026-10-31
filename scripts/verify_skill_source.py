"""Compare a Skill file with one Git blob from an already cloned local source.

This tool reads data only. It never fetches a remote, imports a Skill, or runs
its instructions. Exit 0 means byte-identical; exit 1 means mismatch/error.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


MAX_BYTES = 100_000


def _git(repo, *args):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True,
                            timeout=15, check=False)
    if result.returncode:
        raise ValueError('Git object is unavailable in the local repository')
    return result.stdout


def verify(repo, commit, source_path, skill_file):
    if not re.fullmatch(r'[0-9a-fA-F]{40}', commit):
        raise ValueError('commit must be a full 40-character Git SHA')
    path = PurePosixPath(source_path)
    if (not source_path or path.is_absolute() or '\\' in source_path
            or ':' in source_path or any(part in ('', '.', '..') for part in source_path.split('/'))):
        raise ValueError('source_path must be a repository-relative POSIX path')
    repo = Path(repo).resolve()
    if not (repo / '.git').exists():
        raise ValueError('repo must be an existing local Git clone')
    local = Path(skill_file).read_bytes()
    if len(local) > MAX_BYTES:
        raise ValueError('Skill document exceeds the import size limit')
    object_id = commit.lower() + ':' + source_path
    size = int(_git(repo, 'cat-file', '-s', object_id).strip())
    if size > MAX_BYTES:
        raise ValueError('Upstream Skill document exceeds the import size limit')
    upstream = _git(repo, 'show', object_id)
    if len(upstream) != size:
        raise ValueError('Git blob length changed during verification')
    return {'source_commit': commit.lower(), 'source_path': source_path,
            'document_sha256': hashlib.sha256(local).hexdigest(),
            'upstream_sha256': hashlib.sha256(upstream).hexdigest(),
            'matched': local == upstream}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repo', help='existing local Git clone')
    parser.add_argument('commit', help='full 40-character Git commit SHA')
    parser.add_argument('source_path', help='path inside that commit, e.g. skills/example/SKILL.md')
    parser.add_argument('skill_file', help='local Skill document to compare')
    args = parser.parse_args()
    try:
        result = verify(args.repo, args.commit, args.source_path, args.skill_file)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'matched': False, 'error': str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result['matched'] else 1


if __name__ == '__main__':
    sys.exit(main())
