#!/usr/bin/env bash
# move_moov_to_end.sh
# Moves the moov atom to the END of an MP4 file (opposite of faststart).
# This is useful when you want the moov atom at the end for certain workflows
# (e.g., live streaming ingest, some players that prefer it, or reversing a -movflags faststart).
#
# Usage:
#   ./move_moov_to_end.sh input.mp4 [output.mp4]
#   ./move_moov_to_end.sh *.mp4          # batch mode (overwrites originals via temp files)
#
# Dependencies: ffmpeg

set -euo pipefail

# ── helpers ────────────────────────────────────────────────────────────────────

usage() {
  echo "Usage: $0 <input.mp4> [output.mp4]"
  echo "       $0 *.mp4          # batch — rewrites each file in place"
  exit 1
}

check_deps() {
  if ! command -v ffmpeg &>/dev/null; then
    echo "Error: ffmpeg is not installed or not in PATH." >&2
    exit 1
  fi
}

process_file() {
  local input="$1"
  local output="$2"
  local in_place=false

  if [[ "$input" == "$output" ]]; then
    in_place=true
    output="$(mktemp --suffix=.mp4)"
  fi

  echo "Processing: $input → $output"

  # Default ffmpeg behavior writes moov after mdat (end of file).
  # We explicitly avoid +faststart (which would move moov to the front).
  ffmpeg -y \
    -i "$input" \
    -c copy \
    "$output" \
    2>&1 | grep -E "^(ffmpeg|Input|Output|Stream|frame|size|video|audio|Error)" || true

  if $in_place; then
    mv "$output" "$input"
    echo "  ✓ Replaced in place: $input"
  else
    echo "  ✓ Done: $output"
  fi
}

verify_moov_position() {
  # Quick sanity check: print where 'moov' atom starts in the file (byte offset).
  local file="$1"
  local offset
  offset=$(python3 -c "
import sys, struct
with open('$file', 'rb') as f:
    data = f.read()
idx = data.rfind(b'moov')   # rfind → last occurrence (should be near end)
print(idx, 'of', len(data), 'bytes')
" 2>/dev/null) || return
  echo "  moov atom found at byte: $offset"
}

# ── main ───────────────────────────────────────────────────────────────────────

check_deps

if [[ $# -eq 0 ]]; then
  usage
fi

if [[ $# -eq 1 ]]; then
  # Single file, overwrite in place
  input="$1"
  [[ -f "$input" ]] || { echo "Error: file not found: $input" >&2; exit 1; }
  process_file "$input" "$input"
  verify_moov_position "$input"

elif [[ $# -eq 2 ]]; then
  # Explicit output path
  input="$1"
  output="$2"
  [[ -f "$input" ]] || { echo "Error: file not found: $input" >&2; exit 1; }
  process_file "$input" "$output"
  verify_moov_position "$output"

else
  # Batch mode: all args are input files, rewrite each in place
  for input in "$@"; do
    [[ -f "$input" ]] || { echo "Skipping (not a file): $input" >&2; continue; }
    process_file "$input" "$input"
    verify_moov_position "$input"
  done
fi

echo ""
echo "All done."
