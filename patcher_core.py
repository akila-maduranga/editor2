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
import time
import random
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


def extract_metadata_atoms(data):
    """Extract existing metadata atoms with their exact byte lengths from udta/ilst."""
    moov_off, moov_sz = _find_box(data, b"moov")
    if moov_off == -1:
        return {}
    
    metadata = {}
    udta_off, udta_sz = _find_box(data, b"udta", moov_off + 8, moov_off + moov_sz)
    if udta_off == -1:
        return metadata
    
    meta_off, meta_sz = _find_box(data, b"meta", udta_off + 8, udta_off + udta_sz)
    if meta_off == -1:
        return metadata
    
    ilst_off, ilst_sz = _find_box(data, b"ilst", meta_off + 8, meta_off + meta_sz)
    if ilst_off == -1:
        return metadata
    
    # Iterate through ilst entries
    pos = ilst_off + 8
    ilst_end = ilst_off + ilst_sz
    while pos + 16 <= ilst_end:
        atom_size = int.from_bytes(data[pos:pos+4], 'big')
        atom_type = data[pos+4:pos+8]
        if atom_size < 8 or pos + atom_size > ilst_end:
            break
        
        # Look for data atom inside
        data_off, data_sz = _find_box(data, b"data", pos + 8, pos + atom_size)
        if data_off != -1 and data_sz >= 16:
            # Extract the actual value (after 16-byte data header)
            value_start = data_off + 16
            value_len = data_sz - 16
            if value_len > 0:
                value_bytes = data[value_start:value_start + value_len]
                try:
                    value_str = value_bytes.decode('utf-8', errors='replace')
                    metadata[atom_type] = {'value': value_str, 'length': value_len}
                except:
                    pass
        
        pos += atom_size
    
    return metadata


def build_metadata_tree(artist, copyright, custom_tag, encoder="Lavf60.16.100", original_metadata=None):
    """
    Build metadata tree with exact byte-level preservation.
    If original_metadata is provided, pad values to match original lengths.
    """
    entries = {}
    if encoder:
        entries[b'\xa9too'] = encoder
    if artist:
        entries[b'\xa9ART'] = artist
    if copyright:
        entries[b'\xa9cpy'] = copyright
    if custom_tag:
        entries[b'\xa9cmt'] = custom_tag

    # Build Apple-style meta/ilst/data wrapper
    udta_data = b''
    ilst_data = b''
    for tag_key, value in entries.items():
        value_bytes = value.encode('utf-8')
        
        # If original metadata exists for this key, pad to exact length
        if original_metadata and tag_key in original_metadata:
            orig_len = original_metadata[tag_key]['length']
            if len(value_bytes) < orig_len:
                # Pad with spaces (not null bytes) to match exact original length
                # Null bytes can cause playback issues in some players
                value_bytes = value_bytes + b' ' * (orig_len - len(value_bytes))
            elif len(value_bytes) > orig_len:
                # Truncate if longer (should not happen in normal use)
                value_bytes = value_bytes[:orig_len]
        
        data_atom = struct.pack('>I4sII', 16 + len(value_bytes), b'data', 1, 0)
        data_atom += value_bytes
        ilst_entry = struct.pack('>I4s', 8 + len(data_atom), tag_key) + data_atom
        ilst_data += ilst_entry

    ilst = struct.pack('>I4s', 8 + len(ilst_data), b'ilst') + ilst_data
    hdlr = struct.pack('>I4sI', 41, b'hdlr', 0)
    hdlr += struct.pack('>I4s', 0, b'mdir')
    hdlr += b'appl' + struct.pack('>II', 0, 0)
    hdlr += b'Metadata\x00'  # vendor=Apple, name="Metadata"
    meta_content = b'\x00\x00\x00\x00' + hdlr + ilst
    meta = struct.pack('>I4s', 8 + len(meta_content), b'meta') + meta_content
    udta_data += meta

    return struct.pack('>I4s', 8 + len(udta_data), b'udta') + udta_data


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


def _mdhd_dur_offset(data, mdhd_off):
    """Return (dur_off, dur_size) for mdhd at mdhd_off, or None."""
    if mdhd_off + 12 > len(data):
        return None
    version = data[mdhd_off + 8]
    if version == 0:
        off = mdhd_off + 24
        if off + 4 > len(data):
            return None
        return (off, 4)
    else:
        off = mdhd_off + 32
        if off + 8 > len(data):
            return None
        return (off, 8)


def _find_audio_mdhd_via_tree(data, moov_off, moov_sz):
    """Iterate moov box tree to find audio track mdhd (returns mdhd_off or None)."""
    for trak_off, trak_sz, tt in _iter_boxes(data, moov_off + 8, moov_off + moov_sz):
        if tt != b"trak":
            continue
        mdia_off, mdia_sz = _find_box(data, b"mdia", trak_off + 8, trak_off + trak_sz)
        if mdia_off == -1:
            continue
        hdlr_off, _ = _find_box(data, b"hdlr", mdia_off + 8, mdia_off + mdia_sz)
        if hdlr_off == -1 or hdlr_off + 20 > len(data):
            continue
        if data[hdlr_off + 16:hdlr_off + 20] != b'soun':
            continue
        mdhd_off, _ = _find_box(data, b"mdhd", mdia_off + 8, mdia_off + mdia_sz)
        if mdhd_off != -1:
            return mdhd_off
    return None


