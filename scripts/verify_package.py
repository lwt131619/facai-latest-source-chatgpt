"""Verify development ZIP hashes, safely unpack and optionally run its full tests."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', nargs='?', type=Path, default=ROOT/'dist/FACAI-v1.0-development.zip')
    parser.add_argument('--full-tests', action='store_true')
    parser.add_argument('--chromium', help='Optional installed Chromium executable for real browser tests')
    args = parser.parse_args()
    out = ROOT/'dist'; out.mkdir(exist_ok=True)
    archive = args.archive.resolve()
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = archive.with_suffix(archive.suffix+'.sha256')
    if checksum.exists() and checksum.read_text().split()[0] != sha:
        raise ValueError('Archive checksum mismatch')
    with tempfile.TemporaryDirectory(prefix='facai-package-qa-') as folder:
        destination = Path(folder)
        with zipfile.ZipFile(archive) as source:
            infos = source.infolist()
            if len(infos) > 5000 or sum(info.file_size for info in infos) > 256*1024*1024:
                raise ValueError('Archive exceeds QA extraction limits')
            names = [info.filename for info in infos]
            if len(set(names)) != len(names):
                raise ValueError('Archive contains duplicate paths')
            for info in infos:
                path = PurePosixPath(info.filename)
                mode = info.external_attr >> 16
                if (not path.parts or path.parts[0] != 'FACAI-v1.0' or path.is_absolute()
                        or '..' in path.parts or '\\' in info.filename or ':' in info.filename
                        or stat.S_ISLNK(mode)):
                    raise ValueError('Unsafe archive member')
            manifest = json.loads(source.read('FACAI-v1.0/MANIFEST.json'))
            expected_names = {'FACAI-v1.0/'+name for name in manifest['files']} | {'FACAI-v1.0/MANIFEST.json'}
            if set(names) != expected_names:
                raise ValueError('Archive members differ from the source manifest')
            for name, expected in manifest['files'].items():
                if hashlib.sha256(source.read('FACAI-v1.0/'+name)).hexdigest() != expected:
                    raise ValueError('Source file checksum mismatch: '+name)
            for info in infos:
                target = destination/info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read(info))
                if os.name != 'nt':
                    target.chmod((info.external_attr >> 16) & 0o777 or 0o644)
        package = destination/'FACAI-v1.0'
        subprocess.run([sys.executable, 'scripts/validate_handoff.py'], cwd=package, check=True)
        report = {'package_source_commit': manifest.get('source_commit'),
                  'package_files': len(manifest['files']), 'package_sha256': sha,
                  'source_tree_dirty': manifest.get('source_tree_dirty'),
                  'source_manifest_verified': True, 'full_tests_attempted': args.full_tests}
        code = 0
        if args.full_tests:
            env = dict(os.environ)
            if args.chromium:
                env['FACAI_TEST_CHROMIUM'] = args.chromium
            with open(out/'package-validation.log', 'w') as log:
                result = subprocess.run([sys.executable, '-m', 'pytest', '-q',
                                         '--junitxml='+str(out/'package-validation.xml')],
                                        cwd=package, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
            suite = next(ET.parse(out/'package-validation.xml').getroot().iter('testsuite'))
            counts = {key: int(suite.attrib[key]) for key in ('tests', 'skipped', 'failures', 'errors')}
            report.update(**counts, passed=counts['tests']-counts['skipped']-counts['failures']-counts['errors'],
                          seconds=float(suite.attrib['time']), exit_code=result.returncode,
                          real_chromium_enabled=bool(args.chromium))
            code = result.returncode
        (out/'package-validation.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps(report))
        return code


if __name__ == '__main__':
    raise SystemExit(main())
