"""Verified SQLite backup and restore for the Facai data directory.

This copies all SQLite tables, including encrypted model credentials. It does
not include master.key, local-owner.json, environment credentials or workspace
files; those require a separate private backup. Restore must run with all
FACAI processes stopped. Database integrity is not proof of complete appliance
recovery or availability of external providers.
"""

import argparse
import gc
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REQUIRED_TABLES = {"missions", "steps", "events", "registries", "checkpoints", "costs", "jobs"}
COUNTABLE_TABLES = sorted(REQUIRED_TABLES | {"sessions", "artifacts", "auth_sessions"})


def _timestamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def create_backup(data_dir, dest=None):
    root = Path(data_dir).resolve()
    source_file = root / "facai.sqlite3"
    if not source_file.exists():
        raise FileNotFoundError(f"No database at {source_file}")
    if dest is None:
        directory = root / 'backups'
        directory.mkdir(parents=True, exist_ok=True)
        dest = _unique_path(directory, f'facai-{_timestamp()}.sqlite3')
    dest = Path(dest).resolve()
    if dest == source_file:
        raise ValueError('Backup destination must differ from the live database')
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Reserve a private new file. Never clobber a prior snapshot, including when
    # a caller supplies a destination that another process has just created.
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        _snapshot_database(source_file, dest)
        if not verify_backup(dest)['ok']:
            raise ValueError('Created backup failed integrity verification')
    except BaseException:
        _discard_staged(dest)
        raise
    return dest


