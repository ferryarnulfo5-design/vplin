#!/usr/bin/env python3
"""
stage_audio.py — Part 1: audio disruption graph builder.
"""
from __future__ import annotations
import math
import random

def _sweep_segments(rng, duration, strength):
    lines = []
    if not duration or duration <= 0:
        return lines
    W = 25.0
    n = int(min(16, max(4, math.ceil(duration / W))))
    win = duration / n
    f0, f1 = 80.0, 9000.0
    segs = []
    for k in range(n):
        t = k / (n - 1) if n > 1 else 0.5
        f_ = f0 * (f1 / f0) ** t * rng.uniform(0.96, 1.04)
        g_ = max(-rng.uniform(2.0, 7.0) * strength, -14.0)
        w_ = rng.uniform(60, 150)
        a = k * win
        b = min((k + 1) * win, duration)
        fade = min(0.3, win / 4)
        eq = f"equalizer=f={f_:.1f}:width_type=h:width={w_:.1f}:g={g_:.2f}"
        segs.append(
            f"[s{k}]atrim=start={a:.3f}:end={b:.3f},"
            f"asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={fade:.2f},"
            f"afade=t=out:st={max(0.0, b - a - fade):.2f}:d={fade:.2f},"
            f"{eq}[sw{k}]")
    lines.append(f"[mix3]asplit={n}" + "".join(f"[s{k}]" for k in range(n)))
    lines.extend(segs)
    lines.append("".join(f"[sw{k}]" for k in range(n)) +
                 f"concat=n={n}:v=0:a=1[mix4]")
    return lines

def build_audio_graph(sr=44100, seed=None, strength=1.0, compat=None,
                      out_sr=None, duration=None):
    rng = random.Random(seed)
    g = []

    g.append("[0:a]asplit=2[dry][main]")
    g.append("[dry]volume=0.12[dryl]")

    rs = [1 + rng.uniform(-0.02, 0.02) * strength for _ in range(3)]
    g.append("[main]asplit=3[lo][mid][hi]")
    g.append(f"[lo]lowpass=f=4000,asetrate={int(sr * rs[0])},"
             f"aresample={sr},atempo={1 / rs[0]:.4f}[lo2]")
    g.append(f"[mid]bandpass=f=8000:width_type=o:w=2.0,"
             f"asetrate={int(sr * rs[1])},aresample={sr},"
             f"atempo={1 / rs[1]:.4f}[mid2]")
    g.append(f"[hi]highpass=f=12000,asetrate={int(sr * rs[2])},"
             f"aresample={sr},atempo={1 / rs[2]:.4f}[hi2]")
    g.append("[lo2][mid2][hi2]amix=inputs=3:normalize=0[mix1]")
    g.append("[mix1][dryl]amix=inputs=2:normalize=0[mix2]")

    notches = [(97, -4.5), (997, -6.0), (3121, -3.5), (7901, -5.5)]
    chain = ",".join(
        f"equalizer=f={f_}:width_type=h:width=50:g={g_ * strength:.2f}"
        for f_, g_ in notches)
    g.append(f"[mix2]{chain}[mix3]")

    sweep = _sweep_segments(rng, duration, strength)
    if sweep:
        g.extend(sweep)
    else:
        g.append("[mix3]anull[mix4]")

    if compat is None or compat.has_firequalizer:
        fg = -2.0 * strength
        g.append(f"[mix4]firequalizer="
                 f"gain='if(between(f,300,3400),{fg:.2f}*sin(f/1200),0)'"
                 f":delay=0.02[mix5]")
    else:
        g.append("[mix4]anull[mix5]")

    g.append(f"[mix5]aphaser=type=sinusoidal:in_gain=0.8:out_gain=0.74:"
             f"delay=3:decay=0.5:speed={rng.uniform(0.2, 0.4):.3f},"
             f"acontrast={1.28 * strength:.2f},"
             f"crystalizer=i={0.45 * strength:.2f},"
             f"vibrato=f=0.27:d=0.18,"
             f"tremolo=f=0.13:d=0.10[muta]")

    if compat is None or compat.has_anoisesrc:
        amp = 0.004 * strength
        nz = f"anoisesrc=color=pink:amplitude={amp:.5f}:sample_rate={sr}"
        if seed is not None:
            nz += f":seed={seed}"
        g.append(f"{nz}[noise]")
        g.append("[muta][noise]amix=inputs=2:normalize=0[mixn]")
    else:
        g.append("[muta]anull[mixn]")

    final_sr = out_sr or sr
    g.append("[mixn]aecho=0.8:0.72:55|115|195:0.11|0.06|0.03[echo]")
    g.append(f"[echo]dynaudnorm=f=150:g=15:p=0.9,"
             f"alimiter=limit=0.95,"
             f"loudnorm=I=-16:TP=-1.5:LRA=11,"
             f"aformat=sample_rates={final_sr}[aout]")
    return ";\n".join(g)
