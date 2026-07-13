#!/bin/sh
set -eu

APP_DIR=${1:-$(pwd)}
APP_DIR=$(CDPATH= cd -- "$APP_DIR" && pwd)
RUN_USER=${2:-${SUDO_USER:-$(id -un)}}
RUN_GROUP=$(id -gn "$RUN_USER")
SERVICE_NAME=policy-opportunity-agent

if [ ! -x "$APP_DIR/.venv/bin/policy-agent" ]; then
  echo "Run scripts/bootstrap-pi.sh first." >&2
  exit 1
fi
if [ ! -f "$APP_DIR/.env" ]; then
  echo "Create $APP_DIR/.env from .env.example first." >&2
  exit 1
fi

TMP_SERVICE=$(mktemp)
TMP_TIMER=$(mktemp)
trap 'rm -f "$TMP_SERVICE" "$TMP_TIMER"' EXIT HUP INT TERM

sed \
  -e "s|__APP_DIR__|$APP_DIR|g" \
  -e "s|__RUN_USER__|$RUN_USER|g" \
  -e "s|__RUN_GROUP__|$RUN_GROUP|g" \
  systemd/policy-opportunity-agent.service > "$TMP_SERVICE"
cp systemd/policy-opportunity-agent.timer "$TMP_TIMER"

sudo install -m 0644 "$TMP_SERVICE" "/etc/systemd/system/$SERVICE_NAME.service"
sudo install -m 0644 "$TMP_TIMER" "/etc/systemd/system/$SERVICE_NAME.timer"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME.timer"

echo "Installed and enabled $SERVICE_NAME.timer."
echo "Run immediately: sudo systemctl start $SERVICE_NAME.service"
echo "View logs:      journalctl -u $SERVICE_NAME.service -n 200 --no-pager"
echo "View schedule:  systemctl list-timers $SERVICE_NAME.timer"
