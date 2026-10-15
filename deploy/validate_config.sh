#!/usr/bin/env bash
# Validate the deployment plane without a live server.
#
#   ./deploy/validate_config.sh
#
# Checks:
#   * every deploy/*.sh passes `bash -n`;
#   * the HTTP bootstrap needs no certificate and exposes no 443/ssl_certificate;
#   * the TLS config references the cert and does not set includeSubDomains;
#   * the TLS config declares limit_req_zone in the http context (outside any
#     server block), applies the limit and allows data: images in the CSP;
#   * deploy.sh gates TLS on `has_cert`, scopes rsync --delete, backs up,
#     refuses a partial source tree / self-copy and installs every unit;
#   * the env template declares FACAI_BASIC_READONLY;
#   * if nginx (and openssl) are installed, both configs pass `nginx -t`.
#
# Exits non-zero if any check fails so it can gate a release.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DOMAIN="${FACAI_DOMAIN:-whstapp.lol}"
FAIL=0

pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*"; FAIL=1; }
skip() { printf 'SKIP: %s\n' "$*"; }
# Ignore comment lines so explanatory text cannot satisfy or break a check.
# Consume the whole pipe: grep -q can SIGPIPE its producer under pipefail,
# intermittently treating a real match as a failed deployment check.
strip_comments() { grep -v -E '^[[:space:]]*#' "$1"; }
contains() { strip_comments "$1" | grep -- "$2" >/dev/null; }
not_contains() { ! strip_comments "$1" | grep -- "$2" >/dev/null; }
contains_fixed() { strip_comments "$1" | grep -F -- "$2" >/dev/null; }
not_contains_fixed() { ! strip_comments "$1" | grep -F -- "$2" >/dev/null; }

# --- shell syntax -----------------------------------------------------------
for sh in "${ROOT}"/deploy/*.sh; do
  [ -f "$sh" ] || continue
  if bash -n "$sh"; then pass "bash -n $(basename "$sh")"; else fail "bash -n $(basename "$sh")"; fi
done

HTTP_CONF="${ROOT}/deploy/nginx/facai-cloud.http.conf"
TLS_CONF="${ROOT}/deploy/nginx/facai-cloud.conf"
DEPLOY="${ROOT}/deploy/deploy.sh"

# --- HTTP bootstrap must not require a certificate --------------------------
[ -f "$HTTP_CONF" ] && pass "HTTP bootstrap exists" || fail "HTTP bootstrap missing"
contains "$HTTP_CONF" 'listen 80' && pass "bootstrap listens on 80" || fail "bootstrap missing listen 80"
contains "$HTTP_CONF" '.well-known/acme-challenge' && pass "bootstrap serves the ACME challenge" || fail "bootstrap missing ACME location"
not_contains "$HTTP_CONF" 'listen 443' && pass "bootstrap has no 443 server" || fail "bootstrap unexpectedly has 443"
not_contains "$HTTP_CONF" 'ssl_certificate' && pass "bootstrap references no certificate" || fail "bootstrap references a certificate path"

# --- TLS config -------------------------------------------------------------
contains "$TLS_CONF" 'ssl_certificate.*/etc/letsencrypt/live/' && pass "TLS config references the issued certificate" || fail "TLS config missing ssl_certificate"
contains "$TLS_CONF" 'Strict-Transport-Security' && pass "TLS config sets HSTS" || fail "TLS config missing HSTS"
not_contains "$TLS_CONF" 'includeSubDomains' && pass "HSTS omits includeSubDomains" || fail "HSTS sets includeSubDomains"

