## Test isolation governed: single-process full suite green (2026-10-29)

The 2026-10-28 handoff debt ("single-process full run wedges, files must be
run individually") is closed. Root cause, measured on this Windows host with
a dedicated socketpair hammer and thread dumps: host security software
intermittently drops/aborts loopback connections. Three faces of one cause:
(1) dropped SYNs during CPython's Windows socketpair fallback — its
unbounded accept() wedged every later event-loop creation forever
(hammer: 7/2000 single-thread, 61/3200 at 8 threads, accept delays
quantised at 2/4/6s retransmit steps); (2) aborted ESTABLISHED connections
(WinError 10053) kill an event loop's self-pipe wake channel, stranding
callbacks and any run_sync_from_thread waiter (TestClient portal exits);
(3) Chromium connects to the local test server hit ETIMEDOUT — the
previously recorded "3 browser flakes" were this, not browser races.

Hardening (all pinned by tests): conftest replaces the Windows socketpair
fallback with a bounded-accept (10s) + 3-retry version; a session stall
watchdog (300s default, `FACAI_TEST_STALL_LIMIT` override) dumps all thread
stacks to a temp file (bypassing pytest fd capture) and exits 70 so no
unknown wedge can ever hang a run again; `attack_agent._fetch` retries
transport-level failures boundedly without loosening the differential
oracle; `trust.export` pins ZIP entry mtimes to the proof's `created_at`,
making one proof always export byte-identical bytes (immutable-proof
contract; a rare closed-loop restart flake came from zipfile's wall-clock
mtimes). New pins: tests/test_socketpair_hardening.py (4),
tests/test_attack_agent.py (3, offline), test_trust_workflow determinism
(1). Windows single-process full suite: 1553 passed / 10 skipped / 0
failed in 521s (two earlier complete runs 1550/2 and 1551/2; the residual
failures were browser ETIMEDOUT environment flakes that pass solo, and the
closed-loop proof failure is gone). Two other runs were bounded-terminated
by the watchdog exactly as designed (exit 70 + on-disk dumps). Linux CI
verdicts come from GitHub Actions, not from these local numbers.

## Skill source version visibility (2026-10-17)

Owner-scoped `/registries/skills/{key}/versions` lists immutable source
versions, SHA256 and import timestamps without bodies, including after soft
removal. The Command Center Skill drawer shows the stored version summary.
The owner-isolation/lifecycle suite passed 40 tests. With Playwright installed
in this test environment, the real Chromium drawer test passed (1/1).
Full post-change regression is pending.

## Durable Skill governance journal (2026-10-17)

Lifecycle journal entries now flush to the boundary state file immediately;
the previous disable/enable/remove path only appended in memory. Guarded
updates add `SKILL_UPDATED` with previous/current version and SHA256, never
source body. Reload tests verify the journal survives restart. Skill and
boundary tests: 65 passed. Registry mutation and the boundary JSON journal
are separate stores, so a crash between them is still a cross-store gap.
Full regression after the journal change: 1408 passed, 36 skipped, 0 failed
in 106.25 seconds with real Chromium on 2026-10-17.

## Guarded Skill updates (2026-10-17)

`POST /api/v1/registries/skills/{key}/update` compares expected version and
source SHA256, then inserts the new source and updates registry metadata in one
SQLite IMMEDIATE transaction. Stale callers receive 409; two concurrent calls
cannot both win. Old versions remain available to pinned missions and enabled
state is preserved. Focused conversion/Skill/execution regression: 96 passed.
Legacy `/skills/import` remains an unguarded overwrite for compatibility, so
this is an opt-in safe update path rather than universal update protection.
Full regression after this increment: 1407 passed, 36 skipped, 0 failed in
104.11 seconds with real Chromium on 2026-10-17.
Follow-up: metadata-only legacy Skills can enter the guarded path by supplying
an empty expected version and a null expected source SHA. Focused
conversion/Skill/execution regression after this change: 97 passed.

## Explicit Skill routing (2026-10-17)

Assignment candidates now record matched Skill ID and score as structured
fields, including durable assignment events. Conversion missions auto-bind and
pin a Skill only when the owner's raw request names exactly one enabled Skill
ID with a positive invocation verb and its imported source exists. A vague
similarity score remains advisory; negated mentions, multiple positive names
or a missing source leave the mission unbound. Conversion suite:
36 passed after the negation refinement. Full regression after the refinement:
1405 passed, 36 skipped, 0 failed in 99.21 seconds with real Chromium on
2026-10-17. The first full attempt was interrupted by an environment restart;
these numbers come from the completed rerun.
Follow-up clause-level negation check rejects longer negative requests and
accepts a subsequent positive clause. Focused conversion/Skill/execution
regression after this follow-up: 94 passed, 0 failed. The 1405-result above
predates this follow-up; native/live-provider acceptance remains open.

## Checkpoint bundle size correction (2026-10-17)

GitHub remains the primary resume source. Tracking `FACAI-resume.bundle` inside
the same repository made every new full-history bundle include the previous
bundle's blob (43 MiB → 51 MiB → 61 MiB). Checkpoint now refuses that tracked
state; bundle/checksum/status are generated locally and ignored by Git, while
the complete source/history and validation docs continue to be pushed to both
remote refs. Historical tracked bundles remain in Git history only. An offline
handoff must transfer the latest local bundle as a separate file. This does
not erase Git history or change source semantics.

## Skill source runtime integration (2026-10-17)

Commits `12cb873` → `6403ae3` add owner-scoped, version/hash-checked Skill
source persistence and attach its body to bound adapter tasks after authority
checks. `OPENCODE_STARTED` retains the original user prompt; terminal events
record only the source hash and whether the body reached the adapter. Usage
counts verified attachments separately from legacy metadata attribution.
Focused lifecycle/execution tests: 57 passed; one real adapter-path test asserts
the command receives the source and the event contains no source body. Full
regression after these source/usage changes: 1401 passed, 36 skipped, 0 failed,
136.17 seconds with FACAI_TEST_CHROMIUM=/usr/bin/chromium on 2026-10-17.
The skipped tests and native package/provider acceptance are not certified.
At the 6403ae3 checkpoint, the bound mission did not pin its Skill version.
Adapter following the instructions and factual correctness remain unverified.

Subsequent increment: manual mission binding now stores the resolved Skill
version and source hash in mission columns and BINDING_CHANGED. Both synchronous
API and scheduler resolve this snapshot for execution even after registry
updates, while disable/removal still blocks dispatch. Existing automatic
bindings without a source snapshot retain prior latest-registry semantics.
Focused execution/lifecycle/scheduler tests: 52 passed, including a registry
update between binding and execution. Full regression after this increment:
1401 passed, 36 skipped, 0 failed in 138.54 seconds with real Chromium on
2026-10-17. Native installers, live provider calls and production acceptance
remain unverified.

Same-body version migration follow-up: `skill_sources` now keys each version
separately even when SHA256 is identical. Startup migrates the previous
owner/skill/hash primary key and preserves existing rows. A populated legacy
database and repeated import across two versions are covered by tests.
Full regression after this migration: 1402 passed, 36 skipped, 0 failed in
160.83 seconds with FACAI_TEST_CHROMIUM=/usr/bin/chromium on 2026-10-17.

## Skill attribution semantics correction (historical)

Audit found that SKILL.md import persists metadata only; no adapter loads the
body. The new /usage endpoint therefore explicitly reports resolved-skill
attributed terminal events, with skill_invocation_verified=false. The Command
Center labels these as attribution and says Skill body execution is unverified.
Actual version-locked Skill body storage/invocation is the next engineering
gap, not claimed complete. Full regression before this wording/metadata-only
correction: 1427 passed /7 skipped /0 failed with real Chromium, 267.49s on
d338b3c. Focused regression after correction: 38 passed /0 failed, including
real Chromium. Details: docs/validation/skill-attribution-20261016.json.

## Skill observed usage WIP

Skill execution attribution now records the resolved skill version and omits
skill_id when a bound skill was disabled/removed before dispatch. Owner-scoped
GET /api/v1/registries/skills/{key}/usage aggregates terminal CLI/model events
attributed to resolved skill metadata, across versions and soft deletion. It
does not prove actual Skill invocation, causal effectiveness or verification.
Focused skill/isolation/execution/registry tests: 61 passed /0 failed. Command
Center skill cards now fetch this owner-scoped endpoint on demand; a real
Chromium drawer test plus lifecycle suite passed 34/34. It labels terminal
execution success separately from factual verification. Full post-change
regression remains pending at this checkpoint.

## Reprompt audit provenance

JOB_REPROMPTED now links the exact FEEDBACK_RESOLVED event and gate while
recording previous/new prompt SHA-256 values in the same update transaction.
The job row remains the prompt text source; the event does not repeat it.
Cross-mission feedback-event references are rejected. Focused console/model/
router regression: 55 passed /0 failed. Previous full Python regression
1423/7 predates this change; full post-change run not yet recorded.

## 582f3d8 takeover and conversion fallback audit

Fast-forwarded c307493 to origin/main 582f3d8; no divergent commits. External
MODEL/TRUST/SKILL/ROUTER work already present, so no duplicate implementation.
Focused new-module regression: 83 passed /1 skipped; 27 task directions valid.
Channel model-conversion fallback now records a CONVERSION_FALLBACK mission
event in the same transaction as mission creation, with only model id, error
class and deterministic fallback source. PROMPT_CONVERTED also carries these
provenance fields. No-model conversion creates no false fallback event.
Focused conversion/router tests: 55 passed /0 failed. Full regression on
a38808b: 1423 passed /7 skipped /0 failed with real Chromium, 242.72 seconds.
Machine-readable details: docs/validation/takeover-582f3d8.json. Native
package remains NOT_VERIFIED.

## Bundled desktop source WIP

2026-10-16: Electron installed-mode wiring now chooses bundled local FACAI
source by default; versioned code is copied beneath Electron userData/runtime,
with persistent Owner data in userData/data. Packaged config includes Python
source/static assets and vendor metadata, but requires system Python and first
start network dependency install. Node 12/12 pass, including real FastAPI from
a simulated resources directory, setup URL and persistent-data separation.
Native Windows/macOS installer and Electron binary remain NOT_VERIFIED.
Prior full Python regression (1364/6) predates this Node/package-only change.

## Desktop current full regression

2026-10-16 source code after fa0b798 and Electron wiring tests: Python suite
1364 passed /6 skipped /0 failed with real Chromium,225.02 seconds. Node suite
8 passed /0 failed, including a real backend process and mocked Electron
window lifecycle. Bundle/ZIP validation pending next artifact commit. Exact
source hashes and unverified platform items:docs/validation/desktop-increment.json.
GitHub Actions status has not been independently checked this turn.

Electron integration wiring checks added after backend test: mocked Electron
window verified local Owner setup URL and stop-on-quit; remote window verified
same-origin boundaries. Node total8 passed /0 failed. First test run exposed a
test harness asynchronous environment-reset race; harness fixed, product code
unchanged. Native Electron binary/browser window remains untested on this host.

Package metadata correction: tracked dist/ archives change while a new ZIP
is produced. source_tree_dirty now excludes dist/ from git status but still
reports any source change. A temporary-repository test covers both cases.
Next package rebuild will embed the corrected metadata; previous ZIP with
source_tree_dirty=true was hash-valid but carried that misleading status.

Desktop increment checkpoint: Linux Node integration test launches/stops a
real FACAI API; 6/6 Node checks passed in both normal and explicit-Python modes.
Python launcher/setup browser path:5/5 passed with Chromium. CI workflow now
runs these Node checks before Python suite. See docs/validation/desktop-increment.json
for source hashes and unverified native installer limits. DESKTOP-001 PARTIAL.
Full Python regression after this increment: NOT_RUN locally; GitHub CI result
must be read from the workflow, not inferred from focused checks.

Desktop CI runner fix: an explicitly selected installed Python now uses its
existing dependencies, while normal source mode still creates/updates .venv.
Both default and external-Python Node test runs passed 6/6; this avoids a
second pip install in GitHub Actions. Previous package ZIP was built before
this adjustment; rebuild follows this checkpoint.

Desktop source mode increment: Electron can start the existing Python launcher
from a source checkout, wait for loopback health, receive a one-use setup URL
only via its private child stdout pipe, and stop its child on quit. Remote mode
still works. Node tests start a real API process and check failure cases:
6 passed. Native Electron installer/browser lifecycle on Windows/macOS remains
NOT_VERIFIED; installed Electron package currently omits bundled Python/source.

## Desktop increment WIP

Fresh fetch:4381ef9 equals origin/main; clean baseline. Electron navigation now
compares parsed origins and restricts external opening to HTTP(S), avoiding
same-prefix lookalike hosts. Node navigation checks:2 passed. Local service
lifecycle is the next incremental step; packaged native installers remain untested.

## Latest full regression:1363 passed /6 skipped /0 failures

2026-10-16 recovery increment, real Chromium enabled,221.55 seconds.
Code tested:bfda07d; subsequent89c427c changes agent-entry documentation only.
Exact runtime/launcher hashes and limitations:docs/validation/recovery-increment.json.
20 related backup/launcher tests passed, including real POSIX launcher subprocess.
No full-appliance key/config/workspace restoration or VPS/Windows/macOS claim.
UPDATE-001 remains PARTIAL. Rebuilt package metadata identifies its own source SHA.

Agent entry now explicitly requires upstream comparison before edits, protects
diverged work, and marks03434f4 handoff as integrated historical context.
Local launcher + backup tests:20 passed /0 failed; full Chromium regression running.

Local backup launchers added with independent source/working-directory handling;
package allowlist includes both launchers and scripts. Linux subprocess tests
exercise actual snapshot+restore drill and failure on missing database; Windows
batch syntax is supplied but not certified on a Windows host. Related tests are
recorded in the next validation checkpoint; full regression follows.

## Recovery increment after overlap check

Fetched origin/main and confirmed 9b49940 already includes external03434f4 and
all six follow-up commits. No new upstream changes or overlap were found.
Database backup now reserves private snapshots without clobbering existing files,
handles URI-special filenames, validates created snapshots, and cleans failed
snapshots. Recovery drills compare against the immutable backup, report live
write drift separately, and count incremental tables including evidence/knowledge.
CLI verify/drill failure returns nonzero. Focused backup suite: 14 passed.
Scope is DATABASE_ONLY; key/config/workspaces are not claimed restored.
This is the next increment, not covered by the prior1355-pass full regression.

## Latest complete regression: 1355 passed / 6 skipped / 0 failures

2026-10-16: real Chromium enabled; full suite completed in 184.22 seconds on
source e32fd39. Source hashes and counts: docs/validation/mobile-increment.json.
The earlier Runtime assertion failure was resolved by the integrated upstream
changes; it does not describe this tested source. Previous reports remain
historical snapshots. Mobile remains PARTIAL pending physical-device HTTPS,
real notification delivery and complete Production acceptance. No VPS deployment.

## Mobile execution/resource increment validated locally

Real Chromium + static mobile suite: 15 passed, 0 failed at 390x844.
Live backend covers Agent/model/runtime bindings, queued Job/lease state and
account budget summary, plus artifacts/download/logout and offline shell tests.
Full regression is running; see docs/validation/mobile-increment.json.
Task registry remains PARTIAL; no physical-device, paid Provider or VPS claim.

CI portability fixes: model-registration DNS fixtures now include mission control;
three browser launch sites support FACAI_TEST_CHROMIUM and container flags;
shared browser servers hand an already bound socket to uvicorn, removing the
bind/close/rebind race. Real Chromium focused suite: 52 passed, 0 failed.
Mobile execution-assignment/resource-summary UI is next WIP, not covered by that run.

Mobile follow-up: real Chromium artifact ownership/inert preview/download/logout
passed. Offline shell test exposed all-or-nothing cache.addAll failure when / is
401 before login; installation now caches each shell resource independently.
Real Chromium offline reload and API-cache exclusion passed (1 test).
Full refreshed mobile regression is next. No real-device/HTTPS deployment claim.

## 2026-10-16 GitHub handoff integration / MOBILE-001 WIP

Fast-forward integrated origin/main 03434f4 and its four audit/runbook documents.
Mobile files now use owner-scoped artifact listing and bounded text previews,
with explicit truncation and UTF-8 text-copy download labels. Mobile gets its own
manifest; SW v2 caches mobile shell/CSS, never API responses. No VPS changes.
Focused artifact/runtime/workspace/static regression: 41 passed, 0 failed.
Browser coverage for this new file/PWA path is pending; MOBILE-001 stays PARTIAL.
Historical audit and package validation remain historical evidence, not this build.

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

## Delivery package rebuilt and QA-verified at a871959 (takeover agent, 2026-10-16)

scripts/package.py rebuilt dist/FACAI-v1.0-development.zip from the current
tree (408 files) and scripts/verify_package.py passed independently: archive
and manifest hashes verified per file, the packaged validate_handoff runs
clean (27 directions, no cycles, all required files present), and the QA
record pins package_source_commit=a871959 with source_manifest_verified=true.
One honest finding from the pass: editing task-registry.json without rerunning
validate_handoff.py --render makes the in-package handoff check fail (it
should — the traceability doc was stale); the render step is now part of the
flow. PKG-001 keeps PARTIAL: Windows is exercised constantly (start chain,
smokes, package QA), macOS and cross-device/VPS acceptance remain open.

## CI green restored on 79954b5 (takeover agent, 2026-10-16)

The "FACAI development package" workflow had been failing on GitHub since
Codex's 6c9ab35 — the suite runs on ubuntu-latest where the deploy-config
validator's real `nginx -t` step cannot bind 80/443 as a non-root runner
(Windows runs skip it, so local runs stayed green while CI was red). Two root
causes fixed in sequence on d28e996's successors: (1) the -t validation copies
now listen on 18080/18443 while the real port contract stays asserted
textually (486fed8); (2) the TLS copy's sed mixed one -E expression with a
bare second expression, which GNU sed read as a filename — both expressions
now ride explicit -e flags (79954b5). Verified: workflow run on 79954b5
completed with conclusion=success (full ubuntu pytest + packaging). Discipline
note: local green never implies remote green — check the Actions API, not the
local skip path.

## Two read-only audits landed and packaged for Codex handoff (takeover agent, 2026-10-16)

MOBILE-001 audit (agent, 405s): all 13 mobile.html network calls hit real api.py
routes — zero dead endpoints; verdicts PARTIAL / VERIFIED_LOCAL / PARTIAL. The
gaps are precise: the files tab is a placeholder while GET /api/v1/artifacts
exists (wire it + a facaiGo refresh hook), mobile.html never registers /sw.js
(copy the index.html:2091 https-gated registration), the sw.js SHELL lacks
/static/mobile.html (add it, bump CACHE to v2), no mobile Agent view (reuse
OPENCODE_* events), no mobile settings summary (/api/v1/billing/me is ready),
and the A20 real-device 390x844+HTTPS walkthrough is still not executed.

Linux portability audit (agent): with 486fed8's failure isolated to the sed
case (pytest does not stop on one failure), every other case — browsers,
concurrency — has already passed on ubuntu-latest, so deterministic platform
bugs are unlikely. Top non-deterministic risks: test_mission_control_flow's
unstubbed real DNS call (one-line conftest allowlist fix), runtime-capacity
lease timing, CJK-font-dependent pixel assertions + three launches missing
--no-sandbox, the _free_port() TOCTOU, and eventbus SQLite lock contention.

Both reports plus a prioritized not-yet-wired list live in
docs/handoff-20261016/ (additive only — no source files touched) and ship as
the update package for the Codex-side merge.

## Takeover round 2: Codex increment reviewed, CI races fixed, package refreshed (2026-10-16)

Codex's 21-commit run (03434f4..c307493) was reviewed read-only by an agent:
all four themes are real implementations (mobile files tab wired to
GET /api/v1/artifacts with auth-gated preview, independent mobile.webmanifest,
v2 SW shell, agent assignments off the real /missions/{id}/jobs queue,
billing summary, DNS/port CI races, O_EXCL backup chain, Electron
source-mode) with honest PARTIAL declarations where native/real-device
acceptance is still open. No high-severity findings; the medium one —
dist ZIP one commit behind HEAD — is what this checkpoint fixes.

Takeover fixes on top:
- 511fb54: the c307493 CI red was test_runtime_capacity's "every worker
  actually shared the work" assertion starving a thread on slow runners —
  seed one synchronous claim per worker before the concurrent free-for-all;
  and test_backup's 'data?# space' fixture is illegal on Windows filenames
  (WinError 123), so the URI-stress characters degrade to space+unicode on
  win32. Full local suite 1362 passed / 0 failed after both; CI green.
- 06ed4f9: A-7 literal assertions pinning the files tab, independent
  manifest, secure-context SW registration and the v2 shell; store sqlite
  busy timeout 10s->30s for the 8-thread fleet (review item B-5).
- Package rebuilt at a clean 06ed4f9 (430 files, manifest verified
  independently), closing the ZIP-behind-HEAD gap.

## MODEL-001 pool wiring + TRUST-001 source verification (takeover agents, 2026-10-16)

Two read-only audits first: MODEL-001 found the quota pools built but never
wired (QuotaPools.reserve/report had zero production callers — watermarks and
cooldown only fed the matrix view); TRUST-001 found three of the five goal
segments real with all three acceptance lines held, the gaps being source
verification, a trust graph, and auto-collection.

Then two implementation agents landed, disjoint file scopes, integrated here:
- MODEL-001: scheduler now reserves before dispatch and reports terminal
  state for models that have a pool (unregistered models keep legacy behavior
  bit-for-bit, tested); reroute consumes pool health order; PoolState tracks
  rolling latency, error rate and call counts; create_app shares one pools
  instance between dispatch and the matrix API. Nine new stub-adapter tests,
  zero network.
- TRUST-001: evidence-level human source verification records
  (trust_source_verifications, canonical+SHA-256, revision optimistic lock,
  human_writer gate — machine keys 403), TRUST_EVIDENCE_VERIFIED events, and
  a per-owner source registry (trust_sources) surfaced as read-only
  source-trust tiers and per-claim distributions. Annotation only: verdicts,
  acceptance blockers and the three confidence=None sites are untouched, the
  never-fetches rule is pinned by a socket/urllib-blocking test, and the
  registry PK is (owner_id, source_key) to keep cross-owner tiers from
  leaking.

Full suite: 1376 passed / 8 skipped / 0 failed. Registry next_actions for
both tasks updated and rendered; next in queue — matrix UI columns +
watermark editing, a live-probe marker for real-connection tests, event-side
auto-collection of runtime evidence, multi-key pools (separate item).

## Four-agent round: pools UI, auto-collection, skill lifecycle, routing audit trail (2026-10-16)

Four parallel implementation agents (disjoint file scopes) integrated on one
tree, plus two integration stitches by the coordinator:
- MODEL-001 ④⑤: matrix UI latency/error-rate/call-count columns and
  watermark editing (PUT /modelmatrix/{id}/limits, admin-gated); @pytest.mark.live
  separates real-upstream probes (FACAI_LIVE_API_KEY, honest skip without it)
  from the default suite.
- TRUST-001 ③④: RUNTIME_TYPES events auto-collect as RUNTIME_EVENT evidence
  in the same transaction (idempotent, failure never blocks dispatch,
  FACAI_TRUST_AUTO_COLLECT default on, system:auto-collect attribution);
  artifact drift outside snapshots annotated + TRUST_ARTIFACT_DRIFT audit
  events, acceptance-blocker semantics untouched.
- SKILL-001 ①②③: version persisted (no fake backfill), disabled skills
  filtered from scoring/execution/dispatch (scheduler stitch by coordinator),
  soft-delete tombstones with enable/remove/audit endpoints + boundary journal.
- ROUTER-001 ①②③: ASSIGNMENT_RECORDED persists the candidate/score/exclusion
  snapshot (survives restarts), BINDING_CHANGED carries reason (legacy None
  compatible), AGENT_OVERRIDDEN + gold banner closes the goose-displayed/
  opencode-executed silent gap.
- Coordinator stitches: test tail assertions now tolerate the auto-collect
  event (test_opencode_execution, test_phase1); scheduler dispatch applies
  dispatchable_skill.

Full suite: 1421 passed / 9 skipped / 0 failed. Handoff back to Codex from
here; next queue — skill usage aggregation, version-aware updates, DISPUTED
adjudication exit, conversion fallback events, multi-key pools.

## Measured mission progress increment (2026-10-21)

The mission API now adds `progress_measured` (two decimal places, completed
steps / total steps) and `progress_basis` (unit, completed, total and active
unmeasured count). Each step also exposes `progress_measured`; active/failed
steps with no runtime work denominator return null. The desktop console uses
these measured values and says “未计量” instead of claiming 1% or 90% for an
active step. The legacy integer `progress` fields remain for older clients.
This proves only step-completion measurement. Agent work-unit measurement,
20+ concurrency, and full A10 acceptance remain open. Focused API and real
Chromium checks are run for this increment; see the latest commit/checkpoint
for final validation status.
Full-suite attempt: 1448 passed / 7 skipped / 1 failed (old theme assertion
expected integer 33 and active 1). After updating only those expectations,
real Chromium focused retest of theme/progress/console: 31 passed / 0 failed.
A second complete suite was not run; full regression status is partial.

The second complete suite on commit 7512c4d passed: 1449 passed / 7 skipped /
0 failed in 202.25 seconds with real Chromium. External acceptance remains open.

## 7×24 VPS workstation integration plan (2026-10-27)

Fast-forwarded clean local tree from 08097ae to GitHub main 411b125, then mapped
existing systemd/nginx/SQLite/worker/model/backup paths into
`docs/VPS_24X7_WORKSTATION.md`. This is a source-backed deployment plan only:
VPS last documented deployment was 578a3c5, and this environment's HTTPS
CONNECT was denied by its proxy (403), so current live SHA/health was not
verified. No production service or credentials were changed. Handoff registry
render and validation passed for 27 directions; application tests were not
rerun because runtime code was unchanged.

## Workstation storage and routing baseline (2026-10-28)

Compared latest facai-latest-source-chatgpt main 2c3aadf with old facai-agent
master e4f74eb (snapshot 17a1d5a). ZCode's own local data is not mounted in
this environment; no ZCode record or private storage was inspected. The FACAI
SQLite Store and TrustStore remain the source of truth. New source-backed
inventory/configuration and acceptance order: docs/WORKSTATION_ROUTING_STORAGE.md.
QuotaPools.ranked now sinks pools at the concurrency watermark behind pools
that can actually reserve even when the saturated pool has more minute budget;
reserve remains authoritative. Focused quota/router regression: 31 passed.
Production route quality optimization and VPS/Provider acceptance remain open.
Full worktree regression on commit 0924836 with real Chromium:
1479 passed / 7 skipped / 0 failed in 222.95 seconds. This verifies the
routing increment against existing code, not real Provider/VPS performance.

## URL passive assessment upstream review (2026-10-28)

Cloned and inspected upstream Scrapy 5c7886a (BSD-3-Clause), current MDN HTTP
Observatory 2776308 (MPL-2.0), and the deprecated Mozilla predecessor
8b59e6e (MPL-2.0). Selection, source provenance, FACAI integration points,
claim/report access and bounded passive acceptance are recorded in
`docs/PASSIVE_SITE_ASSESSMENT_UPSTREAM.md`. This is an upstream review and
implementation contract, not an implemented scanner; no live target was
scanned and no third-party source was copied into FACAI.
