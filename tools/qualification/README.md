Qualification harness (hardware-in-the-loop, ub-mode).

Run from this directory with the board attached. Each script RAM-loads the
firmware with xrun and keeps the load alive for the whole run, because a
backgrounded xrun is reaped at shell exit and the board silently falls back
to the EEPROM image (stale firmware, invalid result).

- full_gate.sh <label>   mute gate + volume gate in one load window (the gate
                         every unit was accepted on)
- mute_gate.sh           1 kHz tone, mic FFT with mute off/on/off
- r441.sh                PCM sweep 44.1/88.2/176.4/352.8 kHz with stream-open
                         retry (first open after enumerate can fail silently)
- dsd128_final.sh        DoP DSD64 + DSD128 carrier check
- ears_fft.py <wav> <f>  1 kHz-style FFT level of a mono pair, per channel

Sound-card indices are discovered by name (XMOS / E36dB); never hard-code
card numbers, they renumber across loads.
