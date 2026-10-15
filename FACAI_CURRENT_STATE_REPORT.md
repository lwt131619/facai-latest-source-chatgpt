# FACAI Current State — 2026-10-15

Audit baseline: public snapshot `933b431`, 350 tracked files. This is a new public
history; equivalence to private commit dc6208c has not been independently established.
This report supersedes historical deployment claims only for this local checkout.

## Current Architecture
Python FastAPI (`facai/api.py`) serves static desktop/mobile consoles and a SQLite
Store. Scheduler runs durable jobs through OpenCode/Goose adapters. Registries bind
agents, models, skills and runtimes independently. Authority gate wraps execution;
provider gateway/router/quota pools handle upstream selection. Channel and n8n
integrations enter the same mission/event store. Electron currently loads a fixed
remote website rather than bundling a local backend.

## Implemented Features
Real source contains intent preservation objects, original/prompt revisions, mission
steps, scheduler leases, event timeline/SSE, tenant authentication, grant persistence,
model quota pools, skill import, artifacts, execution checkpoints, costs, encrypted
customer credentials, backup/restore, Linux systemd/nginx deployment and mobile UI.
These are source findings, not a claim that external services work in this environment.

## Missing Features
Root cross-platform one-click launch, local onboarding and Docker appliance packaging
are absent. Current IntentObject does not expose the full frozen action/target/purpose/
scope/constraints/confidence contract. Evidence currently assembles persistent rows;
its `verified` means terminal steps plus a cost row, not semantic truth verification.
Source trust graph, contextual conflict classification, confidence calibration and a
full proof acceptance gate require integration. Rust core is not present. 20-agent
end-to-end concurrency and resource exhaustion behavior remain unverified.

## Technical Debt
Large API/store modules, historical handoff documents with conflicting live-state
claims, broad dependency ranges and a fixed remote Electron URL. Do not delete these
modules or replace the existing core to address packaging gaps. No running VPS or
private repository has been inspected. Browser checks may skip without Playwright.

## Production Gap
Distinguish infra health, configured model/runtime availability and verified result.
Do not interpret `health: ready` or evidence completeness as product acceptance.
Native Windows/macOS, first-install networking, real credentials and real agent
execution require independent acceptance. Tests are being run against this checkout;
results will be recorded separately without copying the old 1278-pass claim.

## Migration Plan
1. Establish current regression baseline and preserve the existing API/database.
2. Add reversible loopback-only startup and documented Docker/local delivery.
3. Integrate structured intent fields additively, with explicit confirmation and existing authority.
4. Extend durable evidence/proof semantics without changing legacy verified meaning silently.
5. Validate real runtime/model configuration, concurrency, restart recovery and UI flow.
6. Package checksummed release, execute native acceptance, then label production-ready.

## Measured baseline and first delivery slice
Initial local run: 1237 passed, 28 skipped, 8 failed, 11 errors (193.44 seconds).
Failures: deploy/deploy.sh lost its Git executable bit; registration/storage tests
used real DNS for fake-key model endpoints. Fixes retain production SSRF checks,
use scoped fixture DNS, and restore the script mode. New launcher tests cover private
credential creation, restart retention and explicit credential overrides.
Scheduler constants are DEFAULT_CONCURRENCY=2, CONCURRENCY_LIMIT=8,
MAX_PARALLEL_PER_MISSION=3. A tested 20-agent implementation is still outstanding.
Added native launcher and single-service Docker packaging around the existing app;
no new fake runtime, database or evidence engine. Real loopback health/admin login
smoke passed. Native Windows/macOS and Docker container execution remain untested.

Final regression: 1259 passed, 28 skipped, 1 warning (84.52s). Docker image build,
UID10001 execution, login and session persistence across restart passed after fixing
source read permissions. Detailed evidence: docs/BUILD_VALIDATION_2026-10-15.md.
GitHub write dry-run returned 403; changes remain local until remote authorization works.
