#!/usr/bin/env bash
set -euo pipefail

# Switch existing Nginx vhost for socmed.codefirhan.my.id to reverse-proxy
# TikTok Auto-Pilot Bot on 127.0.0.1:8000 while preserving existing SSL certificates.

DOMAIN="${1:-socmed.codefirhan.my.id}"

systemctl disable --now caddy >/dev/null 2>&1 || true

VHOST_FILE="$(grep -rl "${DOMAIN}" /etc/nginx/sites-enabled /etc/nginx/sites-available /etc/nginx/conf.d 2>/dev/null | head -n 1 || true)"

if [ -z "${VHOST_FILE}" ]; then
  echo "Could not find existing Nginx vhost for ${DOMAIN}." >&2
  ls -la /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ || true
  exit 1
fi

REAL_VHOST="$(readlink -f "${VHOST_FILE}")"
BACKUP_FILE="/root/$(basename "${REAL_VHOST}").bak.$(date +%Y%m%d%H%M%S)"
cp -a "${REAL_VHOST}" "${BACKUP_FILE}"
echo "Backed up ${REAL_VHOST} -> ${BACKUP_FILE}"

SSL_CERT_LINE="$(grep -E '^\s*ssl_certificate\s+' "${REAL_VHOST}" | head -n 1 || true)"
SSL_KEY_LINE="$(grep -E '^\s*ssl_certificate_key\s+' "${REAL_VHOST}" | head -n 1 || true)"

if [ -n "${SSL_CERT_LINE}" ] && [ -n "${SSL_KEY_LINE}" ]; then
  cat > "${REAL_VHOST}" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name ${DOMAIN};

${SSL_CERT_LINE}
${SSL_KEY_LINE}

    client_max_body_size 10M;

    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    location ~ /\. {
        deny all;
        return 404;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
}
EOF
else
  cat > "${REAL_VHOST}" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    client_max_body_size 10M;

    location ~ /\. {
        deny all;
        return 404;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
}
EOF
fi

if ! nginx -t; then
  echo "nginx -t failed; restoring backup ${BACKUP_FILE}" >&2
  cp -a "${BACKUP_FILE}" "${REAL_VHOST}"
  exit 1
fi

systemctl reload nginx
echo "Nginx successfully reloaded for ${DOMAIN} -> http://127.0.0.1:8000"
