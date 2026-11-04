# Performance & Storage Optimization — baseline 2026-11-04

Current code already has a SQLite-backed job queue, bounded scheduler concurrency,
leases/recovery and model quota pools (`facai/store.py`, `facai/scheduler.py`,
`facai/quota.py`). Artifact metadata is stored in the `artifacts` table; content
is kept as files below `data/workspaces`. This is **not** a content-addressed
object store. No safe mark/quarantine/sweep lifecycle exists yet.

## Reproducible storage inventory

Run `python scripts/artifact_inventory.py --data-dir /path/to/FACAI_DATA_DIR` on
the actual deployment data directory. The tool opens SQLite read-only, hashes
only registered files inside that data directory's workspace tree, and reports
logical exact-byte duplicates within the **same Owner**. It neither moves nor
deletes files. `logical_duplicate_bytes_same_owner` is potential logical
duplication, not measured recoverable disk bytes. An artifact's independent
mission/Proof/audit reference stays intact regardless of identical content.

This development workspace's `data/` inventory: 0 artifact references, 0
duplicate bytes, 0 missing files, 0.0013 seconds scan. It contains no production
workload, so these numbers **cannot** establish real-world speed or savings.
Targeted local regression: 20 passed, including exact duplicate isolation by
Owner and missing-file handling. No after-optimization benchmark yet.

## Next implementation order

1. Measure real representative jobs (queue wait, P50/P95/P99, model latency,
   tokens, throughput) with existing events and anonymized metrics. No invented
   speedup percentage.
2. Introduce immutable owner-scoped content objects and logical artifact refs
   with atomic writes and recovery. Migrate reads only after old artifact routes
   and Proof exports pass compatibility tests.
3. Build preview-only GC reachability from active artifacts, Proof/backup refs,
   pinned records and retention rules. Quarantine and sweep require separate
   tested recovery stages; never scan or remove user-wide temp directories.
4. Add caches only for explicitly deterministic read-only computations, keyed by
   Owner, permissions, versions and source snapshot. Never reuse execution
   side effects or unverified model output as completed work.

Required acceptance remains real before/after workload comparison, cross-owner
isolation, interrupted write and restore, zero protected evidence deletion.
