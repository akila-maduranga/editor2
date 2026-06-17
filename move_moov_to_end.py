#!/usr/bin/env python3
"""
move_moov_to_end.py
Moves the moov atom to the END of an MP4 file using ffmpeg.

Usage:
    python3 move_moov_to_end.py input.mp4
    python3 move_moov_to_end.py input.mp4 output.mp4
    python3 move_moov_to_end.py *.mp4              # batch
    python3 move_moov_to_end.py /path/to/folder    # all mp4s in folder
"""

import os
import sys
import shutil
import subprocess
import tempfile
import argparse


def check_ffmpeg():
    if not shutil.which("ffmpeg"):
        print("Error: ffmpeg is not installed or not in PATH.", file=sys.stderr)
        print("Install it with: sudo apt install ffmpeg", file=sys.stderr)
        sys.exit(1)


def moov_position(filepath: str) -> tuple[int, int]:
    """Return (byte_offset, file_size) of the last 'moov' atom in the file."""
    with open(filepath, "rb") as f:
        data = f.read()
    idx = data.rfind(b"moov")
    return idx, len(data)


def process_file(input_path: str, output_path: str) -> bool:
    in_place = os.path.abspath(input_path) == os.path.abspath(output_path)

    # Write to a temp file if rewriting in place
    if in_place:
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".mp4")
        os.close(tmp_fd)
        dest = tmp_path
    else:
        dest = output_path
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)

    print(f"Processing: {input_path} -> {output_path}")

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-c", "copy",
        "-movflags", "+omit_tfhd_offset",
        dest
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if result.returncode != 0:
            print(f"  X ffmpeg error:\n{result.stderr[-1000:]}", file=sys.stderr)
            if in_place and os.path.exists(tmp_path):
                os.remove(tmp_path)
            return False

        if in_place:
            shutil.move(tmp_path, input_path)
            print(f"  V Replaced in place: {input_path}")
        else:
            print(f"  V Done: {output_path}")

        # Verify moov position
        target = input_path if in_place else output_path
        offset, size = moov_position(target)
        if offset == -1:
            print("  W Could not find moov atom in output.")
        else:
            pct = (offset / size) * 100
            print(f"  moov atom at byte {offset:,} of {size:,} ({pct:.1f}% into file)")

        return True

    except Exception as e:
        print(f"  X Unexpected error: {e}", file=sys.stderr)
        return False


def collect_inputs(paths: list[str]) -> list[str]:
    """Expand folders to .mp4 files; keep file paths as-is."""
    collected = []
    for p in paths:
        if os.path.isdir(p):
            for f in sorted(os.listdir(p)):
                if f.lower().endswith(".mp4"):
                    collected.append(os.path.join(p, f))
        elif os.path.isfile(p):
            collected.append(p)
        else:
            print(f"Skipping (not found): {p}", file=sys.stderr)
    return collected


def main():
    parser = argparse.ArgumentParser(
        description="Move moov atom to the END of MP4 file(s)."
    )
    parser.add_argument(
        "inputs", nargs="+",
        help="Input MP4 file(s) or folder(s)"
    )
    parser.add_argument(
        "-o", "--output",
        help="Output file (only valid when processing a single file)"
    )
    args = parser.parse_args()

    check_ffmpeg()

    files = collect_inputs(args.inputs)

    if not files:
        print("No MP4 files found.", file=sys.stderr)
        sys.exit(1)

    if args.output and len(files) > 1:
        print("Error: -o/--output can only be used with a single input file.", file=sys.stderr)
        sys.exit(1)

    success, failed = 0, 0
    for f in files:
        out = args.output if args.output else f  # default: overwrite in place
        ok = process_file(f, out)
        if ok:
            success += 1
        else:
            failed += 1

    print(f"\nDone. {success} succeeded, {failed} failed.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
