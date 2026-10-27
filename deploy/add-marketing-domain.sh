#!/usr/bin/env bash
# Add opencodemarketing.top as a second HTTPS entry to the existing FACAI VPS.
# Run from the deployed source as root; never replaces the whstapp.lol vhost.
set -euo pipefail

DOMAIN=opencodemarketing.top
IP="${FACAI_SERVER_IP:-77.221.148.118}"
EMAIL="${FACAI_EMAIL:-}"
SITE=/etc/nginx/sites-available/facai-marketing.conf
ENABLED=/etc/nginx/sites-enabled/facai-marketing.conf
WEBROOT=/var/www/certbot
CERT=/etc/letsencrypt/live/$DOMAIN

[[ ${EUID} -eq 0 ]] || { echo 'Run as root' >&2; exit 1; }
[[ -n $EMAIL ]] || { echo 'Set FACAI_EMAIL for certificate registration' >&2; exit 1; }
[[ -e /etc/nginx/sites-enabled/facai-cloud.conf ]] || {
  echo 'Existing FACAI vhost is missing; stop rather than replace it' >&2; exit 1;
}
command -v dig >/dev/null || { echo 'Install dnsutils first' >&2; exit 1; }
command -v certbot >/dev/null || { echo 'Install certbot first' >&2; exit 1; }
for ns in ns1.dnsowl.com ns2.dnsowl.com ns3.dnsowl.com; do
  for host in "$DOMAIN" "www.$DOMAIN"; do
    answer=$(dig +short "@$ns" "$host" A | sort -u)
    [[ $answer == "$IP" ]] || {
      echo "DNS not ready: $ns $host A = ${answer:-<empty>}, expected $IP" >&2; exit 1;
    }
  done
done
mkdir -p "$WEBROOT"
BACKUP=$(mktemp)
HAD_SITE=0
if [[ -f $SITE ]]; then cp -p "$SITE" "$BACKUP"; HAD_SITE=1; fi
rollback() {
  if [[ $HAD_SITE -eq 1 ]]; then
    cp -p "$BACKUP" "$SITE"
  else
    rm -f "$SITE" "$ENABLED"
  fi
  nginx -t && systemctl reload nginx || true
  rm -f "$BACKUP"
}
trap rollback ERR

if [[ ! -f $CERT/fullchain.pem ]]; then
  # ACME-only vhost first. Never expose the app over plaintext HTTP.
  cat > "$SITE" <<'NGINX'
server {
    listen 80;
    listen [::]:80;
    server_name opencodemarketing.top www.opencodemarketing.top;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 503 "TLS certificate is being configured\n"; }
}
NGINX
  ln -sfn "$SITE" "$ENABLED"
  nginx -t
  systemctl reload nginx
  certbot certonly --webroot -w "$WEBROOT" -d "$DOMAIN" -d "www.$DOMAIN" \
    --email "$EMAIL" --agree-tos --non-interactive --keep-until-expiring \
    --deploy-hook 'systemctl reload nginx'
fi

[[ -f $CERT/fullchain.pem && -f $CERT/privkey.pem ]] || {
  echo 'Certificate files are missing; keeping the existing FACAI vhost untouched' >&2; exit 1;
}
cat > "$SITE" <<'NGINX'
server {
    listen 80;
    listen [::]:80;
    server_name opencodemarketing.top www.opencodemarketing.top;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://$host$request_uri; }
}
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name opencodemarketing.top www.opencodemarketing.top;
    ssl_certificate /etc/letsencrypt/live/opencodemarketing.top/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/opencodemarketing.top/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    add_header Strict-Transport-Security "max-age=63072000" always;
    add_header Content-Security-Policy "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'self'; form-action 'self'" always;
    client_max_body_size 2m;
    location /api/ {
        limit_req zone=facai_edge burst=20 nodelay;
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
    location = /login {
        limit_req zone=facai_edge burst=20 nodelay;
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 3600s;
    }
}
NGINX
ln -sfn "$SITE" "$ENABLED"
nginx -t
systemctl reload nginx
trap - ERR
rm -f "$BACKUP"
echo "Second domain enabled: https://$DOMAIN/ (existing vhost preserved)"
