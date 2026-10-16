#!/usr/bin/env bash
# Send a direct message via the Hermes Agent CLI. Can be called from other
# scripts, git hooks, or cron for scheduled notifications.
#
# Usage:
#   ./send-message.sh hermes-beta "Q3 report is ready"
#
# Optional variables:
#   HERMES_CONFIG  path to config.json (default: config.json in the package)
#   HERMES_PYTHON  python interpreter (default: python3)
set -eu

if [ "$#" -lt 2 ]; then
  echo "Pakai: $0 <target-agent> <message...>" >&2
  exit 64
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
CONFIG="${HERMES_CONFIG:-$AGENT_DIR/config.json}"
PYTHON_BIN="${HERMES_PYTHON:-python3}"

TARGET="$1"
shift

PYTHONPATH="$AGENT_DIR" exec "$PYTHON_BIN" -m hermes.main --config "$CONFIG" send "$TARGET" "$@"
