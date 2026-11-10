#!/usr/bin/env python3
"""Generate the upsampling polyphase bank as XMOS fd_block_fir tables.

The upsampler IS the DSP (owner directive 2026-11-10): both are FIR on the
audio path and both run on lib_audio_dsp's fd_block_fir block engine
(AN02027, examples/app_fd_block_fir), reached through the same shape the
shelf engine uses (csd_dsp_fd.c / gen_fd_fir.py).

Polyphase identity: an R-times upsampler is R same-rate convolutions. Each
phase filter is the comb h[k::R] of one windowed-sinc interpolation
prototype, fed the SAME host-rate input stream at frame_advance samples at a
time; the R outputs of a block are de-interleaved into frame_advance*R wire
words. frame_advance is independent of the block length, which is what makes
a per-phase filter (few taps) satisfy the valid-output window
td_block_length + 1 - taps_per_block >= frame_advance.

Prototype design provenance: the 80 dB Kaiser design the property-tested
table generator used (cutoff 0.45*fs_in, >=64 taps/phase, prototype sums to R
so every phase carries its share of the DC gain -> unity output amplitude).
Design lives here now that the per-call interpolator and its table generator
are deleted; 48-series and 44.1-series share one table per ratio because the
design is normalised against its own input rate -- only MCLK differs between
families.

Run with the generator venv:
  tools/dsp/.venv-dspgen/bin/python tools/dsp/gen_ups_block.py
"""
import math
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audio_dsp.dsp.fd_block_fir import fir_block_fd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.abspath(os.path.join(HERE, "..", "..", "src", "extensions"))

FS_OUT = 384000
# Canonical input rate per integer ratio. Rates of either family map onto the
# same normalised prototype, exactly as the old table selector keyed by ratio.
RATIO_FS_IN = {8: 48000, 4: 96000, 2: 192000}

ATTEN_DB = 80.0     # image rejection target (proven spec)
PASS_EDGE = 0.45    # cutoff as a fraction of fs_in (proven spec)
TAPS_MIN = 64       # per phase minimum (proven spec)
FRAME_ADVANCE = int(os.environ.get("CSD_UPS_FRAME_ADVANCE", "128"))


def kaiser_beta(atten_db):
    if atten_db > 50:
        return 0.1102 * (atten_db - 8.7)
    if atten_db > 21:
        return 0.5842 * (atten_db - 21) ** 0.4 + 0.07886 * (atten_db - 21)
    return 0.0


def bessel_i0(x):
    s, t, k = 1.0, 1.0, 1
    while t > 1e-12 * s:
        t *= (x / (2.0 * k)) ** 2
        s += t
        k += 1
    return s


def kaiser_window(n, length, beta):
    d = (length - 1) / 2.0
    denom = bessel_i0(beta)
    return (bessel_i0(beta * math.sqrt(max(0.0, 1.0 - ((n - d) / d) ** 2)))
            / denom)


def lowpass_sinc(n_taps, fc_norm):
    """fc_norm = cutoff / sample_rate, 0 < fc_norm < 0.5."""
    d = (n_taps - 1) / 2.0
    return [2 * fc_norm * (1.0 if n == d else
                           math.sin(2 * math.pi * fc_norm * (n - d)) /
                           (2 * math.pi * fc_norm * (n - d)))
            for n in range(n_taps)]


def design_prototype(ratio):
    """Interpolation prototype at the OUTPUT rate, cut-ready for R phases."""
    L = ratio
    fs_in = RATIO_FS_IN[ratio]
    # The first image sits centred on fs_in, so the stopband starts at
    # fs_in - f_pass (e.g. 26.4 kHz at a 48 kHz host).
    f_pass = PASS_EDGE * fs_in
    f_stop = fs_in - f_pass
    df = (f_stop - f_pass) / FS_OUT
    beta = kaiser_beta(ATTEN_DB)
    n = int(math.ceil((ATTEN_DB - 7.95) / (2.285 * 2 * math.pi * df))) + 1
    n = max(n, TAPS_MIN * L)
    n += (-n) % L                       # whole number of phases
    fc = (f_pass + f_stop) / 2.0 / FS_OUT
    proto = [h * kaiser_window(i, n, beta)
             for i, h in enumerate(lowpass_sinc(n, fc))]
    # Each output sample is built from ONE phase of the polyphase bank, so
    # every phase must carry its share of the DC gain: the prototype sums to
    # L, each phase then sums to ~1 and the interleaved output is unity gain.
    gain = sum(proto) / L
    return [h / gain for h in proto]


