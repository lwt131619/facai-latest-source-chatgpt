"""Read-only inventory of registered artifact files; never deletes evidence."""

import hashlib
import sqlite3
import time
from collections import defaultdict
from pathlib import Path


def inventory(data_dir):
    root = Path(data_dir).resolve()
    database = root / "facai.sqlite3"
    if not database.is_file():
        raise FileNotFoundError(database)
    start = time.monotonic()
    # Read-only connection prevents this diagnostic from migrating or creating data.
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        rows = db.execute("""SELECT a.id, a.path, m.owner_id FROM artifacts a
                             JOIN missions m ON m.id=a.mission_id
                             ORDER BY m.owner_id, a.id""").fetchall()
    workspace_root = root / "workspaces"
    seen_paths = {}
    groups = defaultdict(list)
    missing = unsafe = changed = total_bytes = 0
    for artifact_id, raw_path, owner in rows:
        path = Path(raw_path)
        try:
            resolved = path.resolve(strict=True)
            if workspace_root not in resolved.parents or not resolved.is_file():
                unsafe += 1
                continue
            before = resolved.stat()
            key = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            if key not in seen_paths:
                digest = hashlib.sha256()
                with resolved.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                seen_paths[key] = digest.hexdigest()
            after = resolved.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                changed += 1
                continue
        except FileNotFoundError:
            missing += 1
            continue
        except (OSError, ValueError):
            unsafe += 1
            continue
        total_bytes += after.st_size
        groups[(owner, after.st_size, seen_paths[key])].append(artifact_id)
    duplicate_references = sum(len(ids) - 1 for ids in groups.values())
    # This is logical duplicate size, not guaranteed recoverable physical bytes:
    # paths may already be hard-linked or a backup may retain all versions.
    duplicate_bytes = sum(size * (len(ids) - 1)
                          for (_, size, _), ids in groups.items())
    return {
        "artifact_references": len(rows), "readable_bytes": total_bytes,
        "duplicate_references_same_owner": duplicate_references,
        "logical_duplicate_bytes_same_owner": duplicate_bytes,
        "missing_files": missing, "unsafe_paths": unsafe, "changed_during_scan": changed,
        "scan_seconds": round(time.monotonic() - start, 4),
        "mode": "READ_ONLY", "deduplicated": False,
    }
