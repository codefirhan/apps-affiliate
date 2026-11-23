#!/usr/bin/env bash
set -euo pipefail

# Atomic release deployment for TikTok Auto-Pilot Bot.
# Usage: sudo bash deployment/scripts/deploy.sh /path/to/source_repo [domain]

SRC_DIR="${1:-$(pwd)}"
BOT_DOMAIN="${2:-bot.example.com}"
APP_USER="tiktokbot"
APP_ROOT="/opt/tiktok-bot"
TIMESTAMP="$(date +%Y%m%d%H%M%S)"
RELEASE_DIR="${APP_ROOT}/releases/${TIMESTAMP}"

mkdir -p "${RELEASE_DIR}"
cp -a "${SRC_DIR}/app" "${RELEASE_DIR}/app"
cp -a "${SRC_DIR}/requirements.txt" "${RELEASE_DIR}/requirements.txt"
cp -a "${SRC_DIR}/pyproject.toml" "${RELEASE_DIR}/pyproject.toml"

python3 -m venv "${RELEASE_DIR}/.venv"
"${RELEASE_DIR}/.venv/bin/pip" install --upgrade pip
"${RELEASE_DIR}/.venv/bin/pip" install -r "${RELEASE_DIR}/requirements.txt"

ln -sfn "${APP_ROOT}/shared/data" "${RELEASE_DIR}/data"
chown -R "${APP_USER}:${APP_USER}" "${RELEASE_DIR}"
ln -sfn "${RELEASE_DIR}" "${APP_ROOT}/current"

# Install systemd units and Caddyfile.
cp "${SRC_DIR}/deployment/systemd/tiktok-bot-web.service" /etc/systemd/system/tiktok-bot-web.service
cp "${SRC_DIR}/deployment/systemd/tiktok-bot-worker.service" /etc/systemd/system/tiktok-bot-worker.service
sed "s|{\\$BOT_DOMAIN:bot.example.com}|${BOT_DOMAIN}|g" "${SRC_DIR}/deployment/caddy/Caddyfile" > /etc/caddy/Caddyfile

systemctl daemon-reload
systemctl enable --now tiktok-bot-web tiktok-bot-worker
systemctl restart tiktok-bot-web tiktok-bot-worker
systemctl reload caddy || systemctl restart caddy

# Keep last 3 releases to prevent disk bloat.
ls -1dt "${APP_ROOT}/releases"/* | tail -n +4 | xargs -r rm -rf

echo "Deployed release ${TIMESTAMP} for domain ${BOT_DOMAIN}."
