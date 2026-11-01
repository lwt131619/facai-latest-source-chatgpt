"""Daily database backup: online VACUUM INTO snapshot, keep the last 7.

Runs under the facai-backup systemd timer (01:00 UTC / 09:00 Beijing). Uses
SQLite's ``VACUUM INTO`` so the snapshot is a self-contained, consistent
database even while the service is writing (WAL mode) — no file-copy races,
no stale sidecars in the copy.
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[2]
KEEP = 7


def backup(data_dir, keep=KEEP):
    if not isinstance(keep, int) or isinstance(keep, bool) or keep < 1:
        raise ValueError('keep must be a positive integer')
    root = Path(data_dir)
    out_dir = root / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dst = out_dir / f"db-daily-{stamp}-{uuid4().hex}.sqlite3"

    from ..store import Store

    store = Store(root)
    con = store.connect()
    try:
        con.execute("VACUUM INTO ?", (str(dst),))
    except BaseException:
        con.close()
        dst.unlink(missing_ok=True)
        raise
    else:
        con.close()
    # Retention runs only after the newly written snapshot is proven readable.
    # If validation fails, keep every previous backup for recovery.
    from ..backup import verify_backup
    try:
        if not verify_backup(dst)['ok']:
            raise ValueError('New daily backup failed integrity verification')
    except BaseException:
        dst.unlink(missing_ok=True)
        raise
    # UUIDs make same-second names unique but cannot be sorted by age. Always
    # retain the verified new snapshot, then keep the newest older files.
    previous = sorted((path for path in out_dir.glob('db-daily-*.sqlite3') if path != dst),
                      key=lambda path: (path.stat().st_mtime_ns, path.name))
    stale = previous[:max(0, len(previous) - (keep - 1))]
    for old in stale:
        old.unlink(missing_ok=True)
    return dst, len(stale)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Daily FACAI database backup")
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args(argv)
    root = Path(args.data_dir or os_getenv_data_dir())
    dst, pruned = backup(root)
    print(f"backup: {dst} (pruned {pruned})")
    return 0


def os_getenv_data_dir():
    import os

    return os.getenv("FACAI_DATA_DIR") or (REPO_ROOT / "data")


if __name__ == "__main__":
    sys.exit(main())
