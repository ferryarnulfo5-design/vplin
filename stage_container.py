#!/usr/bin/env python3
"""
stage_container.py -- Deep container & stream obfuscation (FINAL FIX).
Robust: handles all MP4 brands, no decode errors, creates ftyp if missing.
"""
from __future__ import annotations

import mmap
import os
import random
import struct
import hashlib
from typing import Dict, List, Optional, Tuple

# ----------------------------------------------------------------------
# Forensic signatures
# ----------------------------------------------------------------------
SIGNATURES: List[bytes] = [
    b'Lavf', b'Lavc', b'FFmpeg', b'ffmpeg',
    b'x264', b'libx264', b'HandBrake',
    b'libavcodec', b'libavformat', b'libavutil',
    b'Libav', b'avcodec', b'avformat', b'L-SMASH',
]

DEVICE_PROFILES: List[Tuple[str, int, List[str]]] = [
    ('qt  ', 512, ['qt  ', 'isom']),
    ('qt  ', 538, ['qt  ', 'isom', 'iso2']),
    ('mp42', 0, ['isom', 'mp42']),
    ('mp42', 0, ['mp42', 'isom', 'avc1']),
    ('avc1', 0, ['avc1', 'mp42', 'isom']),
    ('mp41', 0, ['mp41', 'isom']),
]
_BRAND_POOL = ['isom', 'mp42', 'avc1', 'iso2', 'iso5', 'mp41', 'qt  ', 'dash']

# ----------------------------------------------------------------------
# 1) Signature scan (no mmap.count)
# ----------------------------------------------------------------------
def scan_signatures(path: str) -> Dict[str, int]:
    found: Dict[str, int] = {}
    with open(path, "rb") as fh:
        data = fh.read()
    for sig in SIGNATURES:
        c = data.count(sig)
        if c:
            found[sig.decode("ascii", "replace")] = c
    return found

def scrub_signatures(path: str) -> int:
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
# 2) ftyp randomization (FIXED: uses latin1 decoding)
# ----------------------------------------------------------------------
def randomize_ftyp(path: str, seed: Optional[int] = None,
                   profile_idx: Optional[int] = None) -> Dict:
    rng = random.Random(seed)
    with open(path, "r+b") as fh:
        data = bytearray(fh.read())
        ftyp_offset = data.find(b"ftyp")
        
        if ftyp_offset == -1:
            # Create new ftyp
            if profile_idx is None:
                profile_idx = rng.randrange(len(DEVICE_PROFILES))
            major, minor, brands = DEVICE_PROFILES[profile_idx]
            major = major.encode('ascii')
            brands_bytes = [b.encode('ascii') for b in brands[:4]]
            while len(brands_bytes) < 4:
                brands_bytes.append(b"isom")
            ftyp_box = b"ftyp" + major + struct.pack(">I", minor) + b"".join(brands_bytes[:4])
            fh.seek(0)
            fh.write(ftyp_box + bytes(data))
            fh.flush()
            return {
                "major_brand_after": major.decode("latin1"),
                "minor_version_after": minor,
                "compatible_brands_after": [b.decode("latin1") for b in brands_bytes[:4]],
                "note": "ftyp created"
            }
        
        # Parse existing ftyp
        size = struct.unpack(">I", data[ftyp_offset:ftyp_offset+4])[0]
        if size == 1:
            size = struct.unpack(">Q", data[ftyp_offset+8:ftyp_offset+16])[0]
            header_len = 16
        else:
            header_len = 8
        
        major_before = data[ftyp_offset+4:ftyp_offset+8]
        minor_before = struct.unpack(">I", data[ftyp_offset+8:ftyp_offset+12])[0]
        
        brands_before: List[bytes] = []
        pos = ftyp_offset + 12
        while pos < ftyp_offset + size and pos + 4 <= len(data):
            brands_before.append(data[pos:pos+4])
            pos += 4
        
        n = max(4, len(brands_before))
        
        if profile_idx is None:
            profile_idx = rng.randrange(len(DEVICE_PROFILES))
        major_new, minor_new, profile_brands = DEVICE_PROFILES[profile_idx]
        major_new = major_new.encode('ascii')
        
        # Build new brands - using latin1 to avoid decode errors
        pool = list(dict.fromkeys(profile_brands + 
                   [b.decode("latin1", errors="replace") for b in brands_before] + 
                   _BRAND_POOL))
        pool_bytes = [b.encode('ascii', errors="replace") for b in pool]
        
        brands_new: List[bytes] = []
        for _ in range(n):
            if pool_bytes:
                chosen = pool_bytes.pop(rng.randrange(len(pool_bytes)))
                brands_new.append(chosen)
            else:
                brands_new.append(b"isom")
        rng.shuffle(brands_new)
        
        new_ftyp = b"ftyp" + major_new + struct.pack(">I", minor_new) + b"".join(brands_new)
        while len(new_ftyp) < size:
            new_ftyp += b" "
        if len(new_ftyp) > size:
            new_ftyp = new_ftyp[:size]
        
        fh.seek(ftyp_offset)
        fh.write(new_ftyp)
        fh.flush()
    
    return {
        "major_brand_before": major_before.decode("latin1", errors="replace"),
        "major_brand_after": major_new.decode("latin1"),
        "minor_version_before": minor_before,
        "minor_version_after": minor_new,
        "compatible_brands_before": [b.decode("latin1", errors="replace") for b in brands_before[:6]],
        "compatible_brands_after": [b.decode("latin1") for b in brands_new[:6]],
        "profile": DEVICE_PROFILES[profile_idx][0].strip(),
    }

