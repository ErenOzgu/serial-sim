#!/usr/bin/env bash
# Regenerate examples/demo/transcript.txt, or with --check compare it with what the simulator does today.
set -euo pipefail
cd "$(dirname "$0")/../.."
python=${PYTHON:-python3}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
"$python" -m serial_sim exec examples/demo/meter.toml --log "$tmp/t.jsonl" -- "$python" examples/demo/ask.py
"$python" -m serial_sim show "$tmp/t.jsonl" --no-time > "$tmp/transcript.txt"
if [ "${1:-}" = "--check" ]; then
  if ! diff -u examples/demo/transcript.txt "$tmp/transcript.txt"; then
    echo "examples/demo/transcript.txt is out of date: run examples/demo/generate.sh" >&2
    exit 1
  fi
else
  cp "$tmp/transcript.txt" examples/demo/transcript.txt
fi
