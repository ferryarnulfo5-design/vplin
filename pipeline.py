#!/usr/bin/env python3
"""
pipeline.py — Anti-Fingerprinting Media Mutation Pipeline (Parts 1-4)
SIMPLIFIED: container obfuscation disabled (only signature scrub kept).
"""
import argparse
import datetime
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

try:
    from ffmpeg_compat import probe_compat
    from stage_audio import build_audio_filtergraph
    from stage_video import build_video_filtergraph
    from stage_container import scrub_signatures, scan_signatures, fingerprint
except ImportError as e:
    sys.exit(f"missing module: {e}")

PRESETS = {
    "transparent": {"audio": 0.5, "video": 0.5, "interpolate": False},
    "standard":    {"audio": 1.0, "video": 1.0, "interpolate": False},
    "aggressive":  {"audio": 1.5, "video": 1.4, "interpolate": True},
}

THRESHOLDS = {
    "audio_divergence": 0.75,
    "video_crc_divergence": 0.98,
    "dhash_mean": 0.35,
}

_WARP_RE = re.compile(r"setpts='PTS\+([\d.]+)\*sin\(2\*PI\*N/(\d+)\)'")

# --------------------------------------------------------------------------
# Windows-safe subprocess runner
# --------------------------------------------------------------------------
def run(cmd, label="", quiet=False):
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", creationflags=creationflags)
    time_re = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
    last = 0.0
    for line in proc.stdout:
        s = line.strip()
        if not s or s.startswith(("frame=", "size=", "bitrate=",
                                  "speed=", "progress=", "dup=", "drop=")):
            continue
        if "out_time_ms" in s or "N/A" in s:
            continue
        m = time_re.search(s)
        if m:
            h, mi, sec = m.groups()
            t = int(h) * 3600 + int(mi) * 60 + float(sec)
            if t - last >= 5.0 and not quiet:
                print(f"  [{label}] {t:9.1f} s", flush=True)
                last = t
            continue
        if not quiet:
            print(f"  [{label}] {s}", flush=True)
    rc = proc.wait()
    if rc != 0 and not quiet:
        print(f"  [{label}] FAILED rc={rc}", flush=True)
    return rc


# --------------------------------------------------------------------------
# Source probe
# --------------------------------------------------------------------------
def probe(path, ffprobe="ffprobe"):
    p = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format",
         "-of", "json", path],
        capture_output=True, text=True)
    info = json.loads(p.stdout or "{}")
    v = a = None
    for s in info.get("streams", []):
        if s.get("codec_type") == "video" and v is None:
            v = s
        elif s.get("codec_type") == "audio" and a is None:
            a = s
    if v is None:
        raise RuntimeError("no video stream in source")
    fr = str(v.get("r_frame_rate") or "30/1")
    try:
        num, den = map(int, fr.split("/"))
        fps = num / den if den else 30.0
    except ValueError:
        fps = 30.0
    return {
        "width": int(v["width"]), "height": int(v["height"]),
        "fps": round(fps, 6), "vcodec": v.get("codec_name", "?"),
        "has_audio": a is not None,
        "sr": int(a.get("sample_rate", 44100)) if a else 44100,
        "acodec": a.get("codec_name", "?") if a else None,
        "duration": float(info.get("format", {}).get("duration") or 0.0),
    }


# --------------------------------------------------------------------------
# Combined single-pass encode (Parts 1 + 2)
# --------------------------------------------------------------------------
def _extract_warp(video_graph):
    m = _WARP_RE.search(video_graph)
    return (float(m.group(1)), int(m.group(2))) if m else (0.0, 1)

def build_combined_graph(probed, seed, preset, av_sync=True,
                         compat=None, audio_out_sr=None):
    cfg = PRESETS[preset]
    parts = []

    vg, _, _ = build_video_filtergraph(
        compat, low_cpu=(os.cpu_count() < 4),
        duration_s=probed["duration"],
        fps=probed["fps"],
        width=probed["width"],
        height=probed["height"]
    )
    parts.append(vg)

    if probed["has_audio"]:
        ag = build_audio_filtergraph(
            compat, low_cpu=(os.cpu_count() < 4),
            duration_s=probed["duration"],
            sr=probed["sr"]
        )
        # We skip AV sync completely (--no-av-sync always true)
        parts.append(ag)
    return ";\n".join(parts)


