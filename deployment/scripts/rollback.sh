#!/usr/bin/env bash
set -euo pipefail

# Roll back current symlink to the previous release directory.

APP_ROOT="/opt/tiktok-bot"
CURRENT_TARGET="$(readlink -f "${APP_ROOT}/current")"
PREVIOUS_RELEASE="$(ls -1dt "${APP_ROOT}/releases"/* | grep -v "^${CURRENT_TARGET}$" | head -n 1 || true)"

if [ -z "${PREVIOUS_RELEASE}" ]; then
  echo "No previous release found to roll back to." >&2
  exit 1
fi

ln -sfn "${PREVIOUS_RELEASE}" "${APP_ROOT}/current"
systemctl restart tiktok-bot-web tiktok-bot-worker
echo "Rolled back to ${PREVIOUS_RELEASE}."
