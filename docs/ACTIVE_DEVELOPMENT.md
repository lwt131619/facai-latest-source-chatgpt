# Active checkpoint — Mission specification and Trust v1

## WIP checkpoint (not a release)

Local-appliance Owner bootstrap/recovery and /setup are now wired and locally
tested: setup+launcher+Intent checks **20 passed**, expanded auth/isolation/Trust
checks **87 passed**. Real desktop/mobile Setup and Evidence flows **4 passed**.
Existing deployments keep environment auth; fresh local installs get a one-use
loopback bootstrap URL fragment. The Owner password is hashed; recovery rotates
its one-use key and invalidates Owner sessions. No live Provider call was made.
Final complete regression and package refresh are still pending.

The user requested immediate, frequent GitHub WIP checkpoints to prevent loss.
The first checkpoint was untested. Subsequent targeted validation: existing
Intent and legacy Proof tests **13 passed**, syntax compilation passed.
New Trust/Intent/legacy Proof targeted tests: **27 passed**. Do not interpret committed or
uploaded code as production acceptance. Existing AGENTS.md and frozen invariants
continue to apply.

Implemented but awaiting verification:
- Immutable Mission Specification, written in the intent-confirmation transaction.
- Additive owner-scoped evidence/claim/review storage and optimistic revisions.
- Contextual conflict classification, unknown-by-default conclusions.
- Immutable proof snapshots, bounded artifact hashing, independent Owner acceptance.
- New `/evidence-center` and `/api/v1/missions/{id}/trust` API family.

Verified in synthetic local execution fixtures: owner isolation, session-only
review (automation keys cannot review), optimistic concurrency, rollback,
contextual conflicts, proof hash/staleness, restart persistence, bounded
mission-local artifact hashes, ZIP manifest and legacy compatibility.
This is not live external execution evidence or a certified truth engine.

Next actions:
1. Expand edge-case validation and review execution-evidence limitations.
2. Entry links are connected; real Chromium desktop 1440×1000 and mobile 390×844 flow: **2 passed** (login → intent → confirmation → evidence → explicit review → snapshot ZIP → reload). No JS errors or horizontal overflow.
3. Run the complete regression suite; update task registry and handoff.
4. Refresh development package and resumable bundle.

Environment dependencies installed in `/workspace/facai-venv`; use its Python.
No real provider execution or VPS deployment has been performed in this stage.

Canonical GitHub repository is `lwt131619/facai-latest-source-chatgpt`.
Git push through the inherited GitHub integration was verified for c0271fc;
old 403 statements in older handoff files are historical and need correction.
Never print or persist credentials; no force push is needed for these checkpoints.
