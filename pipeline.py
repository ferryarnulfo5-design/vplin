#!/usr/bin/env python3
"""
pipeline.py — Anti-Fingerprinting Media Mutation Pipeline (Parts 1-4) v2.1.

Refactored for FFmpeg 5.x .. git-master-2026 (your build: master 2026):
  - all version-sensitive filter/muxer options come from the runtime
    capability probe (ffmpeg_compat.py), never hardcoded
  - hue: no eval (removed in 7.x; per-frame is the built-in default)
  - scale/eq: eval=frame only when the probe confirms the build has it
  - audio: firequalizer used gain-only (f inside the gain expression);
    the time-swept EQ is a segmented atrim/asetpts/afade/equalizer/concat
    chain (equalizer has no guaranteed timeline/enable support)
  - filter_units remove_types uses '|' (',' is the -bsf list separator)
  - muxer flags emitted only when the build advertises them
  - --odd-sr: resamples audio to a nonstandard rate -> mutates the audio
    TRACK timescale (movenc pins audio timescale to the sample rate)

Usage:
    python pipeline.py input.mp4 output.mp4 [--seed 42]
    python pipeline.py input.mp4 output.mp4 --preset aggressive --odd-sr
    python pipeline.py input.mp4 output.mp4 --print-graph   # debug only
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
    from stage_audio import build_audio_graph
    from stage_video import build_video_graph
    from stage_container import run_container_stage, forensic_report
except ImportError as e:
    sys.exit(f"missing module: {e}\nffmpeg_compat.py, stage_audio.py, "
             "stage_video.py and stage_container.py must sit next to pipeline.py")

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

_WARP_RE = re.compile(r"setpts='PTS\*\(1\+([\d.]+)\*sin\(2\*PI\*N/(\d+)\)\)'")


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

    vg = build_video_graph(probed["width"], probed["height"],
                           fps=probed["fps"], seed=seed,
                           strength=cfg["video"],
                           interpolate=cfg["interpolate"], compat=compat)
    parts.append(vg)

    if probed["has_audio"]:
        ag = build_audio_graph(sr=probed["sr"], seed=seed,
                               strength=cfg["audio"], compat=compat,
                               out_sr=audio_out_sr,
                               duration=probed["duration"])
        if av_sync:
            warp, wpn = _extract_warp(vg)
            if warp > 0:
                ag += (f";\n[aout]asetpts="
                       f"'PTS*(1+{warp}*sin(2*PI*T*{probed['fps']:.6f}/{wpn}))'"
                       f"[aout2]")
        parts.append(ag)
    return ";\n".join(parts)


def encode_stage(src, inter, probed, seed, preset, av_sync, ffmpeg, quiet,
                 compat=None, audio_out_sr=None):
    graph = build_combined_graph(probed, seed, preset, av_sync,
                                 compat=compat, audio_out_sr=audio_out_sr)
    maps = ["-map", "[vout]"]
    if probed["has_audio"]:
        maps += ["-map", "[aout2]" if av_sync else "[aout]"]
    cmd = ([ffmpeg, "-y", "-nostdin", "-hide_banner", "-i", src,
            "-filter_complex", graph] + maps +
           ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p"] + compat.fps_mode_args())
    if probed["has_audio"]:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += [inter]
    return run(cmd, label="encode", quiet=quiet)


# --------------------------------------------------------------------------
# Verification suite
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
    ap = argparse.ArgumentParser(
        description="Anti-fingerprinting media mutation pipeline "
                    "(audio disruption + video lattice + container obfuscation)")
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--preset", choices=list(PRESETS), default="standard")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--profile", choices=["iphone_mov", "android_mp4", "gopro_mp4"],
                    default=None)
    ap.add_argument("--no-av-sync", action="store_true")
    ap.add_argument("--no-sei", action="store_true",
                    help="keep SEI NALs (needed for HDR sources)")
    ap.add_argument("--no-scrub", action="store_true")
    ap.add_argument("--no-fake-creation", action="store_true")
    ap.add_argument("--odd-sr", action="store_true",
                    help="resample audio to a pseudo-random nonstandard rate "
                         "to mutate the audio TRACK timescale (mdhd)")
    ap.add_argument("--keep-intermediate", action="store_true")
    ap.add_argument("--print-graph", action="store_true",
                    help="print the combined filtergraph and exit (debug)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--ffprobe", default="ffprobe")
    ap.add_argument("--fpcalc", default=None)
    args = ap.parse_args()

    if not os.path.isfile(args.src):
        sys.exit(f"source not found: {args.src}")
    seed = args.seed if args.seed is not None else \
        random.SystemRandom().randint(0, 2 ** 31)
    fpcalc = args.fpcalc or shutil.which("fpcalc")

    # ---- runtime capability probe (the version-proofing core) --------------
    compat = probe_compat(args.ffmpeg)
    print(f"== pipeline run  seed={seed}  preset={args.preset}  "
          f"source={os.path.basename(args.src)}")
    print(f"   ffmpeg {compat.major} | hue_eval={'yes' if compat.hue_has_eval else 'no(removed)'} "
          f"| scale_eval={'yes' if compat.scale_has_eval else 'no(static)'} "
          f"| eq_eval={'yes' if compat.eq_has_eval else 'no(static)'} "
          f"| filter_units={'yes' if compat.has_filter_units else 'no'}")

    # 1 ---- probe ----------------------------------------------------------
    print("[1/4] probing source ...")
    probed = probe(args.src, ffprobe=args.ffprobe)
    print(f"      {probed['width']}x{probed['height']} @ {probed['fps']} fps "
          f"{probed['vcodec']} | audio: {probed['acodec'] or 'none'} "
          f"{probed['sr']} Hz | {probed['duration']:.1f} s")

    # optional audio-track-timescale mutation via nonstandard sample rate
    audio_out_sr = None
    if args.odd_sr and probed["has_audio"]:
        rng = random.Random(seed)
        audio_out_sr = max(8000, min(192000,
                                     probed["sr"] + rng.randint(-220, 220)))
        print(f"      audio track timescale mutation: "
              f"{probed['sr']} -> {audio_out_sr} Hz")

    # 2 ---- encode (Parts 1+2, single pass) --------------------------------
    print("[2/4] encode: audio disruption + video lattice (single pass) ...")
    if args.print_graph:
        print(build_combined_graph(probed, seed, args.preset,
                                   not args.no_av_sync, compat, audio_out_sr))
        return
    workdir = tempfile.mkdtemp(prefix="apfp_")
    inter = os.path.join(workdir, "stage12.mp4")
    rc = encode_stage(args.src, inter, probed, seed, args.preset,
                      not args.no_av_sync, args.ffmpeg, args.quiet,
                      compat=compat, audio_out_sr=audio_out_sr)
    if rc != 0:
        sys.exit("encode stage failed")

    # 3 ---- remux (Part 3 container obfuscation) ---------------------------
    print("[3/4] remux: container obfuscation (strip/forge/scrub) ...")
    cmeta = run_container_stage(
        inter, args.out, seed=seed, profile=args.profile, codec="h264",
        strip_sei=not args.no_sei, fake_creation=not args.no_fake_creation,
        scrub=not args.no_scrub, ffmpeg=args.ffmpeg, compat=compat)
    print(f"      profile={cmeta['profile']} timescale={cmeta['timescale']} "
          f"frag_ms={cmeta['frag_ms']} signatures_scrubbed={cmeta['scrubbed']}")

    # 4 ---- verify ----------------------------------------------------------
    print("[4/4] verification ...")
    ver = {"audio": None, "video_crc": None, "dhash": None, "forensic": None}
    if probed["has_audio"] and fpcalc:
        ver["audio"] = audio_divergence(args.src, args.out, fpcalc)
        if ver["audio"]:
            print(f"      fpcalc divergence : {ver['audio']['divergence']:.1%} "
                  f"({ver['audio']['src_frames']} -> "
                  f"{ver['audio']['out_frames']} frames)")
        else:
            print("      fpcalc: no fingerprint frames extracted")
    elif not fpcalc:
        print("      fpcalc not found -> audio divergence SKIPPED "
              "(install chromaprint CLI)")

    ver["video_crc"] = video_crc_divergence(args.src, args.out, args.ffmpeg)
    if ver["video_crc"]:
        print(f"      framecrc divergence: {ver['video_crc']['divergence']:.1%} "
              f"({ver['video_crc']['frames_compared']} frames)")

    ver["dhash"] = dhash_divergence(args.src, args.out, args.ffmpeg,
                                    compat=compat)
    if ver["dhash"]:
        print(f"      dHash mean distance: {ver['dhash']['mean']:.3f} "
              f"(>{THRESHOLDS['dhash_mean']} = broken, "
              f"{ver['dhash']['beyond_0.35']:.0%} beyond 0.35)")
    else:
        print("      dHash SKIPPED (pip install pillow imagehash)")

    ver["forensic"] = forensic_report(args.out, ffprobe=args.ffprobe)

    # thresholds -------------------------------------------------------------
    fails = []
    if ver["audio"] and ver["audio"]["divergence"] < THRESHOLDS["audio_divergence"]:
        fails.append("audio divergence below threshold")
    if ver["video_crc"] and ver["video_crc"]["divergence"] < THRESHOLDS["video_crc_divergence"]:
        fails.append("framecrc divergence below threshold")
    if ver["dhash"] and ver["dhash"]["mean"] < THRESHOLDS["dhash_mean"]:
        fails.append("dHash distance below threshold")
    if ver["forensic"]["signatures_found"]:
        fails.append("encoder signatures still present: "
                     f"{ver['forensic']['signatures_found']}")

    # manifest ----------------------------------------------------------------
    manifest = {
        "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": os.path.abspath(args.src),
        "output": os.path.abspath(args.out),
        "seed": seed,
        "preset": args.preset,
        "audio_out_sr": audio_out_sr,
        "ffmpeg_compat": {
            "major": compat.major,
            "hue_eval": compat.hue_has_eval,
            "scale_eval": compat.scale_has_eval,
            "eq_eval": compat.eq_has_eval,
            "filter_units": compat.has_filter_units,
            "mov_brand": compat.mov_opt("brand"),
            "mov_video_track_timescale": compat.mov_opt("video_track_timescale"),
            "mov_min_frag_duration": compat.mov_opt("min_frag_duration"),
        },
        "probe": probed,
        "container": cmeta,
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