def _find_audio_mdhd_binary(data, moov_off, moov_sz):
    """Fallback: binary-scan for 'mdhd' within moov and verify it belongs to a soun track."""
    moov_end = moov_off + moov_sz
    pos = moov_off
    while True:
        off = data.find(b'mdhd', pos, moov_end)
        if off == -1:
            break
        # quick sanity: preceding bytes look like a size value
        if off >= 4:
            sz = int.from_bytes(data[off - 4:off], 'big')
            if sz < 16 or off + sz > moov_end:
                pos = off + 4
                continue
            # walk up to find a trak parent with soun hdlr
            up = off - 8
            while up >= moov_off:
                up_sz = int.from_bytes(data[up:up + 4], 'big')
                up_ty = data[up + 4:up + 8]
                if up_ty == b'trak':
                    # check for soun hdlr within this trak
                    hdlr_off = data.find(b'hdlr', up + 8, up + up_sz)
                    if hdlr_off != -1 and hdlr_off + 20 <= len(data):
                        if data[hdlr_off + 16:hdlr_off + 20] == b'soun':
                            return off
                    break
                up -= 4
        pos = off + 4
    return None


def _get_audio_mdhd_off(data):
    """Locate audio track mdhd offset using tree iteration, falling back to binary scan."""
    moov_off, moov_sz = _find_box(data, b"moov")
    if moov_off == -1:
        return None
    mdhd_off = _find_audio_mdhd_via_tree(data, moov_off, moov_sz)
    if mdhd_off is not None:
        return mdhd_off
    return _find_audio_mdhd_binary(data, moov_off, moov_sz)


def read_audio_duration(data):
    """Read the audio track's mdhd duration from file data."""
    mdhd_off = _get_audio_mdhd_off(data)
    if mdhd_off is None:
        return None
    r = _mdhd_dur_offset(data, mdhd_off)
    if r is None:
        return None
    dur_off, dur_size = r
    return int.from_bytes(data[dur_off:dur_off + dur_size], 'big')


def patch_audio_duration(data, original_duration):
    """Restore audio track mdhd duration in patched data."""
    mdhd_off = _get_audio_mdhd_off(data)
    if mdhd_off is None:
        return data
    r = _mdhd_dur_offset(data, mdhd_off)
    if r is None:
        return data
    dur_off, dur_size = r
    p = bytearray(data)
    p[dur_off:dur_off + dur_size] = original_duration.to_bytes(dur_size, 'big')
    return bytes(p)


