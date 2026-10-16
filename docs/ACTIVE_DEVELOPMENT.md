# Active development checkpoint

Runtime isolation increment is committed WIP. Latest focused run: **35 passed,
1 failed**. Failure: test_parallel_branches_execute_in_distinct_worktrees_and_merge
expects the full mission ID in git show --stat output, but long filenames are
abbreviated. Fix and rerun before claiming acceptance; no full-suite validation
of this new runtime increment has occurred. Earlier package acceptance below
belongs to the prior runtime. Existing dist ZIP and bundles are historical
checkpoints; use the final Git HEAD for latest source.

Current stage is validated development, not Production v1.0. The working tree's
code is committed and backed up in the user-selected GitHub repository. Verify
actual final SHA with git ls-remote; never infer success from this sentence alone.

Latest complete extracted-package suite: **1319 passed / 13 skipped / 0 failures**,
real Chromium enabled. Includes the final validation-error credential redaction,
portable nested-repository test and checkpoint read-back behavior. Source hashes
and exact tested commit: docs/validation/package-acceptance.json.

Actual native launcher/restart and non-root Docker internal service/persistence
checks passed. Docker host-port browser access is not verified in this cloud
namespace. No live paid provider call, VPS deployment or Windows/macOS native
certification occurred. No 20+ real-agent acceptance is claimed.

Implemented: immutable Mission Specification, local Owner Setup/recovery/model
configuration, persistent Evidence/Claim/context conflicts/retraction, Proof ZIP
and independent Owner acceptance, explicit source-bound Knowledge Memory.

Read AGENT_HANDOFF.md, docs/SETUP_WIZARD.md, docs/TRUST_WORKFLOW.md and
KNOWLEDGE_MEMORY.md. Continue the frozen 27-direction task registry, beginning with
Runtime capacity and safe workspace isolation. Every small step must be explicitly
committed/pushed; WIP is allowed with an honest validation label. Important scripts
are in Git, including smoke_appliance.py, smoke_docker.py and verify_package.py.


Post-package CI fix: optional real nginx parsing was exercising unwritable system
cache defaults. The validation wrapper now uses isolated temporary caches/logs;
real nginx1.30.6 under UID10001 passed HTTP/TLS for both default/custom domains.
No production template or VPS changed. See docs/validation/nginx-validation.json.
Check GitHub Actions for final remote results; do not infer CI success from local skips.

## Runtime fleet acceptance measured (takeover agent, 2026-10-16)

tests/test_runtime_capacity.py: 24 independent jobs across 4 schedulers sharing
one store — all complete, zero double-claims (per-workspace execution count == 1),
per-worker peak never exceeds its concurrency, every execution attributed to one
OPENCODE_STARTED. Lease renewal: a 1.5s job under a 0.4s claim lease is re-armed
by touch_job_lease and never stolen by a hunting peer. Crash recovery: an expired
lease with attempts left requeues exactly once (no budget -> honest FAILED), and
the peer runs it once after the not-before buffer clears. Status stays PARTIAL:
real OpenCode CLI / real model 20+ acceptance is still outstanding.

## Fleet-wide claim ceiling fixed (takeover agent, 2026-10-16)

RUNTIME-001 runtime bug found by the fleet tests and fixed in
facai/scheduler.py: the scheduler passed its OWN concurrency as the store's
GLOBAL running ceiling, so a fleet was throttled to whichever worker claimed
first (a 2x2 fleet ran 2 jobs while 2 stayed queued). The optional
FACAI_MAX_RUNNING_JOBS is now parsed by _fleet_ceiling (None = unlimited) and
passed to every claim; each worker's own concurrency still binds locally.
tests/test_runtime_capacity.py pins the contract deterministically at three
layers — store claim semantics across two worker ids, scheduler env->claim
wiring (concurrency=4 + ceiling=2 claims exactly 2), and a two-worker drain
that only asserts deterministic outcomes (all complete, per-worker peak <= own
concurrency). The earlier thread-interleaving tests were rewritten; the full
suite passes twice consecutively (1351 passed, 7 skipped).

## Local product smoke acceptance: real CLI end to end (takeover agent, 2026-10-16)

scripts/smoke_local.py (new, follows the smoke_* convention) starts the real
uvicorn server on loopback with an isolated data dir and random throwaway
credentials, then walks the real customer surface: health, session, web
console (178 KB HTML) and mobile PWA (68 KB), member self-registration,
funding-pool top-up (the 0-balance 402 budget gate fires first and honestly),
mission creation, registry discovery, opencode/deepseek-flash binding, job
dispatch, and the job's terminal state. Two consecutive runs: 11/11 and 20/20
checks (the second was a longer inline driver; the committed script is the
reproducible one). The in-app scheduler claimed the job, the REAL OpenCode CLI
executed it, and the evidence chain shows the full honest arc — MISSION_CREATED
-> BINDING_CHANGED -> JOB_QUEUED -> JOB_STARTED -> SESSION_PREPARED ->
OPENCODE_STARTED -> 6x OUTPUT -> ARTIFACT_CREATED -> COST_RECORDED ->
COST_PRICED -> GIT_CHECKPOINT -> SESSION_COMPLETED -> OPENCODE_COMPLETED ->
JOB_COMPLETED (19 events) — and the mission workspace contains the real
hello.txt with the requested content. The unbound-dispatch gate also verified:
without a binding the API refuses with 409 instead of faking progress.
RUNTIME-001 keeps status PARTIAL: real-CLI 20+ concurrent execution/recovery
acceptance still needs real provider quota.

## Concurrent real-CLI acceptance + event-bus replay idempotency (takeover agent, 2026-10-16)

scripts/smoke_fleet.py (new): four missions through the real customer surface,
dispatched together on a server configured FACAI_MAX_WORKERS=4 /
FACAI_OPENCODE_SERIAL=0. Verdict 16/16: all four jobs COMPLETED in 6.5s wall
time, each mission shows exactly one OPENCODE_STARTED (zero double-claims) with
six OUTPUT events, the four requested files exist with exactly the requested
content, and four cost rows are recorded. Combined with smoke_local.py's single
19-event chain, RUNTIME-001's real-CLI concurrency line is now measured; crash
recovery on the real CLI and 20+ scale still need real provider quota.

tests/test_eventbus.py gains the replay-idempotency pin: 24 concurrent appends
(8 threads behind a barrier) leave the owner-level global feed strictly ordered
and duplicate-free, and two clients replaying from the same cursor receive
byte-identical tails. EVENT-001's registry entry now records what is actually
built (vocabulary validation, owner-scoped cursor replay, SSE contract, legacy
pass-through, isolation, auth) and the one honest remainder: new paths switch
to canonical names when the first client consumes them — not before.
