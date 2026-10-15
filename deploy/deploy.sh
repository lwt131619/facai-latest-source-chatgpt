#!/usr/bin/env bash
# Facai Cloud production deployment helper (Ubuntu/Debian + systemd + nginx).
#
# Run from a staging/checkout copy of the repo (deploy-now.py unpacks one to
# /opt/facai-staging); running these from the live /opt/facai-cloud tree is
# refused, because syncing the app dir onto itself ships nothing.
#
#   sudo FACAI_DOMAIN=whstapp.lol ./deploy/deploy.sh install
#   sudo ./deploy/deploy.sh update
#   sudo FACAI_EMAIL=you@example.com ./deploy/deploy.sh cert
#   sudo ./deploy/deploy.sh backup
#   sudo ./deploy/deploy.sh rollback [archive.tar.gz]
#        ./deploy/deploy.sh status | logs
#
# The script never prints secrets. It only copies environment variable *names*;
# real values live in /etc/facai-cloud/facai.env (app login/runtime) and one file
# per service under /etc/facai-cloud/providers/ (model credentials), both mode
# 0640, root:facai, loaded by the systemd unit.
#
# Safety model:
#   * A first install never enables the 443 server before a certificate exists.
#     nginx starts on the HTTP ACME bootstrap; `cert` swaps in the TLS server
#     only after certbot has written the certificate files.
#   * Code sync is scoped: only the managed code paths are replaced and deleted.
#     Anything else under APP_DIR (user files, previews, data) is excluded from
#     rsync and therefore never deleted or overwritten.
#   * Code sync refuses an incomplete source tree and a self-copy, so a partial
#     bundle can never use `--delete` to wipe the managed paths in production.
#   * Every change to APP_DIR is preceded by a timestamped backup under
#     BACKUP_DIR, so a failed deploy can be undone with `rollback`.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/facai-cloud}"
APP_USER="${APP_USER:-facai}"
ENV_DIR="${ENV_DIR:-/etc/facai-cloud}"
ENV_FILE="${ENV_DIR}/facai.env"
PROVIDER_ENV_DIR="${PROVIDER_ENV_DIR:-${ENV_DIR}/providers}"
LEGACY_ENV_FILE="${LEGACY_ENV_FILE:-/etc/facai-cloud.env}"
DATA_DIR="${DATA_DIR:-${APP_DIR}/data}"
DOMAIN="${FACAI_DOMAIN:-whstapp.lol}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/facai-cloud}"
BACKUP_KEEP="${BACKUP_KEEP:-10}"
CERT_DIR="${CERT_DIR:-/etc/letsencrypt/live/${DOMAIN}}"
WEBROOT="${WEBROOT:-/var/www/certbot}"
NGINX_SITE="/etc/nginx/sites-available/facai-cloud.conf"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

