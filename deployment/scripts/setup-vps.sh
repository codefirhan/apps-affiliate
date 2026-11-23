#!/usr/bin/env bash
set -euo pipefail

# Provision Ubuntu 24.04 VPS (2 vCPU / 2 GB RAM) for TikTok Auto-Pilot Bot.
# Run as root or via sudo on the target VPS.

APP_USER="tiktokbot"
APP_ROOT="/opt/tiktok-bot"
ENV_DIR="/etc/tiktok-bot"

apt-get update
apt-get install -y --no-install-recommends \
  ffmpeg \
  python3 \
  python3-venv \
  python3-pip \
  caddy \
  ufw \
  curl \
  fonts-dejavu-core

# Create 2 GB swapfile if no active swap exists (OOM protection for FFmpeg on 2 GB RAM).
if ! swapon --show | grep -q '/swapfile'; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  if ! grep -q '^/swapfile ' /etc/fstab; then
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
fi

# Create unprivileged service user.
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "${APP_USER}"
fi

mkdir -p "${APP_ROOT}/releases" "${APP_ROOT}/shared/data" /tmp/tiktok-bot "${ENV_DIR}"
chown -R "${APP_USER}:${APP_USER}" "${APP_ROOT}" /tmp/tiktok-bot
chown -R root:"${APP_USER}" "${ENV_DIR}"
chmod 750 "${ENV_DIR}"

if [ ! -f "${ENV_DIR}/app.env" ]; then
  cat > "${ENV_DIR}/app.env" <<'EOF'
APP_ENV=production
DRY_RUN=false
DATABASE_PATH=/opt/tiktok-bot/shared/data/tiktok_bot.db
TEMP_ROOT=/tmp/tiktok-bot
TIMEZONE=Asia/Jakarta
APP_SECRET_KEY=REPLACE_WITH_LONG_RANDOM_SECRET
APP_BASE_URL=https://bot.example.com
TIKTOK_REDIRECT_URI=https://bot.example.com/auth/tiktok/callback
EOF
  chown root:"${APP_USER}" "${ENV_DIR}/app.env"
  chmod 640 "${ENV_DIR}/app.env"
fi

# Configure UFW firewall (SSH, HTTP, HTTPS only).
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "VPS base provisioning complete. Next: edit /etc/tiktok-bot/app.env and run deploy.sh."
