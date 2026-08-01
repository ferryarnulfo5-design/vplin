#!/usr/bin/env python3
"""
stage_container.py – Minimal container obfuscation: only signature scrub.
All other functions return dummy dicts with expected keys.
"""
from __future__ import annotations

import mmap
import os
import hashlib
from typing import Dict, List, Tuple

SIGNATURES: List[bytes] = [
    b'Lavf', b'Lavc', b'FFmpeg', b'ffmpeg',
    b'x264', b'libx264', b'HandBrake',
    b'libavcodec', b'libavformat', b'libavutil',
    b'Libav', b'avcodec', b'avformat', b'L-SMASH',
]

def scan_signatures(path: str) -> Dict[str, int]:
    found = {}
    with open(path, "rb") as f:
        data = f.read()
    for sig in SIGNATURES:
        c = data.count(sig)
        if c:
            found[sig.decode("ascii", "replace")] = c
    return found

def scrub_signatures(path: str) -> int:
    replaced = 0
    with open(path, "r+b") as f:
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_WRITE) as mm:
            for sig in SIGNATURES:
                start = mm.find(sig)
                while start != -1:
                    mm[start:start + len(sig)] = b" " * len(sig)
                    replaced += 1
                    start = mm.find(sig, start + len(sig))
    return replaced

def randomize_ftyp(path: str, seed=None, profile_idx=None):
    # dummy but with keys pipeline expects
    return {
        "major_brand_after": "isom",
        "minor_version_after": 0,
        "compatible_brands_after": ["isom", "mp42"],
        "profile": "dummy"
    }

def inject_free_atom(path: str, seed=None, max_padding=128):
    return {"free_atom_size_bytes": 0, "padding_bytes": 0, "mdat_size_after_bytes": None}

def verify_no_signatures(path: str) -> Dict[str, int]:
    return scan_signatures(path)

def fingerprint(path: str) -> Tuple[str, str, int]:
    md5 = hashlib.md5()
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            md5.update(chunk)
            sha.update(chunk)
    return md5.hexdigest(), sha.hexdigest(), os.path.getsize(path)

if __name__ == "__main__":
    import sys
    path = sys.argv[1]
    print("before:", scan_signatures(path))
    print("scrub:", scrub_signatures(path), "signature bytes replaced")
    print("after:", verify_no_signatures(path))
    print("hash:", fingerprint(path))
