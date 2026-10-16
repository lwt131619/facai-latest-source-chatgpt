# Local backup and recovery

Run `./backup.sh` on Linux/macOS or `backup.bat` on Windows. The launcher uses
`.venv` when available, creates a private SQLite snapshot, restores it into an
isolated temporary directory, compares all application table counts against that
snapshot, checks SQLite integrity, then removes the temporary rehearsal.
It never replaces the live database. Windows/macOS native execution is not yet
certified; Linux subprocess behavior has automated coverage.

Optional arguments: `--data-dir /private/facai-data --dest /private/new.sqlite3`.
Default data location follows `FACAI_DATA_DIR`, otherwise this package's `data/`.
Existing destinations are rejected; repeated same-second snapshots have distinct
names. A failed backup or drill returns nonzero. Live writes after a snapshot
appear under `live_drift`; they do not invalidate the restore of that snapshot.
Counts and integrity do not establish byte-for-byte file restoration, successful
external tools or real provider connectivity.

## Preserve the whole appliance

A SQLite snapshot is **DATABASE_ONLY**. Separately preserve, privately:

- The matching `master.key`, or the externally supplied `FACAI_MASTER_KEY`.
  Encrypted model keys in the database cannot be recovered without it.
- `local-owner.json`, deployment environment configuration and external credential
  sources. Do not print these values or upload them to GitHub / a source ZIP.
- Workspace/artifact/proof files and other user storage. SQLite rows contain
  references and hashes, not copies of every artifact file.
- The source revision/package and dependency configuration for that snapshot.

For a coherent full-appliance snapshot, stop all FACAI/API/scheduler/worker
processes before copying the matching configuration and workspace files. Do not
mistake the online SQLite snapshot for atomic backup of all external files.

## Offline restore

1. Stop **all** FACAI processes and keep a private copy of current data/config.
2. Verify the selected snapshot using the package's Python environment:
   `python -m facai.backup verify --dest /private/snapshot.sqlite3`.
3. Restore with
   `python -m facai.backup restore --data-dir /private/facai-data --dest /private/snapshot.sqlite3`.
   The operation preserves a verified pre-restore SQLite rollback snapshot.
4. Restore matching key/config/workspace files from your separate private backup.
   Keep permissions private. Do not overwrite those files with source templates.
5. Restart with the original source/package; verify Owner login, Intent history,
   mission/events, evidence, proof hashes and decryption of configured models.
   Test a paid provider only when explicitly intended. Report missing files or
   keys as failures, never as a completed recovery.

This launcher does not deploy/update code, restore a live service automatically,
copy credentials, claim VPS access or certify a full-appliance recovery.