# ----------------------------------------------------------------------
# 3) free-atom injection (simplified, no nested box walking)
# ----------------------------------------------------------------------
def inject_free_atom(path: str, seed: Optional[int] = None, max_padding: int = 128) -> Dict:
    rng = random.Random(seed)
    padding = rng.randint(8, max_padding)
    free_box = struct.pack(">I", 8 + padding) + b"free" + bytes(rng.randrange(256) for _ in range(padding))
    delta = len(free_box)
    
    with open(path, "r+b") as fh:
        data = bytearray(fh.read())
        ftyp_offset = data.find(b"ftyp")
        
        if ftyp_offset == -1:
            # No ftyp, prepend one
            ftyp_box = b"ftyp" + b"isom" + struct.pack(">I", 0) + b"isom" + b"mp42"
            fh.seek(0)
            fh.write(ftyp_box + free_box + bytes(data))
            fh.flush()
            return {"free_atom_size_bytes": delta, "padding_bytes": padding, "note": "ftyp created"}
        
        # Read ftyp size
        size = struct.unpack(">I", data[ftyp_offset:ftyp_offset+4])[0]
        if size == 1:
            size = struct.unpack(">Q", data[ftyp_offset+8:ftyp_offset+16])[0]
        ftyp_data = data[ftyp_offset:ftyp_offset+size]
        rest = data[ftyp_offset+size:]
        
        fh.seek(ftyp_offset)
        fh.write(ftyp_data)
        fh.write(free_box)
        fh.write(rest)
        fh.flush()
    
    # Patch stco/co64 offsets (simple byte search)
    with open(path, "r+b") as fh:
        data = bytearray(fh.read())
        # Patch stco
        pos = data.find(b"stco")
        while pos != -1:
            if pos + 12 <= len(data):
                cnt = struct.unpack(">I", data[pos+8:pos+12])[0]
                p = pos + 12
                for _ in range(cnt):
                    if p + 4 <= len(data):
                        val = struct.unpack(">I", data[p:p+4])[0]
                        struct.pack_into(">I", data, p, val + delta)
                        p += 4
            pos = data.find(b"stco", pos + 1)
        
        # Patch co64
        pos = data.find(b"co64")
        while pos != -1:
            if pos + 12 <= len(data):
                cnt = struct.unpack(">I", data[pos+8:pos+12])[0]
                p = pos + 12
                for _ in range(cnt):
                    if p + 8 <= len(data):
                        val = struct.unpack(">Q", data[p:p+8])[0]
                        struct.pack_into(">Q", data, p, val + delta)
                        p += 8
            pos = data.find(b"co64", pos + 1)
        
        fh.seek(0)
        fh.write(data)
        fh.flush()
    
    mdat_size = None
    with open(path, "rb") as fh:
        data = fh.read()
        mpos = data.find(b"mdat")
        if mpos != -1 and mpos + 8 <= len(data):
            mdat_size = struct.unpack(">I", data[mpos:mpos+4])[0]
    
    return {
        "free_atom_size_bytes": delta,
        "padding_bytes": padding,
        "mdat_size_after_bytes": mdat_size,
    }

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

if __name__ == "__main__":
    import sys
    path = sys.argv[1]
    print("before signatures:", scan_signatures(path))
    print("ftyp:", randomize_ftyp(path))
    print("scrub:", scrub_signatures(path), "signature bytes replaced")
    print("inject:", inject_free_atom(path))
    print("after signatures:", verify_no_signatures(path))
    print("hash:", fingerprint(path))
