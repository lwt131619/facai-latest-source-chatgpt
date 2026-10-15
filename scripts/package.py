"""Build a checksummed development appliance from an explicit source allowlist."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ('AGENTS.md', 'AGENT_HANDOFF.md', 'pytest.ini', 'start-opencode.ps1', 'FACAI_CODEX_HANDOFF.md', 'README.md', 'requirements.txt', 'start.sh', 'start.bat', 'Dockerfile',
              'docker-compose.yml', '.env.example', '.dockerignore',
              'FACAI_CURRENT_STATE_REPORT.md', 'ARCHITECTURE_MAP.md',
              'MODULE_INVENTORY.md', 'DEVELOPMENT_PLAN.md')
SECRET = re.compile(rb'github_pat_[A-Za-z0-9_]{30,}|gh[pousr]_[A-Za-z0-9]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')


def build():
    files = {ROOT / name for name in ROOT_FILES}
    files.update((ROOT/'facai').rglob('*.py'))
    files.update(p for p in (ROOT/'facai/static').rglob('*') if p.is_file())
    files.update((ROOT/'scripts').glob('*.py'))
    files.update((ROOT/'tests').rglob('*.py'))
    files.update(p for p in (ROOT/'vendor').rglob('*') if p.is_file() and p.suffix in {'.md', '.json'})
    files.update(p for p in (ROOT/'docs').rglob('*') if p.is_file() and p.suffix in {'.md', '.json'})
    files.update(p for p in (ROOT/'packaging/app').glob('*') if p.is_file() and p.suffix in {'.js', '.json', '.md', '.ps1', '.png'})
    files.update((ROOT/'.github/workflows').glob('*.yml'))
    files.add(ROOT/'docs/LOCAL_APPLIANCE.md')
    files.add(ROOT/'docs/BUILD_VALIDATION_2026-10-15.md')
    files.add(ROOT/'docs/DEPLOYMENT.md')
    files.update(p for p in (ROOT/'deploy').rglob('*') if p.is_file() and (p.suffix in {'.sh', '.ps1', '.conf', '.service', '.timer', '.md', '.yaml'} or p.name.endswith('.env.example')))
    contents = {}
    for file in sorted(files):
        if not file.is_file() or file.is_symlink():
            raise ValueError('Required source is missing or is a symlink: '+str(file.relative_to(ROOT)))
        data = file.read_bytes()
        if SECRET.search(data):
            raise ValueError('Credential-shaped content blocks packaging: '+str(file.relative_to(ROOT)))
        contents[file.relative_to(ROOT).as_posix()] = data
    revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True)
    dirty = subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT, capture_output=True)
    manifest = {'status': 'DEVELOPMENT_NOT_PRODUCTION',
                'source_commit': revision.stdout.decode().strip() if revision.returncode == 0 else None,
                'source_tree_dirty': bool(dirty.stdout) if dirty.returncode == 0 else None,
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}}
    out = ROOT/'dist'; out.mkdir(exist_ok=True)
    package = out/'FACAI-v1.0-development.zip'
    with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in contents.items():
            info = zipfile.ZipInfo('FACAI-v1.0/'+name)
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if name.endswith('.sh') else 0o644
            info.external_attr = (0o100000 | mode) << 16
            archive.writestr(info, data)
        archive.writestr('FACAI-v1.0/MANIFEST.json', json.dumps(manifest, indent=2))
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    package.with_suffix('.zip.sha256').write_text(digest+'  '+package.name+'\n')
    with zipfile.ZipFile(package) as archive:
        for name, expected in manifest['files'].items():
            assert hashlib.sha256(archive.read('FACAI-v1.0/'+name)).hexdigest() == expected
    print(f'{package}: {len(contents)} files; archive and manifest hashes verified')


if __name__ == '__main__':
    build()
