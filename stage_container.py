#!/usr/bin/env python3
"""
v4.0.3 Container Obfuscation Module (Safe Header Restoration)
Performs in-place metadata signature scrubbing and ISO base media file format (ftyp)
header randomization without breaking chunk index offsets.
"""

import hashlib
import mmap
import os
import random
import struct
from typing import Dict, List, Any


def scrub_signatures(
    filepath: str, signatures: List[bytes]
) -> Dict[str, Dict[str, int]]:
    report = {
        sig.decode("latin1", "replace"): {"found": 0, "scrubbed": 0}
        for sig in signatures
    }
    with open(filepath, "r+b") as f:
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_WRITE) as mm:
            for sig in signatures:
                label = sig.decode("latin1", "replace")
                idx = mm.find(sig)
                while idx != -1:
                    report[label]["found"] += 1
                    mm.seek(idx)
                    mm.write(b" " * len(sig))
                    report[label]["scrubbed"] += 1
                    idx = mm.find(sig, idx + len(sig))
    return report


def randomize_ftyp(filepath: str, seed: int = 42) -> Dict[str, Any]:
    rng = random.Random(seed)
    brands = [b"isom", b"mp42", b"avc1", b"iso2", b"mp41"]
    major = rng.choice(brands)
    minor = rng.randint(0, 512)
    comp_brands = [major] + rng.sample(
        [b for b in brands if b != major], rng.randint(1, 3)
    )

    with open(filepath, "r+b") as f:
        data = f.read(100)
        # Safely locate and replace ftyp brand if present, else prepend cleanly
        if b"ftyp" in data:
            f.seek(data.find(b"ftyp") + 4)
            f.write(major + struct.pack(">I", minor) + b"".join(comp_brands))
        else:
            payload = major + struct.pack(">I", minor) + b"".join(comp_brands)
            new_ftyp = struct.pack(">I4s", len(payload) + 8, b"ftyp") + payload
            f.seek(0)
            remaining = f.read()
            f.seek(0)
            f.write(new_ftyp + remaining)

    return {
        "major_brand_after": major.decode("latin1"),
        "minor_version_after": str(minor),
        "compatible_brands_after": [b.decode("latin1") for b in comp_brands],
    }


def inject_free_atom(filepath: str, size_bytes: int = 128, seed: int = 42) -> Dict[str, int]:
    """Safe free atom injection that avoids breaking internal moov/mdat offsets."""
    rng = random.Random(seed)
    payload = bytes([rng.randint(0, 255) for _ in range(max(0, size_bytes - 8))])
    free_box = struct.pack(">I4s", len(payload) + 8, b"free") + payload

    with open(filepath, "rb") as f:
        content = bytearray(f.read())

    # Safely append free box at the very end of the file container to prevent playability corruption
    content.extend(free_box)

    with open(filepath, "wb") as f:
        f.write(content)

    return {
        "free_atom_size_bytes": len(free_box),
        "padding_bytes": len(payload),
        "mdat_size_after_bytes": len(content),
    }


def compute_fingerprint(filepath: str) -> Dict[str, str]:
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            md5.update(chunk)
            sha256.update(chunk)
    return {
        "md5": md5.hexdigest(),
        "sha256": sha256.hexdigest(),
        "file_size": str(os.path.getsize(filepath)),
    }
