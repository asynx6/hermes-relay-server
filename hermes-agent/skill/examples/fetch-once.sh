#!/usr/bin/env bash
# One polling cycle for Hermes Agent, designed to be called by cron.
#
# Every minute via cron:
#   * * * * * /path/to/hermes-agent/skill/examples/fetch-once.sh >> /path/to/hermes-agent/data/cron.log 2>&1
#
# Exit codes:
#   0 = server responds, cycle finished
#   1 = server does not respond (messages stay in the outbox, retried later)
#   2 = token rejected (401) - needs human attention
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
CONFIG="${HERMES_CONFIG:-$AGENT_DIR/config.json}"

# Use python3; change this if your environment needs python.
PYTHON_BIN="${HERMES_PYTHON:-python3}"

PYTHONPATH="$AGENT_DIR" exec "$PYTHON_BIN" -m hermes.main --config "$CONFIG" fetch-once
