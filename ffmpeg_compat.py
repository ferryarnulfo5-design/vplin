#!/usr/bin/env python3
"""
v4.0 FFmpeg Compatibility & Capability Profiler
Detects available filters, dynamic options, and API version flags to ensure
graceful fallback chains across FFmpeg 4.x - 7.x+.
"""

import re
import subprocess
from typing import Dict, List, Tuple


class Capabilities:
    def __init__(self, raw_version: str, filters: List[str], options: Dict[str, List[str]]):
        self.raw_version = raw_version
        self.version_tuple = self._parse_version(raw_version)
        self.major = self.version_tuple[0]
        self._filters = set(filters)
        self._options = options

        # Derived dynamic compatibility flags
        self.scale_eval = self.opt("scale", "eval")
        self.crop_eval = self.opt("crop", "eval")
        self.tmix_weights = self.opt("tmix", "weights")
        self.amix_normalize = self.opt("amix", "normalize")
        self.firequalizer_tv = self.opt("firequalizer", "gain")
        self.has_chromashift = self.has("chromashift")
        self.has_rgbashift = self.has("rgbashift")
        self.has_zoompan = self.has("zoompan")

    @staticmethod
    def _parse_version(version_str: str) -> Tuple[int, int, int]:
        match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", version_str)
        if match:
            major = int(match.group(1))
            minor = int(match.group(2))
            patch = int(match.group(3)) if match.group(3) else 0
            return (major, minor, patch)
        # Default fallback if build string is non-standard
        return (4, 4, 0)

    def has(self, filter_name: str) -> bool:
        return filter_name in self._filters

    def opt(self, filter_name: str, option_name: str) -> bool:
        if filter_name not in self._options:
            return False
        return option_name in self._options[filter_name]


def _run_cmd(cmd: List[str]) -> str:
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        return proc.stdout + proc.stderr
    except Exception:
        return ""


def probe_compat(ffmpeg_bin: str = "ffmpeg") -> Capabilities:
    # 1. Version checking
    version_out = _run_cmd([ffmpeg_bin, "-version"])
    v_match = re.search(r"ffmpeg version (\S+)", version_out)
    raw_v = v_match.group(1) if v_match else "4.4.0"

    # 2. Filter enumeration
    filters_out = _run_cmd([ffmpeg_bin, "-filters"])
    available_filters = []
    for line in filters_out.splitlines():
        match = re.match(r"^\s*[T.][S.][C.]\s+(\w+)", line)
        if match:
            available_filters.append(match.group(1))

    # 3. Targeted filter option queries for critical transforms
    target_filters = [
        "scale",
        "crop",
        "tmix",
        "amix",
        "firequalizer",
        "equalizer",
        "rgbashift",
        "chromashift",
        "zoompan",
        "aphaser",
        "chorus",
        "compand",
        "noise",
        "tblend",
        "framerate",
        "psnr",
        "ssim",
        "alimiter",
        "asetrate",
        "atempo",
        "aresample",
        "highpass",
        "lowpass",
        "bandpass",
        "atrim",
        "asplit",
        "volume",
        "setpts",
        "format",
        "fps",
        "sendcmd",
        "concat",
        "aecho",
    ]

    options_map = {}
    for f in target_filters:
        if f in available_filters:
            h_out = _run_cmd([ffmpeg_bin, "-h", f"filter={f}"])
            opts = re.findall(r"^\s+([a-zA-Z0-9_]+)\s+<", h_out, re.MULTILINE)
            options_map[f] = list(set(opts))

    return Capabilities(raw_v, available_filters, options_map)
