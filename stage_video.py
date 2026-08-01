#!/usr/bin/env python3
"""
stage_video.py — Part 2: video lattice deformation graph builder.

v2 changes (git-master 2026 removed eval from scale AND eq, not just hue):
  - scale: dynamic w/h expressions ONLY when the build exposes eval=frame;
    otherwise a seeded STATIC zoom (deterministic either way).
  - eq:    dynamic gamma/contrast/saturation ONLY with eval=frame;
    otherwise seeded static values.
  - crop:  dynamic x/y always (crop's default eval mode is per-frame);
    the eval option is appended only when present.
  - hue:   dynamic always — per-frame evaluation is the built-in default.
    NEVER pass eval to hue (removed in 7.x).
"""
from __future__ import annotations
import random


def build_video_graph(width, height, fps=30.0, seed=None, strength=1.0,
                      interpolate=False, ghost=True, grain=True, compat=None):
    rng = random.Random(seed)
    W = int(width) - int(width) % 2
    H = int(height) - int(height) % 2

    # ---- scale: dynamic only if eval=frame exists --------------------------
    zoom = rng.uniform(-0.010, 0.010) * strength
    if compat is None or compat.scale_has_eval:
        pz = rng.randint(60, 140)
        scale = (f"scale=w='iw*(1+{zoom:.6f}*sin(2*PI*n/{pz}))':"
                 f"h=-2:eval=frame")
    else:
        w = int(W * (1 + zoom) / 2) * 2
        scale = f"scale={w}:-2"

    # ---- crop: per-frame expressions (default eval mode) -------------------
    cw, ch = rng.uniform(0.94, 0.99), rng.uniform(0.94, 0.99)
    px, py = rng.randint(50, 120), rng.randint(50, 120)
    panx = rng.uniform(4, 14) * strength
    pany = rng.uniform(3, 10) * strength
    crop = (f"crop=w='iw*{cw:.3f}':h='ih*{ch:.3f}':"
            f"x='(iw-ow)/2+{panx:.3f}*sin(2*PI*n/{px})':"
            f"y='(ih-oh)/2+{pany:.3f}*cos(2*PI*n/{py})'")
    if compat is not None and compat.crop_has_eval:
        crop += ":eval=frame"

    # ---- eq: dynamic only with eval=frame ----------------------------------
    c0 = 1.0 + rng.uniform(-0.04, 0.04) * strength
    g0 = 0.97 + rng.uniform(-0.03, 0.03) * strength
    s0 = 1.0 + rng.uniform(-0.05, 0.05) * strength
    if compat is None or compat.eq_has_eval:
        c1 = rng.uniform(0.02, 0.05) * strength
        g1 = rng.uniform(0.015, 0.04) * strength
        s1 = rng.uniform(0.03, 0.08) * strength
        pc, pg, ps = rng.randint(90, 200), rng.randint(90, 200), rng.randint(90, 200)
        eq = (f"eq=contrast='{c0:.4f}+{c1:.4f}*sin(2*PI*n/{pc})':"
              f"gamma='{g0:.4f}+{g1:.4f}*sin(2*PI*n/{pg})':"
              f"saturation='{s0:.4f}+{s1:.4f}*sin(2*PI*n/{ps})':eval=frame")
    else:
        eq = f"eq=contrast={c0:.4f}:gamma={g0:.4f}:saturation={s0:.4f}"

    # ---- hue: per-frame default; NEVER pass eval ---------------------------
    h0, h1 = rng.uniform(-1.5, 1.5), rng.uniform(2.0, 7.0) * strength
    hs0, hs1 = 1.0, rng.uniform(0.04, 0.12) * strength
    ph, phs = rng.randint(60, 180), rng.randint(60, 180)
    hue = (f"hue=h='{h0:.4f}+{h1:.4f}*sin(2*PI*n/{ph})':"
           f"s='{hs0:.4f}+{hs1:.4f}*sin(2*PI*n/{phs})'")

    # ---- timewarp: keep EXACT format for pipeline _WARP_RE mirror ----------
    warp = rng.uniform(0.001, 0.006) * strength
    wpn = rng.randint(150, 300)
    setpts = f"setpts='PTS*(1+{warp:.6f}*sin(2*PI*N/{wpn}))'"

    # ---- assemble -----------------------------------------------------------
    g = []
    g.append(f"[0:v]fps={fps:.6f}[vcfr]")
    cur = "vcfr"
    if interpolate:
        g.append(f"[{cur}]minterpolate=fps={2 * fps:.6f}:mi_mode=mci:"
                 f"mc_mode=aobmc:me_mode=bidir:me=epzs:vsbmc=1,"
                 f"fps={fps:.6f}[vint]")
        cur = "vint"
    g.append(f"[{cur}]{setpts}[vpts]")
    if ghost:
        g.append("[vpts]tmix=frames=3:weights='1 2 1'[vghost]")
        cur = "vghost"
    else:
        cur = "vpts"
    g.append(f"[{cur}]{scale}[vscl]")
    g.append(f"[vscl]{crop}[vcrop]")
    g.append(f"[vcrop]{eq}[veq]")
    g.append(f"[veq]{hue}[vhue]")
    if grain:
        g.append("[vhue]noise=alls=2:allf=t+u[vnoise]")
        cur = "vnoise"
    else:
        cur = "vhue"
    g.append(f"[{cur}]setsar=1,format=yuv420p[vout]")
    return ";\n".join(g)