def _snapshot_database(source_path, dest_path):
    """Copy a live SQLite database consistently, including pending journal data."""
    source = sqlite3.connect(Path(source_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    target = sqlite3.connect(dest_path, timeout=10)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()


def verify_backup(backup_path):
    backup_path = Path(backup_path).resolve()
    if not backup_path.exists():
        return {"ok": False, "reason": "missing", "path": str(backup_path)}
    connection = sqlite3.connect(backup_path.as_uri() + "?mode=ro", uri=True)
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(REQUIRED_TABLES - tables)
        missions = connection.execute("SELECT COUNT(*) FROM missions").fetchone()[0] if "missions" in tables else 0
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        return {"ok": False, "reason": type(exc).__name__, "path": str(backup_path)}
    finally:
        connection.close()
    return {"ok": not missing and integrity == "ok", "integrity": integrity,
            "missing_tables": missing, "missions": missions, "path": str(backup_path)}


def _unique_path(directory, name):
    """Return a non-existing path in directory, suffixing a counter if needed."""
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, _, suffix = name.rpartition(".")
    for index in range(1, 1000):
        candidate = directory / (f"{stem}-{index}.{suffix}" if stem else f"{name}-{index}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"No free restore path for {name} in {directory}")


def _discard_staged(staged):
    """Remove a staged restore copy and its WAL sidecars."""
    for suffix in ("", "-wal", "-shm"):
        Path(str(staged) + suffix).unlink(missing_ok=True)


def restore_backup(data_dir, backup_path):
    root = Path(data_dir).resolve()
    live = root / "facai.sqlite3"
    source = Path(backup_path).resolve()

    if source == live:
        raise ValueError(f"Refusing to restore the live database onto itself: {live}")

    report = verify_backup(source)
    if not report["ok"]:
        raise ValueError(f"Refusing to restore an unverified backup: {report}")

    root.mkdir(parents=True, exist_ok=True)

    # Stage a verified copy inside the destination directory. Keeping the staged
    # file on the same filesystem lets the final swap be an atomic os.replace.
    staged = _unique_path(root, ".facai-restore.sqlite3")
    try:
        shutil.copy2(source, staged)
        staged_report = verify_backup(staged)
        if not staged_report["ok"]:
            raise ValueError(f"Staged backup failed verification: {staged_report}")
    except BaseException:
        _discard_staged(staged)
        raise

    if live.exists():
        # Keep the previous database so a restore can itself be rolled back.
        rollback = _unique_path(root, f"facai.pre-restore-{_timestamp()}.sqlite3")
        try:
            _snapshot_database(live, rollback)
            if not verify_backup(rollback)["ok"]:
                raise ValueError("Pre-restore database snapshot failed verification")
        except Exception:
            _discard_staged(staged)
            rollback.unlink(missing_ok=True)
            raise

    # Only now that a verified copy is staged do we replace the live database.
    # os.replace is atomic; a failure leaves the previous live database usable.
    for attempt in range(2):
        try:
            os.replace(staged, live)
            # The restored file is self-contained (verified before staging).
            # Stale sidecars are now hazards: the PREVIOUS live database's WAL
            # would be replayed over the restored file by the next connection,
            # and the staged copy's own sidecars are orphans after the rename.
            for suffix in ("-wal", "-shm"):
                Path(str(live) + suffix).unlink(missing_ok=True)
                Path(str(staged) + suffix).unlink(missing_ok=True)
            return live
        except PermissionError:
            # Windows refuses to replace a file that another handle still has
            # open. Dead-but-uncollected sqlite connections can linger, so
            # collect them and retry once; if it still fails, the live database
            # is untouched and remains usable.
            if attempt == 0:
                gc.collect()
                continue
            _discard_staged(staged)
            raise
        except BaseException:
            _discard_staged(staged)
            raise
    return live


def _counts(database):
    connection = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        present = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        # Include incremental schemas (Intent, evidence, claims, knowledge,
        # permissions) rather than a frozen legacy table allowlist.
        return {table: connection.execute('SELECT COUNT(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0]
                for table in sorted(present) if not table.startswith('sqlite_')}
    finally:
        connection.close()


def drill_backup(data_dir, dest=None):
    """Rehearse a full restore into a throwaway directory.

    Takes a verified backup, restores it somewhere temporary and compares row
    counts against that immutable snapshot. Live drift is informational, not
    restore corruption. The live database is never modified and no
    credential value is read or printed: only table names and counts are reported.
    """
    live = Path(data_dir).resolve() / "facai.sqlite3"
    if not live.exists():
        raise FileNotFoundError(f"No database at {live}")
    backup = Path(dest).resolve() if dest is not None else create_backup(data_dir)
    report = verify_backup(backup)
    if not report["ok"]:
        return {"ok": False, "stage": "verify", "backup": str(backup), **report}
    backup_counts = _counts(backup)
    live_counts = _counts(live)
    rehearsal = Path(tempfile.mkdtemp(prefix="facai-drill-"))
    try:
        restored = restore_backup(rehearsal, backup)
        restored_counts = _counts(restored)
        mismatches = {table: (backup_counts.get(table), restored_counts.get(table))
                      for table in sorted(backup_counts.keys() | restored_counts.keys())
                      if backup_counts.get(table) != restored_counts.get(table)}
        live_drift = {table: (backup_counts.get(table), live_counts.get(table))
                      for table in sorted(backup_counts.keys() | live_counts.keys())
                      if backup_counts.get(table) != live_counts.get(table)}
        return {"ok": not mismatches, "stage": "restore", "backup": str(backup),
                "restored": str(restored), "live_counts": live_counts,
                "restored_counts": restored_counts, "backup_counts": backup_counts,
                "mismatches": mismatches, "live_drift": live_drift,
                "scope": "DATABASE_ONLY", "secrets_and_workspace_restored": False}
    finally:
        shutil.rmtree(rehearsal, ignore_errors=True)


def _manifest(path):
    report = verify_backup(path)
    return json.dumps({"backup": str(path), **report}, ensure_ascii=False, indent=2)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Facai database backup and restore")
    parser.add_argument("command", choices=["create", "verify", "restore", "drill"])
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--dest", default=None, help="backup path for create, or source for restore/verify")
    args = parser.parse_args(argv)

    if args.command == "create":
        path = create_backup(args.data_dir, args.dest)
        print(_manifest(path))
        return 0 if verify_backup(path)['ok'] else 1
    elif args.command == "verify":
        if not args.dest:
            parser.error("--dest is required for verify")
        print(_manifest(args.dest))
        return 0 if verify_backup(args.dest)['ok'] else 1
    elif args.command == "restore":
        if not args.dest:
            parser.error("--dest is required for restore")
        restored = restore_backup(args.data_dir, args.dest)
        print(f"Restored {args.dest} -> {restored}")
        return 0
    else:
        report = drill_backup(args.data_dir, args.dest)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report['ok'] else 1


if __name__ == "__main__":
    raise SystemExit(main())
