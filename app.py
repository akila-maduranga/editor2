#!/usr/bin/env python3
"""
TikTok MP4 Patcher — Self-hosted VPS tool
Remuxes MP4 files and applies a binary structural patch to bypass
TikTok's aggressive compression on re-uploaded content.
"""

import os
import uuid
import subprocess
import threading
import queue
import struct
import shutil
from pathlib import Path
from flask import (
    Flask, request, render_template, jsonify,
    send_from_directory, Response, stream_with_context
)

app = Flask(__name__)

BASE_DIR     = Path(__file__).parent
UPLOAD_DIR   = BASE_DIR / "uploads"
OUTPUT_DIR   = BASE_DIR / "outputs"

UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# In-memory job log store: job_id -> queue of log lines
_job_logs: dict[str, queue.Queue] = {}
_job_status: dict[str, str] = {}   # "running" | "done" | "error"
_job_output: dict[str, str] = {}   # job_id -> output filename


# ---------------------------------------------------------------------------
# Patcher logic — mirrors patcher.py exactly
# ---------------------------------------------------------------------------

FTYP_BRANDS = [b"isom", b"iso2", b"avc1", b"mp41"]

def _log(q: queue.Queue, msg: str):
    q.put(msg)

def find_box(data: bytes, box_type: bytes, start: int = 0) -> tuple[int, int]:
    """Return (offset, size) of the first occurrence of box_type, or (-1, 0)."""
    i = start
    while i + 8 <= len(data):
        size = struct.unpack(">I", data[i:i+4])[0]
        btype = data[i+4:i+8]
        if size == 0:
            size = len(data) - i          # box extends to EOF
        if size < 8:
            break
        if btype == box_type:
            return i, size
        i += size
    return -1, 0


def patch_ftyp(data: bytes, log: queue.Queue) -> bytes:
    """
    Rewrite the ftyp box so TikTok's decoder treats the file as a
    freshly-encoded stream rather than a re-muxed upload.
    """
    offset, size = find_box(data, b"ftyp")
    if offset == -1:
        _log(log, "[WARN] ftyp box not found — skipping ftyp patch")
        return data

    _log(log, f"[PATCH] ftyp box @ offset {offset}, size {size}")

    # Build replacement ftyp: major brand + version + compatible brands
    major_brand = b"mp42"
    minor_version = struct.pack(">I", 0)
    compatible = b"".join(FTYP_BRANDS)
    new_ftyp_body = major_brand + minor_version + compatible
    new_ftyp_size = 8 + len(new_ftyp_body)
    new_ftyp = struct.pack(">I", new_ftyp_size) + b"ftyp" + new_ftyp_body

    _log(log, f"[PATCH] rewriting ftyp: major_brand=mp42, "
              f"compatible={[b.decode() for b in FTYP_BRANDS]}")
    return data[:offset] + new_ftyp + data[offset + size:]


def patch_moov_flags(data: bytes, log: queue.Queue) -> bytes:
    """
    Clear the 'random access' flag in the moov/mvhd box.
    TikTok uses this flag to detect re-encoded content.
    """
    moov_off, moov_size = find_box(data, b"moov")
    if moov_off == -1:
        _log(log, "[WARN] moov box not found — skipping mvhd patch")
        return data

    mvhd_off, mvhd_size = find_box(data, b"mvhd", moov_off + 8)
    if mvhd_off == -1:
        _log(log, "[WARN] mvhd box not found inside moov — skipping")
        return data

    _log(log, f"[PATCH] mvhd box @ offset {mvhd_off}, size {mvhd_size}")

    # mvhd layout: 4 size + 4 type + 1 version + 3 flags + ...
    flags_off = mvhd_off + 9   # byte offset of the 3-byte flags field
    original_flags = data[flags_off:flags_off+3]
    # Clear flag bit 0x000001 (random access point indicator)
    new_flags = bytes([
        original_flags[0],
        original_flags[1],
        original_flags[2] & 0xFE,
    ])
    _log(log, f"[PATCH] mvhd flags {original_flags.hex()} → {new_flags.hex()}")
    return data[:flags_off] + new_flags + data[flags_off+3:]


