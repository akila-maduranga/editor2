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


def remux(input_path, output_path, comment, log_func=None):
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-c", "copy",
        "-brand", "isom",
        "-video_track_timescale", "90000",
        "-movflags", "+faststart",
        "-bitexact",
        "-map_metadata", "-1",
        "-metadata", "encoder=Lavf60.16.100",
        "-metadata", f"comment={comment}",
        "-metadata", "artist=akila",
        str(output_path),
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
    return True


def stbl_list(atoms, result):
    for atom in atoms:
        if atom['name'] in (b'stco', b'co64'):
            d = atom['data']
            entry_size = 8 if atom['name'] == b'co64' else 4
            n = int.from_bytes(d[4:8], 'big')
            data_start_off = atom['start'] + 16
            vals = []
            for i in range(n):
                vals.append(d[8+i*entry_size:8+i*entry_size+entry_size])
            result.append((data_start_off, vals, entry_size))
        if 'children' in atom:
            stbl_list(atom['children'], result)


def _move_moov_to_end(data):
    pos = 0
    moov_start = moov_size = -1
    while pos + 8 <= len(data):
        box_size = struct.unpack(">I", data[pos:pos+4])[0]
        box_type = data[pos+4:pos+8]
        header_size = 8
        if box_size == 0:
            box_size = len(data) - pos
        elif box_size == 1:
            if pos + 16 > len(data):
                break
            box_size = struct.unpack(">Q", data[pos+8:pos+16])[0]
            header_size = 16
        if box_size < header_size:
            break
        if box_type == b'moov':
            moov_start = pos
            moov_size = box_size
            break
        pos += box_size

    if moov_start < 0 or moov_size < 0:
        return data

    moov_end = moov_start + moov_size
    before = data[:moov_start]
    after = data[moov_end:]
    new_data = bytearray(before + after + data[moov_start:moov_end])
    new_moov_off = len(before) + len(after)

    tree, _ = read_atoms_in_range(new_data, new_moov_off + 8, new_moov_off + moov_size)
    stco_info = []
    stbl_list(tree, stco_info)
    for off, vals, esize in stco_info:
        for i, bval in enumerate(vals):
            if esize == 4:
                old_val = struct.unpack(">I", bval)[0]
                new_val = old_val - moov_size
                new_data[off + i*4:off + i*4+4] = struct.pack(">I", new_val)
            else:
                old_val = struct.unpack(">Q", bval)[0]
                new_val = old_val - moov_size
                new_data[off + i*8:off + i*8+8] = struct.pack(">Q", new_val)
    return bytes(new_data)


def patch_all(input_path, output_path, comment="@akila", log_func=None):
    if log_func:
        log_func("[JOB] starting patch pipeline")

    input_path = Path(input_path)
    output_path = Path(output_path)

    # ---- 1. Remux ----
    if log_func:
        log_func("")
        log_func("── 1/7  Remux + encoder spoof + comment injection ──────────────")
    remuxed = input_path.parent / f"{input_path.stem}_remuxed{input_path.suffix}"
    if not remux(input_path, remuxed, comment, log_func):
        return False

    # ---- 2. Read remuxed file ----
    data = remuxed.read_bytes()
    if log_func:
        log_func(f"[READ] {len(data):,} bytes")

    # ---- 3. Insert free atom after ftyp ----
    if log_func:
        log_func("")
        log_func("── 2/7  Insert free atom after ftyp ───────────────────────────")
    ftyp_size = int.from_bytes(data[0:4], 'big')
    data = data[:ftyp_size] + b'\x00\x00\x00\x08free' + data[ftyp_size:]
    if log_func:
        log_func("[PATCH] free atom inserted (size=8)")

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

    # ---- 6. Frame count inflation (10x) + prepare metadata tree ----
    if log_func:
        log_func("")
        log_func(f"── 5/7  Frame inflation (10x, stts overflow) ──────────────────")
    md_tree = build_metadata_tree("akila", "akila", comment)
    md_growth = len(md_tree)
    pre_shift = 8 + md_growth
    patched = inject_fake_frames(data, pre_shift=pre_shift, stts_overflow=True, moov_before_mdat=True)
    if patched is None:
        if log_func:
            log_func("[ERROR] Frame injection failed (moov/trak/stsz not found)")
        try: remuxed.unlink(missing_ok=True)
        except: pass
        return False
    data = bytearray(patched)

    # ---- 7. Inject metadata (ilst) at end of moov ----
    if log_func:
        log_func("")
        log_func("── 6/7  Inject metadata (ilst box) ─────────────────────────────")
    moov_atom_start = data.find(b'moov') - 4
    current_moov_size = int.from_bytes(data[moov_atom_start:moov_atom_start+4], 'big')
    moov_end = moov_atom_start + current_moov_size
    data[moov_end:moov_end] = md_tree
    new_moov_size = current_moov_size + md_growth
    data[moov_atom_start:moov_atom_start+4] = new_moov_size.to_bytes(4, 'big')
    if log_func:
        log_func(f"[PATCH] metadata injected: moov {current_moov_size} -> {new_moov_size}")

    # ---- 8. Fake trailer atom ----
    if log_func:
        log_func("")
        log_func("── 7/7  Fake trailer atom ───────────────────────────────────────")
    data += b'\x00\x00\x00\x04junk'
    if log_func:
        log_func("[PATCH] fake trailer atom appended (size=4)")

    # ---- Relocate moov to end (Non-Faststart) ----
    data = _move_moov_to_end(bytes(data))

    # ---- Write output ----
    output_path.write_bytes(data)
    if log_func:
        log_func(f"[WRITE] {output_path.name}  ({len(data):,} bytes)")
        log_func("")
        log_func("── ALL 7 PATCHES APPLIED ───────────────────────────────────────")
        log_func(f"[DONE]  {output_path.name}")

    # Cleanup
    try: remuxed.unlink(missing_ok=True)
    except: pass

    return True
