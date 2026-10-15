# Knowledge Memory — explicit, source-bound curation

Open `/knowledge` to search the current owner's curated proof snapshots. From
`/evidence-center`, first review and accept the current eligible proof; then enter
a title/tags and choose “收录为知识资产”. This is a separate explicit Owner action.

## Guarantees

- No automatic ingestion of unexecuted, unreviewed, disputed or stale proofs.
- A unique proof is stored once, including under concurrent retries.
- The asset preserves source claim classifications, lifecycle/retraction metadata,
  actual review reasons, evidence hashes/references, task objective, Trace and
  the exact accepted proof hash. INFERENCE never becomes FACT by being stored.
- The stored document has its own SHA-256. Source proof integrity is checked when
  opening it. No source URL is fetched or server file recursively packaged.
- Owner-scoped parameterized search covers title, claim statements and tags;
  wildcard characters are literal and results are paginated.
- Lists describe accepted historical snapshots, not current truth. Opening an
  asset recomputes source-current status. Later runtime/evidence/claim/artifact
  changes mark the source historical without rewriting the original asset.
- Human Owner session is required for curation; API keys cannot masquerade as
  human acceptance. Existing distill/skill behavior is unchanged.

`knowledge_assets` is an additive SQLite table linked to mission and proof IDs.
The record's source acceptance is the human decision at curation time, not an
independent scientific or external-source certification. There is no embedding,
automatic retrieval into runtime prompts, or implicit skill execution here.
Those remain future, separately authorized capabilities.

## API

- `GET /api/v1/knowledge?q=&limit=50&offset=0`
- `GET /api/v1/knowledge/{id}`
- `POST /api/v1/missions/{mission_id}/trust/proofs/{proof_id}/knowledge`
  with `sha256`, `title` and bounded `tags`.

Proof ZIP remains downloadable through its authenticated source route. An asset
does not confer access to another owner or change mission permissions.

## Validation

Four real SQLite checks cover acceptance/staleness, restart, literal search,
source classification, tenant isolation, concurrent idempotency and tamper
detection. Desktop/mobile real Chromium tests cover the full UI chain using
explicit synthetic execution records: intent → evidence → review → proof → Owner
acceptance → knowledge curation → search/source view. This is not a live CLI or
provider run. Default UNKNOWN and all production acceptance gaps are retained.
