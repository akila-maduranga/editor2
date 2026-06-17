#!/usr/bin/env python3
"""
TikTok MP4 Patcher — Self-hosted VPS tool
Implements 10 structural patches:

  1. Brand spoofing     — ftyp: major=isom, minor=0x200, compat=[isom,iso2,avc1,mp41]
  2. Date zeroing       — mvhd/tkhd/mdhd creation_time + modification_time → 0
  3. Language spoofing  — mdhd language field → 'und' (0x55C4)
  4. Frame count inflate— stts: collapse to 1 entry, set count=19690, delta=1
  5. Fake trailer atom  — append invalid-size box after mdat
  6. Encoder spoofing   — ffmpeg sets Lavf60.16.100 during remux
  7. Comment injection  — ffmpeg -metadata comment injected during remux
  8. Timescale fix      — mdhd timescale → 120 (120 fps)
  9. B-frame limiter    — ctts: cap non-zero offset entries at 2
 10. Bitrate spoof      — inject btrt box in stsd→avc1 with 18 Mbps
 11. stsz count         — skipped (keeps frame mapping intact for playback)
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


def _adjust_chunk_offsets(data: bytes, delta: int) -> bytes:
    """Add delta to every chunk offset in stco/co64 boxes (recursive scan)."""
    if delta == 0:
        return data
    p = bytearray(data)
    CONTAINERS = (b"moov", b"trak", b"mdia", b"minf", b"stbl")

    def _dfs(start: int, end: int):
        i = start
        while i + 8 <= min(end, len(p)):
            size = struct.unpack(">I", p[i:i+4])[0]
            btype = bytes(p[i+4:i+8])
            if size == 0:
                size = end - i
            if size < 8:
                break
            if btype in (b"stco", b"co64"):
                entry_count = struct.unpack(">I", p[i+12:i+16])[0]
                if entry_count > 200000:
                    return
                entry_size = 4 if btype == b"stco" else 8
                for j in range(entry_count):
                    off = i + 16 + j * entry_size
                    if off + entry_size > len(p):
                        break
                    val = struct.unpack(">I" if entry_size == 4 else ">Q", p[off:off+entry_size])[0]
                    struct.pack_into(">I" if entry_size == 4 else ">Q", p, off, val + delta)
            elif btype in CONTAINERS:
                _dfs(i + 8, i + size)
            i += size

    _dfs(0, len(p))
    return bytes(p)

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
    _log(log, f"[PATCH] ftyp  major={old!r} → isom  minor → 0x00000200")
    result = data[:off] + new_ftyp + data[off+sz:]
    if len(new_ftyp) != sz:
        result = _adjust_chunk_offsets(result, len(new_ftyp) - sz)
    return result

# ─────────────────────────────────────────────────────────────────────────────
# Patch 2 — timestamp zeroing (mvhd / tkhd / mdhd)
# ─────────────────────────────────────────────────────────────────────────────

def _zero_timestamps(data: bytes, off: int, name: str, log: queue.Queue) -> bytes:
    bs = off + 8
    v  = data[bs]
    if v == 0:
        ct_off, mt_off, fmt, w = bs+4, bs+8,  ">I", 4
    elif v == 1:
        ct_off, mt_off, fmt, w = bs+4, bs+12, ">Q", 8
    else:
        _log(log, f"[WARN]  {name} unknown version {v}"); return data
    ct = struct.unpack(fmt, data[ct_off:ct_off+w])[0]
    mt = struct.unpack(fmt, data[mt_off:mt_off+w])[0]
    if ct == 0 and mt == 0:
        _log(log, f"[PATCH] {name}  timestamps already zero"); return data
    _log(log, f"[PATCH] {name}  create={ct} modify={mt} → 0/0")
    p = bytearray(data)
    struct.pack_into(fmt, p, ct_off, 0)
    struct.pack_into(fmt, p, mt_off, 0)
    return bytes(p)

def patch_timestamps(data: bytes, log: queue.Queue) -> bytes:
    moov_off, moov_sz = find_box(data, b"moov")
    if moov_off == -1:
        _log(log, "[WARN]  moov not found — skipping timestamps"); return data

    # mvhd
    mvhd_off, _ = find_box(data, b"mvhd", moov_off+8, moov_off+moov_sz)
    if mvhd_off != -1:
        data = _zero_timestamps(data, mvhd_off, "mvhd", log)
        moov_off, moov_sz = find_box(data, b"moov")

    # each trak → tkhd + mdia → mdhd
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
# Patch 3 — language spoofing → 'und' in every mdhd
# mdhd body layout (v=0): [0]ver [1:4]flags [4:8]ct [8:12]mt
#                          [12:16]timescale [16:20]duration [20:22]language
# ─────────────────────────────────────────────────────────────────────────────

def _pack_lang(s: str) -> bytes:
    val = 0
    for c in s:
        val = (val << 5) | (ord(c) - 0x60)
    return struct.pack(">H", val)

UND = _pack_lang("und")   # 0x55C4

def patch_language(data: bytes, log: queue.Queue) -> bytes:
    moov_off, moov_sz = find_box(data, b"moov")
    if moov_off == -1: return data
    changed = False
    for trak_off, trak_sz, tt in list(iter_boxes(data, moov_off+8, moov_off+moov_sz)):
        if tt != b"trak": continue
        mdia_off, mdia_sz = find_box(data, b"mdia", trak_off+8, trak_off+trak_sz)
        if mdia_off == -1: continue
        mdhd_off, _ = find_box(data, b"mdhd", mdia_off+8, mdia_off+mdia_sz)
        if mdhd_off == -1: continue
        lang_off = mdhd_off + 8 + 20   # box header(8) + body offset 20
        current  = data[lang_off:lang_off+2]
        if current == UND:
            _log(log, f"[PATCH] mdhd  language already 'und'")
            continue
        _log(log, f"[PATCH] mdhd  language {current.hex()} → und (0x55c4)")
        p = bytearray(data)
        p[lang_off:lang_off+2] = UND
        data = bytes(p)
        changed = True
    return data

# ─────────────────────────────────────────────────────────────────────────────
# Patch 4 — frame count inflation via stts rewrite
# moov → trak(video) → mdia → minf → stbl → stts
# Collapse to 1 entry: count=19690, delta=1 (120fps with mdhd timescale=120)
# ─────────────────────────────────────────────────────────────────────────────

def _is_video_trak(data: bytes, trak_off: int, trak_sz: int) -> bool:
    """Return True if this trak contains a video handler."""
    mdia_off, mdia_sz = find_box(data, b"mdia", trak_off+8, trak_off+trak_sz)
    if mdia_off == -1: return False
    hdlr_off, hdlr_sz = find_box(data, b"hdlr", mdia_off+8, mdia_off+mdia_sz)
    if hdlr_off == -1: return False
    # hdlr body: [0]ver [1:4]flags [4:8]pre_defined [8:12]handler_type
    handler_type = data[hdlr_off+8+8:hdlr_off+8+12]
    return handler_type == b"vide"

def patch_frame_count(data: bytes, log: queue.Queue) -> bytes:
    moov_off, moov_sz = find_box(data, b"moov")
    if moov_off == -1: return data

    for trak_off, trak_sz, tt in list(iter_boxes(data, moov_off+8, moov_off+moov_sz)):
        if tt != b"trak": continue
        if not _is_video_trak(data, trak_off, trak_sz): continue

        mdia_off, mdia_sz = find_box(data, b"mdia", trak_off+8, trak_off+trak_sz)
        if mdia_off == -1: continue
        minf_off, minf_sz = find_box(data, b"minf", mdia_off+8, mdia_off+mdia_sz)
        if minf_off == -1: continue
        stbl_off, stbl_sz = find_box(data, b"stbl", minf_off+8, minf_off+minf_sz)
        if stbl_off == -1: continue
        stts_off, stts_sz = find_box(data, b"stts", stbl_off+8, stbl_off+stbl_sz)
        if stts_off == -1: continue

        # Read current stts
        body_off  = stts_off + 8
        entry_count = struct.unpack(">I", data[body_off+4:body_off+8])[0]
        real_frames = sum(
            struct.unpack(">I", data[body_off+8+i*8:body_off+8+i*8+4])[0]
            for i in range(entry_count)
        )

        TARGET = 19690
        _log(log, f"[PATCH] stts  real_frames={real_frames} → {TARGET}  delta=1")

        # Build new stts: 1 entry, delta=1 (120fps with mdhd timescale=120)
        new_body = (
            b"\x00\x00\x00\x00"
            + struct.pack(">I", 1)           # entry_count = 1
            + struct.pack(">I", TARGET)       # sample_count
            + struct.pack(">I", 1)            # sample_delta = 1
        )
        new_stts = struct.pack(">I", 8+len(new_body)) + b"stts" + new_body

        # Pad with a free box to keep all offsets valid
        size_diff = stts_sz - len(new_stts)
        if size_diff >= 8:
            free_box = struct.pack(">I", size_diff) + b"free" + b"\x00"*(size_diff-8)
            replacement = new_stts + free_box
        elif size_diff == 0:
            replacement = new_stts
        else:
            replacement = new_stts

        data = data[:stts_off] + replacement + data[stts_off+stts_sz:]
        break   # only patch video track

    return data

# ─────────────────────────────────────────────────────────────────────────────
# Patch 5 — fake trailer atom (triggers "Unknown trailer with invalid atom size")
# Append a box with size=2 (< minimum valid 8) right after the last byte
# ─────────────────────────────────────────────────────────────────────────────

FAKE_TRAILER = struct.pack(">I", 2) + b"junk"   # size=2 → invalid

def patch_fake_trailer(data: bytes, log: queue.Queue) -> bytes:
    _log(log, f"[PATCH] trailer  appending {len(FAKE_TRAILER)}-byte invalid atom")
    return data + FAKE_TRAILER

# ─────────────────────────────────────────────────────────────────────────────
# Patch 8 — mdhd timescale → 120 (120 fps)
# ─────────────────────────────────────────────────────────────────────────────

def patch_mdhd_timescale(data: bytes, log: queue.Queue) -> bytes:
    moov_off, moov_sz = find_box(data, b"moov")
    if moov_off == -1: return data
    for trak_off, trak_sz, tt in list(iter_boxes(data, moov_off+8, moov_off+moov_sz)):
        if tt != b"trak": continue
        if not _is_video_trak(data, trak_off, trak_sz): continue
        mdia_off, mdia_sz = find_box(data, b"mdia", trak_off+8, trak_off+trak_sz)
        if mdia_off == -1: continue
        mdhd_off, _ = find_box(data, b"mdhd", mdia_off+8, mdia_off+mdia_sz)
        if mdhd_off == -1: continue
        v = data[mdhd_off+8]
        if v == 0:      ts_off = mdhd_off + 20  # ver+flags(4)+ctime(4)+mtime(4)
        elif v == 1:    ts_off = mdhd_off + 32  # ver+flags(4)+ctime(8)+mtime(8)
        else:           continue
        current = struct.unpack(">I", data[ts_off:ts_off+4])[0]
        _log(log, f"[PATCH] mdhd  timescale {current} → 120")
        p = bytearray(data)
        struct.pack_into(">I", p, ts_off, 120)
        return bytes(p)
    return data

# ─────────────────────────────────────────────────────────────────────────────
# Patch 9 — stsz sample count → 19690
# ─────────────────────────────────────────────────────────────────────────────

def patch_stsz_count(data: bytes, log: queue.Queue) -> bytes:
    moov_off, moov_sz = find_box(data, b"moov")
    if moov_off == -1: return data
    for trak_off, trak_sz, tt in list(iter_boxes(data, moov_off+8, moov_off+moov_sz)):
        if tt != b"trak": continue
        if not _is_video_trak(data, trak_off, trak_sz): continue
        mdia_off, mdia_sz = find_box(data, b"mdia", trak_off+8, trak_off+trak_sz)
        if mdia_off == -1: continue
        minf_off, minf_sz = find_box(data, b"minf", mdia_off+8, mdia_off+mdia_sz)
        if minf_off == -1: continue
        stbl_off, stbl_sz = find_box(data, b"stbl", minf_off+8, minf_off+minf_sz)
        if stbl_off == -1: continue
        stsz_off, stsz_sz = find_box(data, b"stsz", stbl_off+8, stbl_off+stbl_sz)
        if stsz_off == -1: continue
        old_count = struct.unpack(">I", data[stsz_off+16:stsz_off+20])[0]
        _log(log, f"[SKIP]  stsz count {old_count} — inflated via stts; real entries kept for playback")
        return data
    return data

# ─────────────────────────────────────────────────────────────────────────────
# Patch 10 — ctts B-frame limiter → 2
# ─────────────────────────────────────────────────────────────────────────────

def patch_ctts_bframes(data: bytes, log: queue.Queue) -> bytes:
    moov_off, moov_sz = find_box(data, b"moov")
    if moov_off == -1: return data
    for trak_off, trak_sz, tt in list(iter_boxes(data, moov_off+8, moov_off+moov_sz)):
        if tt != b"trak": continue
        if not _is_video_trak(data, trak_off, trak_sz): continue
        mdia_off, mdia_sz = find_box(data, b"mdia", trak_off+8, trak_off+trak_sz)
        if mdia_off == -1: continue
        minf_off, minf_sz = find_box(data, b"minf", mdia_off+8, mdia_off+mdia_sz)
        if minf_off == -1: continue
        stbl_off, stbl_sz = find_box(data, b"stbl", minf_off+8, minf_off+minf_sz)
        if stbl_off == -1: continue
        ctts_off, ctts_sz = find_box(data, b"ctts", stbl_off+8, stbl_off+stbl_sz)
        if ctts_off == -1:
            _log(log, "[WARN]  ctts not found — skipping B-frame patch")
            return data

        body_off = ctts_off + 8
        entry_count = struct.unpack(">I", data[body_off+4:body_off+8])[0]
        p = bytearray(data)
        non_zero = 0
        for i in range(entry_count):
            off = body_off + 8 + i*8 + 4
            val = struct.unpack(">i", data[off:off+4])[0]
            if val != 0:
                non_zero += 1
                if non_zero > 2:
                    struct.pack_into(">I", p, off, 0)
        _log(log, f"[PATCH] ctts  B-frame entries → 2 (was {non_zero})")
        return bytes(p)
    return data

# ─────────────────────────────────────────────────────────────────────────────
# Patch 11 — btrt bitrate box → 18 Mbps
# MediaInfo calculates bitrate as stream_size×8/duration.  The stts inflation
# (19690@120fps → 164s) drops calculated bitrate 10×.  We inject a btrt box
# inside stsd→avc1 with explicit avgBitrate/maxBitrate — MediaInfo reads this
# in preference to the calculation.
# ─────────────────────────────────────────────────────────────────────────────

def patch_bitrate(data: bytes, log: queue.Queue) -> bytes:
    TARGET = 18_000_000
    moov_off, moov_sz = find_box(data, b"moov")
    if moov_off == -1: return data

    for trak_off, trak_sz, tt in list(iter_boxes(data, moov_off+8, moov_off+moov_sz)):
        if tt != b"trak": continue
        if not _is_video_trak(data, trak_off, trak_sz): continue

        mdia_off, mdia_sz = find_box(data, b"mdia", trak_off+8, trak_off+trak_sz)
        if mdia_off == -1: continue
        minf_off, minf_sz = find_box(data, b"minf", mdia_off+8, mdia_off+mdia_sz)
        if minf_off == -1: continue
        stbl_off, stbl_sz = find_box(data, b"stbl", minf_off+8, minf_off+minf_sz)
        if stbl_off == -1: continue
        stsd_off, stsd_sz = find_box(data, b"stsd", stbl_off+8, stbl_off+stbl_sz)
        if stsd_off == -1: continue

        # Walk sample entries inside stsd
        pos = stsd_off + 16  # size(4)+type(4)+ver+flags(4)+entry_count(4)
        while pos + 16 < stsd_off + stsd_sz:
            entry_sz = struct.unpack(">I", data[pos:pos+4])[0]
            entry_ty = data[pos+4:pos+8]
            if entry_sz < 8: break
            if entry_ty not in (b"avc1", b"hvc1", b"hev1", b"mp4v"):
                pos += entry_sz
                continue

            # Look for existing btrt box inside this entry
            btrt_off, btrt_sz = find_box(data, b"btrt", pos+8, pos+entry_sz)
            if btrt_off != -1:
                p = bytearray(data)
                struct.pack_into(">I", p, btrt_off+12, TARGET)  # maxBitrate
                struct.pack_into(">I", p, btrt_off+16, TARGET)  # avgBitrate
                _log(log, f"[PATCH] btrt  bitrate → {TARGET:,} bps (existing box)")
                return bytes(p)

            # No btrt — create one after avcC
            avcC_off, avcC_sz = find_box(data, b"avcC", pos+8, pos+entry_sz)
            insert_off = (avcC_off + avcC_sz) if avcC_off != -1 else (pos + entry_sz)
            btrt_box = struct.pack(">I", 20) + b"btrt" + struct.pack(">III", TARGET, TARGET, TARGET)
            delta = len(btrt_box)

            p = bytearray(data)
            struct.pack_into(">I", p, pos, entry_sz + delta)
            p[insert_off:insert_off] = btrt_box
            for poff in (stsd_off, stbl_off, minf_off, mdia_off, trak_off, moov_off):
                old = struct.unpack(">I", p[poff:poff+4])[0]
                struct.pack_into(">I", p, poff, old + delta)
            result = _adjust_chunk_offsets(bytes(p), delta)
            _log(log, f"[PATCH] btrt  created → {TARGET:,} bps")
            return result

    _log(log, "[WARN]  btrt  no video sample entry found — skipping")
    return data

# ─────────────────────────────────────────────────────────────────────────────
# Remux (patches 6 + 7 — encoder spoofing + comment injection)
# ffmpeg automatically writes encoder=Lavf60.16.100 as the muxer tag.
# We inject comment + artist via -metadata flags.
# ─────────────────────────────────────────────────────────────────────────────

def remux(src: Path, dst: Path, comment: str, log: queue.Queue) -> bool:
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-c", "copy",
        "-movflags", "+faststart",
        "-map_metadata", "-1",          # strip original metadata first
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

        _log(log, ""); _log(log, "── 1/11 Remux + encoder spoof + comment inject ──────────────")
        if not remux(src, remuxed, comment, log): raise RuntimeError("Remux failed")

        _log(log, ""); _log(log, "── 2/11 Reading remuxed file ────────────────────────────────")
        raw = remuxed.read_bytes()
        _log(log, f"[READ] {len(raw):,} bytes")

        _log(log, ""); _log(log, "── 3/11 ftyp brand spoof ────────────────────────────────────")
        raw = patch_ftyp(raw, log)

        _log(log, ""); _log(log, "── 4/11 Timestamp zeroing (mvhd / tkhd / mdhd) ─────────────")
        raw = patch_timestamps(raw, log)

        _log(log, ""); _log(log, "── 5/11 Language spoof → 'und' ──────────────────────────────")
        raw = patch_language(raw, log)

        _log(log, ""); _log(log, "── 6/11 Frame count inflation (stts) ────────────────────────")
        raw = patch_frame_count(raw, log)

        _log(log, ""); _log(log, "── 7/11 mdhd timescale → 120 (120 fps) ─────────────────────")
        raw = patch_mdhd_timescale(raw, log)

        _log(log, ""); _log(log, "── 8/11 stsz sample count (skipped — keeps frame mapping) ───")
        raw = patch_stsz_count(raw, log)

        _log(log, ""); _log(log, "── 9/11 ctts B-frame limiter → 2 ────────────────────────────")
        raw = patch_ctts_bframes(raw, log)

        _log(log, ""); _log(log, "── 10/11 btrt bitrate box → 18 Mbps ─────────────────────────")
        raw = patch_bitrate(raw, log)

        _log(log, ""); _log(log, "── 11/11 Fake trailer atom ───────────────────────────────────")
        raw = patch_fake_trailer(raw, log)

        out_path.write_bytes(raw)
        _log(log, f"\n[WRITE] {out_path.name}  ({out_path.stat().st_size:,} bytes)")
        _job_output[job_id] = f"{job_id}_{out_name}"
        _job_status[job_id] = "done"
        _log(log, ""); _log(log, "── ALL 11 PATCHES APPLIED ✓ ─────────────────────────────────")
        _log(log, f"[DONE]  {out_name}")

    except Exception as exc:
        _log(log, f"[ERROR] {exc}")
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
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.route("/download/<filename>")
def download(filename: str):
    if filename not in set(_job_output.values()): return "Not found", 404
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