def patch_all(input_path, output_path, comment=None, artist=None, copyright_text=None, log_func=None):
    if log_func:
        log_func("[JOB] starting patch pipeline")

    input_path = Path(input_path)
    output_path = Path(output_path)
    stem = input_path.stem
    suffix = input_path.suffix

    if comment is None or comment == "@akila":
        ts = int(time.time())
        tag = f"{ts}_{random.randint(0, 0xFFFFFFFF):08x}"
        comment = f"Patched by method.akila - {tag}"
    
    if artist is None:
        artist = "akila"
    if copyright_text is None:
        copyright_text = "akila"

    # Save original audio duration and metadata before ffmpeg remux
    original_data = input_path.read_bytes()
    original_audio_dur = read_audio_duration(original_data)
    if log_func and original_audio_dur is not None:
        log_func(f"[AUDIO] original duration={original_audio_dur}")
    
    # Extract original metadata with exact byte lengths
    original_metadata = extract_metadata_atoms(original_data)
    if log_func and original_metadata:
        log_func(f"[META] extracted {len(original_metadata)} metadata fields with exact byte lengths")
        for key, info in original_metadata.items():
            key_name = key.decode('latin1', errors='replace')
            log_func(f"[META]   {key_name}: '{info['value']}' (length={info['length']})")

    # ---- 1. Remux to Faststart via ffmpeg -movflags +faststart ----
    if log_func:
        log_func("")
        log_func("── 1/8  Remux (Faststart, normalize layout) ──────────────────────")
    clean = input_path.parent / f"{stem}_clean{suffix}"
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-c", "copy",
        "-map_metadata", "-1",
        "-brand", "isom",
        "-movflags", "+faststart",
        "-metadata:s:a:0", "handler_name=SoundHandler",
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
        log_func("── 2/8  Insert free atom after ftyp ───────────────────────────")
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
        log_func("── 3/8  Date zeroing (mvhd/tkhd/mdhd) ─────────────────────────")
    data = patch_timestamps(data)

    # ---- 5. Language spoofing -> und ----
    if log_func:
        log_func("")
        log_func("── 4/8  Language spoofing -> 'und' ────────────────────────────")
    data = patch_language(data)

    # ---- 6. Frame count inflation (10x) ----
    if log_func:
        log_func("")
        log_func(f"── 5/8  Frame inflation (10x, stts overflow) ──────────────────")
    md_tree = build_metadata_tree(artist, copyright_text, comment, original_metadata=original_metadata)
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

    # ---- 7. Inject metadata (ilst) — replace existing udta or append ----
    if log_func:
        log_func("")
        log_func("── 6/8  Inject metadata (ilst box) ─────────────────────────────")
    moov_idx = data.rfind(b'moov')
    moov_start = moov_idx - 4
    current_size = int.from_bytes(data[moov_start:moov_start+4], 'big')
    moov_end = moov_start + current_size

    # Search for existing udta inside moov and remove it
    pos = moov_start + 8
    udta_removed = 0
    while pos + 8 <= moov_end:
        atom_size = int.from_bytes(data[pos:pos+4], 'big')
        atom_type = data[pos+4:pos+8]
        if atom_size < 8:
            break
        if atom_type == b'udta':
            del data[pos:pos + atom_size]
            udta_removed = atom_size
            current_size -= udta_removed
            moov_end -= udta_removed
            break
        pos += atom_size

    # Append metadata tree (starts with udta) at end of moov
    data[moov_end:moov_end] = md_tree
    new_size = current_size + md_growth
    data[moov_start:moov_start+4] = new_size.to_bytes(4, 'big')
    # Adjust stco for the net shift in moov size (md_growth - udta_removed)
    net_shift = md_growth - udta_removed
    if net_shift != 0:
        _adjust_stco(data, net_shift, moov_start, moov_start + new_size)
    if log_func:
        log_func(f"[PATCH] metadata injected: moov {current_size} -> {new_size}"
                 f"  (removed udta={udta_removed}, added={md_growth}, net={net_shift:+d})")

    # ---- 7. Relocate to non-faststart with exact MediaDataOffset ----
    if log_func:
        log_func("")
        log_func("── 7/8  Relocate to non-faststart (MediaDataOffset=237436) ────")
    ftyp_size = int.from_bytes(data[0:4], 'big')
    if data[ftyp_size:ftyp_size+8] == b'\x00\x00\x00\x08free':
        # Calculate exact free size to place mdat at 237436
        # The formula: ftyp_size + free_size + moov_size = 237436
        # So: free_size = 237436 - ftyp_size - moov_size
        free_size = 237436 - ftyp_size - new_size
        if free_size < 8:
            if log_func:
                log_func(f"[RELOC] free_size too small ({free_size}), using minimum 8")
            free_size = 8
        else:
            saved_moov = data[ftyp_size+8:ftyp_size+8+new_size]
            new_free = struct.pack('>I4s', free_size, b'free') + b'\x00' * (free_size - 8)
            data[ftyp_size:ftyp_size+8+new_size] = new_free
            data.extend(saved_moov)
            # Calculate stco delta: mdat moves from (ftyp_size + 8 + new_size) to 237436
            old_mdat_offset = ftyp_size + 8 + new_size
            stco_delta = 237436 - old_mdat_offset
            _adjust_stco(data, stco_delta, ftyp_size, len(data))
            if log_func:
                log_func(f"[RELOC] non-faststart: free({free_size}), "
                         f"media_data_offset=237436, stco delta={stco_delta:+d}")
    else:
        if log_func:
            log_func("[RELOC] expected free(8) after ftyp, skipping")

    # ---- 9. Fake trailer atom ----
    if log_func:
        log_func("")
        log_func("── 8/8  Fake trailer atom ────────────────────────────────────────")
    data += b'\x00\x00\x00\x04junk'
    if log_func:
        log_func("[PATCH] fake trailer atom appended (size=4)")

    # Restore original audio duration (ffmpeg may truncate it)
    if original_audio_dur is not None:
        fixed = patch_audio_duration(bytes(data), original_audio_dur)
        if fixed is not None:
            data = bytearray(fixed)
            if log_func:
                log_func(f"[AUDIO] restored duration to {original_audio_dur}")

    # ---- 11. Final verify ----
    if log_func:
        log_func("")
        log_func("── Atom layout ────────────────────────────────────────────────────")
        _dump_atoms(data, "FINAL", log_func)
        md = data.find(b'mdat')
        mv = data.find(b'moov')
        log_func(f"[VERIFY] mdat at {md}, moov at {mv}, {'non-faststart' if mv > md else 'faststart'}")
        log_func(f"[VERIFY] media_data_offset={md}, media_data_size={len(data) - md - 8}")
        log_func(f"[VERIFY] ftyp major brand: {data[8:12].decode('latin1', errors='replace')!r}")
        f_sz = int.from_bytes(data[0:4], 'big')
        if data[f_sz:f_sz+4] == b'free':
            free_hex = data[f_sz:f_sz+4].hex(' ', 1).upper()
            log_func(f"[VERIFY] free atom size hex: {free_hex}")

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
