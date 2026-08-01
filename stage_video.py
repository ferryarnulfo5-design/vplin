#!/usr/bin/env python3
"""
stage_video.py -- Temporal-spatial lattice deformation & hash-breaking.

Anti-Visual-Fingerprinting layer. Targets:
  * Temporal memory / block-matching frame hashes (Content ID visual, VGG hashes):
      tmix temporal blending + optional tblend smear destroy frame-to-frame
      correspondence, with ~90% less CPU than minterpolate (no motion search).
  * SIFT/SURF/ORB keypoint matching (local feature descriptors):
      chromashift/rgbashift sub-pixel plane misalignment, time-modulated via
      sendcmd. Global composition is untouched; local descriptors are not.
  * Grid/geometric fingerprinting (pixel-layout correlation):
      zoompan per-frame zoom+pan (expression var `on`) => no two frames share
      the original lattice. On FFmpeg < 5.0 the cheaper scale/crop eval=frame
      path is used instead; static fallback for stripped builds.
  * Global histogram fingerprinting (color-distribution matching):
      out-of-phase sinusoidal modulation of contrast/gamma/saturation (eq,
      eval=frame).
  * Denoise-based normalization: light temporal+uniform noise (noise filter).

FFmpeg reality notes (why this is engineered this way):
  * rgbashift/chromashift options are STATIC INTS -- no expression eval.
    True time-variance is achieved with ONE filter instance + sendcmd
    step-commands (quantized sine), keeping filter count O(1) in duration.
  * chromashift (planar YUV, no conversion) is preferred over rgbashift
    (needs format=rgb24 round-trip) on yuv420p input.
  * scale/crop eval=frame was removed in FFmpeg 5.0; zoompan d=1 is the
    universal per-frame geometry primitive (z/x/y evaluated each output
    frame via `on`).
  * tmix weights= requires FFmpeg >= 4.4; probed and degraded to uniform.
  * Every heavy choice is probe-gated; the whole chain stays O(N) per frame.
"""
from __future__ import annotations

import math
import os
import tempfile
from typing import Dict, List, Optional, Tuple

from ffmpeg_compat import Capabilities, _run

# ---------------------------------------------------------------------------
# L1 -- temporal hash breaker (replaces minterpolate entirely)
# ---------------------------------------------------------------------------
def _temporal_chain(caps: Capabilities, low_cpu: bool, in_lbl: str) -> Tuple[str, str, Dict]:
    """tmix 3-frame weighted blending; optional tblend smear on strong runners.

    tmix: O(frames_per_window) memory ops per pixel -- no motion estimation.
    weights='1 2 1' gives a binomial temporal kernel (Gaussian-ish smoothing
    of the temporal axis), which smears per-frame energy into neighbors.
    """
    meta: Dict = {"tmix": False, "tmix_weights": False, "tblend": False}
    cur = in_lbl
    parts = []
    
    if caps.has("tmix"):
        if caps.tmix_weights:
            parts.append(f"[{cur}]tmix=frames=3:weights='1 2 1'[v1]")
            meta["tmix_weights"] = True
        else:
            parts.append(f"[{cur}]tmix=frames=3[v1]")
        meta["tmix"] = True
        cur = "v1"
        
        if not low_cpu and caps.has("tblend"):
            parts.append(f"[{cur}]tblend=all_mode=average[v1b]")
            meta["tblend"] = True
            cur = "v1b"
        return ";".join(parts), cur, meta
    
    if caps.has("tblend"):
        meta["tblend"] = True
        return f"[{cur}]tblend=all_mode=average[v1]", "v1", meta
    
    return "", cur, meta


# ---------------------------------------------------------------------------
# L2 -- sub-visual gamma / chrominance modulation (eval=frame)
# ---------------------------------------------------------------------------
def _color_modulation_chain(caps: Capabilities, in_lbl: str) -> Tuple[Optional[str], str, Dict]:
    """Slow out-of-phase sinusoidal eq. `t` is seconds; expressions re-evaluated
    per frame via eval=frame. Shifts global color histograms over time so
    histogram-based matching sees a moving target."""
    meta: Dict = {"eq_eval_frame": False}
    if not caps.opt("eq", "eval"):
        return None, in_lbl, meta
    meta["eq_eval_frame"] = True
    frag = (
        f"[{in_lbl}]"
        "eq=contrast=1+0.06*sin(2*PI*t/12)"
        ":gamma=1+0.05*sin(2*PI*t/17+1)"
        ":saturation=1+0.08*sin(2*PI*t/23+2)"
        ":eval=frame[v2]"
    )
    return frag, "v2", meta


