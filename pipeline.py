#!/usr/bin/env python3
"""
pipeline.py -- Orchestrator for the deep media mutation pipeline.

Usage:
  python3 pipeline.py INPUT.mp4 OUT_DIR [--fps 30] [--verify] [--baseline]
                       [--seed N] [--low-cpu] [--max-minutes 100]
                       [--dry-run] [--no-faststart]

Flow:
  1. Probe ffmpeg capabilities (ffmpeg_compat) + input media (ffprobe).
  2. Detect runner: os.cpu_count() < 4 -> low_cpu profile:
       audio: static 3-band aequalizer (no firequalizer time-variant),
              2 split bands instead of 3
       video: no tblend smear, noise strength 4, zoompan amplitude 12
  3. Build audio + video filtergraphs (stage_audio / stage_video), single
     -filter_complex, one x264 veryfast pass.
  4. Encode with -progress pipe:1 and a watchdog; Windows-safe subprocess
     (CREATE_NO_WINDOW, stderr merged -> no deadlock, out_time_ms=N/A safe).
  5. Container surgery: ftyp randomization -> signature scrub -> free-atom
     injection with stco/co64 patching.
  6. Verification: sampled PSNR/SSIM (1 frame per 300), AV-sync check,
     forensic report (signatures / ftyp / metadata / hash). Artifacts staged.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from typing import Dict, List, Optional, Tuple

import ffmpeg_compat as fc
import stage_audio as sa
import stage_container as sc
import stage_video as sv

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
VERIFY_SAMPLE = 300          # evaluate PSNR/SSIM on 1 frame per 300 (10-min file -> ~60 frames)
SSIM_THRESHOLD = 0.85
PSNR_DROP_THRESHOLD_DB = 2.0
ASSUMED_BASELINE_PSNR_DB = 38.0   # x264 veryfast CRF23 clean encode, if --baseline not used
AV_SYNC_TOLERANCE_S = 0.5


# ---------------------------------------------------------------------------
# Windows-safe subprocess runner with progress parsing + watchdog
# ---------------------------------------------------------------------------
def run(cmd: List[str], label: str, max_seconds: float,
        progress: bool = False) -> Tuple[int, str]:
    """Run cmd; merge stderr into stdout (no pipe deadlock); parse progress.

    With `-progress pipe:1`, FFmpeg emits key=value progress lines on stdout.
    out_time_ms may be N/A on early frames; out_time_us is preferred when
    present (FFmpeg >= 4.3). The loop never blocks: lines are read
    incrementally, and a wall-clock watchdog kills the process if it exceeds
    max_seconds (GitHub Actions 120-min safety net).
    """
    flags = CREATE_NO_WINDOW
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,          # merge -> single pipe, no deadlock
        creationflags=flags,
        universal_newlines=True,
        encoding="utf-8", errors="replace", bufsize=1,
    )
    assert proc.stdout is not None
    t0 = time.monotonic()
    log: List[str] = []
    last_pct = -1
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            if progress and line.startswith("out_time"):
                key, _, val = line.partition("=")
                if key == "out_time_us" and val.isdigit():
                    pct = min(100.0, int(val) / 1_000_000.0 /
                              max(1e-9, _DURATION) * 100.0)
                elif key == "out_time_ms" and val.isdigit():
                    pct = min(100.0, int(val) / 1000.0 /
                              max(1e-9, _DURATION) * 100.0)
                else:
                    continue            # out_time_ms=N/A etc. -- never crashes us
                if pct - last_pct >= 5:
                    print(f"  [{label}] {pct:5.1f}%  "
                          f"elapsed {time.monotonic()-t0:6.1f}s", flush=True)
                    last_pct = pct
            elif line.startswith("progress="):
                if line == "progress=end":
                    break
            else:
                if line.strip():
                    log.append(line)
            if time.monotonic() - t0 > max_seconds:
                print(f"  [{label}] TIMEOUT after {max_seconds:.0f}s -- killing",
                      flush=True)
                proc.kill()
                raise TimeoutError(f"{label} exceeded {max_seconds:.0f}s")
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    out = "\n".join(log)
    rc = proc.returncode if proc.returncode is not None else -1
    if rc != 0:
        print(f"  [{label}] failed rc={rc}\n{out[-3000:]}", flush=True)
    return rc, out


_DURATION = 600.0   # set by probe_media; read by run() progress calculator


# ---------------------------------------------------------------------------
# media probing
# ---------------------------------------------------------------------------
def probe_media(path: str) -> Dict:
    global _DURATION
    cmd = [fc.Capabilities().ffprobe if False else _ffprobe,
           "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    rc, out = fc._run(cmd, timeout=60)
    if rc != 0:
        raise RuntimeError(f"ffprobe failed on {path}:\n{out[:800]}")
    data = json.loads(out)
    fmt = data.get("format", {})
    dur = float(fmt.get("duration", 0.0) or 0.0)
    _DURATION = dur if dur > 0 else 600.0
    v = a = None
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and v is None:
            fr = s.get("avg_frame_rate", "0/1")
            try:
                num, den = fr.split("/")
                fps = float(num) / float(den or 1)
            except Exception:
                fps = 0.0
            v = {"codec": s.get("codec_name"), "width": int(s.get("width", 0)),
                 "height": int(s.get("height", 0)), "fps": fps,
                 "duration": float(s.get("duration", 0.0) or 0.0)}
        elif s.get("codec_type") == "audio" and a is None:
            a = {"codec": s.get("codec_name"),
                 "sample_rate": int(s.get("sample_rate", 0) or 0),
                 "channels": int(s.get("channels", 0) or 0)}
    if v is None:
        raise RuntimeError("no video stream in input")
    return {"duration": dur, "video": v, "audio": a, "format": fmt.get("format_name", "?")}


_ffprobe = "ffprobe"   # set in main()


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------
def verify_av_sync(path: str) -> Dict:
    rc, out = fc._run([
        _ffprobe, "-v", "error", "-print_format", "json",
        "-show_streams", path], timeout=60)
    if rc != 0:
        return {"ok": False, "error": out[:300]}
    data = json.loads(out)
    vd = ad = 0.0
    vs = as_ = 0.0
    for s in data.get("streams", []):
        d = float(s.get("duration", 0.0) or 0.0)
        st = float(s.get("start_time", 0.0) or 0.0)
        if s.get("codec_type") == "video":
            vd, vs = d, st
        elif s.get("codec_type") == "audio":
            ad, as_ = d, st
    diff = abs(vd - ad)
    return {
        "ok": diff <= AV_SYNC_TOLERANCE_S and abs(vs) < 0.1 and abs(as_) < 0.1,
        "video_duration_s": round(vd, 3), "audio_duration_s": round(ad, 3),
        "av_diff_s": round(diff, 3),
        "video_start_s": round(vs, 3), "audio_start_s": round(as_, 3),
    }


def _parse_metric(log: str, pat: str) -> Optional[float]:
    vals = re.findall(pat, log)
    if not vals:
        return None
    v = vals[-1]
    return float("inf") if v == "inf" else float(v)


def compute_metrics(src: str, out: str, caps: fc.Capabilities) -> Dict:
    """Sampled PSNR + SSIM: normalize src to output geometry/fps, then
    compare 1 frame per 300. ~60 frames of work for a 10-min file."""
    if not (caps.has("psnr") and caps.has("ssim")):
        return {"ok": False, "error": "psnr/ssim filters unavailable"}
    graph = (
        "[0:v]fps=30,scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,"
        f"select='not(mod(n,{VERIFY_SAMPLE}))',setpts=N/FRAME_RATE/TB[a];"
        "[1:v]fps=30,setsar=1,"
        f"select='not(mod(n,{VERIFY_SAMPLE}))',setpts=N/FRAME_RATE/TB[b];"
        "[a][b]psnr;"
        "[a][b]ssim"
    )
    cmd = [caps.ffmpeg, "-hide_banner", "-nostats", "-v", "info",
           "-i", src, "-i", out,
           "-filter_complex", graph, "-frames:v", "500", "-f", "null", "-"]
    rc, log = fc._run(cmd, timeout=300)
    psnr = _parse_metric(log, r"average:([0-9.]+|inf)\s+min:")
    ssim = _parse_metric(log, r"All:([0-9.]+)")
    return {"ok": rc == 0, "psnr_db": psnr, "ssim": ssim}


def check_playable(path: str, caps: fc.Capabilities) -> Dict:
    rc, _ = fc._run([
        caps.ffmpeg, "-hide_banner", "-v", "error", "-i", path,
        "-t", "3", "-f", "null", "-"], timeout=120)
    return {"decodes_ok": rc == 0}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    global _ffprobe
    ap = argparse.ArgumentParser(description="Deep media mutation pipeline")
    ap.add_argument("input")
    ap.add_argument("outdir")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--verify", action="store_true", help="run PSNR/SSIM + forensic checks")
    ap.add_argument("--baseline", action="store_true",
                    help="encode clean baseline to measure true PSNR drop (adds ~5-8 min)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--low-cpu", action="store_true", help="force low-cpu profile")
    ap.add_argument("--max-minutes", type=float, default=100.0)
    ap.add_argument("--dry-run", action="store_true",
                    help="print filtergraphs and exit without encoding")
    ap.add_argument("--no-faststart", action="store_true")
    args = ap.parse_args()

    t_start = time.monotonic()
    seed = args.seed if args.seed is not None else random.randrange(1 << 31)
    rng = random.Random(seed)
    timing: Dict[str, float] = {}

    print("=" * 72)
    print("DEEP MEDIA MUTATION PIPELINE")
    print(f"input={args.input}  seed={seed}  max={args.max_minutes:.0f}min")

    # ---- probe ---------------------------------------------------------
    t = time.monotonic()
    caps = fc.probe()
    _ffprobe = caps.ffprobe
    print(caps.summary())
    media = probe_media(args.input)
    dur = media["duration"]
    print(f"media: {media['video']['codec']} "
          f"{media['video']['width']}x{media['video']['height']} "
          f"@{media['video']['fps']:.2f}fps, {dur:.1f}s, "
          f"audio={media['audio']}")
    timing["probe"] = time.monotonic() - t

    low_cpu = args.low_cpu or (os.cpu_count() or 2) < 4
    print(f"runner profile: {'LOW-CPU (static EQ, 2 bands)' if low_cpu else 'FULL'} "
          f"(os.cpu_count()={os.cpu_count()})")

    fps = args.fps or (30 if media["video"]["fps"] <= 0 else int(media["video"]["fps"]))

    # ---- graphs --------------------------------------------------------
    t = time.monotonic()
    vgraph, cmd_path, vmeta = sv.build_video_filtergraph(
        caps, low_cpu, dur, fps=fps, width=args.width, height=args.height)
    agraph = None
    if media["audio"]:
        agraph = sa.build_audio_filtergraph(caps, low_cpu, dur, in_label="0:a")
    timing["graph_build"] = time.monotonic() - t

    if args.dry_run:
        print("\n-- VIDEO GRAPH --\n", vgraph)
        if agraph:
            print("\n-- AUDIO GRAPH --\n", agraph)
        if cmd_path:
            os.unlink(cmd_path)
        return 0

    os.makedirs(args.outdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(args.input))[0]
    out_enc = os.path.join(args.outdir, f"{base}_encoded.mp4")
    out_final = os.path.join(args.outdir,
                             f"{base}_mutated_{args.width}x{args.height}_"
                             f"{fps}fps_{seed}.mp4")

    # ---- encode --------------------------------------------------------
    t = time.monotonic()
    try:
        fc_graph = f"{vgraph}"
        maps = ["-map", "[vout]"]
        if agraph:
            fc_graph += f";{agraph}"
            maps += ["-map", "[aout]"]
        cmd = [caps.ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
               "-nostats", "-i", args.input,
               "-filter_complex", fc_graph] + maps + [
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
               "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
               "-map_metadata", "-1", "-dn", "-sn",
               "-fflags", "+bitexact", "-flags", "+bitexact",
               "-avoid_negative_ts", "make_zero",
               ]
        if not args.no_faststart:
            cmd += ["-movflags", "+faststart"]
        cmd += ["-progress", "pipe:1", out_enc]
        rc, log = run(cmd, "encode", args.max_minutes * 60, progress=True)
        timing["encode"] = time.monotonic() - t
        if rc != 0:
            print("ENCODE FAILED")
            return 1
        print(f"  encode done in {timing['encode']:.1f}s "
              f"({os.path.getsize(out_enc)/1e6:.0f} MB)")
    finally:
        if cmd_path:
            try:
                os.unlink(cmd_path)
            except OSError:
                pass

    # ---- container surgery ----------------------------------------------
    t = time.monotonic()
    sig_before = sc.scan_signatures(out_enc)
    ftyp_report = sc.randomize_ftyp(out_enc, seed=seed)
    scrubbed = sc.scrub_signatures(out_enc)
    free_report = sc.inject_free_atom(out_enc, seed=seed)
    sig_after = sc.verify_no_signatures(out_enc)
    os.replace(out_enc, out_final)
    md5, sha256, fsize = sc.fingerprint(out_final)
    timing["container"] = time.monotonic() - t
    print(f"  container: ftyp->{ftyp_report['major_brand_after'].strip()!r}, "
          f"scrubbed {scrubbed} sig bytes, free atom {free_report['free_atom_size_bytes']}B, "
          f"stco+{free_report['stco_patched']} co64+{free_report['co64_patched']}")

    # ---- verification ----------------------------------------------------
    report: Dict = {
        "input": {"path": args.input, "duration_s": dur,
                  "video": media["video"], "audio": media["audio"]},
        "output": {"path": out_final, "md5": md5, "sha256": sha256,
                   "bytes": fsize},
        "config": {"fps": fps, "width": args.width, "height": args.height,
                   "seed": seed, "low_cpu": low_cpu, "caps": caps.summary()},
        "video_meta": vmeta, "timing": timing,
        "container": {**ftyp_report, **free_report,
                      "signatures_before": sig_before,
                      "signatures_scrubbed": scrubbed,
                      "signatures_after": sig_after},
    }

    verdict = {"pass": True, "checks": []}

    if args.verify:
        # playability + AV sync
        play = check_playable(out_final, caps)
        av = verify_av_sync(out_final)
        report["verification"] = {"playable": play, "av_sync": av}
        verdict["checks"].append(("playable", play.get("decodes_ok", False)))
        verdict["checks"].append(("av_sync", av.get("ok", False)))

        # PSNR/SSIM vs source
        m = compute_metrics(args.input, out_final, caps)
        report["verification"]["metrics_vs_source"] = m

        # optional clean baseline -> true PSNR drop
        if args.baseline:
            t = time.monotonic()
            base_path = os.path.join(args.outdir, f"{base}_baseline.mp4")
            rc, _ = run([
                caps.ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
                "-nostats", "-i", args.input,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart",
                "-progress", "pipe:1", base_path], "baseline",
                args.max_minutes * 60, progress=True)
            timing["baseline"] = time.monotonic() - t
            if rc == 0:
                b = compute_metrics(args.input, base_path, caps)
                report["verification"]["metrics_baseline"] = b
                if m.get("psnr_db") is not None and b.get("psnr_db") is not None:
                    drop = b["psnr_db"] - m["psnr_db"]
                    report["verification"]["psnr_drop_db"] = round(drop, 2)
        else:
            if m.get("psnr_db") is not None:
                report["verification"]["psnr_drop_db_est"] = round(
                    ASSUMED_BASELINE_PSNR_DB - m["psnr_db"], 2)

        # verdict math
        ssim = m.get("ssim")
        psnr = m.get("psnr_db")
        drop = report["verification"].get("psnr_drop_db")
        if drop is None:
            drop = report["verification"].get("psnr_drop_db_est")
        ssim_ok = ssim is not None and ssim < SSIM_THRESHOLD
        psnr_ok = drop is not None and drop > PSNR_DROP_THRESHOLD_DB
        verdict["checks"].append(("ssim<0.85", bool(ssim_ok), ssim))
        verdict["checks"].append(("psnr_drop>2dB", bool(psnr_ok), drop))
        verdict["pass"] = all(c[1] for c in verdict["checks"] if len(c) > 2) \
            and all(c[1] for c in verdict["checks"][:4])
        report["verification"]["verdict"] = verdict

    report["total_seconds"] = round(time.monotonic() - t_start, 1)
    report["verdict"] = verdict

    with open(os.path.join(args.outdir, "report.json"), "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    with open(os.path.join(args.outdir, "report.txt"), "w") as fh:
        fh.write(json.dumps(report, indent=2, default=str))

    print("=" * 72)
    print(f"TOTAL {report['total_seconds']}s -> {out_final}")
    print(f"PSNR={psnr} dB  SSIM={ssim}  drop={drop} dB")
    print(f"VERDICT: {'PASS' if verdict['pass'] else 'FAIL'}")
    print(f"artifacts: {args.outdir}/report.json, report.txt")
    return 0 if verdict["pass"] else 2


if __name__ == "__main__":
    sys.exit(main())
