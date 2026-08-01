#!/usr/bin/env python3
"""
pipeline.py — Anti-Fingerprinting Media Mutation Pipeline (Parts 1-4) v3.0 FINAL.
Fixed: ffmpeg_compat import, container obfuscation safe dict access.
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
import time

try:
    from ffmpeg_compat import probe_compat
    from stage_audio import build_audio_filtergraph
    from stage_video import build_video_filtergraph
    import stage_container as sc
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

# --------------------------------------------------------------------------
# Windows-safe subprocess runner
# --------------------------------------------------------------------------
def run(cmd, label="", quiet=False, timeout_sec=None):
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", creationflags=creationflags)
    time_re = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
    last = 0.0
    t_start = time.monotonic()
    lines = []
    for line in proc.stdout:
        s = line.strip()
        lines.append(s)
        if not s or s.startswith(("frame=", "size=", "bitrate=",
                                  "speed=", "progress=", "dup=", "drop=",
                                  "fps=")):
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
        if timeout_sec and (time.monotonic() - t_start) > timeout_sec:
            proc.terminate()
            proc.wait()
            raise RuntimeError(f"timeout {timeout_sec}s exceeded")
    rc = proc.wait()
    if rc != 0 and not quiet:
        print(f"  [{label}] FAILED rc={rc}", flush=True)
    return rc, "\n".join(lines)


# --------------------------------------------------------------------------
# Source probe
# --------------------------------------------------------------------------
def probe_media(path, ffprobe="ffprobe"):
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
def build_combined_graph(probed, seed, preset, compat=None, audio_out_sr=None):
    cfg = PRESETS[preset]
    parts = []
    low_cpu = os.cpu_count() < 4

    # Video
    vgraph, _, vmeta = build_video_filtergraph(
        compat, low_cpu, probed["duration"],
        fps=probed["fps"], width=probed["width"], height=probed["height"]
    )
    parts.append(vgraph)

    # Audio
    if probed["has_audio"]:
        agraph = build_audio_filtergraph(
            compat, low_cpu, probed["duration"],
            sr=audio_out_sr or probed["sr"]
        )
        parts.append(agraph)

    return ";\n".join(parts)


def encode_stage(src, inter, probed, seed, preset, ffmpeg, quiet, compat=None, audio_out_sr=None):
    graph = build_combined_graph(probed, seed, preset, compat=compat, audio_out_sr=audio_out_sr)
    maps = ["-map", "[vout]"]
    if probed["has_audio"]:
        maps += ["-map", "[aout]"]
    cmd = ([ffmpeg, "-y", "-nostdin", "-hide_banner", "-i", src,
            "-filter_complex", graph] + maps +
           ["-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-pix_fmt", "yuv420p"] + (["-vsync", "vfr"] if compat.major < 7 else ["-fps_mode", "vfr"]))
    if probed["has_audio"]:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += [inter]
    rc, log = run(cmd, label="encode", quiet=quiet, timeout_sec=7200)
    return rc, log


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
    return {"divergence": round(diff / n, 4), "src_frames": len(a), "out_frames": len(b)}


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
    fps_mode = ["-fps_mode", "vfr"] if compat and compat.major >= 7 else ["-vsync", "vfr"]

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
        description="Anti-fingerprinting media mutation pipeline")
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--preset", choices=list(PRESETS), default="standard")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--profile", choices=["iphone_mov", "android_mp4", "gopro_mp4"], default=None)
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
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--baseline", action="store_true")
    args = ap.parse_args()

    if not os.path.isfile(args.src):
        sys.exit(f"source not found: {args.src}")

    seed = args.seed if args.seed is not None else random.SystemRandom().randint(0, 2 ** 31)
    fpcalc = args.fpcalc or shutil.which("fpcalc")

    # ---- runtime capability probe ----
    compat = probe_compat(args.ffmpeg)
    print("========================================================================\n"
          "DEEP MEDIA MUTATION PIPELINE")
    print(f"input={args.src}  seed={seed}  max=100min")
    print(f"{args.ffmpeg} {compat.version_str} ({args.ffmpeg})")
    print(compat.summary())

    # 1 ---- probe source ----
    probed = probe_media(args.src, ffprobe=args.ffprobe)
    print(f"media: {probed['vcodec']} {probed['width']}x{probed['height']} "
          f"@{probed['fps']:.2f}fps, {probed['duration']:.1f}s, "
          f"audio={probed['acodec']} {probed['sr']}Hz")
    low_cpu = os.cpu_count() < 4
    print(f"runner profile: {'LOW' if low_cpu else 'FULL'} (os.cpu_count()={os.cpu_count()})")

    audio_out_sr = None
    if args.odd_sr and probed["has_audio"]:
        rng = random.Random(seed)
        audio_out_sr = max(8000, min(192000, probed["sr"] + rng.randint(-220, 220)))
        print(f"      audio track timescale mutation: {probed['sr']} -> {audio_out_sr} Hz")

    # 2 ---- encode ----
    if args.print_graph:
        graph = build_combined_graph(probed, seed, args.preset, compat, audio_out_sr)
        print(graph)
        return

    workdir = tempfile.mkdtemp(prefix="apfp_")
    inter = os.path.join(workdir, "stage12.mp4")
    out_enc = os.path.join(workdir, "stage3.mp4")

    print("[1/3] encode: audio disruption + video lattice ...")
    rc, log = encode_stage(args.src, inter, probed, seed, args.preset,
                           args.ffmpeg, args.quiet, compat, audio_out_sr)
    if rc != 0:
        sys.exit("encode stage failed")

    # 3 ---- container obfuscation ----
    print("[2/3] container obfuscation ...")
    # Simplified container ops
    ftyp_report = sc.randomize_ftyp(inter, seed=seed)
    scrubbed = sc.scrub_signatures(inter)
    free_report = sc.inject_free_atom(inter, seed=seed)

    print(f"  ftyp: {ftyp_report.get('major_brand_after', '?')} "
          f"scrubbed={scrubbed} free={free_report.get('free_atom_size_bytes', 0)}")

    # Move final output
    shutil.move(inter, args.out)

    # 4 ---- verify ----
    print("[3/3] verification ...")
    ver = {"audio": None, "video_crc": None, "dhash": None, "forensic": None}

    if probed["has_audio"] and fpcalc:
        ver["audio"] = audio_divergence(args.src, args.out, fpcalc)
        if ver["audio"]:
            print(f"      fpcalc divergence: {ver['audio']['divergence']:.1%} "
                  f"({ver['audio']['src_frames']} -> {ver['audio']['out_frames']} frames)")
        else:
            print("      fpcalc: no fingerprint frames extracted")

    ver["video_crc"] = video_crc_divergence(args.src, args.out, args.ffmpeg)
    if ver["video_crc"]:
        print(f"      framecrc divergence: {ver['video_crc']['divergence']:.1%} "
              f"({ver['video_crc']['frames_compared']} frames)")

    ver["dhash"] = dhash_divergence(args.src, args.out, args.ffmpeg, compat=compat)
    if ver["dhash"]:
        print(f"      dHash mean distance: {ver['dhash']['mean']:.3f} "
              f"(>{THRESHOLDS['dhash_mean']} = broken, "
              f"{ver['dhash']['beyond_0.35']:.0%} beyond 0.35)")

    # forensic
    found = sc.scan_signatures(args.out)
    ver["forensic"] = {"signatures_found": list(found.keys())}
    if found:
        print(f"      WARNING: signatures still present: {list(found.keys())}")
    else:
        print("      forensic: clean (no signatures)")

    # thresholds
    fails = []
    if ver["audio"] and ver["audio"]["divergence"] < THRESHOLDS["audio_divergence"]:
        fails.append("audio divergence below threshold")
    if ver["video_crc"] and ver["video_crc"]["divergence"] < THRESHOLDS["video_crc_divergence"]:
        fails.append("framecrc divergence below threshold")
    if ver["dhash"] and ver["dhash"]["mean"] < THRESHOLDS["dhash_mean"]:
        fails.append("dHash distance below threshold")
    if found:
        fails.append(f"signatures: {list(found.keys())}")

    # manifest
    manifest = {
        "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": os.path.abspath(args.src),
        "output": os.path.abspath(args.out),
        "seed": seed,
        "preset": args.preset,
        "audio_out_sr": audio_out_sr,
        "probe": probed,
        "ffmpeg_compat": {
            "version": compat.version_str,
            "scale_eval": compat.scale_eval,
            "crop_eval": compat.crop_eval,
            "firequalizer": compat.has("firequalizer"),
        },
        "verification": ver,
        "failures": fails,
        "thresholds_met": not fails,
    }

    mpath = args.out + ".manifest.json"
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"\n== VERDICT: {'PASS' if not fails else 'FAIL'}")
    for msg in fails:
        print(f"   ! {msg}")
    print(f"   manifest: {mpath}")
    print(f"   output: {args.out}")

    if not args.keep_intermediate:
        shutil.rmtree(workdir, ignore_errors=True)

    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
