# Active development checkpoint

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
