"""Parse the real deployment configs as an unprivileged user in official nginx."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    out = ROOT/'dist'; out.mkdir(exist_ok=True)
    config = out/'docker-smoke-config'; config.mkdir(exist_ok=True)
    env = dict(os.environ, DOCKER_CONFIG=str(config))

    def docker(*args, check=True):
        return subprocess.run(['docker', *args], env=env, text=True,
                              capture_output=True, check=check, timeout=120)

    # Explicit tracked sources only: no .git, runtime data, environment files,
    # local credentials or ignored workspaces are copied into the QA container.
    files = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    with tempfile.TemporaryDirectory(prefix='facai-nginx-') as folder:
        source = Path(folder)/'source'; source.mkdir(mode=0o755)
        source.chmod(0o755)
        for name in filter(None, files):
            original = ROOT/name
            if original.is_symlink():
                raise ValueError('Refusing source symlinks in nginx QA')
            target = source/name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
            target.chmod(0o755 if original.stat().st_mode & 0o111 else 0o644)
        # The host shell may use umask 077; copied public source directories
        # must be traversable by the container's unrelated unprivileged UID.
        for directory in (path for path in source.rglob('*') if path.is_dir()):
            directory.chmod(0o755)
        cid = docker('run', '-d', '--user', '10001:10001', '--entrypoint', '/bin/sh',
                     'nginx:stable', '-c', 'sleep 120').stdout.strip()
        try:
            docker('cp', str(source), cid+':/tmp/facai-nginx-source')
            version = docker('exec', cid, 'nginx', '-v')
            variants = []
            for domain in ('whstapp.lol', 'example.test'):
                result = docker('exec', '-e', 'FACAI_DOMAIN='+domain, cid, '/bin/bash',
                                '/tmp/facai-nginx-source/deploy/validate_config.sh', check=False)
                (out/('nginx-'+domain+'-validation.log')).write_text(result.stdout+result.stderr)
                if result.returncode:
                    raise RuntimeError('Real nginx validation failed for '+domain+'; inspect dist/nginx-*-validation.log')
                assert 'nginx -t accepts the HTTP bootstrap' in result.stdout
                assert 'nginx -t accepts the TLS config' in result.stdout
                variants.append(domain)
            report = {'status': 'PASS', 'nginx_version': (version.stdout+version.stderr).strip(),
                      'uid': 10001, 'http_and_tls_parsed': True, 'domains': variants,
                      'production_server_modified': False, 'network_requests_from_configs': False}
            (out/'nginx-validation.json').write_text(json.dumps(report, indent=2)+'\n')
            print(json.dumps(report))
        finally:
            docker('rm', '-f', cid)


if __name__ == '__main__':
    main()
