# Evidence / Claim / Proof v1 — implemented scope

This is an additive, persistent workflow, not a certified truth engine. Existing
`GET /api/v1/missions/{id}/proof` and its `verified` field remain unchanged.

## Task contract

New confirmations write `mission_specifications` in the same SQLite transaction
as mission creation and intent association. A failure rolls everything back and
removes the new workspace. The contract preserves raw text, explicit interpreted
objective/constraints, confirmed revision, actual step IDs/dependencies and Trace.
Expected result format stays unspecified when the user did not supply one.
The deterministic five-stage scaffold is a process plan, not completed work.
Existing missions are not retroactively represented as confirmed specifications.

## Operator flow

Open `/workspace` and confirm the understood request, then use the existing mission
console for independent authorization, binding and execution. `/evidence-center`
lists the same owner's missions and displays:

1. The immutable confirmed task contract.
2. Owner-submitted source text, or references to supported persisted runtime events.
3. Explicitly normalized subject/predicate/value claims with time, scope,
   environment and definition contexts, and linked evidence.
4. Separate Owner review and its reason; FACT/INFERENCE/UNKNOWN are human review
   decisions, not automatically calibrated or externally certified conclusions.
5. Contextual conflict results and review-required unknown contexts.
6. Versioned Proof snapshots, downloadable ZIP and separate Owner acceptance.

Sources are never fetched implicitly. An address or local filename in a note does
not cause SSRF or file reading. Event collection imports safe event metadata only,
never raw event payloads. These references attest to persisted records, not to an
external executable's authenticity. Existing event-write/step-edit APIs allow
operators to maintain records; this layer does not replace them with signed tool
attestation. A future trusted collector must preserve this distinction.

## Conflict semantics

Comparison requires the same explicitly normalized subject and predicate and
different values. Explicit differing contexts produce TEMPORAL, SCOPE (including
environment), or SEMANTIC. The result retains all differing dimensions. A
DIRECT_CONFLICT requires all four context dimensions explicitly equal. Otherwise
the pair is REVIEW, rather than guessing a contradiction. DIRECT_CONFLICT and
REVIEW make the linked conclusions DISPUTED and prevent acceptance. This is not
an NLP synonym, time-interval or universal semantic inference engine.

## Proof and acceptance

Proof creation is allowed while incomplete; its verification file contains the
actual blockers. Snapshot creation never authorizes, dispatches, completes a task
or accepts a result. Acceptance requires:

- A confirmed task specification.
- Completed mission and steps, with a collected completion-event reference for
  every actual step.
- At least one claim; all current claims explicitly reviewed as FACT or INFERENCE
  and not disputed.
- Registered runtime output files, if any, are bounded mission-local regular files
  with computable SHA-256 hashes.
- The supplied proof hash and the current snapshot token match.
- A real Owner console session and a non-empty acceptance reason.

Automation keys can submit records but cannot act as human reviewer or acceptor.
Basic auth also cannot perform human review while authentication is enabled.
All data is checked against the actual current owner, including reads and exports;
administrator assist does not impersonate a customer's Owner acceptance.

The fingerprint binds trust revision/content, task contract, mission status and
bindings, step states, non-Trust event metadata/payload hashes, cost records and
runtime artifact bytes. Trust audit events do not invalidate their own snapshot.
Later changes make an earlier acceptance historical (`acceptance_current=false`).
Filesystem hashes describe bytes at observation time; they do not lock a running
runtime's filesystem. Full external tool attestation and independent semantic
verification remain future work. Confidence remains `null` without calibration.

ZIP contains report.md, evidence.json, claims.json, verification.json, trace.json,
specification.json, metadata.json, proof.json and manifest.json with per-file hashes.
Only explicit proof data is included; it does not recursively package workspace
files, databases, environment files or runtime output contents. User-submitted
evidence is intentionally exported, so do not submit secrets as evidence.

## API and persistence

All new resources inherit existing authentication and CSRF middleware:

- `GET /api/v1/missions/{id}/specification`
- `GET /api/v1/missions/{id}/trust`
- `POST .../trust/evidence`
- `POST .../trust/claims`
- `POST .../trust/claims/{claim_id}/reviews`
- `POST .../trust/proofs`
- `POST .../trust/proofs/{proof_id}/accept`
- `GET .../trust/proofs/{proof_id}/download`

Evidence/claim/review writes require the current `expected_revision` and use
`BEGIN IMMEDIATE`; state and audit event commit together. Evidence records and
reviews are append-only. Limits per mission: 1,000 evidence records, 200 claims,
2,000 reviews, 100 snapshots; artifact hashing is limited to 256 outputs,
8 MiB per file and 32 MiB total. Limit/unavailable output hashes block acceptance.

Tables are additive: mission_specifications, trust_versions, trust_evidence,
trust_claims, trust_reviews, trust_proofs, trust_acceptances. Existing Store schema
is preserved. Back up the SQLite database before deployment using the established
backup path. Code rollback leaves these unused tables intact; do not delete
evidence to make a rollback appear clean.

## Validation

27 targeted Trust/Intent/legacy Proof checks passed. Real Chromium desktop and
mobile workflow passed 2 checks, including authenticated login, intent revision,
confirmation, evidence, review, ZIP manifest validation and reload. Execution
completion in unit tests uses explicit synthetic fixtures, not paid model calls.
Complete regression at the Trust runtime checkpoint: 1282 passed / 28 skipped.