# ---------------------------------------------------------------------------
# L3 -- per-frame dynamic zoom/pan (lattice deformation)
# ---------------------------------------------------------------------------
def _geometry_chain(
    caps: Capabilities, in_lbl: str, width: int, height: int, fps: float, low_cpu: bool
) -> Tuple[str, str, Dict]:
    """Three probe-selected strategies, cheapest-first:
      1) FFmpeg < 5.0: scale/crop with eval=frame (variables n, iw, ih).
      2) FFmpeg >= 5.0 (and any build with zoompan): zoompan d=1, expressions
         evaluated per output frame via `on`.
      3) static scale+crop fallback (degraded but safe).
    zoompan notes: d=1 -> one output frame per input frame; z>=1 so we only
    ever crop inward; x/y pan within bounds; s=WxH pins output resolution;
    fps= forces exact frame rate (redundant safety with our fps= pre-step).
    """
    meta: Dict = {"geometry": "static"}
    if caps.has("zoompan"):
        amp = 12 if low_cpu else 24
        zoom_expr = "min(1+0.0025*on,1.12)"
        x_expr = f"iw/2-(iw/zoom/2)+{amp}*sin(2*PI*on/150)"
        y_expr = f"ih/2-(ih/zoom/2)+{amp}*cos(2*PI*on/190)"
        frag = (
            f"[{in_lbl}]zoompan="
            f"z='{zoom_expr}':x='{x_expr}':y='{y_expr}':"
            f"d=1:s={width}x{height}:fps={int(fps)}[vg]"
        )
        meta["geometry"] = "zoompan"
        return frag, "vg", meta
    if caps.scale_eval and caps.crop_eval:
        # 5% supersample, then a constant 24px crop window sliding sinusoidally.
        # x/y bounded to [-12,12], window margin 24 => crop never overruns.
        frag = (
            f"[{in_lbl}]"
            "scale=w='trunc(iw*1.05)':h='trunc(ih*1.05)':eval=frame,"
            "crop=w='iw-24':h='ih-24':"
            "x='12*sin(2*PI*n/90)':y='12*cos(2*PI*n/110)':eval=frame[vg]"
        )
        meta["geometry"] = "scale_crop_eval"
        return frag, "vg", meta
    frag = (
        f"[{in_lbl}]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}[vg]"
    )
    return frag, "vg", meta


# ---------------------------------------------------------------------------
# L4 -- localized planar misalignment (anti-SIFT/SURF), time-modulated
# ---------------------------------------------------------------------------
def _write_shift_cmd_file(target: str, duration_s: float, dt: float = 0.5) -> str:
    """Quantized sinusoidal plane shifts, sampled every dt seconds.

    rh(t) = 2*sin(2*pi*t/10)   rv(t) = 2*cos(2*pi*t/15)
    bh(t) = 1*sin(2*pi*t/8+1)  bv(t) = 1*cos(2*pi*t/12+2)

    On yuv420p, chroma-plane units are HALF resolution, so 1 unit = 2 luma px.
    Amplitudes are therefore small (1-2) to stay sub-visible while still
    destroying keypoint geometry (SIFT works at subpixel scale-space precision).
    """
    lines: List[str] = []
    steps = int(duration_s / dt) + 1
    for i in range(steps):
        t = i * dt
        rh = round(2 * math.sin(2 * math.pi * t / 10.0))
        rv = round(2 * math.cos(2 * math.pi * t / 15.0))
        bh = round(1 * math.sin(2 * math.pi * t / 8.0 + 1.0))
        bv = round(1 * math.cos(2 * math.pi * t / 12.0 + 2.0))
        lines.append(f"{target} rh {rh} {t:.3f}")
        lines.append(f"{target} rv {rv} {t:.3f}")
        lines.append(f"{target} bh {bh} {t:.3f}")
        lines.append(f"{target} bv {bv} {t:.3f}")
    fd, path = tempfile.mkstemp(prefix="planes_", suffix=".cmd", text=True)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    # FFmpeg filtergraph parses ':' and '\' specially; forward slashes are
    # accepted by Windows too, and we will single-quote the path in-graph.
    return path.replace("\\", "/")


