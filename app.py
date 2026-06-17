#!/usr/bin/env python3
"""
TikTok MP4 Patcher — Self-hosted VPS tool
All 7 structural patches, verified against real working output:

  1. Brand spoof      — ftyp: major=isom, minor=0x200, compat=[isom,iso2,avc1,mp41]
  2. Date zeroing     — mvhd/tkhd/mdhd creation_time + modification_time → 0
  3. Language spoof   — mdhd language field → 'und' (0x55C4)
  4. Frame inflate    — stts IN-PLACE rewrite: delta=timescale//120 (forces 120fps),
                        sample_count=19690, padding entries=(0,1); zero size drift
  5. Fake trailer     — append invalid-size atom after mdat (ExifTool warning)
  6. Encoder spoof    — ffmpeg sets Lavf60.16.100 automatically during remux
  7. Comment inject   — ffmpeg -metadata comment/encoder injected during remux
     moov at END      — no +faststart; ffmpeg default puts moov after mdat
"""

import os, uuid, subprocess, threading, queue, struct, shutil
from pathlib import Path
from flask import (
    Flask, request, render_template, jsonify,
    send_from_directory, Response, stream_with_context
)

app = Flask(__name__)

BASE_DIR   = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

_job_logs:   dict[str, queue.Queue] = {}
_job_status: dict[str, str]         = {}
_job_output: dict[str, str]         = {}

TARGET_FPS    = 120
TARGET_FRAMES = 19690   # matches confirmed working output

# ─────────────────────────────────────────────────────────────────────────────
# Box-tree helpers
# ─────────────────────────────────────────────────────────────────────────────

def _log(q, msg): q.put(msg)

def iter_boxes(data: bytes, start: int = 0, end: int | None = None):
    if end is None: end = len(data)
    i = start
    while i + 8 <= end:
        size = struct.unpack(">I", data[i:i+4])[0]
        btype = data[i+4:i+8]
        if size == 0: size = end - i
        if size < 8: break
        yield i, size, btype
        i += size

def find_box(data: bytes, box_type: bytes, start: int = 0, end: int | None = None):
    for off, sz, bt in iter_boxes(data, start, end):
        if bt == box_type: return off, sz
    return -1, 0

def read_mdhd_timescale(data: bytes, trak_off: int, trak_sz: int) -> int:
    """Read timescale from the mdhd box inside this trak."""
    mdia_off, mdia_sz = find_box(data, b"mdia", trak_off+8, trak_off+trak_sz)
    if mdia_off == -1: return 90000  # fallback
    mdhd_off, _ = find_box(data, b"mdhd", mdia_off+8, mdia_off+mdia_sz)
    if mdhd_off == -1: return 90000
    # mdhd body (v=0): ver(1)+flags(3)+ct(4)+mt(4)+timescale(4)+...
    # mdhd body (v=1): ver(1)+flags(3)+ct(8)+mt(8)+timescale(4)+...
    bs = mdhd_off + 8
    v  = data[bs]
    ts_off = bs + 4 + (8 if v == 0 else 16)  # skip ver+flags, then ct+mt
    return struct.unpack(">I", data[ts_off:ts_off+4])[0]

# ─────────────────────────────────────────────────────────────────────────────
# Patch 1 — ftyp brand spoof
# ─────────────────────────────────────────────────────────────────────────────

FTYP_MAJOR  = b"isom"
FTYP_MINOR  = struct.pack(">I", 0x00000200)
FTYP_COMPAT = b"isom" b"iso2" b"avc1" b"mp41"

def patch_ftyp(data: bytes, log: queue.Queue) -> bytes:
    off, sz = find_box(data, b"ftyp")
    if off == -1:
        _log(log, "[WARN]  ftyp not found — skipping"); return data
    old = data[off+8:off+12].decode("latin1")
    body     = FTYP_MAJOR + FTYP_MINOR + FTYP_COMPAT
    new_ftyp = struct.pack(">I", 8+len(body)) + b"ftyp" + body
    _log(log, f"[PATCH] ftyp  {old!r} → isom  minor → 0x00000200")
    return data[:off] + new_ftyp + data[off+sz:]

# ─────────────────────────────────────────────────────────────────────────────
# Patch 2 — timestamp zeroing (mvhd / tkhd / mdhd)
# ─────────────────────────────────────────────────────────────────────────────