def want(src, pat, what, name):
    m = re.search(pat, src)
    if not m:
        raise RuntimeError(f"{name}: generated header missing {what}")
    return m.group(1)


def make_phase(ratio, k, h_phase):
    """One phase filter: vendor generator, geometry read back from the header.

    Coefficient tables stay mutable RAM (vendor default): the shelf generator
    recorded that const-qualifying the equivalent tables coincided with
    tile[1] dying at stream start, so we do not repeat that deviation here.
    """
    name = f"csdups_r{ratio}p{k}"
    coef_path = os.path.join(HERE, f"{name}.npy")
    np.savetxt(coef_path, h_phase, fmt="%.12e")
    for p in (os.path.join(OUT_DIR, f"{name}.h"),
              os.path.join(OUT_DIR, f"{name}_debug.h")):
        if os.path.exists(p):
            os.remove(p)
    fir_block_fd(fs=float(RATIO_FS_IN[ratio]), n_chans=2,
                 coeffs_path=coef_path, filter_name=name,
                 output_path=OUT_DIR, frame_advance=FRAME_ADVANCE)
    path = os.path.join(OUT_DIR, f"{name}.h")
    src = open(path).read()
    meta = dict(
        name=name,
        tdl=int(want(src, rf"#define {name}_TD_BLOCK_LENGTH \((\d+)\)",
                     "TD_BLOCK_LENGTH", name)),
        blocks=int(want(src, rf"#define {name}_BLOCK_COUNT \((\d+)\)",
                        "BLOCK_COUNT", name)),
        fa=int(want(src, rf"#define {name}_FRAME_ADVANCE \((\d+)\)",
                    "FRAME_ADVANCE", name)),
        fo=int(want(src, rf"#define {name}_FRAME_OVERLAP \((\d+)\)",
                    "FRAME_OVERLAP", name)),
        tpb=int(want(src, r"taps_per_block = (\d+)", "taps_per_block", name)),
    )
    # Geometry gate (the same identity csd_dsp_fd.c relies on): the compute
    # emits td_block_length + 1 - taps_per_block valid samples per block; the
    # de-interleave consumes frame_advance of them per phase per block.
    window = meta["tdl"] + 1 - meta["tpb"]
    if window < meta["fa"]:
        raise RuntimeError(f"{name}: valid-output window {window} < "
                           f"frame_advance {meta['fa']}")
    if meta["fa"] != FRAME_ADVANCE:
        raise RuntimeError(f"{name}: frame_advance {meta['fa']} != "
                           f"{FRAME_ADVANCE}")
    # Every phase must carry its share of the prototype DC gain.
    s = sum(h_phase)
    if abs(s - 1.0) > 0.05:
        raise RuntimeError(f"{name}: phase sum {s} not ~1.0")
    return meta


