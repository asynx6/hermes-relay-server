#!/usr/bin/env bash
# Kirim pesan direct lewat CLI Hermes Agent. Bisa dipanggil dari skrip lain,
# git hook, atau cron kalau perlu notifikasi terjadwal.
#
# Pakai:
#   ./send-message.sh hermes-beta "Laporan Q3 sudah siap"
#
# Variabel opsional:
#   HERMES_CONFIG  path config.json (default: config.json di paket)
#   HERMES_PYTHON  interpreter python (default: python3)
set -eu

if [ "$#" -lt 2 ]; then
  echo "Pakai: $0 <target-agent> <pesan...>" >&2
  exit 64
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
CONFIG="${HERMES_CONFIG:-$AGENT_DIR/config.json}"
PYTHON_BIN="${HERMES_PYTHON:-python3}"

TARGET="$1"
shift

PYTHONPATH="$AGENT_DIR" exec "$PYTHON_BIN" -m hermes.main --config "$CONFIG" send "$TARGET" "$@"
