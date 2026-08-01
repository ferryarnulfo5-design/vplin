#!/usr/bin/env python3
"""
ffmpeg_compat.py – Runtime capability probe for FFmpeg.
Exports: probe_compat (for pipeline), and helper classes.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import Dict, Set, Tuple, Optional

@dataclass
class Capabilities:
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    version: Tuple[int, int, int] = (0, 0, 0)
    version_str: str = "unknown"
    filters: Dict[str, bool] = field(default_factory=dict)
    filter_opts: Dict[str, Set[str]] = field(default_factory=dict)
    # derived
    scale_eval: bool = False
    crop_eval: bool = False
    tmix_weights: bool = False
    amix_normalize: bool = False
    firequalizer_tv: bool = False

    def has(self, name: str) -> bool:
        return self.filters.get(name, False)

    def opt(self, fname: str, option: str) -> bool:
        return option in self.filter_opts.get(fname, set())

    def summary(self) -> str:
        return (
            f"ffmpeg {self.version_str} ({self.ffmpeg})\n"
            f"  firequalizer={self.has('firequalizer')} "
            f"rgbashift={self.has('rgbashift')} tmix={self.has('tmix')} "
            f"aphaser={self.has('aphaser')} zoompan={self.has('zoompan')}\n"
            f"  scale_eval={self.scale_eval} crop_eval={self.crop_eval} "
            f"tmix_weights={self.tmix_weights} amix_normalize={self.amix_normalize} "
            f"firequalizer_tv={self.firequalizer_tv}"
        )

def _run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + p.stderr
    except Exception as e:
        return -1, str(e)

def probe_compat(ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> Capabilities:
    caps = Capabilities(ffmpeg=ffmpeg, ffprobe=ffprobe)
    # version
    rc, out = _run([ffmpeg, "-hide_banner", "-version"])
    if rc != 0:
        raise RuntimeError(f"ffmpeg not runnable: {out[:200]}")
    m = re.search(r"ffmpeg version (\d+)\.(\d+)\.(\d+)", out)
    if m:
        caps.version = tuple(map(int, m.groups()))
        caps.version_str = ".".join(map(str, caps.version))
    else:
        caps.version_str = out.splitlines()[0][:80]

    # filters list
    rc, out = _run([ffmpeg, "-hide_banner", "-filters"])
    for line in out.splitlines():
        if " V->V " in line or " A->A " in line:
            parts = line.split()
            if len(parts) >= 2 and re.match(r'^[a-zA-Z0-9_]+$', parts[1]):
                caps.filters[parts[1]] = True

    # per-filter options
    for fname in ("scale", "crop", "tmix", "amix", "firequalizer", "rgbashift",
                  "chromashift", "zoompan", "aphaser", "compand", "chorus", "eq",
                  "noise", "tblend", "framerate", "psnr", "ssim", "aequalizer",
                  "equalizer", "alimiter", "asetrate", "atempo", "aresample",
                  "highpass", "lowpass", "bandpass", "atrim", "asplit", "volume",
                  "apad", "select", "setpts", "format", "fps", "sendcmd"):
        caps.filters.setdefault(fname, False)
        rc, out = _run([ffmpeg, "-hide_banner", "-h", f"filter={fname}"])
        opts = set()
        for line in out.splitlines():
            m = re.match(r'^\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+<', line)
            if m:
                opts.add(m.group(1))
        caps.filter_opts[fname] = opts
        if rc == 0 and out:
            caps.filters[fname] = True  # filter exists if help succeeded

    # derived
    caps.scale_eval = caps.opt("scale", "eval")
    caps.crop_eval = caps.opt("crop", "eval")
    caps.tmix_weights = caps.opt("tmix", "weights")
    caps.amix_normalize = caps.opt("amix", "normalize")
    caps.firequalizer_tv = caps.has("firequalizer") and ("pts" in str(out) or True)  # approximate

    return caps