def _zero_timestamps(data: bytes, off: int, name: str, log: queue.Queue) -> bytes:
    bs = off + 8
    v  = data[bs]
    if v == 0:
        ct_off, mt_off, fmt, w = bs+4,  bs+8,  ">I", 4
    elif v == 1:
        ct_off, mt_off, fmt, w = bs+4,  bs+12, ">Q", 8
    else:
        _log(log, f"[WARN]  {name} unknown version {v}"); return data
    ct = struct.unpack(fmt, data[ct_off:ct_off+w])[0]
    mt = struct.unpack(fmt, data[mt_off:mt_off+w])[0]
    if ct == 0 and mt == 0:
        _log(log, f"[PATCH] {name}  timestamps already 0"); return data
    p = bytearray(data)
    struct.pack_into(fmt, p, ct_off, 0)
    struct.pack_into(fmt, p, mt_off, 0)
    _log(log, f"[PATCH] {name}  create={ct} modify={mt} → 0/0")
    return bytes(p)

def patch_timestamps(data: bytes, log: queue.Queue) -> bytes:
    moov_off, moov_sz = find_box(data, b"moov")
    if moov_off == -1:
        _log(log, "[WARN]  moov not found — skipping timestamps"); return data
    mvhd_off, _ = find_box(data, b"mvhd", moov_off+8, moov_off+moov_sz)
    if mvhd_off != -1:
        data = _zero_timestamps(data, mvhd_off, "mvhd", log)
        moov_off, moov_sz = find_box(data, b"moov")
    for trak_off, trak_sz, tt in list(iter_boxes(data, moov_off+8, moov_off+moov_sz)):
        if tt != b"trak": continue
        tkhd_off, _ = find_box(data, b"tkhd", trak_off+8, trak_off+trak_sz)
        if tkhd_off != -1:
            data = _zero_timestamps(data, tkhd_off, "tkhd", log)
        mdia_off, mdia_sz = find_box(data, b"mdia", trak_off+8, trak_off+trak_sz)
        if mdia_off != -1:
            mdhd_off, _ = find_box(data, b"mdhd", mdia_off+8, mdia_off+mdia_sz)
            if mdhd_off != -1:
                data = _zero_timestamps(data, mdhd_off, "mdhd", log)
    return data

# ─────────────────────────────────────────────────────────────────────────────
# Patch 3 — language → 'und' in every mdhd
# mdhd body (v=0): ver+flags(4) ct(4) mt(4) timescale(4) duration(4) language(2)
# mdhd body (v=1): ver+flags(4) ct(8) mt(8) timescale(4) duration(8) language(2)
# ─────────────────────────────────────────────────────────────────────────────

def _pack_lang(s: str) -> bytes:
    val = 0
    for c in s: val = (val << 5) | (ord(c) - 0x60)
    return struct.pack(">H", val)

UND = _pack_lang("und")   # 0x55C4

def _mdhd_lang_offset(data: bytes, mdhd_off: int) -> int:
    """Return absolute byte offset of the language field in mdhd."""
    bs = mdhd_off + 8
    v  = data[bs]
    # v=0: 4(ver+flags)+4(ct)+4(mt)+4(ts)+4(dur) = 20 bytes before language
    # v=1: 4(ver+flags)+8(ct)+8(mt)+4(ts)+8(dur) = 32 bytes before language
    return bs + (20 if v == 0 else 32)

def patch_language(data: bytes, log: queue.Queue) -> bytes:
    moov_off, moov_sz = find_box(data, b"moov")
    if moov_off == -1: return data
    for trak_off, trak_sz, tt in list(iter_boxes(data, moov_off+8, moov_off+moov_sz)):
        if tt != b"trak": continue
        mdia_off, mdia_sz = find_box(data, b"mdia", trak_off+8, trak_off+trak_sz)
        if mdia_off == -1: continue
        mdhd_off, _ = find_box(data, b"mdhd", mdia_off+8, mdia_off+mdia_sz)
        if mdhd_off == -1: continue
        lang_off = _mdhd_lang_offset(data, mdhd_off)
        current  = data[lang_off:lang_off+2]
        if current == UND:
            _log(log, "[PATCH] mdhd  language already und"); continue
        p = bytearray(data)
        p[lang_off:lang_off+2] = UND
        data = bytes(p)
        _log(log, f"[PATCH] mdhd  language {current.hex()} → 55c4 (und)")
    return data

# ─────────────────────────────────────────────────────────────────────────────
# Patch 4 — stts IN-PLACE rewrite for fps=120 and nb_frames=TARGET_FRAMES
#
# Key insight: rewrite body bytes only, keep box size identical → no offset drift.
# entry[0] = (TARGET_FRAMES, timescale//120)  → correct fps + frame count
# entry[1..N-1] = (0, 1)                      → no-op padding, fills old space
# ─────────────────────────────────────────────────────────────────────────────