def encode_stage(src, inter, probed, seed, preset, ffmpeg, quiet, compat):
    graph = build_combined_graph(probed, seed, preset, av_sync=False,
                                 compat=compat, audio_out_sr=None)
    maps = ["-map", "[vout]"]
    if probed["has_audio"]:
        maps += ["-map", "[aout]"]
    cmd = ([ffmpeg, "-y", "-nostdin", "-hide_banner", "-i", src,
            "-filter_complex", graph] + maps +
           ["-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-pix_fmt", "yuv420p"] + compat.fps_mode_args())
    if probed["has_audio"]:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += [inter]
    return run(cmd, label="encode", quiet=quiet)


# --------------------------------------------------------------------------
# Verification suite (kept)
# --------------------------------------------------------------------------
def audio_divergence(src, mut, fpcalc):
    def raw(p):
        r = subprocess.run([fpcalc, "-raw", p],
                           capture_output=True, text=True)
        for ln in r.stdout.splitlines():
            if ln.startswith("FINGERPRINT="):
                return ln.split("=", 1)[1].strip().split(",")
        return []
    a, b = raw(src), raw(mut)
    n = min(len(a), len(b))
    if n == 0:
        return None
    diff = sum(x != y for x, y in zip(a[:n], b[:n]))
    return {"divergence": round(diff / n, 4),
            "src_frames": len(a), "out_frames": len(b)}

def video_crc_divergence(src, mut, ffmpeg):
    def crcs(p):
        r = subprocess.run(
            [ffmpeg, "-nostdin", "-i", p, "-vf", "framecrc",
             "-f", "framecrc", "-"],
            capture_output=True, text=True)
        return [ln.split(",")[-1].strip()
                for ln in r.stdout.splitlines() if "0x" in ln]
    a, b = crcs(src), crcs(mut)
    n = min(len(a), len(b))
    if n == 0:
        return None
    diff = sum(x != y for x, y in zip(a[:n], b[:n]))
    return {"divergence": round(diff / n, 4), "frames_compared": n}

def dhash_divergence(src, mut, ffmpeg, compat=None, every=30, limit=40):
    try:
        import imagehash
        from PIL import Image
    except Exception:
        return None
    fps_mode = compat.fps_mode_args() if compat else ["-fps_mode", "vfr"]

    def frames(p):
        tmp = tempfile.mkdtemp()
        subprocess.run(
            [ffmpeg, "-nostdin", "-i", p,
             "-vf", f"select='not(mod(n,{every}))'",
             *fps_mode, os.path.join(tmp, "f%05d.png")],
            capture_output=True, text=True)
        hs = []
        for f in sorted(os.listdir(tmp))[:limit]:
            try:
                hs.append(imagehash.dhash(Image.open(os.path.join(tmp, f))))
            except Exception:
                pass
        return hs

    h1, h2 = frames(src), frames(mut)
    n = min(len(h1), len(h2))
    if n == 0:
        return None
    dists = [(h1[i] - h2[i]) / 64.0 for i in range(n)]
    return {"mean": round(sum(dists) / n, 4),
            "beyond_0.35": round(sum(d > 0.35 for d in dists) / n, 4),
            "frames": n}

# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--preset", choices=list(PRESETS), default="standard")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--no-av-sync", action="store_true")
    ap.add_argument("--no-sei", action="store_true")
    ap.add_argument("--no-scrub", action="store_true")
    ap.add_argument("--no-fake-creation", action="store_true")
    ap.add_argument("--odd-sr", action="store_true")
    ap.add_argument("--keep-intermediate", action="store_true")
    ap.add_argument("--print-graph", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--ffprobe", default="ffprobe")
    ap.add_argument("--fpcalc", default=None)
    args = ap.parse_args()

    if not os.path.isfile(args.src):
        sys.exit(f"source not found: {args.src}")
    seed = args.seed if args.seed is not None else random.SystemRandom().randint(0, 2**31)
    fpcalc = args.fpcalc or shutil.which("fpcalc")

    compat = probe_compat(args.ffmpeg)
    print(f"== pipeline run  seed={seed}  preset={args.preset}  "
          f"source={os.path.basename(args.src)}")
    print(compat.summary())

    # 1 ---- probe
    print("[1/3] probing source ...")
    probed = probe(args.src, ffprobe=args.ffprobe)
    print(f"      {probed['width']}x{probed['height']} @ {probed['fps']} fps "
          f"{probed['vcodec']} | audio: {probed['acodec'] or 'none'} "
          f"{probed['sr']} Hz | {probed['duration']:.1f} s")

    # 2 ---- encode
    print("[2/3] encode: audio disruption + video lattice (single pass) ...")
    if args.print_graph:
        print(build_combined_graph(probed, seed, args.preset, False, compat, None))
        return
    workdir = tempfile.mkdtemp(prefix="apfp_")
    inter = os.path.join(workdir, "stage12.mp4")
    rc = encode_stage(args.src, inter, probed, seed, args.preset,
                      args.ffmpeg, args.quiet, compat)
    if rc != 0:
        sys.exit("encode stage failed")

    # 3 ---- scrub signatures (no ftyp/free)
    print("[3/3] scrubbing encoder signatures ...")
    if not args.no_scrub:
        scrubbed = scrub_signatures(inter)
        print(f"      scrubbed {scrubbed} signature occurrences")
    else:
        scrubbed = 0

    # move to final output
    shutil.move(inter, args.out)

    # 4 ---- verify
    print("[4/3] verification ...")
    ver = {"audio": None, "video_crc": None, "dhash": None}
    if probed["has_audio"] and fpcalc:
        ver["audio"] = audio_divergence(args.src, args.out, fpcalc)
        if ver["audio"]:
            print(f"      fpcalc divergence : {ver['audio']['divergence']:.1%} "
                  f"({ver['audio']['src_frames']} -> {ver['audio']['out_frames']} frames)")
        else:
            print("      fpcalc: no fingerprint frames extracted")
    elif not fpcalc:
        print("      fpcalc not found -> audio divergence SKIPPED")

    ver["video_crc"] = video_crc_divergence(args.src, args.out, args.ffmpeg)
    if ver["video_crc"]:
        print(f"      framecrc divergence: {ver['video_crc']['divergence']:.1%} "
              f"({ver['video_crc']['frames_compared']} frames)")

    ver["dhash"] = dhash_divergence(args.src, args.out, args.ffmpeg, compat=compat)
    if ver["dhash"]:
        print(f"      dHash mean distance: {ver['dhash']['mean']:.3f} "
              f"(>{THRESHOLDS['dhash_mean']} = broken, "
              f"{ver['dhash']['beyond_0.35']:.0%} beyond 0.35)")
    else:
        print("      dHash SKIPPED (pip install pillow imagehash)")

    # forensic report (only signatures now)
    sigs = scan_signatures(args.out)
    if sigs:
        print(f"      WARNING: remaining signatures: {sigs}")
    else:
        print("      forensic: no encoder signatures found")

    # thresholds
    fails = []
    if ver["audio"] and ver["audio"]["divergence"] < THRESHOLDS["audio_divergence"]:
        fails.append("audio divergence below threshold")
    if ver["video_crc"] and ver["video_crc"]["divergence"] < THRESHOLDS["video_crc_divergence"]:
        fails.append("framecrc divergence below threshold")
    if ver["dhash"] and ver["dhash"]["mean"] < THRESHOLDS["dhash_mean"]:
        fails.append("dHash distance below threshold")

    # manifest
    manifest = {
        "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": os.path.abspath(args.src),
        "output": os.path.abspath(args.out),
        "seed": seed,
        "preset": args.preset,
        "ffmpeg_compat": {
            "major": compat.major,
            "hue_eval": compat.hue_has_eval,
            "scale_eval": compat.scale_has_eval,
            "eq_eval": compat.eq_has_eval,
            "filter_units": compat.has_filter_units,
        },
        "probe": probed,
        "container": {"scrubbed": scrubbed},
        "verification": ver,
        "thresholds_met": not fails,
        "failures": fails,
    }
    mpath = args.out + ".manifest.json"
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    if not args.keep_intermediate:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n== result: {'PASS' if not fails else 'FAIL'}")
    for msg in fails:
        print(f"   ! {msg}")
    print(f"   manifest: {mpath}")
    sys.exit(0 if not fails else 1)

if __name__ == "__main__":
    main()
