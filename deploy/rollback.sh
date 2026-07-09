#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${APP_NAME:-xui-manager-panel-backend}"
DEPLOY_ROOT="${DEPLOY_ROOT:-/opt/xui-manager-panel-backend}"
RELEASES_DIR="${RELEASES_DIR:-${DEPLOY_ROOT}/releases}"
CURRENT_LINK="${CURRENT_LINK:-${DEPLOY_ROOT}/current}"
PREVIOUS_LINK="${PREVIOUS_LINK:-${DEPLOY_ROOT}/previous}"
TARGET_RELEASE="${TARGET_RELEASE:-}"
LISTEN_PORT="${LISTEN_PORT:-25889}"
HEALTHCHECK_URL="${HEALTHCHECK_URL:-http://127.0.0.1:${LISTEN_PORT}/api/plans}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run rollback as root or through sudo."
  exit 1
fi

find_latest_previous_release() {
  local current_target=""
  if [ -L "$CURRENT_LINK" ]; then
    current_target="$(readlink -f "$CURRENT_LINK" || true)"
  fi
  find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d -printf "%T@ %p\n" | sort -rn | awk -v current="$current_target" '$2 != current {print $2; exit}'
}

if [ -n "$TARGET_RELEASE" ]; then
  if [ -d "$TARGET_RELEASE" ]; then
    TARGET_DIR="$TARGET_RELEASE"
  else
    TARGET_DIR="${RELEASES_DIR}/${TARGET_RELEASE}"
  fi
elif [ -L "$PREVIOUS_LINK" ] && [ -d "$(readlink -f "$PREVIOUS_LINK")" ]; then
  TARGET_DIR="$(readlink -f "$PREVIOUS_LINK")"
else
  TARGET_DIR="$(find_latest_previous_release)"
fi

if [ -z "${TARGET_DIR:-}" ] || [ ! -d "$TARGET_DIR" ]; then
  echo "No rollback target found. Set TARGET_RELEASE or keep at least two releases."
  exit 1
fi

OLD_TARGET=""
if [ -L "$CURRENT_LINK" ]; then
  OLD_TARGET="$(readlink -f "$CURRENT_LINK" || true)"
fi
if [ -n "$OLD_TARGET" ] && [ -d "$OLD_TARGET" ]; then
  ln -sfn "$OLD_TARGET" "$PREVIOUS_LINK"
fi
ln -sfn "$TARGET_DIR" "$CURRENT_LINK"

systemctl daemon-reload
systemctl restart "$APP_NAME"
curl -fsS "$HEALTHCHECK_URL" >/dev/null

echo "Rolled back ${APP_NAME} to $(basename "$TARGET_DIR")."
echo "Current: ${CURRENT_LINK} -> $(readlink -f "$CURRENT_LINK")"