log() { printf '[facai-deploy] %s\n' "$*"; }
die() { printf '[facai-deploy] ERROR: %s\n' "$*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "run as root (use sudo)"; }

has_cert() {
  [ -f "${CERT_DIR}/fullchain.pem" ] && [ -f "${CERT_DIR}/privkey.pem" ]
}

has_env_file() { [ -f "${ENV_FILE}" ]; }

# `update` re-installs a unit that references ${ENV_FILE}; if it is missing the
# service would fail (or, worse, start without credentials). Fail fast instead.
require_env_file() {
  has_env_file || die "missing ${ENV_FILE}: run 'install' (or create it) before 'update' so the service keeps its credentials"
}

# Once TLS is enabled the API is reachable from the internet, so never expose it
# without a password. The value itself is only pattern-checked, never printed.
has_admin_password() {
  has_env_file && grep -qE '^[[:space:]]*FACAI_ADMIN_PASSWORD=.+$' "${ENV_FILE}"
}

require_admin_password() {
  has_admin_password || die "FACAI_ADMIN_PASSWORD is not set in ${ENV_FILE}; refusing to enable public HTTPS without authentication"
}

print_dns_gate() {
  cat <<EOF
[facai-deploy] DNS gate: before requesting a certificate, confirm the A records
for ${DOMAIN} and www.${DOMAIN} resolve to this server's public IP on the
authoritative nameservers, not merely a cached resolver:
  dig +short NS  ${DOMAIN}
  dig +short @ns1.dnsowl.com ${DOMAIN} A
  dig +short @ns2.dnsowl.com ${DOMAIN} A
  dig +short @ns3.dnsowl.com ${DOMAIN} A
  dig +short @ns1.dnsowl.com www.${DOMAIN} A
Do not continue to 'cert' until every authoritative answer matches. Let's Encrypt
validates the HTTP-01 challenge against whichever authoritative server the CA
resolver reaches first, so a partially propagated apex record will fail issuance.
EOF
}

install_packages() {
  log "installing system packages"
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip nginx certbot rsync git
}

ensure_user() {
  if ! id -u "${APP_USER}" >/dev/null 2>&1; then
    log "creating service user ${APP_USER}"
    useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
  fi
}

# Snapshot APP_DIR before any change. Unknown/user files are included; the
# reproducible venv, caches and the live database are excluded.
backup_app_dir() {
  [ -d "${APP_DIR}" ] || return 0
  if [ -z "$(ls -A "${APP_DIR}" 2>/dev/null || true)" ]; then
    log "no existing ${APP_DIR} content to back up"
    return 0
  fi
  install -d -m 0750 "${BACKUP_DIR}"
  local stamp archive
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  archive="${BACKUP_DIR}/app-${stamp}.tar.gz"
  log "backing up ${APP_DIR} -> ${archive} (excludes .venv, data, caches)"
  tar -czf "${archive}" -C "${APP_DIR}" \
    --exclude='./.venv' --exclude='./data' --exclude='./__pycache__' \
    --exclude='./.pytest_cache' --exclude='./.git' . \
    || die "backup failed; aborting before touching ${APP_DIR}"
  ln -sfn "${archive}" "${BACKUP_DIR}/app-latest.tar.gz"
  { ls -1t "${BACKUP_DIR}"/app-*.tar.gz 2>/dev/null || true; } \
    | tail -n +"$((BACKUP_KEEP + 1))" | xargs -r rm -f
}

sync_code() {
  # `rsync --delete` replaces managed paths wholesale, so the source must be a
  # complete tree: a truncated bundle or a half-finished extraction would
  # delete the very files production still needs. Gate on a file from the deep
  # end of the managed set (tar members are extracted in name order, so its
  # presence implies the earlier members arrived too).
  [ -f "${SRC_DIR}/facai/api.py" ] \
    || die "source tree at ${SRC_DIR} is incomplete (no facai/api.py); refusing to rsync --delete production from a partial tree"
  # SRC_DIR and APP_DIR must differ: running this script from the live app dir
  # would rsync the tree onto itself and report "update complete" while
  # shipping nothing. Deploy from a staging/checkout copy (deploy-now.py does).
  if [ "${SRC_DIR}" = "${APP_DIR}" ]; then
    die "SRC_DIR (${SRC_DIR}) equals APP_DIR; run this script from a staging copy, not the live app dir"
  fi
  log "syncing managed code to ${APP_DIR} (unknown files preserved)"
  backup_app_dir
  install -d -m 0755 "${APP_DIR}"

  # Copy only deploy-managed paths. `--delete` removes stale files *inside those
  # paths only*; the trailing `--exclude '/*'` excludes every other top-level
  # entry, and rsync never deletes excluded destination files, so user data,
  # previews and the `data` directory are left untouched.
  # bridge/whatsapp-relay is deliberately not a managed path: the relay runs
  # from /opt/facai-whatsapp-relay with its own runtime and is deployed
  # separately from its own checkout, never through this script.
  rsync -a --delete \
    --filter 'P data' --filter 'P .venv' \
    --include '/facai/***' \
    --include '/tests/***' \
    --include '/docs/***' \
    --include '/deploy/***' \
    --include '/README.md' \
    --include '/requirements.txt' \
    --include '/start-opencode.ps1' \
    --exclude '/*' \
    "${SRC_DIR}/" "${APP_DIR}/"

  install -d -m 0750 -o "${APP_USER}" -g "${APP_USER}" "${APP_DIR}/data"
}

install_python() {
  log "creating virtualenv and installing requirements"
  python3 -m venv "${APP_DIR}/.venv"
  "${APP_DIR}/.venv/bin/pip" install --upgrade pip
  "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"
  # Only managed paths are re-owned; unknown files under APP_DIR keep their owner.
  chown -R "${APP_USER}:${APP_USER}" \
    "${APP_DIR}/.venv" "${APP_DIR}/facai" "${APP_DIR}/tests" \
    "${APP_DIR}/docs" "${APP_DIR}/deploy" "${APP_DIR}/data" 2>/dev/null || true
}

# OpenCode stores its own state under $HOME/.local and the hardened unit sets HOME
# inside the data directory (the service's only writable path). Create it owned by
# the service user so a run can write its store without touching anything else.
prepare_opencode_home() {
  install -d -m 0750 -o "${APP_USER}" -g "${APP_USER}" "${DATA_DIR}/opencode/home"
}

install_env_file() {
  install -d -m 0750 -o root -g "${APP_USER}" "${ENV_DIR}"
  if [ ! -f "${ENV_FILE}" ] && [ -f "${LEGACY_ENV_FILE}" ]; then
    adopt_legacy_env
  fi
  if [ ! -f "${ENV_FILE}" ]; then
    install -m 0640 -o root -g "${APP_USER}" "${APP_DIR}/deploy/facai.env.example" "${ENV_FILE}"
    log "created ${ENV_FILE} from template - EDIT IT and set FACAI_ADMIN_PASSWORD"
  else
    chown root:"${APP_USER}" "${ENV_FILE}"
    chmod 0640 "${ENV_FILE}"
    log "kept existing ${ENV_FILE}"
  fi
}

# Migrate a legacy single-file environment (e.g. /etc/facai-cloud.env) without
# exposing secrets: copy every line verbatim, replace only the managed non-secret
# keys, and keep 0640 root:facai.
adopt_legacy_env() {
  log "adopting ${LEGACY_ENV_FILE} -> ${ENV_FILE} (existing secrets preserved)"
  {
    grep -vE '^[[:space:]]*(FACAI_DATA_DIR|FACAI_COOKIE_SECURE|FACAI_TRUST_PROXY)=' "${LEGACY_ENV_FILE}" || true
    printf '# --- added during migration to %s ---\n' "${ENV_FILE}"
    printf 'FACAI_DATA_DIR=%s\n' "${DATA_DIR}"
    printf 'FACAI_COOKIE_SECURE=1\n'
    printf 'FACAI_TRUST_PROXY=1\n'
  } > "${ENV_FILE}"
  chown root:"${APP_USER}" "${ENV_FILE}"
  chmod 0640 "${ENV_FILE}"
}

# Provider credentials are classified by service: each upstream gets its own file
# under ${PROVIDER_ENV_DIR} (e.g. deepseek.env), while ${ENV_FILE} keeps only the
# application login/runtime settings. Create the directory (root:facai 0750) and
# seed one 0640 file per template, never overwriting a value already present.
install_provider_env_files() {
  install -d -m 0750 -o root -g "${APP_USER}" "${PROVIDER_ENV_DIR}"
  local template name target
  shopt -s nullglob
  for template in "${APP_DIR}"/deploy/providers/*.env.example; do
    name="$(basename "${template}" .example)"
    target="${PROVIDER_ENV_DIR}/${name}"
    if [ ! -f "${target}" ]; then
      install -m 0640 -o root -g "${APP_USER}" "${template}" "${target}"
      log "created ${target} from template - set the credential value"
    else
      chown root:"${APP_USER}" "${target}"
      chmod 0640 "${target}"
      log "kept existing ${target}"
    fi
  done
  shopt -u nullglob
}

# Snapshot the configuration before it is changed so a failed migration can be
# reverted. The archive holds secrets, so it is root-only (mode 0600).
backup_env_dir() {
  [ -d "${ENV_DIR}" ] || return 0
  install -d -m 0750 "${BACKUP_DIR}"
  local stamp archive
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  archive="${BACKUP_DIR}/env-${stamp}.tar.gz"
  log "backing up ${ENV_DIR} -> ${archive} (contains secrets; mode 0600)"
  tar -czf "${archive}" -C "$(dirname "${ENV_DIR}")" "$(basename "${ENV_DIR}")" \
    || die "environment backup failed; aborting before changing ${ENV_DIR}"
  chmod 0600 "${archive}"
  ln -sfn "${archive}" "${BACKUP_DIR}/env-latest.tar.gz"
  # Same retention as the application archives: each env archive holds every
  # provider key plus FACAI_ADMIN_PASSWORD, so an unbounded stack of them is a
  # liability, not a safety net. The env-latest symlink is filtered out so the
  # stable rollback pointer never consumes a retention slot.
  { ls -1t "${BACKUP_DIR}"/env-*.tar.gz 2>/dev/null || true; } \
    | { grep -v '/env-latest\.tar\.gz$' || true; } \
    | tail -n +"$((BACKUP_KEEP + 1))" | xargs -r rm -f
}

# The DeepSeek credential used to live in ${ENV_FILE}. Move it into its classified
# file exactly once, atomically and without ever printing the value. Only the
# actively-used DeepSeek key is migrated; unused provider keys are left untouched.
# Idempotent, so install/update preserve the classification and a rollback is safe.
classify_deepseek_env() {
  [ -f "${ENV_FILE}" ] || return 0
  grep -qE '^[[:space:]]*DEEPSEEK_API_KEY=..+' "${ENV_FILE}" || return 0
  install -d -m 0750 -o root -g "${APP_USER}" "${PROVIDER_ENV_DIR}"
  local provider_file="${PROVIDER_ENV_DIR}/deepseek.env"
  if [ ! -f "${provider_file}" ] \
    || ! grep -qE '^[[:space:]]*DEEPSEEK_API_KEY=..+' "${provider_file}"; then
    log "classifying DEEPSEEK_API_KEY from ${ENV_FILE} into ${provider_file}"
    local tmp
    tmp="$(mktemp "${PROVIDER_ENV_DIR}/.deepseek.env.XXXXXX")"
    if [ -f "${provider_file}" ]; then
      grep -vE '^[[:space:]]*DEEPSEEK_API_KEY=' "${provider_file}" >> "${tmp}" || true
    fi
    printf '# DeepSeek API credential. Referenced from the model registry by api_key_env.\n' >> "${tmp}"
    grep -E '^[[:space:]]*DEEPSEEK_API_KEY=' "${ENV_FILE}" | tail -n 1 >> "${tmp}"
    chown root:"${APP_USER}" "${tmp}"
    chmod 0640 "${tmp}"
    mv -f "${tmp}" "${provider_file}"
  else
    log "DEEPSEEK_API_KEY already classified in ${provider_file}"
  fi
  # Strip the classified key from the application env, atomically.
  local etmp
  etmp="$(mktemp "${ENV_DIR}/.facai.env.XXXXXX")"
  grep -vE '^[[:space:]]*DEEPSEEK_API_KEY=' "${ENV_FILE}" > "${etmp}" || true
  chown root:"${APP_USER}" "${etmp}"
  chmod 0640 "${etmp}"
  mv -f "${etmp}" "${ENV_FILE}"
  log "removed DEEPSEEK_API_KEY from ${ENV_FILE} (now in the classified provider env)"
}

# Every unit that ships with the code must land in /etc/systemd/system: the
# unattended facai-*.service/*.timer files carry hardening (unprivileged user,
# ProtectSystem=strict) that stays ineffective if only facai-cloud.service is
# installed and the server keeps running older manual copies of the rest.
install_systemd() {
  log "installing systemd units"
  local unit
  install -m 0644 "${APP_DIR}/deploy/systemd/facai-cloud.service" /etc/systemd/system/facai-cloud.service
  shopt -s nullglob
  for unit in "${APP_DIR}"/deploy/facai-*.service "${APP_DIR}"/deploy/facai-*.timer; do
    install -m 0644 "${unit}" "/etc/systemd/system/$(basename "${unit}")"
  done
  shopt -u nullglob
  systemctl daemon-reload
  # The main service must enable cleanly; anything else failing to enable is a
  # loud warning, not a broken deploy. Timers only fire when enabled, and the
  # long-running chain sync must come up at boot; timer-triggered oneshot
  # services have no [Install] section and are started by their timers.
  systemctl enable facai-cloud.service
  shopt -s nullglob
  for unit in "${APP_DIR}"/deploy/facai-*.timer; do
    systemctl enable "$(basename "${unit}")" 2>/dev/null \
      || log "warning: could not enable $(basename "${unit}")"
  done
  if [ -f "/etc/systemd/system/facai-tron-sync.service" ]; then
    systemctl enable facai-tron-sync.service 2>/dev/null \
      || log "warning: could not enable facai-tron-sync.service"
  fi
  shopt -u nullglob
}

install_nginx() {
  install -d -m 0755 "${WEBROOT}"
  local src
  if has_cert; then
    src="${APP_DIR}/deploy/nginx/facai-cloud.conf"
    log "installing nginx TLS site (certificate present)"
  else
    src="${APP_DIR}/deploy/nginx/facai-cloud.http.conf"
    log "installing nginx HTTP bootstrap (no certificate yet; ACME challenge only)"
  fi
  install -m 0644 "${src}" "${NGINX_SITE}"
  ln -sf "${NGINX_SITE}" /etc/nginx/sites-enabled/facai-cloud.conf
  nginx -t
}

restart_services() {
  log "starting services"
  systemctl restart facai-cloud.service
  systemctl reload nginx 2>/dev/null || systemctl restart nginx
}

cmd_install() {
  need_root
  install_packages
  ensure_user
  sync_code
  install_python
  prepare_opencode_home
  # A fresh install must be able to run OpenCode. Install the pinned CLI only
  # when it is missing; upgrades stay an explicit `opencode` action.
  if ! command -v opencode >/dev/null 2>&1; then
    log "opencode CLI not found; installing the pinned version"
    "${SCRIPT_DIR}/install-opencode.sh"
  fi
  backup_env_dir
  install_env_file
  install_provider_env_files
  classify_deepseek_env
  install_systemd
  install_nginx
  restart_services
  if has_cert; then
    log "install complete (existing TLS certificate found and enabled)."
  else
    print_dns_gate
    log "install complete. Set FACAI_ADMIN_PASSWORD, then run: $0 cert"
  fi
}

cmd_update() {
  need_root
  require_env_file
  sync_code
  install_python
  prepare_opencode_home
  command -v opencode >/dev/null 2>&1 \
    || log "warning: opencode CLI not on PATH; run '$0 opencode' before running tasks"
  # Preserve the provider/app split across upgrades: back up the config, seed any
  # missing classified provider files and migrate the known DeepSeek key once.
  backup_env_dir
  install_provider_env_files
  classify_deepseek_env
  # Re-apply the service unit and proxy config too, so changes to them are not
  # silently left behind (both are idempotent and certificate-aware).
  install_systemd
  install_nginx
  restart_services
  log "update complete"
}

# Install or upgrade the pinned OpenCode CLI. Explicit on purpose: `update` never
# reaches the network just to fetch a coding agent.
cmd_opencode() {
  need_root
  "${SCRIPT_DIR}/install-opencode.sh"
}

cmd_cert() {
  need_root
  [ -n "${FACAI_EMAIL:-}" ] || die "FACAI_EMAIL is required for certbot"
  require_admin_password
  print_dns_gate
  install -d -m 0755 "${WEBROOT}"
  # Ensure the HTTP ACME challenge is being served before requesting the cert.
  if ! has_cert; then
    install_nginx
    systemctl reload nginx 2>/dev/null || systemctl restart nginx
  fi
  log "requesting certificate for ${DOMAIN} (webroot)"
  certbot certonly --webroot -w "${WEBROOT}" \
    -d "${DOMAIN}" -d "www.${DOMAIN}" \
    --email "${FACAI_EMAIL}" --agree-tos --non-interactive --keep-until-expiring \
    --deploy-hook "systemctl reload nginx"
  has_cert || die "certificate files not found under ${CERT_DIR} after certbot"
  # The certificate now exists: enable the TLS server and validate it.
  install_nginx
  systemctl reload nginx 2>/dev/null || systemctl restart nginx
  log "certificate issued; HTTPS is now served for https://${DOMAIN}/"
}

cmd_backup() {
  need_root
  backup_app_dir
  log "backup complete (see ${BACKUP_DIR})"
}

cmd_rollback() {
  need_root
  local archive="${1:-}"
  [ -n "${archive}" ] || archive="${BACKUP_DIR}/app-latest.tar.gz"
  [ -f "${archive}" ] || die "backup archive not found: ${archive}"
  log "rolling back ${APP_DIR} from ${archive}"
  # Preserve the current (broken) state too, so rollback itself is reversible.
  backup_app_dir
  systemctl stop facai-cloud.service || true
  tar -xzf "${archive}" -C "${APP_DIR}" || die "rollback extraction failed"
  install_nginx || true
  install_python
  systemctl start facai-cloud.service || systemctl restart facai-cloud.service
  log "rollback complete"
}

cmd_status() {
  systemctl --no-pager status facai-cloud.service || true
  nginx -t || true
  if has_cert; then log "certificate: present at ${CERT_DIR}"; else log "certificate: not issued yet"; fi
  log "local check (expects 401 for the API when FACAI_ADMIN_PASSWORD is set):"
  curl -s -o /dev/null -w 'health=%{http_code}\n' "http://127.0.0.1:8765/api/v1/health" || true
}

cmd_logs() { journalctl -u facai-cloud.service -n 80 --no-pager; }

case "${1:-}" in
  install) cmd_install ;;
  update)  cmd_update ;;
  cert)    cmd_cert ;;
  opencode) cmd_opencode ;;
  backup)  cmd_backup ;;
  rollback) cmd_rollback "${2:-}" ;;
  status)  cmd_status ;;
  logs)    cmd_logs ;;
  *) die "usage: $0 {install|update|cert|opencode|backup|rollback [archive]|status|logs}" ;;
esac
