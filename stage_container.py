#!/usr/bin/env python3
"""
stage_container.py -- Deep container & stream obfuscation (hex-level surgery).
All operations are pure-Python on the FINAL encoded file -- no re-encode.
"""
from __future__ import annotations

import mmap
import os
import random
import struct
import hashlib
from typing import Dict, List, Optional, Tuple

# ----------------------------------------------------------------------
# Forensic signatures to scrub (byte strings)
# ----------------------------------------------------------------------
SIGNATURES: List[bytes] = [
    b'Lavf', b'Lavc', b'FFmpeg', b'ffmpeg',
    b'x264', b'libx264', b'HandBrake',
    b'libavcodec', b'libavformat', b'libavutil',
    b'Libav', b'avcodec', b'avformat', b'L-SMASH',
]

# ftyp device profiles: (major_brand, minor_version, compatible_brands)
DEVICE_PROFILES: List[Tuple[str, int, List[str]]] = [
    ('qt  ', 512, ['qt  ', 'isom']),          # iPhone / Apple
    ('qt  ', 538, ['qt  ', 'isom', 'iso2']),  # iPad / Apple
    ('mp42', 0, ['isom', 'mp42']),            # Android (Nexus/Pixel)
    ('mp42', 0, ['mp42', 'isom', 'avc1']),    # Android (Samsung)
    ('avc1', 0, ['avc1', 'mp42', 'isom']),    # GoPro / action cam
    ('mp41', 0, ['mp41', 'isom']),            # generic camcorder
]
_BRAND_POOL = ['isom', 'mp42', 'avc1', 'iso2', 'iso5', 'mp41', 'qt  ', 'dash']

# ----------------------------------------------------------------------
# Box walking helpers
# ----------------------------------------------------------------------
def _read_box_header(buf: bytes, off: int, limit: int) -> Tuple[int, bytes, int]:
    if off + 8 > limit:
        raise ValueError("truncated box header")
    size = struct.unpack('>I', buf[off:off+4])[0]
    btype = buf[off+4:off+8]
    hlen = 8
    if size == 1:
        if off + 16 > limit:
            raise ValueError("truncated extended-size box")
        size = struct.unpack('>Q', buf[off+8:off+16])[0]
        hlen = 16
    elif size == 0:
        size = limit - off
    return size, btype, hlen

def top_level_boxes(path: str) -> List[Tuple[int, bytes, int]]:
    out: List[Tuple[int, bytes, int]] = []
    with open(path, "rb") as fh:
        data = fh.read()
    size = len(data)
    off = 0
    while off + 8 <= size:
        bsiz, btype, hlen = _read_box_header(data, off, size)
        if off + bsiz > size:
            raise ValueError(f"box {btype} overruns file")
        out.append((off, btype, bsiz))
        off += bsiz
    return out

# ----------------------------------------------------------------------
# 1) Forensic signature scan (FIXED: uses bytes.count, not mmap.count)
# ----------------------------------------------------------------------
def scan_signatures(path: str) -> Dict[str, int]:
    """Count occurrences of each forensic signature in the binary."""
    found: Dict[str, int] = {}
    with open(path, "rb") as fh:
        data = fh.read()
    for sig in SIGNATURES:
        c = data.count(sig)
        if c:
            found[sig.decode("ascii", "replace")] = c
    return found

# ----------------------------------------------------------------------
# 2) Forensic signature scrub (uses mmap.find which works)
# ----------------------------------------------------------------------
def scrub_signatures(path: str) -> int:
    """Overwrite every forensic signature with spaces (in place)."""
    replaced = 0
    with open(path, "r+b") as fh:
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_WRITE) as mm:
            for sig in SIGNATURES:
                start = mm.find(sig)
                while start != -1:
                    mm[start:start + len(sig)] = b" " * len(sig)
                    replaced += 1
                    start = mm.find(sig, start + len(sig))
    return replaced

# ----------------------------------------------------------------------
# 3) ftyp randomization (same-size rewrite -> no offset changes)
# ----------------------------------------------------------------------
def randomize_ftyp(path: str, seed: Optional[int] = None,
                   profile_idx: Optional[int] = None) -> Dict:
    """Rewrite ftyp to mimic a hardware recorder."""
    rng = random.Random(seed)
    with open(path, "r+b") as fh:
        data = fh.read()
        size, btype, hlen = _read_box_header(data, 0, len(data))
        if btype != b"ftyp":
            raise ValueError("file does not start with ftyp")
        # Parse existing ftyp
        major_before = data[4:8]
        minor_before = struct.unpack(">I", data[8:12])[0]
        brands_before = [data[i:i+4] for i in range(12, size, 4)]
        
        n = len(brands_before)
        if n < 1:
            raise ValueError("ftyp has no compatible brands")
        
        if profile_idx is None:
            profile_idx = rng.randrange(len(DEVICE_PROFILES))
        major_new, minor_new, profile_brands = DEVICE_PROFILES[profile_idx]
        major_new = major_new.encode('ascii') if isinstance(major_new, str) else major_new
        
        # Build brand list of exactly n entries
        brands_new: List[bytes] = []
        pool = list(dict.fromkeys(profile_brands + [b.decode() for b in brands_before] + _BRAND_POOL))
        pool_bytes = [b.encode('ascii') if isinstance(b, str) else b for b in pool]
        for _ in range(n):
            if pool_bytes:
                chosen = pool_bytes.pop(rng.randrange(len(pool_bytes)))
                brands_new.append(chosen)
            else:
                brands_new.append(b"isom")
        rng.shuffle(brands_new)
        
        new_payload = b"ftyp" + major_new + struct.pack(">I", minor_new) + b"".join(brands_new)
        # Pad if exactly same size? ftyp size should match exactly (n*4 + 12)
        # But we must preserve total size, so pad with spaces if shorter, or truncate if longer.
        if len(new_payload) > size:
            new_payload = new_payload[:size]  # truncate (rare)
        elif len(new_payload) < size:
            new_payload = new_payload + b" " * (size - len(new_payload))
        
        fh.seek(0)
        fh.write(new_payload)
        fh.flush()
        
    return {
        "major_brand_before": major_before.decode("latin1"),
        "major_brand_after": major_new.decode("latin1"),
        "minor_version_before": minor_before,
        "minor_version_after": minor_new,
        "compatible_brands_before": [b.decode("latin1") for b in brands_before],
        "compatible_brands_after": [b.decode("latin1") for b in brands_new],
        "profile": DEVICE_PROFILES[profile_idx][0].strip(),
    }

