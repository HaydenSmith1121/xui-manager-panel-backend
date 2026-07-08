#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${APP_NAME:-xui-manager-panel-backend}"
APP_DIR="${APP_DIR:-/opt/xui-manager-panel-backend}"
DATA_DIR="${DATA_DIR:-/opt/xui-manager-panel-data}"
BRANCH="${BRANCH:-main}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run this upgrade as root."
  exit 1
fi

if [ ! -d "${APP_DIR}/.git" ]; then
  echo "${APP_DIR} is not a git checkout. Run deploy/install.sh first."
  exit 1
fi

if [ -f "${DATA_DIR}/app.db" ]; then
  BACKUP="/root/xui-manager-panel-app.db.bak.$(date +%F-%H%M%S)"
  cp "${DATA_DIR}/app.db" "$BACKUP"
  echo "Database backup: ${BACKUP}"
fi

git -C "$APP_DIR" fetch origin "$BRANCH"
git -C "$APP_DIR" checkout "$BRANCH"
git -C "$APP_DIR" pull --ff-only origin "$BRANCH"

python3 -m compileall -q "${APP_DIR}/xui_manager" "${APP_DIR}/tools"

systemctl daemon-reload
systemctl restart "$APP_NAME"
systemctl status "$APP_NAME" --no-pager