def _probe_shift_command(target: str, pre: str, post: str, cmd_path: str,
                         ffmpeg: str = "ffmpeg") -> bool:
    """Verify the installed build actually accepts runtime commands on this
    filter, using a 1-second 320x240 testsrc. ~1-2s cost, once per run."""
    vf = (
        f"{pre}{target}=rh=0:rv=0:bh=0:bv=0,{post}"
        f"sendcmd=filename='{cmd_path}'"
    )
    rc, out = _run([
        ffmpeg, "-hide_banner", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
        "-vf", vf, "-f", "null", "-",
    ], timeout=30)
    if rc != 0:
        return False
    bad = ("error" in out.lower() or "invalid" in out.lower()
           or "unrecognized" in out.lower())
    return not bad


def _plane_shift_chain(
    caps: Capabilities, in_lbl: str, duration_s: float,
) -> Tuple[Optional[str], str, Dict, Optional[str]]:
    """chromashift (YUV, zero conversion) > rgbashift (RGB round-trip) > skip."""
    meta: Dict = {"plane_shift": None}
    cmd_path: Optional[str] = None
    if not caps.has("sendcmd"):
        return None, in_lbl, meta, None

    candidates = []
    if caps.has("chromashift"):
        candidates.append(("chromashift", "", ""))
    if caps.has("rgbashift"):
        candidates.append(("rgbashift", "format=rgb24,", ",format=yuv420p"))

    for target, pre, post in candidates:
        path = _write_shift_cmd_file(target, duration_s)
        if _probe_shift_command(target, pre, post, path):
            meta["plane_shift"] = target
            cmd_path = path
            frag = (
                f"[{in_lbl}]{pre}{target}=rh=0:rv=0:bh=0:bv=0,{post}"
                f"sendcmd=filename='{path}'[vs]"
            )
            return frag, "vs", meta, cmd_path
        try:
            os.unlink(path)
        except OSError:
            pass
    return None, in_lbl, meta, None


# ---------------------------------------------------------------------------
# L5 -- light noise (anti-normalization) + final resync
# ---------------------------------------------------------------------------
def _finalize_chain(in_lbl: str, low_cpu: bool, caps: Capabilities) -> Tuple[str, str, Dict]:
    """Add noise (if available), reset SAR, restart PTS from 0."""
    meta: Dict = {"noise": 0}
    cur = in_lbl
    parts = []
    
    if caps.has("noise"):
        strength = 4 if low_cpu else 6
        meta["noise"] = strength
        parts.append(f"[{cur}]noise=alls={strength}:allf=t+u[vn]")
        cur = "vn"
    
    # PTS restart + SAR reset => exact 30fps stream, zero A/V drift.
    parts.append(f"[{cur}]format=yuv420p,setsar=1,setpts=PTS-STARTPTS[vout]")
    return ";".join(parts), "vout", meta


# ---------------------------------------------------------------------------
# Public graph builder
# ---------------------------------------------------------------------------
def build_video_filtergraph(
    caps: Capabilities,
    low_cpu: bool,
    duration_s: float,
    fps: float = 30.0,
    width: int = 1920,
    height: int = 1080,
) -> Tuple[str, Optional[str], Dict]:
    """Return (filter_complex_fragment, plane_shift_cmd_path, meta).

    cmd_path is a tempfile the CALLER must unlink in a finally block.
    The returned graph ends at [vout]; pipeline.py feeds it into libx264.
    """
    parts: List[str] = []
    meta_all: Dict = {"low_cpu": low_cpu}

    # input normalization: constant 30fps clock, planar YUV, PTS from 0
    parts.append(f"[0:v]fps={int(fps)},format=yuv420p,setpts=PTS-STARTPTS[v0]")
    cur = "v0"

    frag, cur, m = _temporal_chain(caps, low_cpu, cur)
    if frag:
        parts.append(frag)
        meta_all.update(m)

    frag, cur, m = _color_modulation_chain(caps, cur)
    if frag:
        parts.append(frag)
        meta_all.update(m)

    frag, cur, m = _geometry_chain(caps, cur, width, height, fps, low_cpu)
    parts.append(frag)
    meta_all.update(m)

    frag, cur, m, cmd_path = _plane_shift_chain(caps, cur, duration_s)
    if frag:
        parts.append(frag)
        meta_all.update(m)

    frag, cur, m = _finalize_chain(cur, low_cpu, caps)
    parts.append(frag)
    meta_all.update(m)

    return ";".join(parts), cmd_path, meta_all


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from ffmpeg_compat import probe
    caps = probe()
    graph, cmd, meta = build_video_filtergraph(
        caps, low_cpu=False, duration_s=600.0, fps=30.0,
        width=1920, height=1080,
    )
    print("CAPABILITIES:", caps.summary(), "\n")
    print("META:", meta, "\n")
    print("FILTER_GRAPH:")
    print(graph)
    if cmd:
        os.unlink(cmd)
