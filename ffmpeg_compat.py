#!/usr/bin/env python3
"""
ffmpeg_compat.py -- Runtime capability probe for FFmpeg 5.x -> master.

FFmpeg 5.0 removed the `eval` option from scale/crop; newer builds add
options over time (tmix=weights, amix=normalize, firequalizer time-variant
gain via pts/tb); distro builds compile filters out entirely (rgbashift,
aphaser, firequalizer are all optional at build time). Instead of
hard-coding a graph that breaks on one runner, probe the installed binary
once and degrade gracefully. Results are cached; pipeline.py probes once.

Every subprocess call is Windows-safe (CREATE_NO_WINDOW), merges stderr
into stdout, and cannot deadlock.
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Dict, List, Tuple

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _run(args: List[str], timeout: int = 25) -> Tuple[int, str]:
    """Run a binary with merged output; returns (returncode, output)."""
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,          # merge -> one pipe, no deadlock
            creationflags=CREATE_NO_WINDOW,    # Windows: no console flash
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        out, _ = proc.communicate(timeout=timeout)
        return (proc.returncode if proc.returncode is not None else 0), out
    except (subprocess.TimeoutExpired, OSError) as exc:
        return -1, f"<probe error: {exc}>"


class Capabilities:
    """Snapshot of what the installed ffmpeg/ffprobe can do."""

    def __init__(self) -> None:
        self.ffmpeg: str = "ffmpeg"
        self.ffprobe: str = "ffprobe"
        self.version: Tuple[int, ...] = (0, 0, 0)
        self.version_str: str = "unknown"
        self.filters: Dict[str, bool] = {}
        self.filter_opts: Dict[str, Dict[str, bool]] = {}
        # derived flags
        self.scale_eval: bool = False       # scale supports eval=frame (<5.0)
        self.crop_eval: bool = False
        self.tmix_weights: bool = False
        self.amix_normalize: bool = False
        self.firequalizer_tv: bool = False  # time-variant gain via pts/tb

    def has(self, fname: str) -> bool:
        return bool(self.filters.get(fname))

    def opt(self, fname: str, option: str) -> bool:
        return bool(self.filter_opts.get(fname, {}).get(option))

    def summary(self) -> str:
        return (
            f"ffmpeg {self.version_str} ({self.ffmpeg})\n"
            f"  firequalizer={self.has('firequalizer')} "
            f"rgbashift={self.has('rgbashift')} tmix={self.has('tmix')} "
            f"aphaser={self.has('aphaser')} zoompan={self.has('zoompan')}\n"
            f"  scale_eval={self.scale_eval} crop_eval={self.crop_eval} "
            f"tmix_weights={self.tmix_weights} "
            f"amix_normalize={self.amix_normalize} "
            f"firequalizer_tv={self.firequalizer_tv}"
        )


def probe(ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> Capabilities:
    caps = Capabilities()
    caps.ffmpeg, caps.ffprobe = ffmpeg, ffprobe

    # ---- version -----------------------------------------------------
    rc, out = _run([ffmpeg, "-hide_banner", "-version"])
    if rc != 0 or not out:
        raise RuntimeError(
            f"ffmpeg is not runnable ({ffmpeg!r}). Install it first, e.g.\n"
            f"  sudo apt-get update && sudo apt-get install -y ffmpeg\n{out[:300]}"
        )
    m = re.search(r"ffmpeg version (\d+)\.(\d+)(?:\.(\d+))?", out)
    if m:
        caps.version = tuple(int(x or 0) for x in m.groups())
        caps.version_str = ".".join(str(x) for x in caps.version)
    else:
        caps.version_str = out.splitlines()[0].strip()[:80]

    # ---- filter availability ----------------------------------------
    _, out = _run([ffmpeg, "-hide_banner", "-filters"])
    for line in out.splitlines():
        if "->" not in line:          # filter rows look like " T.. rgbashift V->V ..."
            continue
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r"[A-Za-z0-9_]+", parts[1]):
            caps.filters[parts[1]] = True

    # ---- per-filter option help -------------------------------------
    for fname in (
        "scale", "crop", "tmix", "amix", "firequalizer", "rgbashift",
        "chromashift", "zoompan", "aphaser", "compand", "chorus", "eq",
        "noise", "tblend", "framerate", "psnr", "ssim", "aequalizer",
        "alimiter", "asetrate", "atempo", "aresample", "highpass",
        "lowpass", "bandpass", "atrim", "asplit", "volume", "apad",
        "select", "setpts", "format", "fps",
    ):
        caps.filters.setdefault(fname, False)
        rc, help_out = _run([ffmpeg, "-hide_banner", "-h", f"filter={fname}"])
        opts: Dict[str, bool] = {}
        for line in help_out.splitlines():
            mm = re.match(r"\s+([A-Za-z_][A-Za-z0-9_]*)\s+<", line)
            if mm:
                opts[mm.group(1)] = True
        caps.filter_opts[fname] = opts

    # ---- derived flags ----------------------------------------------
    caps.scale_eval = caps.opt("scale", "eval")
    caps.crop_eval = caps.opt("crop", "eval")
    caps.tmix_weights = caps.opt("tmix", "weights")
    caps.amix_normalize = caps.opt("amix", "normalize")
    # firequalizer's time variance lives in the gain-expression docstring
    _, fq_help = _run([ffmpeg, "-hide_banner", "-h", "filter=firequalizer"])
    caps.firequalizer_tv = ("pts" in fq_help and "tb" in fq_help) or caps.version >= (4, 3)
    return caps


def supported_opts(caps: Capabilities, fname: str, **kwargs: str) -> str:
    """Return ':k=v' pairs only for options the installed build supports.

    Lets stage modules target old AND new FFmpeg with one code path
    (e.g. chorus delays vs delay, amix normalize vs manual volume trim).
    """
    return ":".join(f"{k}={v}" for k, v in kwargs.items() if caps.opt(fname, k))


if __name__ == "__main__":
    caps = probe()
    print(caps.summary())
