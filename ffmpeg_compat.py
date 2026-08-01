#!/usr/bin/env python3
"""
ffmpeg_compat.py — runtime capability introspection for FFmpeg 5.x .. git-master-2026.
"""
from __future__ import annotations
import re
import subprocess
from dataclasses import dataclass, field

KNOWN_MOV = {
    "brand", "movie_timescale", "video_track_timescale", "use_editlist",
    "write_udta", "write_btrt", "write_prft", "write_tmcd",
    "min_frag_duration", "movflags",
}

FILTERS_TO_PROBE = [
    "scale", "crop", "eq", "hue", "tmix", "setpts", "minterpolate", "noise",
    "aphaser", "equalizer", "firequalizer", "anoisesrc", "acontrast",
    "crystalizer", "vibrato", "tremolo", "aecho", "dynaudnorm", "alimiter",
    "loudnorm", "aformat", "asplit", "amix", "asetrate", "aresample",
    "atempo", "atrim", "asetpts", "afade", "concat",
]

@dataclass
class Compat:
    ffmpeg: str = "ffmpeg"
    major: int = 8
    has_filter_units: bool = True
    filter_opts: dict = field(default_factory=dict)
    mov_help: str = ""
    mp4_help: str = ""

    def filter_has_opt(self, fname: str, opt: str) -> bool:
        return opt in self.filter_opts.get(fname, set())

    def mov_opt(self, name: str) -> bool:
        if name not in KNOWN_MOV:
            return False
        h_mov = self.mov_help if _looks_like_help(self.mov_help) else ""
        h_mp4 = self.mp4_help if _looks_like_help(self.mp4_help) else ""
        if not h_mov and not h_mp4:
            return True  # optimistic; remux cascade catches
        pat = re.compile(rf"(?m)^[ \t]*-{re.escape(name)}(?:[ \t]|$)")
        return bool(pat.search(h_mov) or pat.search(h_mp4))

    def fps_mode_args(self) -> list:
        return ["-fps_mode", "vfr"] if self.major >= 7 else ["-vsync", "vfr"]

    @property
    def hue_has_eval(self) -> bool:
        return self.filter_has_opt("hue", "eval")
    @property
    def scale_has_eval(self) -> bool:
        return self.filter_has_opt("scale", "eval")
    @property
    def eq_has_eval(self) -> bool:
        return self.filter_has_opt("eq", "eval")
    @property
    def crop_has_eval(self) -> bool:
        return self.filter_has_opt("crop", "eval")
    @property
    def has_firequalizer(self) -> bool:
        return "firequalizer" in self.filter_opts
    @property
    def has_anoisesrc(self) -> bool:
        return "anoisesrc" in self.filter_opts


def _capture(cmd) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=30)
        return p.stdout + "\n" + p.stderr
    except Exception:
        return ""

def _looks_like_help(text: str) -> bool:
    return any(k in text for k in ("AVOptions", "Muxer", "muxer"))

def _version_major(ffmpeg: str) -> int:
    out = _capture([ffmpeg, "-version"])
    m = re.search(r"ffmpeg version (\d+)", out)
    return int(m.group(1)) if m else 8

def _filter_options(ffmpeg: str, name: str) -> set:
    out = _capture([ffmpeg, "-hide_banner", "-h", f"filter={name}"])
    return {m.group(1) for line in out.splitlines()
            if (m := re.match(r"^[ \t]+([A-Za-z_]\w*)[ \t]+<[A-Za-z_]+>", line))}

def probe_compat(ffmpeg: str = "ffmpeg") -> Compat:
    return Compat(
        ffmpeg=ffmpeg,
        major=_version_major(ffmpeg),
        has_filter_units="filter_units" in _capture([ffmpeg, "-hide_banner", "-bsfs"]),
        filter_opts={name: _filter_options(ffmpeg, name) for name in FILTERS_TO_PROBE},
        mov_help=_capture([ffmpeg, "-hide_banner", "-h", "muxer=mov"]),
        mp4_help=_capture([ffmpeg, "-hide_banner", "-h", "muxer=mp4"]),
    )
