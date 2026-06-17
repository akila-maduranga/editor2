#!/usr/bin/env python3
"""
Core patching engine — merges binary MP4 patching techniques.

All 7 target patches:
  1. Date zeroing       — mvhd/tkhd/mdhd timestamps -> 0
  2. Language spoofing  — mdhd language field -> 'und'
  3. Frame inflation    — stsz entries x10 (stco/co64 adjusted)
  4. Encoder spoofing   -> Lavf60.16.100 via ffmpeg
  5. Comment injection  -> itunes ilst box
  6. Free atom insert   -> between ftyp and mdat
  7. Fake trailer atom  -> 'Unknown trailer with invalid atom size'
"""

import struct
import subprocess
from pathlib import Path

_SCRIPT_DIR = Path(__file__).parent

CONTAINERS = [b'moov', b'trak', b'mdia', b'minf', b'stbl', b'edts', b'udta', b'meta', b'ilst']
VERSION_ATOMS = [b'meta']


def read_atoms_in_range(data, offset, end_pos):
    atoms = []
    while offset + 8 <= end_pos and offset + 8 <= len(data):
        size = int.from_bytes(data[offset:offset+4], 'big')
        if size == 0:
            break
        if size == 1:
            size = int.from_bytes(data[offset+8:offset+16], 'big')
            header_size = 16
        else:
            header_size = 8
        atom_end = offset + size
        if atom_end > end_pos:
            atom_end = end_pos
        name = bytes(data[offset+4:offset+8])
        if name in CONTAINERS:
            version_offset = 4 if name in VERSION_ATOMS else 0
            children, _ = read_atoms_in_range(data, offset + header_size + version_offset, atom_end)
            atoms.append({'name': name, 'children': children, 'start': offset, 'size': size})
        else:
            atoms.append({'name': name, 'data': bytes(data[offset+header_size:atom_end]),
                          'start': offset, 'size': size})
        offset = atom_end
    return atoms, offset


def find_atom(atoms, path):
    if not path:
        return atoms
    for atom in atoms:
        if atom['name'] == path[0]:
            if len(path) == 1:
                return atom
            if 'children' in atom:
                res = find_atom(atom['children'], path[1:])
                if res:
                    return res
    return None


def inject_fake_frames(data, target_frames=None, pre_shift=0, stts_overflow=True, moov_before_mdat=True):
    moov_pos = data.find(b'moov')
    if moov_pos < 4:
        return None
    moov_size_pos = moov_pos - 4
    moov_size = int.from_bytes(data[moov_size_pos:moov_size_pos+4], 'big')

    tree, _ = read_atoms_in_range(data, moov_pos + 4, moov_pos + moov_size)

    video_trak = None
    for atom in tree:
        if atom['name'] == b'trak':
            hdlr = find_atom(atom['children'], [b'mdia', b'hdlr'])
            if hdlr and b'vide' in hdlr['data']:
                video_trak = atom
                break
    if not video_trak:
        return None

    stbl = find_atom(video_trak['children'], [b'mdia', b'minf', b'stbl'])
    if not stbl:
        return None
    minf = find_atom(video_trak['children'], [b'mdia', b'minf'])
    mdia = find_atom(video_trak['children'], [b'mdia'])

    stsz = find_atom(stbl['children'], [b'stsz'])
    if not stsz:
        return None

    stsz_data = bytearray(stsz['data'])
    orig_count = int.from_bytes(stsz_data[8:12], 'big')
    if target_frames is None:
        target_frames = orig_count * 10
    diff = target_frames - orig_count
    if diff <= 0:
        return data

    new_entries = b'\x00\x00\x00\x00' * diff
    result = bytearray(data)

    stsz_start_in_file = stsz['start']
    old_stsz_data_len = len(stsz['data'])
    stsz_data[8:12] = target_frames.to_bytes(4, 'big')
    new_stsz_data = bytes(stsz_data) + new_entries
    growth = len(new_stsz_data) - old_stsz_data_len

    result[stsz_start_in_file + 8:stsz_start_in_file + 8 + old_stsz_data_len] = new_stsz_data

    if stts_overflow:
        stts = find_atom(stbl['children'], [b'stts'])
        if stts:
            stts_start = stts['start']
            old_stts_data_len = len(stts['data'])
            stts_data = bytearray(stts['data'])
            entry_count = int.from_bytes(stts_data[8:12], 'big')
            stts_data[8:12] = (entry_count + diff).to_bytes(4, 'big')
            result[stts_start + 8:stts_start + 8 + old_stts_data_len] = bytes(stts_data)

    for parent in [stsz, stbl, minf, mdia, video_trak]:
        old_sz = parent['size']
        new_sz = old_sz + growth
        result[parent['start']:parent['start'] + 4] = new_sz.to_bytes(4, 'big')
    new_moov_size = moov_size + growth
    result[moov_size_pos:moov_size_pos+4] = new_moov_size.to_bytes(4, 'big')

    mdat_growth = growth if moov_before_mdat else 0
    video_stsz_start = stsz['start']
    for trak in tree:
        if trak['name'] == b'trak':
            t_stbl = find_atom(trak['children'], [b'mdia', b'minf', b'stbl'])
            if not t_stbl:
                continue
            for child in t_stbl['children']:
                if child['name'] == b'stco':
                    pos_shift = growth if child['start'] > video_stsz_start else 0
                    co_data = bytearray(child['data'])
                    entry_count = int.from_bytes(co_data[4:8], 'big')
                    for i in range(entry_count):
                        idx = 8 + i * 4
                        val = int.from_bytes(co_data[idx:idx+4], 'big')
                        co_data[idx:idx+4] = (val + mdat_growth + pre_shift).to_bytes(4, 'big')
                    result[child['start'] + pos_shift + 8:
                           child['start'] + pos_shift + 8 + len(child['data'])] = bytes(co_data)
                elif child['name'] == b'co64':
                    pos_shift = growth if child['start'] > video_stsz_start else 0
                    co_data = bytearray(child['data'])
                    entry_count = int.from_bytes(co_data[4:8], 'big')
                    for i in range(entry_count):
                        idx = 8 + i * 8
                        val = int.from_bytes(co_data[idx:idx+8], 'big')
                        co_data[idx:idx+8] = (val + mdat_growth + pre_shift).to_bytes(8, 'big')
                    result[child['start'] + pos_shift + 8:
                           child['start'] + pos_shift + 8 + len(child['data'])] = bytes(co_data)

    return bytes(result)


