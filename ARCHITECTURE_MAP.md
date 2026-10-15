# Architecture map (source inspection)

Human/UI/channel → prompting + intent → missions/store → authority_gate → scheduler
→ router/quota → OpenCode/Goose/gateway → events/checkpoints/costs/artifacts → evidence.

Control: auth + authority + authority_gate; persistence: store + secretbox + backup.
UI: static/index.html, command-center.html, conversion.html, mobile.html.
Deployment: deploy/systemd + nginx; desktop packaging: packaging/app (remote shell).

Gaps and implementation order: FACAI_CURRENT_STATE_REPORT.md and DEVELOPMENT_PLAN.md.
