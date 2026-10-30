#!/usr/bin/env python3
"""Host check of OUR shelf taps through the VENDOR'S OWN float model.

No re-derivation of the FD algorithm: fir_block_fd.process_frame() is
lib_audio_dsp's reference implementation (what its own test suite gates the C
against). If it matches a direct convolution, our coefficient file is good and
the vendor C kernel -- already verified by XMOS against a TD reference on
target -- is transitively correct.

Run: .venv-dspgen/bin/python verify_fd_stream.py
"""
import os, sys, math
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gen_coeffs as g
from audio_dsp.dsp.fd_block_fir import fir_block_fd

FRAME_ADVANCE = int(os.environ.get("CSD_FRAME_ADVANCE", "128"))
fail = 0
for fs in g.FS_LIST:
    h, D, N = g.design_shelf(fs)
    coef = os.path.join(HERE, f"vfd_{fs}.npy")
    np.savetxt(coef, h, fmt="%.12e")
    out = os.path.join(HERE, "_vfd_out"); os.makedirs(out, exist_ok=True)
    f = fir_block_fd(fs=float(fs), n_chans=1, coeffs_path=coef,
                     filter_name=f"vfd_{fs}", output_path=out,
                     frame_advance=FRAME_ADVANCE)
    rng = np.random.default_rng(11)
    x = rng.uniform(-1.0, 1.0, FRAME_ADVANCE * 64)
    y = []
    for i in range(0, len(x), FRAME_ADVANCE):
        frame = [x[i:i+FRAME_ADVANCE].reshape(1, -1)]
        y.append(np.asarray(f.process_frame(frame)[0]).ravel())
    y = np.concatenate(y)
    ref = np.convolve(x, h)[:len(y)]
    a, b = y[4*FRAME_ADVANCE:], ref[4*FRAME_ADVANCE:len(y)]
    n = min(len(a), len(b)); a, b = a[:n], b[:n]
    err = float(np.max(np.abs(a - b))); sig = float(np.max(np.abs(b)))
    ok = err < 1e-6 * max(sig, 1.0)
    fail |= not ok
    print(f"{fs:6d}: taps={N:4d} nfft={f.nfft} fd_buffers={f.n_fd_buffers} "
          f"max_err={err:.2e} sig={sig:.3f} {'PASS' if ok else 'FAIL'}")
print("HOST-FD", "PASS" if not fail else "FAIL")
sys.exit(fail)