# limit_req_zone is only valid in the http context: moved inside a server{}
# block it fails `nginx -t` on the server, and a host without nginx would
# never catch it (the real nginx -t pass at the bottom is optional and SKIPped
# there). Statically require the declaration to sit above the first server.
tls_code="$(strip_comments "$TLS_CONF")"
after_first_server="$(printf '%s\n' "$tls_code" | sed -n '/^[[:space:]]*server[[:space:]]*{/,$p')"
if printf '%s\n' "$tls_code" | grep -q 'limit_req_zone' \
  && ! printf '%s\n' "$after_first_server" | grep -q 'limit_req_zone'; then
  pass "limit_req_zone stays in the http context (outside server blocks)"
else
  fail "limit_req_zone missing or declared inside a server block"
fi
contains "$TLS_CONF" 'limit_req zone=' \
  && pass "TLS config applies the edge rate limit" || fail "TLS config never applies the rate limit"
contains_fixed "$TLS_CONF" "img-src 'self' data:" \
  && pass "CSP allows data: images (WeChat QR code)" || fail "CSP missing img-src data:"

# --- deploy script gating and safe rsync ------------------------------------
contains "$DEPLOY" 'facai-cloud.http.conf' && pass "deploy installs the HTTP bootstrap" || fail "deploy never installs the bootstrap"
contains "$DEPLOY" 'has_cert' && pass "deploy gates TLS on has_cert" || fail "deploy missing has_cert gate"
contains "$DEPLOY" '--delete' && pass "deploy uses scoped rsync --delete" || fail "deploy missing scoped delete"
contains "$DEPLOY" "\-\-exclude '/\*'" && pass "deploy protects unknown top-level entries" || fail "deploy can delete unknown top-level entries"
contains "$DEPLOY" 'P data' && pass "deploy protects the data directory" || fail "deploy does not protect data"
contains "$DEPLOY" 'backup_app_dir' && pass "deploy backs up before syncing" || fail "deploy has no pre-sync backup"
contains "$DEPLOY" 'cmd_rollback' && pass "deploy supports rollback" || fail "deploy has no rollback"
# `rsync --delete` must never run against a partial source tree or the live
# app dir itself: either turns a bad bundle into a wiped production tree.
contains "$DEPLOY" 'facai/api.py' \
  && pass "deploy gates rsync on a complete source tree" || fail "deploy can rsync --delete from a partial tree"
contains_fixed "$DEPLOY" '"${SRC_DIR}" = "${APP_DIR}"' \
  && pass "deploy refuses to rsync the app dir onto itself" || fail "deploy lacks the self-copy guard"

# The deploy chain must install every unit that ships with the code, so the
# hardened unattended units cannot silently drift from the repo copies.
contains_fixed "$DEPLOY" '/deploy/facai-*.service' \
  && pass "deploy installs the unattended service units" || fail "deploy installs only facai-cloud.service"
contains_fixed "$DEPLOY" '/deploy/facai-*.timer' \
  && pass "deploy installs the timer units" || fail "deploy never installs the timers"
contains "$DEPLOY" 'daemon-reload' \
  && pass "deploy reloads systemd after installing units" || fail "deploy skips daemon-reload"
for unit in facai-backup.service facai-backup.timer facai-digest.service facai-digest.timer facai-tron-sync.service; do
  [ -f "${ROOT}/deploy/${unit}" ] \
    && pass "unattended unit present: ${unit}" || fail "unattended unit missing from deploy/: ${unit}"
done

# Every managed `--include '/path'` must exist, or rsync silently skips it.
while IFS= read -r entry; do
  [ -n "$entry" ] || continue
  rel="${entry##--include \'/}"
  rel="${rel%\'}"
  rel="${rel%%/\*\**}"
  rel="${rel#/}"
  rel="${rel%/}"
  if [ -e "${ROOT}/${rel}" ]; then
    pass "managed path exists: ${entry}"
  else
    fail "managed path is missing from the checkout: ${entry}"
  fi
done < <(strip_comments "$DEPLOY" | grep -oE -- "--include '/[^']+'" || true)

