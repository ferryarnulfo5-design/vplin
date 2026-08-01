#!/usr/bin/env python3
"""
stage_audio.py -- Cryptographic Audio Waveform Disruption (v3.0)
Optimized for FFmpeg 6.1.1 with fallbacks for missing filters.
"""
from __future__ import annotations

import math
import os
from typing import Dict, List, Optional, Tuple

from ffmpeg_compat import Capabilities, _run

DEFAULT_SR = 48000

# ----------------------------------------------------------------------
# L1 + L2: Constant‑time swept EQ (firequalizer) or static aequalizer
# ----------------------------------------------------------------------
def _static_eq() -> str:
    """Static 3-band parametric EQ (safe for all FFmpeg builds)."""
    return (
        "aequalizer=f=250:t=q:w=1.2:g=-3,"
        "aequalizer=f=1600:t=q:w=1.5:g=2.5,"
        "aequalizer=f=6500:t=q:w=2.0:g=-4"
    )

def _fq_swept_notch() -> str:
    """Firequalizer with time‑varying swept notch (if filter exists)."""
    return (
        "firequalizer="
        "gain='if(lt(mod(pts*tb,20),10),-6*sin(f/1200),-1.5)':"
        "fscale=lin:fir=1:fir_size=2048:zero_phase=0"
    )

def _fq_wobble() -> str:
    """Optional slow sinusoidal wobble on top of the swept notch."""
    return (
        "firequalizer="
        "gain='2.5*sin(2*PI*pts*tb/9)*sin(f/700)':"
        "fscale=lin:fir=1:fir_size=2048:zero_phase=0"
    )

# ----------------------------------------------------------------------
# L3: Non‑linear amplitude envelope warping (compand)
# ----------------------------------------------------------------------
_COMPAND = (
    "compand=attacks=0.3:decays=0.8:"
    "points=-80/-80|-30/-15|-12/-9|-6/-6|0/-2:"
    "soft-knee=6:gain=3"
)

# ----------------------------------------------------------------------
# L4: Phase inversion + comb filtering (aphaser + chorus)
# ----------------------------------------------------------------------
def _modulation(caps: Capabilities) -> str:
    """Return a comma‑separated chain of modulation filters; empty if none."""
    chain = []
    if caps.has("aphaser"):
        chain.append("aphaser=type=sinusoidal:decay=0.4:speed=0.6")
    if caps.has("chorus"):
        # Use named options if available, otherwise fallback to positional
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
# L5: Split‑band frequency shifting (2 or 3 bands)
# ----------------------------------------------------------------------
def _bands(low_cpu: bool) -> Tuple[Tuple[str, str, float], ...]:
    """Return (label, split_filter, pitch_factor) for each band."""
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
    pre: List[str] = []
    # ------------------------------------------------------------------
    # L1 + L2: frequency‑domain EQ (firequalizer or static aequalizer)
    # ------------------------------------------------------------------
    if low_cpu or not caps.has("firequalizer"):
        pre.append(_static_eq())
    else:
        pre.append(_fq_swept_notch())
        if caps.firequalizer_tv:
            pre.append(_fq_wobble())

    # L3: non‑linear compand
    pre.append(_COMPAND)

    # L4: modulation (aphaser / chorus)
    mod = _modulation(caps)
    if mod:
        pre.append(mod)

    # L5: split into bands
    n = len(_bands(low_cpu))
    pre.append(f"asplit={n}" + "".join(f"[b{i}]" for i in range(n)))

    # Build the pre‑chain as a single comma‑separated list of non‑empty filters
    # Filter out any empty strings to avoid "No such filter: ''"
    pre_chain = ",".join([p for p in pre if p.strip()])
    graph_parts = [f"[{in_label}]{pre_chain}"]

    # ------------------------------------------------------------------
    # L6: per‑band pitch skew (asetrate + atempo)
    # ------------------------------------------------------------------
    mix_inputs: List[str] = []
    for i, (name, split_filter, rate) in enumerate(_bands(low_cpu)):
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

    # ------------------------------------------------------------------
    # L7: mix, limit, resync, exact duration trim
    # ------------------------------------------------------------------
    mixer = f"amix=inputs={n}:duration=longest"
    if caps.amix_normalize:
        mixer += ":normalize=0"
    graph_parts.append(
        f"{''.join(mix_inputs)}{mixer},"
        f"alimiter=limit=0.95,"
        f"aresample={sr}:async=1:first_pts=0,"
        f"atrim=0:{duration_s:.3f},asetpts=PTS-STARTPTS[aout]"
    )

    # Join all parts with ';' – each part is a complete filter chain
    return ";\n".join(graph_parts)

# ----------------------------------------------------------------------
# Standalone test
# ----------------------------------------------------------------------
if __name__ == "__main__":
    from ffmpeg_compat import probe
    caps = probe()
    graph = build_audio_filtergraph(caps, low_cpu=False, duration_s=600.0)
    print("AUDIO FILTERGRAPH:\n", graph)
