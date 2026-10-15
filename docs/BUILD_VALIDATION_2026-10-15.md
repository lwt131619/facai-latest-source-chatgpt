# Local build validation — 2026-10-15

Source baseline: public snapshot 933b431, not independently equated to private dc6208c.

- Final complete regression: **1259 passed, 28 skipped, 1 warning**, 84.52 seconds.
  Skips include unavailable browser tooling and Windows deployment helpers.
  Warning: upstream Starlette/httpx deprecation. No claim of native Windows/macOS QA.
- Initial baseline had 8 failures / 11 errors. Restored deploy script executable
  mode; scoped three fixture modules' model endpoint DNS to public test answers;
  fixed deployment validator grep/pipefail SIGPIPE race. Production SSRF validation
  was not disabled. Registered raw_budget pytest marker.
- New launcher credentials/restart/override/private-mode tests passed (3 cases).
- Actual native launcher: first start, HTTP health, admin login, shutdown, restart
  and retained password passed using isolated temporary data.
- Compose config, shell syntax, Python compile and diff whitespace checks passed.
- Docker image built with proxy CA secret mount and no TLS bypass. Source copied
  from this workspace needed readable container modes; Dockerfile now ensures those
  while keeping application code root-owned and data owned by UID10001.
- Development ZIP uses an explicit source allowlist; excludes data, credentials,
  virtualenv, Git history and archives; file hashes and ZIP checksum verified.
- GitHub push dry-run failed 403 (permission denied to lwt131619). No remote push,
  workflow execution, Release or production VPS deployment has been claimed.

Remaining production acceptance: setup wizard integration; full intent contract
and confirmation path; contextual evidence/conflict/confidence and proof acceptance;
20+ agent load/recovery; real model/runtime configuration; mobile/desktop acceptance;
native platform installers and externally reachable HTTPS deployment.

Docker runtime acceptance passed: API health, UID10001, authenticated login, restart,
and the same authenticated session restored from the persistent database volume.
Test containers and volumes were removed. Live provider/agent execution was not used.