# --- OpenCode install and service environment -------------------------------
OPENCODE_SH="${ROOT}/deploy/install-opencode.sh"
SERVICE_UNIT="${ROOT}/deploy/systemd/facai-cloud.service"
[ -f "$OPENCODE_SH" ] && pass "OpenCode installer exists" || fail "OpenCode installer missing"
[ -f "$SERVICE_UNIT" ] && pass "systemd unit exists" || fail "systemd unit missing"
# Pinned version and hash: an unverified download must never be installed.
contains "$OPENCODE_SH" 'OPENCODE_VERSION=' && pass "installer pins a version" || fail "installer pins no version"
contains "$OPENCODE_SH" 'sha256sum' && pass "installer verifies a checksum" || fail "installer verifies no checksum"
contains "$OPENCODE_SH" 'SHA-256 mismatch' && pass "installer rejects a bad checksum" || fail "installer does not reject a bad checksum"
contains "$DEPLOY" 'cmd_opencode' && pass "deploy exposes an opencode action" || fail "deploy has no opencode action"
# The unit must place OpenCode on PATH and give it a writable HOME.
contains "$SERVICE_UNIT" 'Environment=PATH=' && pass "unit sets PATH" || fail "unit does not set PATH"
contains "$SERVICE_UNIT" 'Environment=HOME=' && pass "unit sets a service HOME" || fail "unit does not set HOME"
contains "$SERVICE_UNIT" 'ReadWritePaths=/opt/facai-cloud/data' && pass "unit keeps writes inside data" || fail "unit write path is missing"

# `update` must re-apply the unit and proxy config, not just the code.
update_body="$(awk '/^cmd_update\(\)/,/^}/' "$DEPLOY")"
contains <(printf '%s\n' "$update_body") 'install_systemd' \
  && pass "update reinstalls the systemd unit" || fail "update skips the systemd unit"
contains <(printf '%s\n' "$update_body") 'install_nginx' \
  && pass "update reinstalls the nginx config" || fail "update skips the nginx config"

# --- classified provider credentials ----------------------------------------
APP_ENV_EXAMPLE="${ROOT}/deploy/facai.env.example"
PROVIDER_EXAMPLE="${ROOT}/deploy/providers/deepseek.env.example"
[ -f "$PROVIDER_EXAMPLE" ] && pass "provider env template exists" || fail "provider env template missing"
contains "$PROVIDER_EXAMPLE" 'DEEPSEEK_API_KEY=' \
  && pass "provider template declares DEEPSEEK_API_KEY" || fail "provider template missing the key name"
# A committed template must never carry a value after '='.
if strip_comments "$PROVIDER_EXAMPLE" | grep -qE 'DEEPSEEK_API_KEY=.+'; then
  fail "provider template contains a credential value"
else
  pass "provider template keeps the value empty"
fi
not_contains "$APP_ENV_EXAMPLE" 'DEEPSEEK_API_KEY' \
  && pass "app env template no longer holds provider keys" || fail "app env template still holds provider keys"
# The Basic read-only gate must ship in the template: without the variable the
# operator cannot see (and flip) the cross-site-POST protection at all.
contains "$APP_ENV_EXAMPLE" 'FACAI_BASIC_READONLY=' \
  && pass "app env template declares FACAI_BASIC_READONLY" || fail "app env template missing FACAI_BASIC_READONLY"
# Unused provider credentials must not be pulled into the app env.
for name in TAVILY BOCHA AGNES ANTHROPIC CLAUDE POLLINATIONS; do
  not_contains "$APP_ENV_EXAMPLE" "$name" \
    && pass "app template ignores unused provider ${name}" || fail "app template references unused provider ${name}"
done
# The unit must load the app env and the classified provider env directory.
contains_fixed "$SERVICE_UNIT" 'EnvironmentFile=/etc/facai-cloud/facai.env' \
  && pass "unit loads the app env" || fail "unit missing the app env file"
contains_fixed "$SERVICE_UNIT" 'EnvironmentFile=-/etc/facai-cloud/providers/*.env' \
  && pass "unit loads the classified provider env" || fail "unit missing the classified provider env"
