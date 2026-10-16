"""Backup, verify and restore must round-trip real data and stay secret-free."""

import sqlite3
from pathlib import Path

import pytest

import facai.backup as backup_module
from facai.backup import create_backup, drill_backup, restore_backup, verify_backup
from facai.store import Store


def seed(tmp_path):
    store = Store(tmp_path)
    mission = store.create('关键任务', '原始要求', ['开发', '测试'])
    store.bind(mission['id'], 'opencode', 'deepseek-flash')
    store.registry_register('models', 'persisted', {'provider': 'local', 'api_key_env': 'LOCAL_KEY'})
    store.append(mission['id'], 'NOTE', {'text': 'hello'})
    return store, mission['id']


def test_backup_verify_restore_round_trip(tmp_path):
    store, mission_id = seed(tmp_path)
    backup = create_backup(tmp_path)

    report = verify_backup(backup)
    assert report['ok'] is True and report['integrity'] == 'ok'
    assert report['missions'] == 1

    # Mutate the live database, then restore the snapshot.
    store.create('后续任务', 'x', ['开发'])
    store.registry_register('models', 'later', {'provider': 'local', 'api_key_env': 'K'})
    assert len(store.list()) == 2

    restore_backup(tmp_path, backup)
    reopened = Store(tmp_path)
    titles = [m['title'] for m in reopened.list()]
    assert titles == ['关键任务']
    assert reopened.get(mission_id)['model_id'] == 'deepseek-flash'
    assert any(event['type'] == 'NOTE' for event in reopened.events(mission_id))
    assert 'later' not in reopened.registry_list('models')
    assert reopened.registry_list('models')['persisted']['api_key_env'] == 'LOCAL_KEY'

    # A previous database is preserved so the restore itself can be rolled back.
    assert list(tmp_path.glob('facai.pre-restore-*.sqlite3'))


def test_restore_refuses_unverified_file(tmp_path):
    seed(tmp_path)
    bogus = tmp_path / 'bogus.sqlite3'
    bogus.write_text('not a database', encoding='utf-8')
    with pytest.raises(ValueError):
        restore_backup(tmp_path, bogus)


def test_restore_rejects_source_resolving_to_live_database(tmp_path):
    seed(tmp_path)
    live = tmp_path / 'facai.sqlite3'
    assert live.exists()
    with pytest.raises(ValueError):
        restore_backup(tmp_path, live)
    # The rejected restore must not have disturbed the live database.
    assert [m['title'] for m in Store(tmp_path).list()] == ['关键任务']
    assert not list(tmp_path.glob('.facai-restore*'))


def test_restore_stage_failure_leaves_live_usable(tmp_path, monkeypatch):
    store, _ = seed(tmp_path)
    backup = create_backup(tmp_path)
    store.create('后续任务', 'x', ['开发'])
    assert len(store.list()) == 2

    real_copy = backup_module.shutil.copy2

    def failing_copy(src, dst, *args, **kwargs):
        if Path(dst).name.startswith('.facai-restore'):
            raise OSError('staging failed')
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(backup_module.shutil, 'copy2', failing_copy)
    with pytest.raises(OSError):
        restore_backup(tmp_path, backup)

    # Live data is still the post-backup version and no staging files remain.
    assert len(Store(tmp_path).list()) == 2
    assert not list(tmp_path.glob('.facai-restore*'))
    assert not list(tmp_path.glob('facai.pre-restore-*.sqlite3'))


def test_restore_success_replaces_live_and_keeps_rollback(tmp_path):
    store, mission_id = seed(tmp_path)
    backup = create_backup(tmp_path)
    store.create('快照之后', 'x', ['开发'])
    assert len(store.list()) == 2

    restore_backup(tmp_path, backup)

    reopened = Store(tmp_path)
    assert [m['title'] for m in reopened.list()] == ['关键任务']
    assert reopened.get(mission_id)['model_id'] == 'deepseek-flash'
    assert not list(tmp_path.glob('.facai-restore*'))
    rollback_files = list(tmp_path.glob('facai.pre-restore-*.sqlite3'))
    assert len(rollback_files) == 1
    assert verify_backup(rollback_files[0])['ok'] is True


def test_restore_replace_failure_keeps_live_database_usable(tmp_path, monkeypatch):
    store, _ = seed(tmp_path)
    backup = create_backup(tmp_path)
    store.create('后续任务', 'x', ['开发'])

    def fail_replace(source, destination):
        raise PermissionError('live database is busy')

    monkeypatch.setattr(backup_module.os, 'replace', fail_replace)
    with pytest.raises(PermissionError, match='busy'):
        restore_backup(tmp_path, backup)

    assert len(Store(tmp_path).list()) == 2
    assert not list(tmp_path.glob('.facai-restore*'))
    rollback_files = list(tmp_path.glob('facai.pre-restore-*.sqlite3'))
    assert len(rollback_files) == 1 and verify_backup(rollback_files[0])['ok'] is True


def test_restore_drill_rehearses_without_touching_live_data(tmp_path):
    store, mission_id = seed(tmp_path)
    report = drill_backup(tmp_path)
    assert report['ok'] is True
    assert report['live_counts']['missions'] == 1
    assert report['restored_counts']['missions'] == 1
    assert report['mismatches'] == {}
    # The live database is untouched and the rehearsal directory is cleaned up.
    reopened = Store(tmp_path)
    assert [m['title'] for m in reopened.list()] == ['关键任务']
    assert not list(tmp_path.glob('facai.pre-restore-*.sqlite3'))


