#!/usr/bin/env python3
"""
stage_container.py — Part 3 container obfuscation (refactored for master).
"""
import argparse
import datetime
import json
import os
import random
import subprocess
import tempfile
import time
from ffmpeg_compat import Compat

SIGNATURES = [
    (b"Lavf", 24), (b"Lavc", 24),
    (b"libx264", 12), (b"x264", 8),
    (b"libx265", 12), (b"x265", 8),
    (b"FFmpeg", 12), (b"ffmpeg", 12),
    (b"HandBrake", 16), (b"LAME", 16),
    (b"\xa9too", 4), (b"\xa9enc", 4), (b"\xa9swr", 4),
]

PROFILES = {
    "iphone_mov": {"container": "mov", "brand": "qt  ",
                   "timescale_range": (600, 600),
                   "handler_v": "Core Media Video",
                   "handler_a": "Core Media Audio",
                   "audio_first": False},
    "android_mp4": {"container": "mp4", "brand": "mp42",
                    "timescale_range": (9000, 60000),
                    "handler_v": "VideoHandler",
                    "handler_a": "SoundHandler",
                    "audio_first": False},
    "gopro_mp4": {"container": "mp4", "brand": "isom",
                  "timescale_range": (90000, 90000),
                  "handler_v": "VideoHandler",
                  "handler_a": "SoundHandler",
                  "audio_first": True},
}

def _run(cmd):
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    lines = []
    for line in proc.stdout:
        s = line.strip()
        lines.append(s)
        if s and not s.startswith(("frame=", "size=", "time=", "bitrate=",
                                   "speed=", "progress=", "dup=", "drop=",
                                   "fps=")):
            print(s)
    return proc.wait(), "\n".join(lines)

def _is_option_error(log: str) -> bool:
    return any(k in log for k in ("Option not found", "Unrecognized option",
                                  "Unable to find a suitable output format",
                                  "Unknown option", "not found"))

def build_container_args(src: str, dst: str, seed: int | None = None,
                         profile: str | None = None, codec: str = "h264",
                         strip_sei: bool = True, fake_creation: bool = True,
                         compat: Compat | None = None):
    compat = compat or Compat()
    rng = random.Random(seed)
    prof_name = profile or rng.choice(list(PROFILES))
    p = PROFILES[prof_name]
    ts = rng.randint(*p["timescale_range"])
    frag_ms = rng.randint(400, 1200)

    req = ["-y", "-nostdin", "-hide_banner",
           "-fflags", "+bitexact",
           "-i", src,
           "-map_metadata", "-1", "-map_chapters", "-1"]
    req += (["-map", "0:a:0", "-map", "0:v:0"] if p["audio_first"]
            else ["-map", "0:v:0", "-map", "0:a:0"])
    req += ["-c", "copy",
            "-f", p["container"],
            "-use_editlist", "0",
            "-min_frag_duration", str(frag_ms),
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof"
                         "+negative_cts_offsets+omit_tfhd_offset"]
    if compat.mov_opt("brand"):
        req += ["-brand", p["brand"]]

    if strip_sei and compat.has_filter_units:
        nals = "39|40" if codec == "hevc" else "6|12"
        req += ["-bsf:v", f"filter_units=remove_types={nals}"]

    req += ["-avoid_negative_ts", "make_zero",
            "-fflags", "+bitexact"]

    opt_group = []
    if compat.mov_opt("video_track_timescale"):
        opt_group += ["-video_track_timescale", str(ts)]
    for o in ("write_btrt", "write_prft", "write_tmcd", "write_udta"):
        if compat.mov_opt(o):
            opt_group += ["-" + o, "0"]

    if fake_creation:
        dt = datetime.datetime.utcfromtimestamp(
            rng.randint(int(time.time()) - 30 * 86400, int(time.time())))
        req += ["-metadata",
                "creation_time=" + dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z")]
    req += ["-metadata:s:v:0", f"handler_name={p['handler_v']}",
            "-metadata:s:a:0", f"handler_name={p['handler_a']}",
            dst]

    meta = {"profile": prof_name, "timescale": ts,
            "frag_ms": frag_ms, "strip_sei": strip_sei}
    return req, opt_group, meta

def run_container_stage(src: str, dst: str, seed: int | None = None,
                        profile: str | None = None, codec: str = "h264",
                        strip_sei: bool = True, fake_creation: bool = True,
                        scrub: bool = True, ffmpeg: str = "ffmpeg",
                        compat: Compat | None = None):
    compat = compat or Compat(ffmpeg=ffmpeg)
    req, opt_group, meta = build_container_args(
        src, dst, seed, profile, codec, strip_sei, fake_creation, compat)
    variants = [req + opt_group, req]
    last_rc, last_log = -1, ""
    for v in variants:
        rc, log = _run([ffmpeg] + v)
        if rc == 0:
            meta["scrubbed"] = scrub_signatures(dst) if scrub else 0
            meta["returncode"] = 0
            return meta
        last_rc, last_log = rc, log
        if not _is_option_error(log):
            break
    raise RuntimeError(f"container stage failed (rc={last_rc}):\n{last_log[-2000:]}")

def scrub_signatures(path: str, signatures=None) -> int:
    signatures = signatures or SIGNATURES
    with open(path, "rb") as f:
        data = f.read()
    hits = 0
    for sig, wlen in signatures:
        pos = 0
        while True:
            i = data.find(sig, pos)
            if i < 0:
                break
            data = data[:i] + b"\x00" * wlen + data[i + wlen:]
            hits += 1
            pos = i + wlen
    if hits:
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(os.path.abspath(path)), suffix=".scrub")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    return hits

def forensic_report(path: str, ffprobe: str = "ffprobe") -> dict:
    with open(path, "rb") as f:
        blob = f.read()
    head = blob[:64]
    major = (head[8:12].decode("latin1") if head[4:8] == b"ftyp" else "?")
    compat_brands = [head[i:i + 4].decode("latin1")
                     for i in range(16, min(len(head), 64) - 3, 4)
                     if head[i:i + 4] != b"\x00" * 4]
    found = [s.decode("latin1") for s, _ in SIGNATURES if s in blob]
    p = subprocess.run([ffprobe, "-v", "error", "-show_format",
                        "-show_streams", "-of", "json", path],
                       capture_output=True, text=True)
    info = json.loads(p.stdout or "{}")
    fmt, streams = info.get("format", {}), info.get("streams", [])
    report = {"major_brand": major, "compatible_brands": compat_brands,
              "moof_count": blob.count(b"moof"),
              "mdat_count": blob.count(b"mdat"),
              "signatures_found": found,
              "format_tags": fmt.get("tags", {}),
              "stream_tags": [s.get("tags", {}) for s in streams],
              "nb_streams": len(streams),
              "start_time": fmt.get("start_time"),
              "duration": fmt.get("duration")}
    print("=== container forensic report ===")
    print(f"major_brand      : {major}  compat: {compat_brands}")
    print(f"moof/mdat        : {report['moof_count']}/{report['mdat_count']} "
          f"(fragmented = {report['moof_count'] > 1})")
    print(f"encoder sigs     : {found if found else 'NONE (clean)'}")
    print(f"format tags      : {fmt.get('tags', {})}")
    print(f"stream tags      : {[s.get('tags', {}) for s in streams]}")
    return report
