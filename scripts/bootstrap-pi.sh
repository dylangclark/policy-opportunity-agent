#!/bin/sh
set -eu

APP_DIR=${1:-$(pwd)}
APP_DIR=$(CDPATH= cd -- "$APP_DIR" && pwd)

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required." >&2
  exit 1
fi
if ! command -v git >/dev/null 2>&1; then
  echo "git is required." >&2
  exit 1
fi

cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install .

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created $APP_DIR/.env. Set POLICY_AGENT_CONTACT_EMAIL before enabling the timer."
fi

mkdir -p .state docs/data
.venv/bin/policy-agent --config config/sources.yml --rules config/rules.yml list-sources >/dev/null

echo "Bootstrap complete. Run:"
echo "  $APP_DIR/.venv/bin/policy-agent --config $APP_DIR/config/sources.yml --rules $APP_DIR/config/rules.yml --output $APP_DIR/docs/data --state-dir $APP_DIR/.state run"