def emit_conf(metas_by_ratio):
    L = []
    a = L.append
    a("// Auto-generated by tools/dsp/gen_ups_block.py -- DO NOT EDIT.")
    a("#ifndef __csd_ups_block_conf_h__")
    a("#define __csd_ups_block_conf_h__")
    a("")
    a('#include "dsp/fd_block_fir.h"')
    for ratio in sorted(metas_by_ratio, reverse=True):
        for m in metas_by_ratio[ratio]:
            a(f'#include "{m["name"]}.h"')
    a("")
    a(f"#define CSD_UPS_FRAME_ADVANCE ({FRAME_ADVANCE})")
    a(f"#define CSD_UPS_MAX_RATIO ({max(metas_by_ratio)})")
    a("")
    a("/* One full block of wire words per channel: frame_advance inputs x")
    a(" * ratio outputs. Power of two so the delivery ring wraps with a mask,")
    a(" * never a divide on the pop side. */")
    a("#define CSD_UPS_RING_WORDS (CSD_UPS_FRAME_ADVANCE * CSD_UPS_MAX_RATIO)")
    a("#define CSD_UPS_RING_MASK (CSD_UPS_RING_WORDS - 1)")
    a("")
    a("/* Kernel-state sizing by the C compiler from the vendor's own")
    a(" * DATA_BUFFER_ELEMENTS macros (they contain sizeof(bfp_complex_s32_t)). */")
    a("#define CSD_UPS_MAX2(a, b) ((a) > (b) ? (a) : (b))")
    # Fold the MAX over the generated macro NAMES pairwise (the C compiler
    # evaluates the macros; never splice their expansions -- they contain
    # commas inside sizeof(...)).
    def fold(items):
        if len(items) == 1:
            return items[0]
        mid = (len(items) + 1) // 2
        return (f"CSD_UPS_MAX2({fold(items[:mid])}, "
                f"{fold(items[mid:])})")
    def per_ratio(suffix):
        return [f'{m["name"]}_{suffix}'
                for r in sorted(metas_by_ratio, reverse=True)
                for m in metas_by_ratio[r]]
    a("#define CSD_UPS_MAX_DATA_WORDS \\")
    a("    " + fold(per_ratio("DATA_BUFFER_ELEMENTS")))
    a("#define CSD_UPS_MAX_TD_BLOCK_LENGTH \\")
    a("    " + fold(per_ratio("TD_BLOCK_LENGTH")))
    a("")
    a("/* One per-phase fd filter: same rate, same frame_advance, few taps.")
    a(" * coef_blocks live in the mutable-RAM tables the vendor generator")
    a(" * emitted (see the header note in the generator about why they are")
    a(" * NOT const-qualified). */")
    a("typedef struct {")
    a("    const bfp_complex_s32_t *coef_blocks;")
    a("    unsigned td_block_length;")
    a("    unsigned block_count;")
    a("    unsigned taps_per_block;")
    a("    unsigned frame_advance;")
    a("    unsigned frame_overlap;")
    a("    unsigned data_words;")
    a("} csd_ups_phase_t;")
    a("")
    for ratio in sorted(metas_by_ratio, reverse=True):
        a(f"static const csd_ups_phase_t csd_ups_ph_r{ratio}[{ratio}] = {{")
        for m in metas_by_ratio[ratio]:
            a(f"    {{ coef_blocks_{m['name']}, {m['tdl']}, {m['blocks']}, "
              f"{m['tpb']}, {m['fa']}, {m['fo']}, {m['name']}"
              f"_DATA_BUFFER_ELEMENTS }},")
        a("};")
    a("")
    a("/* Indexed by ratio/2 - 1: {2,4,8} -> {0,1,2}. The ratio is decided by")
    a(" * the host rate (family table), never by a second rate source. */")
    a("typedef struct {")
    a("    const csd_ups_phase_t *phases;")
    a("    unsigned num_phases;")
    a("} csd_ups_bank_t;")
    a("")
    a("static const csd_ups_bank_t csd_ups_banks[] = {")
    for ratio in sorted(metas_by_ratio):
        a(f"    {{ csd_ups_ph_r{ratio}, {ratio} }},   /* ratio {ratio} */")
    a("};")
    a("")
    a("#endif /* __csd_ups_block_conf_h__ */")
    with open(os.path.join(OUT_DIR, "csd_ups_block_conf.h"), "w") as f:
        f.write("\n".join(L) + "\n")


def main():
    metas_by_ratio = {}
    for ratio in (8, 4, 2):
        proto = design_prototype(ratio)
        assert len(proto) % ratio == 0
        tpp = len(proto) // ratio
        metas = []
        for k in range(ratio):
            h_phase = np.array(proto[k::ratio], dtype=np.float64)
            metas.append(make_phase(ratio, k, h_phase))
        print(f"ratio {ratio}: prototype {len(proto)} taps, {tpp}/phase, "
              f"tdl={metas[0]['tdl']} blocks={metas[0]['blocks']} "
              f"tpb={metas[0]['tpb']} fa={metas[0]['fa']} "
              f"fo={metas[0]['fo']}")
        metas_by_ratio[ratio] = metas
    emit_conf(metas_by_ratio)
    print("wrote csd_ups_block_conf.h +",
          sum(len(v) for v in metas_by_ratio.values()), "phase headers to",
          OUT_DIR)


if __name__ == "__main__":
    main()