def build_metadata_tree(artist, copyright, custom_tag, encoder="Lavf60.16.100"):
    entries = {}
    if encoder:
        entries[b'\xa9too'] = encoder
    if artist:
        entries[b'\xa9ART'] = artist
    if copyright:
        entries[b'\xa9cpy'] = copyright
    if custom_tag:
        entries[b'\xa9cmt'] = custom_tag

    ilst_data = b''
    for tag_key, value in entries.items():
        value_bytes = value.encode('utf-8')
        data_atom = struct.pack('>I4sII', 16 + len(value_bytes), b'data', 1, 0)
        data_atom += value_bytes
        ilst_entry = struct.pack('>I4s', 8 + len(data_atom), tag_key) + data_atom
        ilst_data += ilst_entry

    ilst = struct.pack('>I4s', 8 + len(ilst_data), b'ilst') + ilst_data
    hdlr = struct.pack('>I4sI', 32, b'hdlr', 0)
    hdlr += struct.pack('>I4s', 0, b'mdta')
    hdlr += struct.pack('>III', 0, 0, 0)
    meta_content = b'\x00\x00\x00\x00' + hdlr + ilst
    meta = struct.pack('>I4s', 8 + len(meta_content), b'meta') + meta_content
    return struct.pack('>I4s', 8 + len(meta), b'udta') + meta


def _zero_timestamps(data, off):
    bs = off + 8
    v = data[bs]
    if v == 0:
        ct_off, mt_off, fmt, w = bs+4, bs+8, ">I", 4
    elif v == 1:
        ct_off, mt_off, fmt, w = bs+4, bs+12, ">Q", 8
    else:
        return data
    p = bytearray(data)
    struct.pack_into(fmt, p, ct_off, 0)
    struct.pack_into(fmt, p, mt_off, 0)
    return bytes(p)


def patch_timestamps(data):
    moov_off, moov_sz = _find_box(data, b"moov")
    if moov_off == -1:
        return data

    mvhd_off, _ = _find_box(data, b"mvhd", moov_off+8, moov_off+moov_sz)
    if mvhd_off != -1:
        data = _zero_timestamps(data, mvhd_off)
        moov_off, moov_sz = _find_box(data, b"moov")

    for trak_off, trak_sz, tt in _iter_boxes(data, moov_off+8, moov_off+moov_sz):
        if tt != b"trak":
            continue
        tkhd_off, _ = _find_box(data, b"tkhd", trak_off+8, trak_off+trak_sz)
        if tkhd_off != -1:
            data = _zero_timestamps(data, tkhd_off)
        mdia_off, mdia_sz = _find_box(data, b"mdia", trak_off+8, trak_off+trak_sz)
        if mdia_off != -1:
            mdhd_off, _ = _find_box(data, b"mdhd", mdia_off+8, mdia_off+mdia_sz)
            if mdhd_off != -1:
                data = _zero_timestamps(data, mdhd_off)
    return data


def _pack_lang(s):
    val = 0
    for c in s:
        val = (val << 5) | (ord(c) - 0x60)
    return struct.pack(">H", val)


_UND = _pack_lang("und")


