"""Daily timer backups must be unique, verified and safely retained."""

import pytest

import facai.backup as backup_module
from facai.ops.backup_db import backup
from facai.store import Store


def test_daily_backup_same_second_is_unique_and_retention_follows_validation(tmp_path):
    store = Store(tmp_path)
    store.create('保留任务', '原始要求', ['执行'])
    first, pruned = backup(tmp_path, keep=2)
    assert pruned == 0 and backup_module.verify_backup(first)['ok']
    second, pruned = backup(tmp_path, keep=2)
    assert first != second and pruned == 0
    third, pruned = backup(tmp_path, keep=2)
    assert pruned == 1 and third.exists() and second.exists() and not first.exists()
    assert backup_module.verify_backup(third)['missions'] == 1


def test_failed_daily_backup_does_not_prune_previous_snapshots(tmp_path, monkeypatch):
    Store(tmp_path).create('保留任务', '原始要求', ['执行'])
    good, _ = backup(tmp_path, keep=1)
    monkeypatch.setattr(backup_module, 'verify_backup', lambda path: {'ok': False})
    with pytest.raises(ValueError, match='integrity verification'):
        backup(tmp_path, keep=1)
    assert list((tmp_path / 'backups').glob('db-daily-*.sqlite3')) == [good]


@pytest.mark.parametrize('keep', [0, -1, True, 1.5])
def test_daily_backup_rejects_invalid_retention(tmp_path, keep):
    with pytest.raises(ValueError, match='positive integer'):
        backup(tmp_path, keep=keep)
    assert not (tmp_path / 'backups').exists()
