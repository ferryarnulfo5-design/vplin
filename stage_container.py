#!/usr/bin/env python3
"""
stage_container.py -- Deep container & stream obfuscation (hex-level surgery).
Ultra-robust: handles corrupted ftyp brands, missing boxes, and binary data.
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
    ('qt  ', 512, ['qt  ', 'isom']),
    ('qt  ', 538, ['qt  ', 'isom', 'iso2']),
    ('mp42', 0, ['isom', 'mp42']),
    ('mp42', 0, ['mp42', 'isom', 'avc1']),
    ('avc1', 0, ['avc1', 'mp42', 'isom']),
    ('mp41', 0, ['mp41', 'isom']),
]
_BRAND_POOL = ['isom', 'mp42', 'avc1', 'iso2', 'iso5', 'mp41', 'qt  ', 'dash']

# ----------------------------------------------------------------------
# 1) Forensic signature scan & scrub
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
# 2) ftyp randomization (robust: no Unicode decode errors)
# ----------------------------------------------------------------------
def randomize_ftyp(path: str, seed: Optional[int] = None,
                   profile_idx: Optional[int] = None) -> Dict:
    """Rewrite ftyp to mimic a hardware recorder. Handles binary brand bytes."""
    rng = random.Random(seed)
    
    with open(path, "r+b") as fh:
        data = bytearray(fh.read())
        ftyp_offset = data.find(b"ftyp")
        
        if ftyp_offset == -1:
            # No ftyp, create one
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
                "major_brand_after": major.decode('latin1'),
                "minor_version_after": minor,
                "compatible_brands_after": [b.decode('latin1') for b in brands_bytes[:4]],
                "note": "ftyp missing, created new one"
            }
        
        # Parse ftyp size
        size = struct.unpack(">I", data[ftyp_offset:ftyp_offset+4])[0]
        if size == 1:
            size = struct.unpack(">Q", data[ftyp_offset+8:ftyp_offset+16])[0]
            header_len = 16
        else:
            header_len = 8
        
        major_before = data[ftyp_offset+4:ftyp_offset+8]
        minor_before = struct.unpack(">I", data[ftyp_offset+8:ftyp_offset+12])[0]
        brands_before = []
        pos = ftyp_offset + 12
        while pos < ftyp_offset + size:
            if pos + 4 <= len(data):
                brands_before.append(bytes(data[pos:pos+4]))
                pos += 4
            else:
                break
        
        n = max(1, len(brands_before))
        
        if profile_idx is None:
            profile_idx = rng.randrange(len(DEVICE_PROFILES))
        major_new, minor_new, profile_brands = DEVICE_PROFILES[profile_idx]
        major_new = major_new.encode('ascii')
        
        # Build new brands - use bytes everywhere, no decode
        brands_new: List[bytes] = []
        # Start with profile brands
        pool = [b.encode('ascii') for b in profile_brands]
        # Add existing brands (as bytes)
        for b in brands_before:
            if b not in pool:
                pool.append(b)
        # Add fallback pool
        for b in _BRAND_POOL:
            bbytes = b.encode('ascii')
            if bbytes not in pool:
                pool.append(bbytes)
        
        # Randomly pick n brands from pool
        for _ in range(n):
            if pool:
                chosen = pool.pop(rng.randrange(len(pool)))
                brands_new.append(chosen)
            else:
                brands_new.append(b"isom")
        rng.shuffle(brands_new)
        
        # Create new ftyp box
        new_ftyp = b"ftyp" + major_new + struct.pack(">I", minor_new) + b"".join(brands_new)
        while len(new_ftyp) < size:
            new_ftyp += b" "
        if len(new_ftyp) > size:
            new_ftyp = new_ftyp[:size]
        
        fh.seek(ftyp_offset)
        fh.write(new_ftyp)
        fh.flush()
        
    return {
        "major_brand_before": major_before.decode('latin1', errors='replace'),
        "major_brand_after": major_new.decode('latin1'),
        "minor_version_before": minor_before,
        "minor_version_after": minor_new,
        "compatible_brands_before": [b.decode('latin1', errors='replace') for b in brands_before[:6]],
        "compatible_brands_after": [b.decode('latin1', errors='replace') for b in brands_new[:6]],
        "profile": DEVICE_PROFILES[profile_idx][0].strip(),
    }

# ----------------------------------------------------------------------
# 3) free-atom injection (simplified, robust)
# ----------------------------------------------------------------------
def inject_free_atom(path: str, seed: Optional[int] = None, max_padding: int = 128) -> Dict:
    """Insert a random free box after ftyp; patch stco/co64 offsets."""
    rng = random.Random(seed)
    padding = rng.randint(8, max_padding)
    free_box = struct.pack(">I", 8 + padding) + b"free" + bytes(rng.randrange(256) for _ in range(padding))
    delta = len(free_box)
    
    with open(path, "r+b") as fh:
        data = bytearray(fh.read())
        ftyp_offset = data.find(b"ftyp")
        if ftyp_offset == -1:
            # No ftyp, just prepend
            ftyp_box = b"ftyp" + b"isom" + struct.pack(">I", 0) + b"isom" + b"mp42" + b"avc1"
            fh.seek(0)
            fh.write(ftyp_box + free_box + bytes(data))
            fh.flush()
            return {"free_atom_size_bytes": delta, "padding_bytes": padding, "note": "ftyp created"}
        
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
    
    # Patch stco/co64 offsets
    with open(path, "r+b") as fh:
        data = bytearray(fh.read())
        # stco
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
        
        # co64
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
    
    return {"free_atom_size_bytes": delta, "padding_bytes": padding}

# ----------------------------------------------------------------------
# 4) Verify
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
    print("before signatures:", scan_signatures(path))
    print("ftyp:", randomize_ftyp(path))
    print("scrub:", scrub_signatures(path), "signature bytes replaced")
    print("inject:", inject_free_atom(path))
    print("after signatures:", verify_no_signatures(path))
    print("hash:", fingerprint(path))