def _is_video_trak(data: bytes, trak_off: int, trak_sz: int) -> bool:
    mdia_off, mdia_sz = find_box(data, b"mdia", trak_off+8, trak_off+trak_sz)
    if mdia_off == -1: return False
    hdlr_off, _ = find_box(data, b"hdlr", mdia_off+8, mdia_off+mdia_sz)
    if hdlr_off == -1: return False
    # hdlr body: ver+flags(4) pre_defined(4) handler_type(4)
    return data[hdlr_off+8+8:hdlr_off+8+12] == b"vide"

def patch_frame_count(data: bytes, log: queue.Queue) -> bytes:
    moov_off, moov_sz = find_box(data, b"moov")
    if moov_off == -1:
        _log(log, "[WARN]  moov not found — skipping stts patch"); return data

    for trak_off, trak_sz, tt in list(iter_boxes(data, moov_off+8, moov_off+moov_sz)):
        if tt != b"trak": continue
        if not _is_video_trak(data, trak_off, trak_sz): continue

        # Read mdhd timescale for this track
        timescale = read_mdhd_timescale(data, trak_off, trak_sz)
        sample_delta = timescale // TARGET_FPS
        if sample_delta == 0: sample_delta = 1

        mdia_off, mdia_sz = find_box(data, b"mdia", trak_off+8, trak_off+trak_sz)
        if mdia_off == -1: continue
        minf_off, minf_sz = find_box(data, b"minf", mdia_off+8, mdia_off+mdia_sz)
        if minf_off == -1: continue
        stbl_off, stbl_sz = find_box(data, b"stbl", minf_off+8, minf_off+minf_sz)
        if stbl_off == -1: continue
        stts_off, stts_sz = find_box(data, b"stts", stbl_off+8, stbl_off+stbl_sz)
        if stts_off == -1:
            _log(log, "[WARN]  stts not found inside stbl"); continue

        body_off    = stts_off + 8
        entry_count = struct.unpack(">I", data[body_off+4:body_off+8])[0]

        # Real frame count (sum of sample_counts)
        real_frames = 0
        for i in range(entry_count):
            base = body_off + 8 + i*8
            real_frames += struct.unpack(">I", data[base:base+4])[0]

        _log(log, f"[PATCH] stts  timescale={timescale}  delta={sample_delta}  "
                  f"→ {timescale/sample_delta:.1f} fps")
        _log(log, f"[PATCH] stts  frames {real_frames} → {TARGET_FRAMES}  "
                  f"entry_count={entry_count} (in-place)")

        # Build replacement body — EXACT same byte length as original
        # body layout: ver+flags(4) + entry_count(4) + entry_count×8
        p = bytearray(data)

        # entry[0]: real data
        e0_off = body_off + 8
        struct.pack_into(">I", p, e0_off,   TARGET_FRAMES)
        struct.pack_into(">I", p, e0_off+4, sample_delta)

        # entry[1..N-1]: no-op (sample_count=0 entries are skipped by decoders)
        for i in range(1, entry_count):
            base = body_off + 8 + i*8
            struct.pack_into(">I", p, base,   0)
            struct.pack_into(">I", p, base+4, 1)

        data = bytes(p)
        _log(log, f"[PATCH] stts  ✓ wrote {entry_count} entries, box size unchanged ({stts_sz}B)")
        break   # only patch video trak

    return data

# ─────────────────────────────────────────────────────────────────────────────
# Patch 5 — fake trailer atom
# ─────────────────────────────────────────────────────────────────────────────

FAKE_TRAILER = struct.pack(">I", 2) + b"junk"

def patch_fake_trailer(data: bytes, log: queue.Queue) -> bytes:
    _log(log, f"[PATCH] trailer  appending invalid-size atom ({len(FAKE_TRAILER)} bytes)")
    return data + FAKE_TRAILER

# ─────────────────────────────────────────────────────────────────────────────
# Remux — patches 6+7: encoder tag + comment injection
# NOTE: no +faststart → moov stays at END of file (after mdat)
# ─────────────────────────────────────────────────────────────────────────────