# deploy.sh must own the split and the one-time, backed-up migration.
contains "$DEPLOY" 'PROVIDER_ENV_DIR' && pass "deploy defines the provider env dir" || fail "deploy has no provider env dir"
contains "$DEPLOY" 'install_provider_env_files' \
  && pass "deploy seeds classified provider files" || fail "deploy does not seed provider files"
contains "$DEPLOY" 'classify_deepseek_env' \
  && pass "deploy migrates the DeepSeek key out of the app env" || fail "deploy lacks the key migration"
contains "$DEPLOY" 'backup_env_dir' \
  && pass "deploy backs up the config before migrating" || fail "deploy migrates without a config backup"
for phase in install update; do
  body="$(awk "/^cmd_${phase}\\(\\)/,/^}/" "$DEPLOY")"
  contains <(printf '%s\n' "$body") 'install_provider_env_files' \
    && pass "${phase} seeds classified provider files" || fail "${phase} skips provider files"
  contains <(printf '%s\n' "$body") 'classify_deepseek_env' \
    && pass "${phase} preserves the key classification" || fail "${phase} skips the classification"
done

# --- real nginx parse (optional) -------------------------------------------
if command -v nginx >/dev/null 2>&1; then
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  mkdir -p "$tmp/logs" "$tmp/certs"

  cat > "$tmp/nginx-http.conf" <<EOF
pid ${tmp}/logs/nginx.pid;
error_log ${tmp}/logs/error.log;
events {}
http {
    access_log off;
    client_body_temp_path ${tmp}/client_temp;
    proxy_temp_path ${tmp}/proxy_temp;
    fastcgi_temp_path ${tmp}/fastcgi_temp;
    uwsgi_temp_path ${tmp}/uwsgi_temp;
    scgi_temp_path ${tmp}/scgi_temp;
    include ${HTTP_CONF};
}
EOF
  if nginx -t -c "$tmp/nginx-http.conf" -p "$tmp" >"$tmp/http-check.log" 2>&1; then
    pass "nginx -t accepts the HTTP bootstrap"
  else
    cat "$tmp/http-check.log"
    fail "nginx -t rejects the HTTP bootstrap"
  fi

  if command -v openssl >/dev/null 2>&1; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
      -keyout "$tmp/certs/privkey.pem" -out "$tmp/certs/fullchain.pem" \
      -subj "/CN=${DOMAIN}" >/dev/null 2>&1
    sed -E "s#/etc/letsencrypt/live/[^/]+/#${tmp}/certs/#g" "$TLS_CONF" > "$tmp/tls.conf"
    cat > "$tmp/nginx-tls.conf" <<EOF
pid ${tmp}/logs/nginx.pid;
error_log ${tmp}/logs/error.log;
events {}
http {
    access_log off;
    client_body_temp_path ${tmp}/client_temp;
    proxy_temp_path ${tmp}/proxy_temp;
    fastcgi_temp_path ${tmp}/fastcgi_temp;
    uwsgi_temp_path ${tmp}/uwsgi_temp;
    scgi_temp_path ${tmp}/scgi_temp;
    include ${tmp}/tls.conf;
}
EOF
    if nginx -t -c "$tmp/nginx-tls.conf" -p "$tmp" >"$tmp/tls-check.log" 2>&1; then
      pass "nginx -t accepts the TLS config (self-signed cert)"
    else
      cat "$tmp/tls-check.log"
      fail "nginx -t rejects the TLS config"
    fi
  else
    skip "openssl not available; TLS nginx -t check skipped"
  fi
else
  skip "nginx not installed; nginx -t checks skipped"
fi

printf '\n== validate_config summary ==\n'
if [ "$FAIL" = 0 ]; then printf 'OK\n'; else printf 'FAILED\n'; fi
exit "$FAIL"