# ----------------------------------------------------------------------
# 4) free-atom injection with offset patching (simplified for 50MB files)
# ----------------------------------------------------------------------
def inject_free_atom(path: str, seed: Optional[int] = None, max_padding: int = 128) -> Dict:
    """Insert a random free box after ftyp; patch stco/co64 offsets."""
    rng = random.Random(seed)
    padding = rng.randint(8, max_padding)
    free_box = struct.pack(">I", 8 + padding) + b"free" + bytes(rng.randrange(256) for _ in range(padding))
    delta = len(free_box)
    
    with open(path, "r+b") as fh:
        data = fh.read()
        size, btype, hlen = _read_box_header(data, 0, len(data))
        if btype != b"ftyp":
            raise ValueError("file does not start with ftyp")
        
        # Write ftyp + free_box + rest
        ftyp_data = data[:size]
        rest = data[size:]
        fh.seek(0)
        fh.write(ftyp_data)
        fh.write(free_box)
        fh.write(rest)
        fh.flush()
    
    # Now we need to patch stco/co64 offsets by adding `delta` to every chunk offset.
    # Since we are doing it the simple way, just read/write all data (50MB is fine).
    with open(path, "r+b") as fh:
        data = bytearray(fh.read())
        # Find all 'stco' and 'co64' boxes and patch
        # For simplicity, we'll use the top_level_boxes function to locate moov and traverse.
        # But for a quick fix, we can just search for stco/co64 and patch.
        # However, proper way is to walk the box tree.
        # Since this is a small file, we can use a simple replace strategy with structural walk.
        # Let's implement a basic walker.
        def patch_box_tree(buf: bytearray, start: int, end: int, delta: int) -> int:
            patched = 0
            off = start
            while off + 8 <= end:
                bsiz, btype, hlen = _read_box_header(buf, off, end)
                if bsiz <= 0:
                    break
                body = off + hlen
                if btype == b"stco" and bsiz >= 12:
                    cnt = struct.unpack(">I", buf[body+4:body+8])[0]
                    p = body + 8
                    for _ in range(cnt):
                        v = struct.unpack(">I", buf[p:p+4])[0]
                        struct.pack_into(">I", buf, p, v + delta)
                        p += 4
                    patched += cnt
                elif btype == b"co64" and bsiz >= 12:
                    cnt = struct.unpack(">I", buf[body+4:body+8])[0]
                    p = body + 8
                    for _ in range(cnt):
                        v = struct.unpack(">Q", buf[p:p+8])[0]
                        struct.pack_into(">Q", buf, p, v + delta)
                        p += 8
                    patched += cnt
                elif btype in (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"dinf", b"edts", b"udta", b"meta", b"mvex"):
                    # Recurse into container boxes
                    sub_start = body
                    if btype == b"meta":
                        sub_start = body + 4  # skip version/flag
                    patched += patch_box_tree(buf, sub_start, off + bsiz, delta)
                off += bsiz
            return patched
        
        # Find moov
        boxes = top_level_boxes(path)
        moov_start = None
        for off, btype, bsize in boxes:
            if btype == b"moov":
                moov_start = off
                break
        
        if moov_start is not None:
            patch_box_tree(data, moov_start, moov_start + bsize, delta)
        
        fh.seek(0)
        fh.write(data)
        fh.flush()
    
    return {
        "free_atom_size_bytes": delta,
        "padding_bytes": padding,
        "mdat_size_after_bytes": None,  # optional, skip for now
    }

# ----------------------------------------------------------------------
# 5) Verify
# ----------------------------------------------------------------------
def verify_no_signatures(path: str) -> Dict[str, int]:
    return scan_signatures(path)

def fingerprint(path: str) -> Tuple[str, str, int]:
    md5 = hashlib.md5()
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            md5.update(chunk)
            sha.update(chunk)
    return md5.hexdigest(), sha.hexdigest(), os.path.getsize(path)

# ----------------------------------------------------------------------
# Standalone test
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    path = sys.argv[1]
    print("before:", scan_signatures(path))
    print("ftyp:", randomize_ftyp(path))
    print("scrub:", scrub_signatures(path), "signature bytes replaced")
    print("inject:", inject_free_atom(path))
    print("after:", verify_no_signatures(path))
    print("hash:", fingerprint(path))
