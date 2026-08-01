#!/usr/bin/env python3
"""
stage_audio.py -- Simplified but DEADLY audio disruption.
Engineered specifically for FFmpeg 6.1.1 (avoids firequalizer/fscale bugs).
Targets: Chromaprint temporal energy envelopes + ACRCloud spectral phases.
"""
from __future__ import annotations

import math
from typing import Dict, List, Tuple

from ffmpeg_compat import Capabilities

DEFAULT_SR = 48000

def _static_eq() -> str:
    """Static notches to warp spectral energy distribution."""
    return (
        "equalizer=f=250:width_type=h:width=50:g=-3,"
        "equalizer=f=1600:width_type=h:width=80:g=2.5,"
        "equalizer=f=6500:width_type=h:width=100:g=-4"
    )

def _modulation(caps: Capabilities) -> str:
    """Phase rotation + comb filtering (breaks ACRCloud reflections)."""
    chain = []
    if caps.has("aphaser"):
        chain.append("aphaser=type=sinusoidal:decay=0.4:speed=0.6")
    if caps.has("chorus"):
        chain.append("chorus=in_gain=0.5:out_gain=0.7:delays=40|55:decays=0.3|0.2:speeds=0.4|0.6:depths=0.5|0.7")
    # Light echo adds micro-reverb to destroy temporal peaks (replaces swept notch)
    if caps.has("aecho"):
        chain.append("aecho=0.8:0.7:60|120:0.2|0.1")
    return ",".join(chain)

def _bands(low_cpu: bool) -> Tuple[Tuple[str, str, float], ...]:
    """Split bands for pitch skewing."""
    if low_cpu:
        return (("hb", "highpass=f=5000", 0.955), ("lb", "lowpass=f=5000", 1.045))
    return (
        ("hf", "highpass=f=4500", 0.955),
        ("mf", "bandpass=f=1500:w=3000", 1.045),
        ("lf", "lowpass=f=450", 0.985),
    )

def build_audio_filtergraph(
    caps: Capabilities,
    low_cpu: bool,
    duration_s: float,
    in_label: str = "0:a",
    sr: int = DEFAULT_SR,
) -> str:
    pre: List[str] = []
    
    # L1: Spectral notches
    pre.append(_static_eq())
    
    # L2: Non-linear amplitude envelope warping (Chromaprint's #1 enemy)
    pre.append("compand=attacks=0.3:decays=0.8:points=-80/-80|-30/-15|-12/-9|-6/-6|0/-2:soft-knee=6:gain=3")
    
    # L3: Phase + Comb + Echo (ACRCloud's enemy)
    mod = _modulation(caps)
    if mod:
        pre.append(mod)
    
    # L4: Split for pitch skew
    n = len(_bands(low_cpu))
    pre.append(f"asplit={n}" + "".join(f"[b{i}]" for i in range(n)))
    
    pre_chain = ",".join(pre)
    graph_parts = [f"[{in_label}]{pre_chain}"]

    # L5: Per-band pitch shifting
    mix_inputs: List[str] = []
    for i, (_, split_filter, rate) in enumerate(_bands(low_cpu)):
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

    # L6: Mix, limit, and exact duration lock
    mixer = "amix=inputs={}:duration=longest".format(n)
    if caps.amix_normalize:
        mixer += ":normalize=0"
    graph_parts.append(
        f"{''.join(mix_inputs)}{mixer},"
        f"alimiter=limit=0.95,"
        f"aresample={sr}:async=1:first_pts=0,"
        f"atrim=0:{duration_s:.3f},asetpts=PTS-STARTPTS[aout]"
    )
    return ";\n".join(graph_parts)

if __name__ == "__main__":
    from ffmpeg_compat import probe
    caps = probe()
    graph = build_audio_filtergraph(caps, low_cpu=False, duration_s=600.0)
    print("AUDIO FILTERGRAPH:\n", graph)
