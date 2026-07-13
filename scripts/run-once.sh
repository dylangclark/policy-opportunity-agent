#!/bin/sh
set -eu
APP_DIR=${1:-$(pwd)}
APP_DIR=$(CDPATH= cd -- "$APP_DIR" && pwd)
cd "$APP_DIR"
set -a
[ ! -f .env ] || . ./.env
set +a
exec .venv/bin/policy-agent \
  --config config/sources.yml \
  --rules config/rules.yml \
  --output docs/data \
  --state-dir .state \
  publish
