# First-run Setup Wizard — local appliance

New local installs launched with start.bat/start.sh open `/setup` with a random
bootstrap token in the URL fragment. Fragments are not HTTP request paths. The
page immediately removes it from the visible URL and keeps it only in page
memory. Reloading before initialization requires relaunching the launcher; each
launcher call rotates the token. `--no-browser` does not print bootstrap secrets;
use normal startup for first initialization, or supply existing environment auth.

## Owner boundary

Initialization is allowed only when all of these hold:

- The launcher supplied a valid bootstrap token and explicitly selected the
  private local-owner.json under this application's data root.
- That file marks this new installation pending.
- The peer is loopback, the Host is localhost/127.0.0.1/::1, no forwarding headers
  are present, and POST Origin exactly matches the local origin.
- The database has no missions or registered platform users to overwrite.
- Username/password pass validation; existing login throttle also protects tokens.

The endpoint writes a salted PBKDF2 password hash and a recovery-key hash to a
0600 file via atomic replacement. The initial random plaintext password is
removed. It revokes bootstrap-era sessions, changes the live operator identity,
and consumes bootstrap by persisting setup_complete and removing its token.
On subsequent startup, the launcher loads the Owner name and password hash;
AuthManager already supports the same PBKDF2 format. This local path is designed
for the launcher's single uvicorn process, not multi-worker bootstrap deployment.
Unix permissions are enforced; Windows users must protect the data directory ACL.

Externally configured FACAI_ADMIN_PASSWORD/FACAI_USERS_JSON takes precedence;
existing local-owner files without pending metadata retain their existing login.
Docker/VPS environment-managed accounts are not replaced through this endpoint.
No middleware-wide authentication bypass is introduced: only the static setup
page and two purpose-specific bootstrap/recovery endpoints are public, and the
latter independently enforce the loopback/same-origin secret boundary.

## Recovery

The random recovery key is returned once, displayed masked and can be copied to
the user's password manager. It is not written raw to disk, logs or session
storage. It is a **local login recovery key**, not a Runtime grant or a second
root authority. `/setup` can use it to rotate the Owner password on the same
computer; recovery revokes Owner login sessions and rotates the recovery key.
Existing scoped API keys and Runtime grants are explicitly unchanged. Invalid
keys are throttled. Old recovery keys no longer work after successful rotation.
Save the new key before leaving the page. File-write failure can revoke old
sessions but leaves the old login configuration intact; relogin/retry is safe.

## Configuration and truthful checks

After Owner login, `/setup` detects Python, optional Rust compiler/Docker CLI,
SQLite read access, actual temporary-file storage read/write, and OpenCode/Goose
CLI presence. It does not call these executables, run containers or claim a task
has executed. `/status` exposes these protected operator-only checks; `/health`
is a minimal public alias to the existing health endpoint. `/metrics` is
authenticated and reports the operator's persisted mission/event gauges.

OpenAI/DeepSeek/Custom configuration reuses the existing encrypted user-model
registration and public-endpoint SSRF validation. Local Ollama is registered only
by the authenticated operator, at the fixed loopback endpoint, without borrowing
any operator secret or weakening tenant endpoint rules.

Saving a model does not probe it. Only an explicit button submits a small model
request and may incur provider charges. Classified probe results and timestamps
are persisted in setup_model_probes; a configuration/credential fingerprint change
invalidates the previous result. A past successful probe does not certify future
availability. Missing runtime/model checks remain visible; the operator must
explicitly acknowledge them to save incomplete setup progress. Completion of
setup never sets production_ready or skips mission permissions/budgets/quotas.

## Validation

20 targeted setup/launcher/Intent tests passed. Expanded auth/isolation/Trust
regression: 87 passed. Real Chromium desktop1440×1000/mobile390×844 passed creation,
one-time recovery-key display, Owner login, diagnostics, local model registration,
explicit acknowledgment and reload. No real provider was called. Existing desktop
and phone console first-screen/layout checks passed separately (2 checks).

Remaining acceptance: Windows/macOS native launcher and ACL/browser handoff,
Docker/VPS real model/Runtime configuration, full signed execution and cross-device
HTTPS flow. This is a development release, not completed Production v1.0.