def remux(src: Path, dst: Path, comment: str, log: queue.Queue) -> bool:
    # No -movflags at all → ffmpeg default is Non-Faststart (ftyp → mdat → moov).
    # +faststart is explicitly NOT used.  default_base_moof is for fragmented MP4
    # and must NOT be used here.
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-c", "copy",
        "-map_metadata", "-1",
        "-metadata", f"comment={comment}",
        "-metadata", "encoder=Lavf60.16.100",
        str(dst),
    ]
    _log(log, f"[REMUX] $ {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        line = line.rstrip()
        if line: _log(log, f"[ffmpeg] {line}")
    proc.wait()
    if proc.returncode != 0:
        _log(log, f"[ERROR] ffmpeg exited {proc.returncode}"); return False
    _log(log, "[REMUX] done ✓")
    return True


def assert_moov_at_end(data: bytes, log: queue.Queue) -> None:
    """Hard abort if moov appears before mdat — Non-Faststart is mandatory."""
    moov_off, _ = find_box(data, b"moov")
    mdat_off, _ = find_box(data, b"mdat")
    if moov_off == -1: raise RuntimeError("moov box not found after remux")
    if mdat_off == -1: raise RuntimeError("mdat box not found after remux")
    if moov_off < mdat_off:
        raise RuntimeError(
            f"moov@{moov_off} is BEFORE mdat@{mdat_off} — "
            f"file is Faststart (forbidden). Remux produced wrong atom order."
        )
    _log(log, f"[CHECK] Non-Faststart ✓  mdat@{mdat_off} → moov@{moov_off}")

# ─────────────────────────────────────────────────────────────────────────────
# Full pipeline
# ─────────────────────────────────────────────────────────────────────────────

def run_job(job_id: str, src: Path, original_name: str, comment: str):
    log = _job_logs[job_id]
    _job_status[job_id] = "running"

    remuxed  = UPLOAD_DIR / f"{job_id}_remuxed.mp4"
    stem     = Path(original_name).stem
    out_name = f"{stem}_patched.mp4"
    out_path = OUTPUT_DIR / f"{job_id}_{out_name}"

    try:
        _log(log, f"[JOB]  {job_id[:8]}… started")
        _log(log, f"[JOB]  input: {original_name}  ({src.stat().st_size:,} bytes)")

        _log(log, ""); _log(log, "── 1/7  Remux (stream-copy, moov at end, meta inject) ───────")
        if not remux(src, remuxed, comment, log): raise RuntimeError("Remux failed")

        _log(log, ""); _log(log, "── 2/7  Verify Non-Faststart (moov at end) ──────────────────")
        raw = remuxed.read_bytes()
        _log(log, f"[READ] {len(raw):,} bytes")
        assert_moov_at_end(raw, log)   # hard abort if moov is before mdat

        _log(log, ""); _log(log, "── 3/7  ftyp brand spoof ────────────────────────────────────")
        raw = patch_ftyp(raw, log)

        _log(log, ""); _log(log, "── 4/7  Timestamp zeroing (mvhd / tkhd / mdhd) ─────────────")
        raw = patch_timestamps(raw, log)

        _log(log, ""); _log(log, "── 5/7  Language spoof → und ────────────────────────────────")
        raw = patch_language(raw, log)

        _log(log, ""); _log(log, "── 6/7  Frame count + fps inflate (stts in-place) ───────────")
        raw = patch_frame_count(raw, log)

        _log(log, ""); _log(log, "── 7/7  Fake trailer atom ───────────────────────────────────")
        raw = patch_fake_trailer(raw, log)

        out_path.write_bytes(raw)
        _log(log, f"\n[WRITE] {out_path.name}  ({out_path.stat().st_size:,} bytes)")
        _job_output[job_id] = f"{job_id}_{out_name}"
        _job_status[job_id] = "done"
        _log(log, ""); _log(log, "── ALL 7 PATCHES APPLIED ✓ ──────────────────────────────────")
        _log(log, f"[DONE]  {out_name}")

    except Exception as exc:
        import traceback
        _log(log, f"[ERROR] {exc}")
        _log(log, traceback.format_exc())
        _job_status[job_id] = "error"
    finally:
        for p in [src, remuxed]:
            try: p.unlink(missing_ok=True)
            except: pass
        log.put(None)

# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index(): return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".mp4"):
        return jsonify({"error": "Only .mp4 files accepted"}), 400
    comment = request.form.get("comment", "Patched")
    job_id  = str(uuid.uuid4())
    dest    = UPLOAD_DIR / f"{job_id}_input.mp4"
    f.save(dest)
    _job_logs[job_id] = queue.Queue()
    threading.Thread(target=run_job, args=(job_id, dest, f.filename, comment), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/stream/<job_id>")
def stream(job_id: str):
    def generate():
        q = _job_logs.get(job_id)
        if q is None: yield "data: [ERROR] Unknown job\n\n"; return
        while True:
            try: line = q.get(timeout=60)
            except queue.Empty: yield ": keepalive\n\n"; continue
            if line is None:
                status = _job_status.get(job_id, "error")
                out    = _job_output.get(job_id, "")
                yield f"data: __STATUS__{status}|{out}\n\n"; return
            yield f"data: {line}\n\n"
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/download/<filename>")
def download(filename: str):
    if filename not in set(_job_output.values()): return "Not found", 404
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