def test_restore_drill_reports_count_drift(tmp_path):
    store, _ = seed(tmp_path)
    backup = create_backup(tmp_path)
    # Add data after the snapshot; the drill restores the snapshot into a fresh
    # directory, so counts must match the backup, not the mutated live database.
    store.create('快照之后', 'x', ['开发'])
    report = drill_backup(tmp_path, dest=backup)
    assert report['ok'] is True
    assert report['restored_counts']['missions'] == 1
    assert report['backup_counts']['missions'] == 1
    assert report['live_drift']['missions'] == (1, 2)


def test_backup_contains_no_raw_api_keys(tmp_path):
    store = Store(tmp_path)
    store.registry_register('models', 'persisted', {
        'provider': 'local', 'endpoint': 'http://127.0.0.1:11434/v1',
        'model': 'example', 'api_key_env': 'LOCAL_KEY'})
    backup = create_backup(tmp_path)
    with sqlite3.connect(backup) as db:
        values = ' '.join(str(row[0]) for row in db.execute("SELECT value FROM registries"))
    assert 'api_key_env' in values and 'sk-' not in values


def test_backup_does_not_overwrite_live_or_existing_snapshot(tmp_path):
    store, _ = seed(tmp_path)
    live = tmp_path / 'facai.sqlite3'
    with pytest.raises(ValueError, match='live database'):
        create_backup(tmp_path, live)
    snapshot = create_backup(tmp_path)
    original = snapshot.read_bytes()
    store.create('新任务', '原话', ['测试'])
    with pytest.raises(FileExistsError):
        create_backup(tmp_path, snapshot)
    assert snapshot.read_bytes() == original
    assert len(store.list()) == 2


def test_same_second_backups_are_distinct_and_private(tmp_path, monkeypatch):
    seed(tmp_path)
    monkeypatch.setattr(backup_module, '_timestamp', lambda: 'same-second')
    first = create_backup(tmp_path)
    second = create_backup(tmp_path)
    assert first != second
    assert verify_backup(first)['ok'] and verify_backup(second)['ok']
    if backup_module.os.name != 'nt':
        assert first.stat().st_mode & 0o077 == 0
        assert second.stat().st_mode & 0o077 == 0


def test_backup_uri_paths_and_incremental_tables_are_rehearsed(tmp_path):
    # '?' and '#' are legal path characters on POSIX and survive Path.as_uri()
    # percent-encoding round-trips; Windows forbids both in filenames, so the
    # URI-stress characters degrade to space + unicode on nt (still non-trivial
    # for as_uri(), which percent-encodes the non-ASCII).
    import sys
    uri_dir = 'data?# space' if sys.platform != 'win32' else 'data space 中文'
    uri_file = 'snapshot?#.sqlite3' if sys.platform != 'win32' else 'snapshot 中文.sqlite3'
    root = tmp_path / uri_dir
    seed(root)
    with sqlite3.connect(root / 'facai.sqlite3') as db:
        db.execute('CREATE TABLE "future evidence" (id INTEGER, value TEXT)')
        db.execute('INSERT INTO "future evidence" VALUES (1, ?)', ('preserved',))
    snapshot = create_backup(root, root / uri_file)
    report = drill_backup(root, snapshot)
    assert report['ok']
    assert report['backup_counts']['future evidence'] == 1
    assert report['restored_counts']['future evidence'] == 1
    assert report['scope'] == 'DATABASE_ONLY'
    assert report['secrets_and_workspace_restored'] is False


def test_backup_cli_failure_has_nonzero_exit_code(tmp_path):
    import subprocess
    import sys
    missing = tmp_path / 'missing.sqlite3'
    result = subprocess.run([sys.executable, '-m', 'facai.backup', 'verify', '--dest', str(missing)],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert '"ok": false' in result.stdout


def test_failed_online_backup_removes_reserved_destination(tmp_path, monkeypatch):
    seed(tmp_path)
    destination = tmp_path / 'failed.sqlite3'

    def fail(*args):
        raise OSError('disk failure')

    monkeypatch.setattr(backup_module, '_snapshot_database', fail)
    with pytest.raises(OSError, match='disk failure'):
        create_backup(tmp_path, destination)
    assert not destination.exists()
    assert len(Store(tmp_path).list()) == 1


def test_local_backup_launcher_rehearses_without_mutating_live(tmp_path):
    import json
    import subprocess
    import sys
    seed(tmp_path)
    script = Path(__file__).resolve().parents[1] / 'scripts/backup_local.py'
    result = subprocess.run([sys.executable, str(script), '--data-dir', str(tmp_path)],
                            capture_output=True, text=True, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    report, _ = json.JSONDecoder().raw_decode(result.stdout)
    assert report['ok'] and report['backup_counts']['missions'] == 1
    assert Path(report['backup']).is_file()
    assert len(Store(tmp_path).list()) == 1
    assert not list(tmp_path.glob('facai.pre-restore-*'))


def test_local_backup_launcher_missing_database_fails_without_creating_one(tmp_path):
    import subprocess
    import sys
    script = Path(__file__).resolve().parents[1] / 'scripts/backup_local.py'
    result = subprocess.run([sys.executable, str(script), '--data-dir', str(tmp_path)],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert not (tmp_path / 'facai.sqlite3').exists()


@pytest.mark.skipif(backup_module.os.name == 'nt', reason='POSIX launcher')
def test_posix_backup_entry_works_from_other_directory(tmp_path):
    import subprocess
    seed(tmp_path)
    script = Path(__file__).resolve().parents[1] / 'backup.sh'
    result = subprocess.run(['sh', str(script), '--data-dir', str(tmp_path)],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'DATABASE_ONLY' in result.stdout
    assert len(list((tmp_path / 'backups').glob('*.sqlite3'))) == 1
