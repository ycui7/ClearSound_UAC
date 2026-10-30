#!/usr/bin/env python3
"""T6 (B1): verify the GENERATED FD tables, per rate.

Parses the int32 coefficient arrays out of src/extensions/csdfd_<fs>.h exactly
as the firmware sees them (bfp blocks, half-spectrum, per-block exponent),
reconstructs the time-domain taps, and checks:
  1. reconstruction == gen_coeffs.design_shelf taps (quantization SNR gate),
  2. frequency response meets the shelf spec (passband/stopband probes).
This gates the shipped artifact — not the intermediate .npy.

Run: tools/dsp/.venv-dspgen/bin/python tools/dsp/verify_fd_tables.py
"""
import os, re, sys
import numpy as np
from scipy.signal import freqz

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gen_coeffs as g

EXT = os.path.abspath(os.path.join(HERE, "..", "..", "src", "extensions"))

PASSBAND = 60.0          # Hz: shelf target region
STOPBAND = 1500.0        # Hz: shelf settled to 0 dB
TOL_PB_DB = 0.3          # +/- vs +6 dB target
TOL_SB_DB = 0.1
SNR_FLOOR_DB = 80.0      # quantization gate: ||h_hat - h|| / ||h||


def parse_header(fs):
    """Recover (data, exp, length) per coef block + filter geometry."""
    path = os.path.join(EXT, f"csdfd_{fs}.h")
    src = open(path).read()
    name = f"csdfd_{fs}"

    m = re.search(rf"(?:const )?int32_t __attribute__\(\(aligned \(8\)\)\) "
                  rf"coefs_{name}\[\d*\] = \{{(.*?)\}};", src, re.S)
    if not m:
        raise RuntimeError(f"{fs}: coefs array not found")
    words = np.array([int(t.replace(",", "")) for t in
                      re.findall(r"-?\d+", m.group(1))], dtype=np.int64)

    blocks = []
    for d, ln, ex in re.findall(
            r"\{\.data = \(complex_s32_t\*\)\(coefs_%s \+ (\d+)\), "
            r"\.length = (\d+), \.exp = (-?\d+)" % name, src):
        blocks.append((int(d), int(ln), int(ex)))
    tdl = int(re.search(rf"#define {name}_TD_BLOCK_LENGTH \((\d+)\)", src).group(1))
    bc = int(re.search(rf"#define {name}_BLOCK_COUNT \((\d+)\)", src).group(1))
    tpb = int(re.search(r"\.taps_per_block = (\d+)", src).group(1))
    if len(blocks) != bc:
        raise RuntimeError(f"{fs}: {len(blocks)} blocks parsed, header says {bc}")
    return words, blocks, tdl, tpb


def reconstruct_taps(words, blocks, tdl, tpb, num_taps):
    """Vendor packing, per _emit_filter in fd_block_fir.py:
       per section: flat = [re0, NQ, re1, im1, re2, im2, ...] (im0 dropped,
       Nyquist real moved to slot 1, tail trimmed); .length words == tdl/2.
       Section i is the rfft of h[i*tpb : (i+1)*tpb] zero-padded to tdl
       (ceil(num_taps/tpb) sections)."""
    half_n = tdl // 2
    h = np.zeros(tpb * len(blocks))
    pad_err = 0.0
    for i, (ofs, ln, exp) in enumerate(blocks):
        seg = words[ofs:ofs + 2 * ln]
        full = np.zeros(tdl, dtype=np.complex128)
        full[0] = seg[0]                       # DC real
        full[half_n] = seg[1]                  # Nyquist real (packed slot 1)
        k = min(ln - 1, half_n - 1)            # bins 1..127
        bins = seg[2:2 + 2 * k]
        full[1:1 + k] = bins[0::2] + 1j * bins[1::2]
        full[tdl - k:] = np.conj(full[1:1 + k][::-1])
        td = np.fft.ifft(full).real * (2.0 ** exp)
        h[i * tpb:(i + 1) * tpb] = td[:tpb]
        pad_err = max(pad_err, np.max(np.abs(td[tpb:])))
    return h[:num_taps], pad_err


def check_rate(fs):
    h, D, N = g.design_shelf(fs)
    words, blocks, tdl, tpb = parse_header(fs)
    hh, pad_err = reconstruct_taps(words, blocks, tdl, tpb, N)
    if hh.shape != h.shape:
        return f"shape {hh.shape} != {h.shape}"
    # exact scale must come from the exponents alone; allow no free gain
    scale = np.dot(hh, h) / np.dot(hh, hh)
    if abs(scale - 1.0) > 1e-3:
        return f"tap scale {scale!r} off unity (exponent/Q_sig unpacking wrong)"
    snr = 20 * np.log10(np.linalg.norm(h) / max(np.linalg.norm(hh - h), 1e-300))
    if snr < SNR_FLOOR_DB:
        return f"tap SNR {snr:.1f} dB < {SNR_FLOOR_DB}"
    if pad_err > 1e-3 * max(np.max(np.abs(h)), 1e-9):
        return f"section padding not zero ({pad_err:.2e})"

    w, H = freqz(hh, worN=8192, fs=fs)
    g_db = 20 * np.log10(np.abs(H) + 1e-30)

    def mag(f):
        return g_db[np.argmin(np.abs(w - f))]
    errs = []
    pb = mag(PASSBAND)
    if abs(pb - 6.0) > TOL_PB_DB:
        errs.append(f"passband@{PASSBAND}Hz {pb:.2f} dB (want 6+/-{TOL_PB_DB})")
    for f in (STOPBAND, min(fs / 2 * 0.9, 20000.0)):
        sb = mag(f)
        if abs(sb) > TOL_SB_DB:
            errs.append(f"stopband@{f:.0f}Hz {sb:.2f} dB (want 0+/-{TOL_SB_DB})")
    return None if not errs else "; ".join(errs) + f" (SNR {snr:.0f} dB)"


def main():
    bad = 0
    for fs in g.FS_LIST:
        try:
            err = check_rate(fs)
        except Exception as e:
            err = f"EXC {e}"
        print(f"{'FAIL' if err else 'ok  '} {fs:6d} {err or ''}")
        bad += err is not None
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
