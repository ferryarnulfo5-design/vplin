#!/usr/bin/env python3
"""
stage_audio.py -- Cryptographic audio waveform disruption.

Goal: fracture Chromaprint / ACRCloud feature spaces while keeping speech
intelligible. Everything below is single-pass and O(N) in duration. The
v2 segmented atrim/afade/concat approach (hundreds of filters on long
files) is replaced by ONE firequalizer whose gain expression is
time-variant via pts*tb -- constant filter count regardless of duration.

Layer plan (all inside a single -filter_complex graph):
  L1  aresample=48000                normalize the clock
  L2  firequalizer swept notch +     O(1) wrt duration; gain uses f (Hz)
      slow wobble (time-variant)     and t = pts*tb (seconds)
      |  fallback: static 3-band aequalizer when cpu_count() < 4
  L3  compand envelope warping       destroys Chromaprint temporal peaks
  L4  aphaser(sinusoidal) + chorus   comb-filter/reflection artifacts
                                    (ACRCloud room-acoustic models)
  L5  2 or 3 band asetrate/atempo    band-wise pitch skew; 2 bands when
      split (hp/bp/lp)               os.cpu_count() < 4
  L6  amix + alimiter + aresample    exact duration + PTS reset => AV sync
      async=1 + atrim
"""
from __future__ import annotations

from ffmpeg_compat import Capabilities, supported_opts

DEFAULT_SR = 48000

# --------------------------------------------------------------------------
# L2 -- time-variant spectral surgery (one filter, any duration)
# --------------------------------------------------------------------------
def _fq_swept_notch() -> str:
    """Swept notch: every 20 s the notch depth oscillates via sin(f/1200).

    t = pts * tb evaluates to seconds; f is the bin frequency in Hz.
    One filter instance regardless of input length (vs v2's N atrim loops).
    """
    return (
        "firequalizer=gain='if(lt(mod(pts*tb,20),10),-6*sin(f/1200),-1.5)':"
        "fscale=lin:fir=1:fir_size=2048:zero_phase=0"
    )


def _fq_wobble() -> str:
    """Slow out-of-phase upper-spectrum wobble (kills steady-state bins)."""
    return (
        "firequalizer=gain='2.5*sin(2*PI*pts*tb/9)*sin(f/700)':"
        "fscale=lin:fir=1:fir_size=2048:zero_phase=0"
    )


def _static_eq() -> str:
    """Cheap fallback for <4-core runners: static 3-band parametric EQ.

    aequalizer is IIR -- no FFT, no time-variant recomputation, minimal CPU.
    """
    return (
        "aequalizer=f=250:t=q:w=1.2:g=-3,"
        "aequalizer=f=1600:t=q:w=1.5:g=2.5,"
        "aequalizer=f=6500:t=q:w=2.0:g=-4"
    )


# --------------------------------------------------------------------------
# L3 -- non-linear amplitude envelope warping
# --------------------------------------------------------------------------
_COMPAND = (
    "compand=attacks=0.3:decays=0.8:"
    "points=-80/-80|-30/-15|-12/-9|-6/-6|0/-2:soft-knee=6:gain=3"
)


# --------------------------------------------------------------------------
# L4 -- phase inversion + comb filtering
# --------------------------------------------------------------------------
def _modulation(caps: Capabilities) -> str:
    """aphaser (sinusoidal) + chorus, probing option names per build."""
    chain = []
    aphaser = supported_opts(
        caps, "aphaser",
        type="sinusoidal", decay="0.4", speed="0.6", gain="0.5", feedback="0.25",
    )
    if aphaser:
        chain.append(f"aphaser={aphaser}")
    if caps.has("chorus"):
        if caps.opt("chorus", "delays"):          # FFmpeg >= 4.2 naming
            chorus = (
                "chorus=in_gain=0.5:out_gain=0.7:delays=40|55:"
                "decays=0.3|0.2:speeds=0.4|0.6:depths=0.5|0.7"
            )
        else:                                     # legacy positional form
            chorus = "chorus=0.5:0.7:40|55:0.3|0.2:0.4|0.6:0.5|0.7"
        chain.append(chorus)
    return ",".join(chain)


# --------------------------------------------------------------------------
# L5 -- split-band frequency shifting
# --------------------------------------------------------------------------
def _bands(low_cpu: bool):
    """(label, split_filter, asetrate_factor). 2 bands on weak runners."""
    if low_cpu:
        return (
            ("hb", "highpass=f=5000", 0.955),
            ("lb", "lowpass=f=5000", 1.045),
        )
    return (
        ("hf", "highpass=f=4500", 0.955),
        ("mf", "bandpass=f=1500:w=3000", 1.045),
        ("lf", "lowpass=f=450", 0.985),
    )


# --------------------------------------------------------------------------
# Graph builder
# --------------------------------------------------------------------------
def build_audio_filtergraph(
    caps: Capabilities,
    low_cpu: bool,
    duration_s: float,
    in_label: str = "0:a",
    sr: int = DEFAULT_SR,
) -> str:
    """Return the full audio filter_complex fragment ending in [aout].

    duration_s MUST be the *video* duration (probed by pipeline.py) so the
    final atrim locks audio length to video length -- that is what keeps
    A/V sync exact after all the pitch/tempo surgery.
    """
    bands = _bands(low_cpu)
    n = len(bands)
    segs: list[str] = []

    # --- L1+L2+L3+L4, then asplit into bands --------------------------
    pre: list[str] = [f"[{in_label}]aresample={sr}"]
    if low_cpu or not caps.has("firequalizer"):
        pre.append(_static_eq())
    else:
        pre.append(_fq_swept_notch())
        if caps.firequalizer_tv:
            pre.append(_fq_wobble())     # skip if the build can't do pts/tb
    pre.append(_COMPAND)
    mod = _modulation(caps)
    if mod:
        pre.append(mod)
    pre.append(f"asplit={n}")
    segs.append(",".join(pre) + "".join(f"[b{i}]" for i in range(n)))

    # --- L5: per-band pitch skew with duration compensation ------------
    mix_inputs: list[str] = []
    for i, (name, split, rate) in enumerate(bands):
        pitch = f"{sr}*{rate:.6f}"
        tempo = f"{1.0 / rate:.6f}"
        vol = "volume=0.34" if n == 3 else "volume=0.5"
        segs.append(
            f"[b{i}]{split},asetrate={pitch},aresample={sr},atempo={tempo},"
            f"{vol}[o{i}]"
        )
        mix_inputs.append(f"[o{i}]")

    # --- L6: mix, limit, resync, exact duration ------------------------
    if caps.amix_normalize:
        mixer = f"amix=inputs={n}:normalize=0:duration=longest"
    else:
        mixer = f"amix=inputs={n}:duration=longest"   # we pre-scaled volumes
    segs.append(
        f"{','.join(mix_inputs)}{mixer},"
        "alimiter=limit=0.95,"
        f"aresample={sr}:async=1:first_pts=0,"
        f"atrim=0:{duration_s:.3f},asetpts=PTS-STARTPTS[aout]"
    )
    return ";".join(segs)


if __name__ == "__main__":
    from ffmpeg_compat import probe
    caps = probe()
    g = build_audio_filtergraph(caps, low_cpu=False, duration_s=600.0)
    print(g)
