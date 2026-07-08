#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${APP_NAME:-xui-manager-panel-backend}"
APP_DIR="${APP_DIR:-/opt/xui-manager-panel-backend}"
DATA_DIR="${DATA_DIR:-/opt/xui-manager-panel-data}"
ENV_DIR="${ENV_DIR:-/etc/xui-manager-panel-backend}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
PURGE_APP="${PURGE_APP:-0}"
PURGE_DATA="${PURGE_DATA:-0}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run this uninstaller as root."
  exit 1
fi

systemctl stop "$APP_NAME" 2>/dev/null || true
systemctl disable "$APP_NAME" 2>/dev/null || true
rm -f "$SERVICE_FILE"
systemctl daemon-reload

if [ "$PURGE_APP" = "1" ]; then
  case "$APP_DIR" in
    /opt/xui-manager-panel-backend|/opt/xui-manager-panel)
      rm -rf "$APP_DIR"
      echo "Removed ${APP_DIR}."
      ;;
    *)
      echo "Refusing to remove unsafe APP_DIR: ${APP_DIR}"
      exit 1
      ;;
  esac
fi

if [ "$PURGE_DATA" = "1" ]; then
  case "$DATA_DIR" in
    /opt/xui-manager-panel-data)
      rm -rf "$DATA_DIR" "$ENV_DIR"
      echo "Removed ${DATA_DIR} and ${ENV_DIR}."
      ;;
    *)
      echo "Refusing to remove unsafe DATA_DIR: ${DATA_DIR}"
      exit 1
      ;;
  esac
else
  echo "Keeping data/config. Set PURGE_DATA=1 to remove ${DATA_DIR} and ${ENV_DIR}."
fi

echo "Uninstalled ${APP_NAME}."
