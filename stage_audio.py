#!/usr/bin/env python3
"""
v4.0 Audio Disruption Module
Implements dynamic equalization, split-band pitch skewing, phase warping,
and non-linear amplitude compression to break audio fingerprinting (Chromaprint).
"""

import random
from typing import List
from ffmpeg_compat import Capabilities


def get_equalizer_chain(compat: Capabilities) -> str:
    """3-Tier Fallback Equalization Chain."""
    if compat.has("firequalizer"):
        # Safe minimal FIR equalization (avoiding OOM on 2-core runners)
        return "firequalizer=gain='if(lt(mod(pts*tb,20),10),-6*sin(f/1200),-1.5)'"
    elif compat.has("equalizer"):
        return "equalizer=f=250:width_type=h:width=50:g=-3,equalizer=f=4000:width_type=h:width=200:g=2"
    elif compat.has("aequalizer"):
        return "aequalizer=c0=250|h|50|-3|0|0"
    return "anull"


def build_audio_filterchain(compat: Capabilities, low_cpu: bool = True, seed: int = 42) -> str:
    rng = random.Random(seed)

    # 1. Base equalization fallback tier
    eq_node = get_equalizer_chain(compat)

    # 2. Non-linear amplitude warping (compand)
    compand_node = (
        "compand=attacks=0.3:decays=0.8:"
        "points=-80/-80|-30/-15|-12/-9|-6/-6|0/-2:"
        "soft-knee=6:gain=3"
    )

    # 3. Modulation effects chain (probe-gated)
    mod_filters: List[str] = []
    if compat.has("aphaser"):
        mod_filters.append("aphaser=type=sinusoidal:decay=0.4:speed=0.6")
    if compat.has("chorus"):
        mod_filters.append(
            "chorus=in_gain=0.5:out_gain=0.7:"
            "delays=40|55:decays=0.3|0.2:"
            "speeds=0.4|0.6:depths=0.5|0.7"
        )
    if compat.has("aecho") and not low_cpu:
        mod_filters.append("aecho=0.8:0.7:60|120:0.2|0.1")

    mod_chain = ",".join(mod_filters) if mod_filters else "anull"

    # 4. Split-band pitch skew
    if low_cpu:
        # 2-Band split for 2-core vCPU efficiency
        skew_graph = (
            f"[0:a]{eq_node},{compand_node},{mod_chain},asplit=2[in1][in2];"
            f"[in1]highpass=f=5000,asetrate=44100*1.02,atempo=0.98039,volume=0.5[high];"
            f"[in2]lowpass=f=5000,asetrate=44100*0.98,atempo=1.0204,volume=0.5[low];"
            f"[high][low]amix=inputs=2:normalize=0[mixed];"
        )
    else:
        # 3-Band split for full CPU capacity
        skew_graph = (
            f"[0:a]{eq_node},{compand_node},{mod_chain},asplit=3[in1][in2][in3];"
            f"[in1]highpass=f=4500,asetrate=44100*1.03,atempo=0.97087,volume=0.34[high];"
            f"[in2]bandpass=f=1500:width_type=h:w=1000,asetrate=44100*1.01,atempo=0.99009,volume=0.34[mid];"
            f"[in3]lowpass=f=450,asetrate=44100*0.97,atempo=1.0309,volume=0.34[low];"
            f"[high][mid][low]amix=inputs=3:normalize=0[mixed];"
        )

    # 5. Final Stage Normalization
    final_node = (
        "[mixed]alimiter=limit=-0.5dB:level=false,aresample=44100,"
        "asetpts=PTS-STARTPTS[aout]"
    )

    return skew_graph + final_node