def remux(src: Path, dst: Path, log: queue.Queue) -> bool:
    """Remux src → dst using ffmpeg (stream copy, moov at front)."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-c", "copy",          # no re-encoding
        "-movflags", "+faststart",   # moov at front (required for patching)
        "-map_metadata", "0",
        str(dst),
    ]
    _log(log, f"[REMUX] $ {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            _log(log, f"[ffmpeg] {line}")
    proc.wait()
    if proc.returncode != 0:
        _log(log, f"[ERROR] ffmpeg exited with code {proc.returncode}")
        return False
    _log(log, "[REMUX] done ✓")
    return True


def patch_file(remuxed: Path, output: Path, log: queue.Queue) -> bool:
    """Apply binary patches to the remuxed file."""
    _log(log, f"[PATCH] reading {remuxed.name} ({remuxed.stat().st_size:,} bytes)")
    data = remuxed.read_bytes()

    data = patch_ftyp(data, log)
    data = patch_moov_flags(data, log)

    output.write_bytes(data)
    _log(log, f"[PATCH] written {output.name} ({output.stat().st_size:,} bytes) ✓")
    return True


def run_job(job_id: str, src: Path, original_name: str):
    """Full pipeline: remux → patch → cleanup."""
    log = _job_logs[job_id]
    _job_status[job_id] = "running"

    remuxed = UPLOAD_DIR / f"{job_id}_remuxed.mp4"
    stem = Path(original_name).stem
    out_name = f"{stem}_patched.mp4"
    out_path = OUTPUT_DIR / f"{job_id}_{out_name}"

    try:
        _log(log, f"[JOB] {job_id[:8]}… started")
        _log(log, f"[JOB] input: {original_name} ({src.stat().st_size:,} bytes)")

        # Step 1 — remux
        _log(log, "")
        _log(log, "── STEP 1 / 2  Remux (stream copy + faststart) ─────────────")
        if not remux(src, remuxed, log):
            raise RuntimeError("Remux failed")

        # Step 2 — binary patch
        _log(log, "")
        _log(log, "── STEP 2 / 2  Binary patch (ftyp + mvhd flags) ────────────")
        if not patch_file(remuxed, out_path, log):
            raise RuntimeError("Patch failed")

        _job_output[job_id] = f"{job_id}_{out_name}"
        _job_status[job_id] = "done"
        _log(log, "")
        _log(log, "── ALL STEPS COMPLETE ✓ ─────────────────────────────────────")
        _log(log, f"[DONE] {out_name}")

    except Exception as exc:
        _log(log, f"[ERROR] {exc}")
        _job_status[job_id] = "error"

    finally:
        # Clean up temp files
        for p in [src, remuxed]:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        log.put(None)   # sentinel


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".mp4"):
        return jsonify({"error": "Only .mp4 files are accepted"}), 400

    job_id = str(uuid.uuid4())
    dest = UPLOAD_DIR / f"{job_id}_input.mp4"
    f.save(dest)

    _job_logs[job_id] = queue.Queue()
    t = threading.Thread(target=run_job, args=(job_id, dest, f.filename), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/stream/<job_id>")
def stream(job_id: str):
    """SSE endpoint — streams log lines until job completes."""
    def generate():
        q = _job_logs.get(job_id)
        if q is None:
            yield "data: [ERROR] Unknown job ID\n\n"
            return
        while True:
            try:
                line = q.get(timeout=60)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if line is None:          # sentinel: job done
                status = _job_status.get(job_id, "error")
                out    = _job_output.get(job_id, "")
                yield f"data: __STATUS__{status}|{out}\n\n"
                return
            yield f"data: {line}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/status/<job_id>")
def status(job_id: str):
    s = _job_status.get(job_id, "unknown")
    out = _job_output.get(job_id, "")
    return jsonify({"status": s, "output": out})


@app.route("/download/<filename>")
def download(filename: str):
    # Only serve files that belong to completed jobs
    allowed = set(_job_output.values())
    if filename not in allowed:
        return "Not found", 404
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