def patch_language(data):
    moov_off, moov_sz = _find_box(data, b"moov")
    if moov_off == -1:
        return data
    for trak_off, trak_sz, tt in _iter_boxes(data, moov_off+8, moov_off+moov_sz):
        if tt != b"trak":
            continue
        mdia_off, mdia_sz = _find_box(data, b"mdia", trak_off+8, trak_off+trak_sz)
        if mdia_off == -1:
            continue
        mdhd_off, _ = _find_box(data, b"mdhd", mdia_off+8, mdia_off+mdia_sz)
        if mdhd_off == -1:
            continue
        lang_off = mdhd_off + 8 + 20
        p = bytearray(data)
        p[lang_off:lang_off+2] = _UND
        data = bytes(p)
    return data


def _iter_boxes(data, start=0, end=None):
    if end is None:
        end = len(data)
    i = start
    while i + 8 <= end:
        size = struct.unpack(">I", data[i:i+4])[0]
        btype = data[i+4:i+8]
        if size == 0:
            size = end - i
        if size < 8:
            break
        yield i, size, btype
        i += size


def _find_box(data, box_type, start=0, end=None):
    for off, sz, bt in _iter_boxes(data, start, end):
        if bt == box_type:
            return off, sz
    return -1, 0


def _adjust_stco(data, delta, search_start=0, search_end=None):
    """Adjust all stco/co64 chunk-offset entries within search range by delta."""
    if search_end is None:
        search_end = len(data)
    pos = search_start
    while pos < search_end:
        idx = data.find(b'stco', pos, search_end)
        if idx == -1:
            idx = data.find(b'co64', pos, search_end)
            if idx == -1:
                break
            entry_size = 8
            pos = idx + 1
        else:
            entry_size = 4
            pos = idx + 1
        entry_count = int.from_bytes(data[idx+8:idx+12], 'big')
        off = idx + 12
        for _ in range(entry_count):
            old = int.from_bytes(data[off:off+entry_size], 'big')
            new_val = old + delta
            data[off:off+entry_size] = new_val.to_bytes(entry_size, 'big')
            off += entry_size


def _dump_atoms(data, label="", log_func=None):
    """Log all top-level atom positions for debugging."""
    if not log_func:
        return
    i = 0
    while i + 8 <= len(data):
        size = int.from_bytes(data[i:i+4], 'big')
        kind = data[i+4:i+8]
        if size == 0:
            size = len(data) - i
        if log_func:
            log_func(f"  [{label}]  offset {i:>8}  size {size:>8}  {kind.decode('latin1', errors='replace')}")
        i += size
        if i >= len(data):
            break


def relocate_to_non_faststart(data, log_func=None):
    """Given data in Faststart layout (ftyp | moov | mdat):
       1. Insert free(8) after ftyp
       2. Adjust stco/co64 by -(moov_size - 8)
       3. Physically move moov to end
       Returns bytearray in Non-Faststart layout (ftyp | free | mdat | moov).
    """
    if log_func:
        log_func("[RELOC] Initial layout (before relocation):")
        _dump_atoms(data, "BEFORE", log_func)

    data = bytearray(data)

    # 1. Insert free(8) after ftyp
    ftyp_size = int.from_bytes(data[0:4], 'big')
    free_atom = b'\x00\x00\x00\x08free'
    data[ftyp_size:ftyp_size] = free_atom  # now: ftyp | free | moov | mdat
    if log_func:
        log_func(f"[RELOC] Inserted free(8) after ftyp (ftyp_size={ftyp_size})")

    # 2. Find moov (right after free)
    moov_start = ftyp_size + 8
    moov_size = int.from_bytes(data[moov_start:moov_start+4], 'big')
    if log_func:
        log_func(f"[RELOC] moov at offset {moov_start}, size={moov_size}")
        log_func(f"[RELOC] stco adjustment delta = -(M-8) = -({moov_size}-8) = {-(moov_size-8)}")
    delta = -(moov_size - 8)  # = -(M - 8): correct for relocation + free atom
    _adjust_stco(data, delta, moov_start, moov_start + moov_size)

    # 3. Physically move moov to end
    end = moov_start + moov_size
    moov_box = data[moov_start:end]
    del data[moov_start:end]
    data.extend(moov_box)

    if log_func:
        log_func("[RELOC] Final layout (after relocation):")
        _dump_atoms(data, "AFTER", log_func)

    return data


