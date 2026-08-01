#!/usr/bin/env python3
"""
ffmpeg_compat.py — runtime capability introspection for FFmpeg 5.x .. git-master-2026.

v2.1 fixes (verified against real -h output):
  - filter help option lines are indented >= 4 spaces: '      contrast <double> ...'
    (old ^[ \t]{2} regex never matched -> false 'no' for scale/eq eval)
  - muxer help option lines are prefixed with '-': '    -brand <string> E........'
  - aphaser 'type' enum is {triangular/t, sinusoidal/s} in EVERY version that
    has the filter (docs-stable since 2015) -> no probe needed, hardcoded.

Self-test:
    python ffmpeg_compat.py
"""
from __future__ import annotations
import re
import subprocess
from dataclasses import dataclass, field

# Options the container stage may emit; anything else is never gated.
KNOWN_MOV = {
    "brand", "movie_timescale", "video_track_timescale", "use_editlist",
    "write_udta", "write_btrt", "write_prft", "write_tmcd",
    "min_frag_duration", "movflags",
}

# Filters the pipeline uses; probed once at startup.
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
    filter_opts: dict = field(default_factory=dict)   # filter -> set(option names)
    mov_help: str = ""
    mp4_help: str = ""

    # ---- generic helpers ---------------------------------------------------
    def filter_has_opt(self, fname: str, opt: str) -> bool:
        return opt in self.filter_opts.get(fname, set())

    def mov_opt(self, name: str) -> bool:
        """Does the running build's MOV/MP4 muxer expose this option?"""
        if name not in KNOWN_MOV:
            return False
        h_mov = self.mov_help if _looks_like_help(self.mov_help) else ""
        h_mp4 = self.mp4_help if _looks_like_help(self.mp4_help) else ""
        if not h_mov and not h_mp4:
            return True  # probe failed -> optimistic; remux cascade catches
        pat = re.compile(rf"(?m)^[ \t]*-{re.escape(name)}(?:[ \t]|$)")
        return bool(pat.search(h_mov) or pat.search(h_mp4))

    def fps_mode_args(self) -> list:
        """-vsync removed in FFmpeg 7.0; -fps_mode is the successor."""
        return ["-fps_mode", "vfr"] if self.major >= 7 else ["-vsync", "vfr"]

    # ---- convenience gates used by the stage modules -----------------------
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


# ---------------------------------------------------------------------------
def _capture(cmd) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           errors="replace", timeout=30)
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
    """Parse `ffmpeg -h filter=<name>` -> set of option names.

    Option lines look like:
        '      contrast           <double>     ..F.A.... set the contrast ...'
    i.e. 1+ leading spaces, option name, whitespace, '<type>'. Enum-value
    lines ('       triangular        t             ..F.A....'), flag lines
    and headers never match because the token after the name must be '<...>'.
    """
    out = _capture([ffmpeg, "-hide_banner", "-h", f"filter={name}"])
    return {m.group(1) for line in out.splitlines()
            if (m := re.match(r"^[ \t]+([A-Za-z_]\w*)[ \t]+<[A-Za-z_]+>", line))}


def probe_compat(ffmpeg: str = "ffmpeg") -> Compat:
    """Probe the running binary once per pipeline invocation."""
    return Compat(
        ffmpeg=ffmpeg,
        major=_version_major(ffmpeg),
        has_filter_units="filter_units" in _capture([ffmpeg, "-hide_banner", "-bsfs"]),
        filter_opts={name: _filter_options(ffmpeg, name)
                     for name in FILTERS_TO_PROBE},
        mov_help=_capture([ffmpeg, "-hide_banner", "-h", "muxer=mov"]),
        mp4_help=_capture([ffmpeg, "-hide_banner", "-h", "muxer=mp4"]),
    )


if __name__ == "__main__":
    c = probe_compat()
    print(f"ffmpeg major version   : {c.major}")
    print(f"filter_units bsf       : {'yes' if c.has_filter_units else 'no'}")
    print(f"hue eval option        : {'yes' if c.hue_has_eval else 'no (removed - per-frame default)'}")
    print(f"scale eval option      : {'yes' if c.scale_has_eval else 'no (static fallback)'}")
    print(f"eq eval option         : {'yes' if c.eq_has_eval else 'no (static fallback)'}")
    print(f"crop eval option       : {'yes' if c.crop_has_eval else 'no (frame default)'}")
    print(f"firequalizer filter    : {'yes (gain-only syntax)' if c.has_firequalizer else 'no (dropped)'}")
    print(f"anoisesrc filter       : {'yes' if c.has_anoisesrc else 'no (noise dropped)'}")
    print(f"aphaser type values    : ['sinusoidal', 'triangular', 's', 't'] (docs-stable)")
    print(f"mov muxer options      : brand={c.mov_opt('brand')} "
          f"vts={c.mov_opt('video_track_timescale')} "
          f"mfd={c.mov_opt('min_frag_duration')} "
          f"wudta={c.mov_opt('write_udta')} "
          f"uel={c.mov_opt('use_editlist')} "
          f"movflags={c.mov_opt('movflags')}")