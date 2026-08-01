#!/usr/bin/env python3
"""
v4.0 Video Lattice Deformation Module
Implements spatial-temporal warping, chroma plane shifting (anti-SIFT/SURF),
temporal frame blending, and dynamic color modulation.
"""

import math
import random
from typing import Tuple
from ffmpeg_compat import Capabilities


def generate_sendcmd_file(path: str, duration_sec: float = 600.0) -> None:
    """Generates quantized sine-wave commands for time-variant plane shifting."""
    with open(path, "w", encoding="utf-8") as f:
        t = 0.0
        while t < duration_sec:
            rh = round(2 * math.sin(2 * math.pi * t / 10))
            rv = round(2 * math.cos(2 * math.pi * t / 15))
            bh = round(1 * math.sin(2 * math.pi * t / 8 + 1))
            bv = round(1 * math.cos(2 * math.pi * t / 12 + 2))

            cmd = (
                f"{t:.1f} chromashift cbh {bh}, chromashift cbv {bv}, "
                f"chromashift crh {rh}, chromashift crv {rv};\n"
            )
            f.write(cmd)
            t += 0.5


def build_video_filterchain(
    compat: Capabilities,
    w: int = 1920,
    h: int = 1080,
    fps: int = 30,
    low_cpu: bool = True,
    seed: int = 42,
    sendcmd_path: str = "chroma_cmd.txt",
) -> str:
    rng = random.Random(seed)
    chain_parts = []

    # 1. Temporal blending (replacing heavy motion-interpolated minterpolate)
    if compat.has("tmix"):
        weights = "1 2 1" if compat.tmix_weights else "1 1 1"
        chain_parts.append(f"tmix=frames=3:weights='{weights}'")
    elif not low_cpu and compat.has("tblend"):
        chain_parts.append("tblend=all_mode=average")

    # 2. Color modulation
    if compat.scale_eval:
        chain_parts.append(
            "eq=contrast=1+0.06*sin(2*PI*t/12):"
            "gamma=1+0.05*sin(2*PI*t/17+1):"
            "saturation=1+0.08*sin(2*PI*t/23+2):eval=frame"
        )
    else:
        chain_parts.append("eq=contrast=1.03:gamma=1.02:saturation=1.05")

    # 3. Geometry deformation (3-tier fallback)
    if compat.major >= 5 and compat.has("zoompan"):
        chain_parts.append(
            f"zoompan=z='min(1+0.0025*on,1.12)':"
            f"x='iw/2-(iw/zoom/2)+24*sin(2*PI*on/150)':"
            f"y='ih/2-(ih/zoom/2)+24*cos(2*PI*on/190)':"
            f"d=1:s={w}x{h}:fps={fps}"
        )
    elif compat.scale_eval and compat.crop_eval:
        chain_parts.append(
            f"scale=w='trunc(iw*1.05)':h='trunc(ih*1.05)':eval=frame,"
            f"crop=w={w}:h={h}:x='(iw-ow)/2+10*sin(n/10)':"
            f"y='(ih-oh)/2+10*cos(n/15)':eval=frame"
        )
    else:
        chain_parts.append(
            f"scale={int(w*1.05)}:{int(h*1.05)}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h}"
        )

    # 4. Planar Misalignment (Anti-SIFT/SURF spatial feature distortion)
    if compat.has_chromashift and compat.has("sendcmd"):
        chain_parts.append(f"sendcmd=f='{sendcmd_path}',chromashift")
    elif compat.has_rgbashift and compat.has("sendcmd"):
        chain_parts.append(
            f"format=rgb24,sendcmd=f='{sendcmd_path}',rgbashift,format=yuv420p"
        )

    # 5. High-Frequency Noise Addition
    noise_param = "alls=4" if low_cpu else "alls=6"
    if compat.has("noise"):
        chain_parts.append(f"noise={noise_param}:allf=t+u")

    # 6. Final normalization
    chain_parts.append("format=yuv420p,setsar=1,setpts=PTS-STARTPTS")

    return ",".join(chain_parts)
