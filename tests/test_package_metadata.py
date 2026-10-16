"""A changed release archive is not a changed source tree."""
import subprocess

from scripts.package import source_tree_dirty


def _git(root, *args):
    subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)


def test_source_dirty_excludes_tracked_dist_but_not_actual_source(tmp_path):
    _git(tmp_path, 'init', '-q')
    _git(tmp_path, 'config', 'user.name', 'Test')
    _git(tmp_path, 'config', 'user.email', 'test@example.invalid')
    (tmp_path / 'dist').mkdir()
    (tmp_path / 'dist' / 'package.zip').write_bytes(b'old')
    (tmp_path / 'source.py').write_text('ok = True\n')
    _git(tmp_path, 'add', 'dist/package.zip', 'source.py')
    _git(tmp_path, 'commit', '-qm', 'initial')
    assert source_tree_dirty(tmp_path) is False
    (tmp_path / 'dist' / 'package.zip').write_bytes(b'new')
    assert source_tree_dirty(tmp_path) is False
    (tmp_path / 'source.py').write_text('ok = False\n')
    assert source_tree_dirty(tmp_path) is True
