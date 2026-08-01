#!/usr/bin/env python3
"""
stage_container.py -- Minimal container obfuscation: only signature scrub.
No ftyp/free atom modifications to avoid binary parsing errors.
"""
from __future__ import annotations

import mmap
import os
import hashlib
from typing import Dict, List, Tuple

# Forensic signatures
SIGNATURES: List[bytes] = [
    b'Lavf', b'Lavc', b'FFmpeg', b'ffmpeg',
    b'x264', b'libx264', b'HandBrake',
    b'libavcodec', b'libavformat', b'libavutil',
    b'Libav', b'avcodec', b'avformat', b'L-SMASH',
]

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

def randomize_ftyp(path: str, seed=None, profile_idx=None):
    # Do nothing, return dummy
    return {"note": "skipped"}

def inject_free_atom(path: str, seed=None, max_padding=128):
    # Do nothing, return dummy
    return {"note": "skipped"}

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
    print("before:", scan_signatures(path))
    print("scrub:", scrub_signatures(path), "signature bytes replaced")
    print("after:", verify_no_signatures(path))
    print("hash:", fingerprint(path))
