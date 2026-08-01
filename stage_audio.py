#!/usr/bin/env python3
"""
stage_audio.py -- Cryptographic Audio Waveform Disruption (v3.0 - Stable)
Uses only filters that exist in ALL FFmpeg builds (aequalizer, aphaser, compand, etc.)
No firequalizer dependency - safe for FFmpeg 6.1.1 and older.
"""
from __future__ import annotations

import math
from typing import List, Tuple

from ffmpeg_compat import Capabilities

DEFAULT_SR = 48000

# ----------------------------------------------------------------------
# L1: Static multi-band EQ (aequalizer - safe for all builds)
# ----------------------------------------------------------------------
def _static_eq() -> str:
    """Static 3-band parametric EQ using aequalizer (always available)."""
    return (
        "aequalizer=f=250:t=q:w=1.2:g=-3,"
        "aequalizer=f=1600:t=q:w=1.5:g=2.5,"
        "aequalizer=f=6500:t=q:w=2.0:g=-4"
    )

# ----------------------------------------------------------------------
# L2: Non-linear amplitude envelope warping (compand)
# ----------------------------------------------------------------------
_COMPAND = (
    "compand=attacks=0.3:decays=0.8:"
    "points=-80/-80|-30/-15|-12/-9|-6/-6|0/-2:"
    "soft-knee=6:gain=3"
)

# ----------------------------------------------------------------------
# L3: Phase inversion (aphaser) + optional chorus (if available)
# ----------------------------------------------------------------------
def _modulation(caps: Capabilities) -> str:
    """Return a comma-separated chain of modulation filters."""
    chain = []
    # aphaser is available in all FFmpeg builds since 2015
    chain.append("aphaser=type=sinusoidal:decay=0.4:speed=0.6")
    # chorus is optional; only add if available
    if caps.has("chorus"):
        if caps.opt("chorus", "delays"):
            chain.append(
                "chorus=in_gain=0.5:out_gain=0.7:"
                "delays=40|55:decays=0.3|0.2:speeds=0.4|0.6:depths=0.5|0.7"
            )
        else:
            chain.append(
                "chorus=0.5:0.7:40|55:0.3|0.2:0.4|0.6:0.5|0.7"
            )
    return ",".join(chain)

# ----------------------------------------------------------------------
# L4: Split-band frequency shifting (2 or 3 bands)
# ----------------------------------------------------------------------
def _bands(low_cpu: bool) -> Tuple[Tuple[str, str, float], ...]:
    if low_cpu:
        return (
            ("hb", "highpass=f=5000", 0.955),
            ("lb", "lowpass=f=5000", 1.045),
        )
    return (
        ("hf", "highpass=f=4500", 0.955),
        ("mf", "bandpass=f=1500:w=3000", 1.045),
        ("lf", "lowpass=f=450", 0.985),
    )

# ----------------------------------------------------------------------
# Main graph builder
# ----------------------------------------------------------------------
def build_audio_filtergraph(
    caps: Capabilities,
    low_cpu: bool,
    duration_s: float,
    in_label: str = "0:a",
    sr: int = DEFAULT_SR,
) -> str:
    """Return the full audio filter_complex fragment ending in [aout]."""
    # ----- Pre-chain: EQ + compand + modulation -----
    pre_parts = []
    pre_parts.append(_static_eq())          # always available
    pre_parts.append(_COMPAND)              # always available
    mod = _modulation(caps)                 # might be empty
    if mod:
        pre_parts.append(mod)
    
    # Join with commas, filtering out any empty strings just in case
    pre_chain = ",".join([p for p in pre_parts if p.strip()])
    
    # Number of bands
    bands = _bands(low_cpu)
    n = len(bands)
    
    # Start building graph parts
    graph_parts = []
    
    # Part 1: Input -> pre-chain -> split into N bands
    split_pads = "".join(f"[b{i}]" for i in range(n))
    graph_parts.append(f"[{in_label}]{pre_chain},asplit={n}{split_pads}")
    
    # Part 2: Per-band pitch shifting
    mix_inputs = []
    for i, (name, split_filter, rate) in enumerate(bands):
        pitch = sr * rate
        tempo = 1.0 / rate
        vol = "volume=0.34" if n == 3 else "volume=0.5"
        seg = (
            f"[b{i}]{split_filter},"
            f"asetrate={pitch:.6f},aresample={sr},atempo={tempo:.6f},"
            f"{vol}[o{i}]"
        )
        graph_parts.append(seg)
        mix_inputs.append(f"[o{i}]")
    
    # Part 3: Mix, limit, resync, trim to exact duration
    mixer = f"amix=inputs={n}:duration=longest"
    if caps.amix_normalize:
        mixer += ":normalize=0"
    graph_parts.append(
        f"{''.join(mix_inputs)}{mixer},"
        f"alimiter=limit=0.95,"
        f"aresample={sr}:async=1:first_pts=0,"
        f"atrim=0:{duration_s:.3f},asetpts=PTS-STARTPTS[aout]"
    )
    
    # Join all parts with ';'
    return ";\n".join(graph_parts)


if __name__ == "__main__":
    from ffmpeg_compat import probe
    caps = probe()
    graph = build_audio_filtergraph(caps, low_cpu=False, duration_s=600.0)
    print("AUDIO FILTERGRAPH:\n", graph)
