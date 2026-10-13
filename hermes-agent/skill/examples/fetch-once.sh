#!/usr/bin/env bash
# Satu siklus polling Hermes Agent, dibuat untuk dipanggil cron.
#
# Cron setiap menit:
#   * * * * * /path/to/hermes-agent/skill/examples/fetch-once.sh >> /path/to/hermes-agent/data/cron.log 2>&1
#
# Exit code:
#   0 = server merespons, siklus selesai
#   1 = server tidak merespons (pesan tetap di outbox, dicoba lagi nanti)
#   2 = token ditolak (401) - butuh perhatian manusia
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
CONFIG="${HERMES_CONFIG:-$AGENT_DIR/config.json}"

# Pakai python3; ganti kalau environment butuh python.
PYTHON_BIN="${HERMES_PYTHON:-python3}"

PYTHONPATH="$AGENT_DIR" exec "$PYTHON_BIN" -m hermes.main --config "$CONFIG" fetch-once
