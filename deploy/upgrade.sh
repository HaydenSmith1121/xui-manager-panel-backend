#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${APP_NAME:-xui-manager-panel-backend}"
APP_DIR="${APP_DIR:-/opt/xui-manager-panel-backend}"
DATA_DIR="${DATA_DIR:-/opt/xui-manager-panel-data}"
ENV_DIR="${ENV_DIR:-/etc/xui-manager-panel-backend}"
ENV_FILE="${ENV_FILE:-${ENV_DIR}/xui-manager.env}"
BRANCH="${BRANCH:-main}"
MIGRATE_DEFAULT_LISTEN_PORT="${MIGRATE_DEFAULT_LISTEN_PORT:-1}"

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

if [ -f "$ENV_FILE" ] && [ "$MIGRATE_DEFAULT_LISTEN_PORT" = "1" ]; then
  if grep -qx 'LISTEN_PORT=25888' "$ENV_FILE"; then
    ENV_BACKUP="/root/xui-manager-panel-env.bak.$(date +%F-%H%M%S)"
    cp "$ENV_FILE" "$ENV_BACKUP"
    sed -i 's/^LISTEN_PORT=25888$/LISTEN_PORT=25889/' "$ENV_FILE"
    echo "Env backup: ${ENV_BACKUP}"
    echo "Migrated LISTEN_PORT from 25888 to 25889 in ${ENV_FILE}"
  fi
fi

systemctl daemon-reload
systemctl restart "$APP_NAME"
systemctl status "$APP_NAME" --no-pager