def patch_all(input_path, output_path, comment="@akila", log_func=None):
    if log_func:
        log_func("[JOB] starting patch pipeline")

    input_path = Path(input_path)
    output_path = Path(output_path)
    stem = input_path.stem
    suffix = input_path.suffix

    # ---- 1. Remux to Faststart via ffmpeg -movflags +faststart ----
    if log_func:
        log_func("")
        log_func("── 1/7  Remux (Faststart, normalize layout) ──────────────────────")
    clean = input_path.parent / f"{stem}_clean{suffix}"
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-c", "copy",
        "-movflags", "+faststart",
        str(clean),
    ]
    if log_func:
        log_func(f"[REMUX] $ {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        line = line.rstrip()
        if line and log_func:
            log_func(f"[ffmpeg] {line}")
    proc.wait()
    if proc.returncode != 0:
        if log_func:
            log_func(f"[ERROR] ffmpeg exited {proc.returncode}")
        return False
    if log_func:
        log_func("[REMUX] done")

    # ---- 2. Read clean file (moov at end) ----
    data = clean.read_bytes()
    if log_func:
        log_func(f"[READ] {len(data):,} bytes")
        log_func("[LAYOUT] After ffmpeg remux:")
        _dump_atoms(data, "REMUX", log_func)
        md = data.find(b'mdat')
        mv = data.find(b'moov')
        log_func(f"[CHECK] mdat at {md}, moov at {mv}, moov at front: {'YES' if mv < md else 'NO'}")

    # ---- 3. Insert free atom after ftyp (for Faststart, this shifts mdat into correct position) ----
    if log_func:
        log_func("")
        log_func("── 2/7  Insert free atom after ftyp ───────────────────────────")
    ftyp_size = int.from_bytes(data[0:4], 'big')
    # Check whether ffmpeg already placed a free atom after ftyp
    next_type = data[ftyp_size+4:ftyp_size+8]
    if next_type == b'free':
        if log_func:
            log_func("[PATCH] free atom already present after ftyp — skipping insertion")
        pre_shift_extra = 0
    else:
        data = data[:ftyp_size] + b'\x00\x00\x00\x08free' + data[ftyp_size:]
        if log_func:
            log_func("[PATCH] free atom inserted (size=8)")
        pre_shift_extra = 8

    # ---- 4. Date zeroing ----
    if log_func:
        log_func("")
        log_func("── 3/7  Date zeroing (mvhd/tkhd/mdhd) ─────────────────────────")
    data = patch_timestamps(data)

    # ---- 5. Language spoofing -> und ----
    if log_func:
        log_func("")
        log_func("── 4/7  Language spoofing -> 'und' ────────────────────────────")
    data = patch_language(data)

    # ---- 6. Frame count inflation (10x) ----
    if log_func:
        log_func("")
        log_func(f"── 5/7  Frame inflation (10x, stts overflow) ──────────────────")
    md_tree = build_metadata_tree("akila", "akila", comment)
    md_growth = len(md_tree)
    # Faststart: free atom shifts moov+mdat; moov is before mdat
    patched = inject_fake_frames(data, pre_shift=pre_shift_extra, stts_overflow=True, moov_before_mdat=True)
    if patched is None:
        if log_func:
            log_func("[ERROR] Frame injection failed")
        try: clean.unlink(missing_ok=True)
        except: pass
        return False
    data = bytearray(patched)

    # ---- 7. Inject metadata (ilst) at end of moov ----
    if log_func:
        log_func("")
        log_func("── 6/7  Inject metadata (ilst box) ─────────────────────────────")
    moov_idx = data.rfind(b'moov')
    moov_start = moov_idx - 4
    current_size = int.from_bytes(data[moov_start:moov_start+4], 'big')
    moov_end = moov_start + current_size
    data[moov_end:moov_end] = md_tree
    new_size = current_size + md_growth
    data[moov_start:moov_start+4] = new_size.to_bytes(4, 'big')
    # Moov grew by md_growth, which shifts mdat right — adjust stco accordingly
    _adjust_stco(data, md_growth, moov_start, moov_start + new_size)
    if log_func:
        log_func(f"[PATCH] metadata injected: moov {current_size} -> {new_size}")

    # ---- 8. Fake trailer atom ----
    if log_func:
        log_func("")
        log_func("── 7/7  Fake trailer atom ───────────────────────────────────────")
    data += b'\x00\x00\x00\x04junk'
    if log_func:
        log_func("[PATCH] fake trailer atom appended (size=4)")

    # ---- 9. Final verify ----
    if log_func:
        log_func("")
        log_func("── Atom layout ────────────────────────────────────────────────────")
        _dump_atoms(data, "FINAL", log_func)
        md = data.find(b'mdat')
        mv = data.find(b'moov')
        log_func(f"[VERIFY] mdat at {md}, moov at {mv}, moov at front: {'YES' if mv < md else 'NO'}")

    # ---- 10. Write final output ----
    output_path.write_bytes(bytes(data))
    if log_func:
        log_func(f"[WRITE] {output_path.name}  ({len(data):,} bytes)")

    # Cleanup
    try: clean.unlink(missing_ok=True)
    except: pass

    if log_func:
        log_func(f"[DONE]  {output_path.name}")
    return True
