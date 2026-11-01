#!/usr/bin/env python3
"""Aggregate repeated case-A loads into a distribution.

Case A must be judged as a POPULATION, never a single run: the same md5 has
measured -27.1, -32.7, -33.4 dBFS and PASS (-50) on different boots. A single
run therefore proves nothing, and every attribution decision we have made on
one number has been wrong. This parses case_a_repeat / repro logs into
per-load lines plus median/spread so 'intermittent' is a number, not a vibe.

Usage: python3 rig/case_a_stats.py <log> [more logs...]
"""
import re
import statistics
import sys

LINE = re.compile(r"VERDICT \[A\] (PASS|FAIL)\s+silence control: tone "
                  r"(-?\d+\.\d+)/(-?\d+\.\d+) dBFS vs A0 no-stream baseline "
                  r"(-?\d+\.\d+)")
LOAD = re.compile(r"===== (\S+)\s+load (\d+)")
MD5 = re.compile(r"ARTIFACT-MD5\D*([0-9a-f]{32})")


def main(paths):
    for path in paths:
        rows, tag, md5 = [], "unknown", None
        cur = None
        for raw in open(path, errors="replace"):
            m = LOAD.search(raw)
            if m:
                tag, cur = m.group(1), int(m.group(2))
            m = MD5.search(raw)
            if m:
                md5 = m.group(1)
            m = LINE.search(raw)
            if m:
                verdict, l, r, base = m.group(1), *map(float, m.groups()[1:])
                rows.append((cur, verdict, l, r, base, max(l, r) - base))
        if not rows:
            print(f"{path}: no case-A lines")
            continue
        fails = [x for x in rows if x[1] == "FAIL"]
        deltas = [x[5] for x in rows]
        print(f"\n=== {path}  tag={tag}  md5={md5 or '?'}")
        for cur, verdict, l, r, base, d in rows:
            print(f"  load {cur}: {verdict:4s} L {l:+6.1f} R {r:+6.1f} "
                  f"base {base:+6.1f}  delta {d:+5.1f} dB")
        print(f"  n={len(rows)}  FAIL {len(fails)}  ({100*len(fails)/len(rows):.0f}%)"
              f"  delta median {statistics.median(deltas):+.1f}  "
              f"min {min(deltas):+.1f}  max {max(deltas):+.1f}  "
              f"stdev {statistics.pstdev(deltas):.1f}" if len(deltas) > 1 else "")
        thr = 6.0
        amb = [d for d in deltas if abs(d - thr) < 3.0]
        if amb:
            print(f"  NOTE {len(amb)} run(s) within 3 dB of the +6.0 threshold:"
                  " the gate itself is in the noise band for this artifact.")


if __name__ == "__main__":
    main(sys.argv[1:])
