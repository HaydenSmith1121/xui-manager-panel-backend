#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${APP_NAME:-xui-manager-panel-backend}"
DEPLOY_ROOT="${DEPLOY_ROOT:-/opt/xui-manager-panel-backend}"
SOURCE_DIR="${SOURCE_DIR:-$(pwd)}"
DATA_DIR="${DATA_DIR:-/opt/xui-manager-panel-data}"
ENV_DIR="${ENV_DIR:-/etc/xui-manager-panel-backend}"
ENV_FILE="${ENV_FILE:-${ENV_DIR}/xui-manager.env}"
SERVICE_FILE="${SERVICE_FILE:-/etc/systemd/system/${APP_NAME}.service}"
RELEASES_DIR="${RELEASES_DIR:-${DEPLOY_ROOT}/releases}"
CURRENT_LINK="${CURRENT_LINK:-${DEPLOY_ROOT}/current}"
PREVIOUS_LINK="${PREVIOUS_LINK:-${DEPLOY_ROOT}/previous}"
KEEP_RELEASES="${KEEP_RELEASES:-5}"
LISTEN_HOST="${LISTEN_HOST:-127.0.0.1}"
LISTEN_PORT="${LISTEN_PORT:-25889}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@example.com}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
FRONTEND_ORIGIN="${FRONTEND_ORIGIN:-}"
CORS_ALLOWED_ORIGINS="${CORS_ALLOWED_ORIGINS:-}"
SESSION_COOKIE_SAMESITE="${SESSION_COOKIE_SAMESITE:-Lax}"
SESSION_COOKIE_SECURE="${SESSION_COOKIE_SECURE:-false}"
HEALTHCHECK_URL="${HEALTHCHECK_URL:-http://127.0.0.1:${LISTEN_PORT}/api/plans}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run this Jenkins deploy as root or through sudo."
  exit 1
fi

if [ ! -d "${SOURCE_DIR}/xui_manager" ] || [ ! -f "${SOURCE_DIR}/xui_manager/app.py" ]; then
  echo "SOURCE_DIR must point to the backend workspace. Current: ${SOURCE_DIR}"
  exit 1
fi

GIT_COMMIT_SHORT="${GIT_COMMIT:-manual}"
GIT_COMMIT_SHORT="${GIT_COMMIT_SHORT:0:12}"
RELEASE_ID="${RELEASE_ID:-$(date +%Y%m%d%H%M%S)-${GIT_COMMIT_SHORT}}"
RELEASE_DIR="${RELEASES_DIR}/${RELEASE_ID}"
OLD_TARGET=""
if [ -L "$CURRENT_LINK" ]; then
  OLD_TARGET="$(readlink -f "$CURRENT_LINK" || true)"
fi

rollback_to_previous() {
  if [ -n "$OLD_TARGET" ] && [ -d "$OLD_TARGET" ]; then
    echo "Deploy failed. Rolling back to ${OLD_TARGET}."
    ln -sfn "$OLD_TARGET" "$CURRENT_LINK"
    systemctl daemon-reload || true
    systemctl restart "$APP_NAME" || true
  fi
}

trap rollback_to_previous ERR

mkdir -p "$RELEASES_DIR" "$DATA_DIR" "$ENV_DIR"
chmod 700 "$DATA_DIR" "$ENV_DIR"
if [ -e "$RELEASE_DIR" ]; then
  echo "Release already exists: ${RELEASE_DIR}"
  exit 1
fi
mkdir -p "$RELEASE_DIR"

if [ -f "${DATA_DIR}/app.db" ]; then
  BACKUP="/root/xui-manager-panel-app.db.bak.$(date +%F-%H%M%S)"
  cp "${DATA_DIR}/app.db" "$BACKUP"
  echo "Database backup: ${BACKUP}"
fi

if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete --exclude ".git" --exclude "__pycache__" --exclude ".pytest_cache" "${SOURCE_DIR}/" "${RELEASE_DIR}/"
else
  tar -C "$SOURCE_DIR" --exclude="./.git" --exclude="./__pycache__" --exclude="./.pytest_cache" -cf - . | tar -C "$RELEASE_DIR" -xf -
fi

python3 -m compileall -q "${RELEASE_DIR}/xui_manager" "${RELEASE_DIR}/tools"

if [ -z "$ADMIN_PASSWORD" ]; then
  ADMIN_PASSWORD="$(python3 -c "import secrets; print(secrets.token_urlsafe(18))")"
fi

if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<EOF
XUI_MANAGER_DATA=${DATA_DIR}
LISTEN_HOST=${LISTEN_HOST}
LISTEN_PORT=${LISTEN_PORT}
ADMIN_EMAIL=${ADMIN_EMAIL}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
FRONTEND_ORIGIN=${FRONTEND_ORIGIN}
CORS_ALLOWED_ORIGINS=${CORS_ALLOWED_ORIGINS}
SESSION_COOKIE_SAMESITE=${SESSION_COOKIE_SAMESITE}
SESSION_COOKIE_SECURE=${SESSION_COOKIE_SECURE}
EOF
  chmod 600 "$ENV_FILE"
  echo "Created ${ENV_FILE}"
else
  echo "Keeping existing ${ENV_FILE}"
fi

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=X-UI Manager Panel Backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${CURRENT_LINK}
EnvironmentFile=${ENV_FILE}
ExecStart=/usr/bin/python3 -m xui_manager.app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

if [ -n "$OLD_TARGET" ] && [ -d "$OLD_TARGET" ]; then
  ln -sfn "$OLD_TARGET" "$PREVIOUS_LINK"
fi
ln -sfn "$RELEASE_DIR" "$CURRENT_LINK"

systemctl daemon-reload
systemctl enable --now "$APP_NAME"
systemctl restart "$APP_NAME"
curl -fsS "$HEALTHCHECK_URL" >/dev/null

trap - ERR
find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d -printf "%T@ %p\n" | sort -rn | awk "NR>${KEEP_RELEASES} {print \$2}" | while IFS= read -r old_release; do
  [ "$(readlink -f "$CURRENT_LINK" || true)" = "$old_release" ] && continue
  [ "$(readlink -f "$PREVIOUS_LINK" || true)" = "$old_release" ] && continue
  rm -rf "$old_release"
done

echo "Deployed ${APP_NAME} release ${RELEASE_ID}."
echo "Current: ${CURRENT_LINK} -> $(readlink -f "$CURRENT_LINK")"
