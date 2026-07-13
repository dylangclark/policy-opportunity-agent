#!/bin/sh
set -eu

APP_DIR=${1:-$(pwd)}
APP_DIR=$(CDPATH= cd -- "$APP_DIR" && pwd)
KEY_PATH=${2:-$HOME/.ssh/policy-opportunity-agent-ed25519}

mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"

if [ ! -f "$KEY_PATH" ]; then
  ssh-keygen -t ed25519 -N "" -C "policy-opportunity-agent" -f "$KEY_PATH"
fi

ssh-keyscan -t ed25519 github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null || true
chmod 600 "$HOME/.ssh/known_hosts" "$KEY_PATH"
chmod 644 "$KEY_PATH.pub"

git -C "$APP_DIR" config core.sshCommand "ssh -i $KEY_PATH -o IdentitiesOnly=yes -o BatchMode=yes"

echo "Add the following public key to the GitHub repository as a deploy key with write access:"
echo
echo "------------------------------------------------------------"
cat "$KEY_PATH.pub"
echo "------------------------------------------------------------"
echo
echo "Then test with: git -C $APP_DIR push --dry-run origin HEAD:main"
