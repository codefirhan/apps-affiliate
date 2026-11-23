#!/usr/bin/env bash
set -euo pipefail

# Create or update dedicated Nginx vhost for socmed.codefirhan.my.id -> 127.0.0.1:8000
# plus stateless /media/ stream proxy -> Cloudflare R2 public bucket URL (zero VPS disk storage).

DOMAIN="${1:-socmed.codefirhan.my.id}"
R2_UPSTREAM_HOST="${2:-pub-42c26168576a4616bed693e74d7cfe5d.r2.dev}"
AVAIL_FILE="/etc/nginx/sites-available/${DOMAIN}"
ENABLED_FILE="/etc/nginx/sites-enabled/${DOMAIN}"

systemctl disable --now caddy >/dev/null 2>&1 || true

cat > "${AVAIL_FILE}" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    client_max_body_size 10M;

    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    location ~ /\. {
        deny all;
        return 404;
    }

    location ^~ /media/ {
        resolver 1.1.1.1 8.8.8.8 valid=300s;
        proxy_pass https://${R2_UPSTREAM_HOST}/;
        proxy_ssl_server_name on;
        proxy_set_header Host ${R2_UPSTREAM_HOST};
        proxy_buffering off;
        proxy_max_temp_file_size 0;
        proxy_hide_header x-amz-id-2;
        proxy_hide_header x-amz-request-id;
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

ln -sfn "${AVAIL_FILE}" "${ENABLED_FILE}"

nginx -t
systemctl reload nginx

if ! command -v certbot >/dev/null 2>&1; then
  apt-get update
  apt-get install -y --no-install-recommends certbot python3-certbot-nginx
fi

certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos --register-unsafely-without-email --redirect || true

nginx -t
systemctl reload nginx
echo "Nginx + SSL + /media/ R2 proxy configured for https://${DOMAIN}"
