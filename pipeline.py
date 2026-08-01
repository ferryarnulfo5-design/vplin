#!/usr/bin/env python3
"""
v4.0.3 Production-Grade Media Mutation Pipeline (Playability & Stream Fix)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Tuple

import ffmpeg_compat
import stage_audio
import stage_container
import stage_video

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def run_process_with_watchdog(
    cmd: List[str], timeout_sec: int = 7200
) -> Tuple[int, str]:
    print(f"[Exec] {' '.join(cmd)}")
    start_time = time.time()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    output_log = []
    while True:
        line = proc.stdout.readline()
        if not line and proc.poll() is not None:
            break
        if line:
            output_log.append(line)
            if "time=" in line:
                print(f"  -> {line.strip()}", end="\r")
        if time.time() - start_time > timeout_sec:
            proc.kill()
            raise TimeoutError(f"Process exceeded {timeout_sec}s watchdog limit.")

    print()
    return proc.returncode, "".join(output_log)


def check_audio_presence(filepath: str) -> bool:
    try:
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=codec_type", "-of",
            "default=noprint_wrappers=1:nokey=1", filepath
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return "audio" in result.stdout.lower()
    except Exception:
        return False


def verify_divergence(src: str, dst: str, compat: ffmpeg_compat.Capabilities) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {
        "audio_divergence_pct": 0.0,
        "video_divergence_pct": 0.0,
        "dhash_mean_distance": 0.0,
        "psnr_drop_db": 0.0,
        "ssim_val": 1.0,
        "verdict": "PASS",
    }
    has_audio = check_audio_presence(src)

    if has_audio:
        try:
            p1 = subprocess.run(["fpcalc", "-raw", src], capture_output=True, text=True)
            p2 = subprocess.run(["fpcalc", "-raw", dst], capture_output=True, text=True)
            if p1.returncode == 0 and p2.returncode == 0:
                fp1 = p1.stdout.split("FINGERPRINT=")[1].strip()
                fp2 = p2.stdout.split("FINGERPRINT=")[1].strip()
                if fp1 and fp2:
                    matches = sum(1 for a, b in zip(fp1, fp2) if a == b)
                    metrics["audio_divergence_pct"] = round(
                        (1.0 - (matches / max(len(fp1), len(fp2)))) * 100.0, 2
                    )
        except Exception as e:
            print(f"[Warn] Chromaprint verification failed: {e}")

    if compat.has("psnr") and compat.has("ssim"):
        try:
            log_psnr = "psnr.log"
            log_ssim = "ssim.log"
            vf = (
                f"[0:v]select='not(mod(n,300))',setpts=N/FRAME_RATE/TB[main];"
                f"[1:v]select='not(mod(n,300))',setpts=N/FRAME_RATE/TB[ref];"
                f"[main][ref]psnr=f={log_psnr},ssim=f={log_ssim}"
            )
            subprocess.run(
                ["ffmpeg", "-y", "-i", dst, "-i", src, "-filter_complex", vf, "-f", "null", "-"],
                capture_output=True,
            )
            if os.path.exists(log_psnr):
                with open(log_psnr, "r") as f:
                    lines = [line.strip() for line in f.readlines() if line.strip()]
                    if lines and "average:" in lines[-1]:
                        psnr_val = float(lines[-1].split("average:")[1].split()[0])
                        metrics["psnr_drop_db"] = round(max(0.0, 45.0 - psnr_val), 2)
            if os.path.exists(log_ssim):
                with open(log_ssim, "r") as f:
                    lines = [line.strip() for line in f.readlines() if line.strip()]
                    if lines and "All:" in lines[-1]:
                        metrics["ssim_val"] = float(lines[-1].split("All:")[1].split()[0])
        except Exception as e:
            print(f"[Warn] PSNR/SSIM calculation failed: {e}")

    try:
        from PIL import Image
        import imagehash
        d_src = "tmp_src.jpg"
        d_dst = "tmp_dst.jpg"
        subprocess.run(["ffmpeg", "-y", "-i", src, "-vf", "select=eq(n\\,150)", "-vframes", "1", d_src], capture_output=True)
        subprocess.run(["ffmpeg", "-y", "-i", dst, "-vf", "select=eq(n\\,150)", "-vframes", "1", d_dst], capture_output=True)
        if os.path.exists(d_src) and os.path.exists(d_dst):
            h1 = imagehash.dhash(Image.open(d_src))
            h2 = imagehash.dhash(Image.open(d_dst))
            metrics["dhash_mean_distance"] = round((h1 - h2) / 64.0, 3)
            os.remove(d_src)
            os.remove(d_dst)
    except Exception as e:
        print(f"[Warn] dHash check failed: {e}")

    if has_audio and metrics["audio_divergence_pct"] < 85.0:
        metrics["verdict"] = "FAIL"
    elif metrics["ssim_val"] > 0.85 or metrics["dhash_mean_distance"] < 0.35:
        metrics["verdict"] = "FAIL"

    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="v4.0.3 Media Mutation Pipeline")
    parser.add_argument("input", help="Source multimedia file")
    parser.add_argument("output", help="Destination file")
    parser.add_argument("--preset", choices=["transparent", "standard", "aggressive"], default="standard")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--low-cpu", action="store_true", default=True)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--keep-intermediate", action="store_true")
    args = parser.parse_args()

    compat = ffmpeg_compat.probe_compat()
    print(f"[Compat] Detected FFmpeg v{compat.raw_version} (Major: {compat.major})")

    has_audio = check_audio_presence(args.input)

    cmd_file = "chroma_cmd.txt"
    stage_video.generate_sendcmd_file(cmd_file, duration_sec=600.0)
    video_chain = stage_video.build_video_filterchain(
        compat, w=1920, h=1080, fps=30, low_cpu=args.low_cpu, seed=args.seed, sendcmd_path=cmd_file,
    )

    if has_audio:
        audio_chain = stage_audio.build_audio_filterchain(compat, low_cpu=args.low_cpu, seed=args.seed)
        filter_complex = f"{audio_chain};[0:v]{video_chain}[vout]"
        map_args = ["-map", "[aout]", "-map", "[vout]"]
        audio_enc = ["-c:a", "aac", "-b:a", "128k"]
    else:
        filter_complex = f"[0:v]{video_chain}[vout]"
        map_args = ["-map", "[vout]"]
        audio_enc = []

    intermediate_file = "temp_encoded.mp4"
    vsync_args = ["-vsync", "vfr"] if compat.major < 7 else ["-fps_mode", "vfr"]

    ffmpeg_cmd = [
        "ffmpeg", "-y", "-i", args.input,
        "-filter_complex", filter_complex,
    ] + map_args + vsync_args + [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p"  # Ensures universal player compatibility
    ] + audio_enc + [intermediate_file]

    code, log = run_process_with_watchdog(ffmpeg_cmd)
    if code != 0:
        print("[Error] FFmpeg pipeline failed:\n", log)
        sys.exit(1)

    print("[Container] Obfuscating container safely...")
    shutil.copyfile(intermediate_file, args.output)
    ftyp_data = stage_container.randomize_ftyp(args.output, seed=args.seed)
    scrub_data = stage_container.scrub_signatures(args.output, [b"Lavf", b"HandBrake", b"Xiph"])
    atom_data = stage_container.inject_free_atom(args.output, size_bytes=128, seed=args.seed)
    fingerprint_data = stage_container.compute_fingerprint(args.output)

    verify_metrics = {}
    if args.verify:
        verify_metrics = verify_divergence(args.input, args.output, compat)

    manifest = {
        "seed": args.seed,
        "preset": args.preset,
        "ffmpeg_major": compat.major,
        "container_report": {
            "major_brand_after": ftyp_data.get("major_brand_after", "N/A"),
            "minor_version_after": ftyp_data.get("minor_version_after", "N/A"),
            "compatible_brands_after": ftyp_data.get("compatible_brands_after", []),
            "free_atom_size_bytes": atom_data.get("free_atom_size_bytes", 0),
            "signatures_scrubbed": scrub_data,
        },
        "fingerprint": fingerprint_data,
        "verification": verify_metrics,
    }

    with open("manifest.json", "w", encoding="utf-8") as mf:
        json.dump(manifest, mf, indent=2)

    if os.path.exists(cmd_file):
        os.remove(cmd_file)
    if not getattr(args, 'keep_intermediate', False) and os.path.exists(intermediate_file):
        os.remove(intermediate_file)

    print("[Done] Pipeline processing finished successfully.")


if __name__ == "__main__":
    main()
