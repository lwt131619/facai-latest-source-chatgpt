# Active checkpoint — Mission specification and Trust v1

## WIP checkpoint (not a release)

The user requested immediate, frequent GitHub WIP checkpoints to prevent loss.
The first checkpoint was untested. Subsequent targeted validation: existing
Intent and legacy Proof tests **13 passed**, syntax compilation passed.
New Trust workflow tests are being run; not yet claimed passing. Do not interpret committed or
uploaded code as production acceptance. Existing AGENTS.md and frozen invariants
continue to apply.

Implemented but awaiting verification:
- Immutable Mission Specification, written in the intent-confirmation transaction.
- Additive owner-scoped evidence/claim/review storage and optimistic revisions.
- Contextual conflict classification, unknown-by-default conclusions.
- Immutable proof snapshots, bounded artifact hashing, independent Owner acceptance.
- New `/evidence-center` and `/api/v1/missions/{id}/trust` API family.

Next actions:
1. Add and run transaction, owner-isolation, session-only review, conflict,
   snapshot-staleness, artifact-integrity and ZIP-manifest tests.
2. Connect console entry links and run real desktop/mobile browser checks.
3. Run the complete regression suite; update task registry and handoff.
4. Refresh development package and resumable bundle.

Environment dependencies installed in `/workspace/facai-venv`; use its Python.
No real provider execution or VPS deployment has been performed in this stage.

Canonical GitHub repository is `lwt131619/facai-latest-source-chatgpt`.
Git push through the inherited GitHub integration was verified for c0271fc;
old 403 statements in older handoff files are historical and need correction.
Never print or persist credentials; no force push is needed for these checkpoints.
