#!/usr/bin/env bash
set -euo pipefail

APP_NAME="xui-manager-panel"
APP_DIR="${APP_DIR:-/opt/xui-manager-panel}"
DATA_DIR="${DATA_DIR:-/opt/xui-manager-panel-data}"
ENV_DIR="${ENV_DIR:-/etc/xui-manager-panel}"
ENV_FILE="${ENV_FILE:-${ENV_DIR}/xui-manager.env}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
REPO_URL="${REPO_URL:-https://github.com/HaydenSmith1121/xui-manager-panel.git}"
LISTEN_HOST="${LISTEN_HOST:-0.0.0.0}"
LISTEN_PORT="${LISTEN_PORT:-25888}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@example.com}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run this installer as root."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git python3

if [ -z "$ADMIN_PASSWORD" ]; then
  ADMIN_PASSWORD="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(18))
PY
)"
fi

if [ -d "${APP_DIR}/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  rm -rf "$APP_DIR"
  git clone "$REPO_URL" "$APP_DIR"
fi

mkdir -p "$DATA_DIR" "$ENV_DIR"
chmod 700 "$DATA_DIR" "$ENV_DIR"

if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<EOF
XUI_MANAGER_DATA=${DATA_DIR}
LISTEN_HOST=${LISTEN_HOST}
LISTEN_PORT=${LISTEN_PORT}
ADMIN_EMAIL=${ADMIN_EMAIL}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
EOF
  chmod 600 "$ENV_FILE"
  echo "Created ${ENV_FILE}"
else
  echo "Keeping existing ${ENV_FILE}"
fi

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=X-UI Manager Panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=/usr/bin/python3 -m xui_manager.app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$APP_NAME"
systemctl restart "$APP_NAME"

if command -v ufw >/dev/null 2>&1 && ufw status | grep -qw active; then
  ufw allow "${LISTEN_PORT}/tcp" || true
fi

echo
echo "Installed ${APP_NAME}."
echo "Open: http://SERVER_IP:${LISTEN_PORT}/"
echo "Admin email: ${ADMIN_EMAIL}"
echo "Admin password: ${ADMIN_PASSWORD}"
echo "Logs: journalctl -u ${APP_NAME} -f"
