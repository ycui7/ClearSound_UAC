#!/usr/bin/env python3
"""Align the two streams, THEN count.

Why this exists. The earlier comparator was incremental: anchor once, then walk
forward, and whenever a frame did not match, SEARCH for it and re-anchor. That
orders the work backwards -- it counts first and aligns as a recovery mechanism,
so anything the rig itself put into the stream (the unaligned head before the
sync marker was found, the tail after the writer stopped, a chunk sampled while
the GIL was held by the writer) lands in the defect tally as if the device made
it. It did exactly that: the identical signature (subst ~192/30 s, max_gap 48,
dup 0, zeros on both channels) reproduced against the kernel loopback card with
no USB and no firmware in the path. A defect that survives removing the device
is a property of the measurement, not of the thing measured.

The order, which is the only order that makes an error rate mean something:

  1. ESTABLISH LATENCY (out->in) as a quantity, from the marker blocks in the
     captured stream. Print it. Nothing else may proceed until it is known,
     because the aligned region cannot be named without it.
  2. ALIGN GLOBALLY. Recover the single absolute schedule offset Delta that maps
     capture position -> schedule index, and CONFIRM it with several
     widely-separated windows. One global constant, not a per-chunk guess: a
     comparator that re-anchors whenever a frame disagrees can hide an
     arbitrarily bad alignment behind an arbitrarily clean score.
  3. DISCARD EVERYTHING OUTSIDE THE ALIGNED REGION, and report how much was
     discarded. A rig that throws away 40 % of the data to reach a low error
     rate is not measuring.
  4. COUNT, only inside the aligned region, with NO re-anchoring. A mismatch
     there is a defect. Its class is still evidence (decided by searching for
     the content), but the alignment is never re-negotiated -- re-negotiating it
     is how rig artifacts become device defects.

Every step is a pure function of (captured arrays, schedule function), so it is
testable against a synthetic stream with a KNOWN latency and KNOWN injected
faults. That self-test runs before any hardware is trusted.
"""
import os
import sys

import numpy as np

# Bounds on the alignment search and on what counts as a usable alignment.
VERIFY_WINDOWS = 5          # independent windows that must all agree on Delta
VERIFY_FRAMES = 64          # consecutive frames that must all match per window
MIN_ALIGNED_FRACTION = 0.5  # of captured frames that must survive alignment;
                            # less means we are scoring a sliver, not a stream
MIN_RUN_MARKERS = 3         # markers that must obey the scheduled period
PROBE_STRIDE = 1 << 16      # offset measured every 65536 frames: the boundary
                            # resolution of a slip, so a dropped 48-frame packet
                            # cannot hide between two probes
WIDE_WINDOW = 1 << 21       # search radius for the FIRST offset only
NEAR_WINDOW = 1 << 14       # search radius once the offset is known
# Both the probe search and the scoring expand the schedule over index ranges.
# Done naively at 30 min (691 M frames) that is ~11 GB of int64 per pass, so
# every range is walked in bounded slices -- a soak must not die of its own
# bookkeeping, which is how a rig ends up reporting nothing.
_SEARCH_SLICE = 1 << 18
_SCORE_SLICE = 1 << 20
MIN_SCORED_FRACTION = MIN_ALIGNED_FRACTION


def find_marker_runs(is_m):
    """Runs of the sync marker as [(start, len), ...]."""
    out = []
    n = len(is_m)
    i = 0
    while i < n:
        if is_m[i]:
            j = i
            while j < n and is_m[j]:
                j += 1
            out.append((i, j - i))
            i = j
        else:
            i += 1
    return out


def measure_latency(cap_L, cap_R, sync_l, sync_r, min_run=1024):
    """Step 1: the out->in latency, read off the marker blocks.

    Returns (runs, spacing_stats). Spacing is the rate-lock evidence: the
    schedule emits markers exactly MARKER_PERIOD apart, so a modal spacing equal
    to the period proves the captured timeline advances at the negotiated rate
    and one constant Delta can describe it. Wrong spacing => no alignment is
    valid and the caller must not score.
    """
    is_m = (cap_L == sync_l) & (cap_R == sync_r)
    runs = [(s, ln) for (s, ln) in find_marker_runs(is_m) if ln >= min_run]
    starts = [s for (s, _) in runs]
    stats = None
    if len(starts) >= 2:
        d = np.diff(np.array(starts))
        vals, cnts = np.unique(d, return_counts=True)
        k = int(np.argmax(cnts))
        stats = {"mode": int(vals[k]), "mode_count": int(cnts[k]),
                 "outliers": int(len(d) - cnts[k]),
                 "top": sorted(zip(vals.tolist(), cnts.tolist()),
                               key=lambda t: -t[1])[:4]}
    return runs, stats


def find_delta(cap_seg_L, cap_seg_R, schedule_vec, centre, window,
               n_frames=VERIFY_FRAMES, unique=False):
    """Absolute schedule index corresponding to capture position `centre`.

    Content is unique per frame (deterministic band-limited noise, see
    csd_signal), so requiring n_frames CONSECUTIVE frames to match pins the
    offset to one value. A single frame would allow an accidental hit, and a
    replayed buffer would match at its original index -- both rejected here.
    """
    if len(cap_seg_L) < n_frames:
        return None
    # A probe of identical frames pins NOTHING. Inside the marker block every
    # frame is the same (SYNC_L, SYNC_R), so such a probe matches every marker
    # position in the schedule and returns an offset off by a whole number of
    # marker periods -- the self-test caught exactly that (off by 3*MARKER_PERIOD).
    # Reject it here rather than trust a confidently wrong alignment.
    if (np.all(cap_seg_L == cap_seg_L[0])
            and np.all(cap_seg_R == cap_seg_R[0])):
        return None
    lo = max(0, centre - window)
    hi = centre + window
    first = cap_seg_L[0]
    second = cap_seg_R[0]
    hits = []
    # Sliced: a 30-minute soak needs a wide first search, and materialising the
    # whole schedule range at once costs hundreds of MB per probe.
    #
    # Each slice is compared over n_frames-1 frames PAST its end. Without that,
    # a match that starts in the last n_frames positions of a slice cannot be
    # verified inside the slice and is silently dropped -- the offset exists and
    # the probe reports it does not, which voids a good stream.
    for s in range(lo, hi, _SEARCH_SLICE):
        e = min(s + _SEARCH_SLICE, hi)
        xe = min(e + n_frames - 1, hi)
        idx = np.arange(s, xe, dtype=np.uint64)
        eL, eR = schedule_vec(idx)
        eL = eL.astype(np.int64)
        eR = eR.astype(np.int64)
        span = e - s                      # candidates may only START here
        cand = np.nonzero((eL[:span] == first) & (eR[:span] == second))[0]
        for c in cand[:64]:
            if c + n_frames > len(eL):
                continue
            if (np.array_equal(eL[c:c + n_frames], cap_seg_L[:n_frames])
                    and np.array_equal(eR[c:c + n_frames], cap_seg_R[:n_frames])):
                hits.append(int(s + int(c)))
    # Ambiguity is a verdict, not a coin flip: taking hits[0] would silently
    # commit to one of several alignments that are indistinguishable from this
    # probe, and a wrong Delta makes the whole error rate meaningless.
    if unique:
        if len(hits) != 1:
            return None
    if not hits:
        return None
    return hits[0]


def align(cap_L, cap_R, schedule_vec, sync_l, sync_r, rate,
          centre_guess=0, window=None, marker_period=786432, min_run=1024,
          probe_stride=None):
    """Steps 1-3. Returns a dict describing the alignment, verdict VOID/ALIGNED.

    Never scores -- scoring is separate, so an unaligned run cannot silently
    return a low defect count.
    """
    res = {"verdict": "VOID", "reason": "", "n_captured": int(len(cap_L))}
    if window is None:
        window = NEAR_WINDOW
    if probe_stride is None:
        probe_stride = PROBE_STRIDE
    if probe_stride <= 48 * 8:
        # A single dropped ISO packet at 384 kHz is 48 frames. If probes are
        # closer than that, two probes can straddle one drop and both report the
        # same offset, so the dropout never appears as a slip and vanishes from
        # the report entirely.
        return dict(res, reason=f"probe_stride {probe_stride} is inside one "
                               f"48-frame packet; drops would go unmeasured")
    if len(cap_L) < VERIFY_FRAMES * 4:
        res["reason"] = "capture too short to align"
        return res

    runs, st = measure_latency(cap_L, cap_R, sync_l, sync_r, min_run)
    res["markers"] = len(runs)
    res["marker_starts"] = [s for (s, _) in runs][:8]
    if len(runs) < 3 or st is None:
        res["reason"] = (f"only {len(runs)} marker block(s) captured; latency "
                         f"is unmeasurable, so alignment would be assumed")
        return res
    # MODAL spacing, not "all spacings equal". The head of the capture is the
    # block the player holds and REPEATS until capture is reading, so head
    # markers arrive at the held-block stride, and the tail can be truncated.
    # Requiring every gap to equal MARKER_PERIOD rejects a good stream -- the
    # aligner's own self-test fed it exactly that head and caught this. What
    # must hold is that a MAJORITY of markers sit at the scheduled period.
    res["spacing"] = st
    if st["mode"] != marker_period:
        res["reason"] = (f"modal marker spacing {st['mode']} != scheduled "
                         f"{marker_period} (top spacings {st['top']}): rate not "
                         f"locked")
        return res
    if st["mode_count"] < MIN_RUN_MARKERS:
        res["reason"] = (f"only {st['mode_count']} markers obey the "
                         f"{marker_period}-frame period; too few to establish a "
                         f"uniform timeline")
        return res
    # NOT the latency: this is the marker spacing, which is the rate-lock
    # evidence. The out->in latency is `delta`, set below once the offset is
    # measured. An earlier version labelled this latency_frames and a run
    # printed a 2048 ms "latency" that was really the marker period.
    res["marker_spacing_frames"] = st["mode"]
    res["marker_spacing_ms"] = 1000.0 * st["mode"] / rate

    # Offset, measured densely along the capture.
    #
    # NOT one global constant. A real dropped packet IS a change of offset:
    # content after the gap sits 48 frames early relative to content before it,
    # so insisting on a single Delta makes a genuinely defective stream look
    # "unaligned" and voids the run -- which is exactly what happened to the
    # loopback-card fixture, refused for a 96-frame slip (2 USB microframes at
    # 384 kHz) with no device, no USB and no firmware in the path. A void is
    # worse than a defect count: it hides the dropout instead of measuring it.
    #
    # So: probe at a fixed stride, and let the capture split itself into
    # REGIONS of constant offset. Every offset change is a measured SLIP -- a
    # real dropout, reported with how many schedule frames vanished. Within a
    # region the offset is still never re-negotiated per frame, which is the
    # discipline that keeps rig artifacts out of the tally.
    deltas = []
    span = runs[0][0]
    tail = runs[-1][0]
    # Probe positions must be OUTSIDE every marker block: a probe inside one is
    # 64 identical frames, which is ambiguous and correctly rejected by
    # find_delta -- but rejecting it there would VOID a good stream merely
    # because the window landed unluckily, so pick the positions deliberately
    # using the marker mask instead of stepping blind (the latency == MARKER_PERIOD
    # self-test case is exactly that unlucky landing, 3 periods into the stream).
    is_m = (cap_L == sync_l) & (cap_R == sync_r)

    def probe_at(p, win, cen):
        """Offset at capture p, or None if it cannot be pinned. Marker content
        is skipped: it is identical frames and cannot pin anything."""
        for step in range(0, 4 * (min_run // VERIFY_FRAMES + 8)):
            q = int(min(p + step * VERIFY_FRAMES, tail - VERIFY_FRAMES - 1))
            if q <= span:
                return None
            if is_m[q:q + VERIFY_FRAMES].any():
                continue
            d = find_delta(cap_L[q:q + VERIFY_FRAMES],
                           cap_R[q:q + VERIFY_FRAMES], schedule_vec,
                           cen + (q - p), win)
            if d is not None:
                return d - q
        return None

    # First offset: unambiguous, from just after the head marker run.
    #
    # PAST THE END, not min_run past the START. The head is the marker block the
    # player holds and repeats while capture opens, so it is one contiguous run
    # that can be hundreds of thousands of frames long. Probing at
    # start + min_run lands inside it, every probe is identical frames, every
    # probe is (correctly) rejected as ambiguous, and a perfectly good stream is
    # refused. The self-test caught this: only the latency=1 case passed, and it
    # passed only because it has no head to sit in.
    head_end = runs[0][0] + runs[0][1]
    delta = None
    for try_off in range(0, 8 * VERIFY_FRAMES, VERIFY_FRAMES):
        delta = probe_at(head_end + min_run + try_off, WIDE_WINDOW,
                         centre_guess + head_end + min_run + try_off)
        if delta is not None:
            break
    if delta is None:
        res["reason"] = ("no unambiguous offset found after the head marker; "
                         "refusing to count errors against an unproven latency")
        return res

    def verdict_at(q, dlt):
        """Direct content test at capture q against offset dlt.

        True  : capture[q:q+VF] IS schedule[q+dlt:]
        False : it is not
        None  : cannot tell (probe landed in marker content, which matches no
                particular offset because every frame in it is identical)

        No search, so this is cheap enough to call from a bisection -- which is
        what makes a slip boundary frame-accurate instead of probe-accurate.
        """
        q = int(q)
        if q < 0 or q + VERIFY_FRAMES > len(cap_L):
            return False
        if is_m[q:q + VERIFY_FRAMES].any():
            return None
        idx = np.arange(q + dlt, q + dlt + VERIFY_FRAMES, dtype=np.uint64)
        eL, eR = schedule_vec(idx)
        return (np.array_equal(eL.astype(np.int64), cap_L[q:q + VERIFY_FRAMES])
                and np.array_equal(eR.astype(np.int64), cap_R[q:q + VERIFY_FRAMES]))

    def refine_boundary(lo, hi, old_d, new_d):
        """Narrow the interval containing an offset change to a frame boundary.

        Returns (safe_end, safe_start): capture[safe_end:] under old_d and
        capture[:safe_start] under new_d are the parts whose offset we actually
        PROVED. Everything between them is DISCARDED -- never scored.

        Two probes PROBE_STRIDE apart can straddle a dropped packet, so without
        this the whole stride between them is scored against whichever offset the
        walk happens to hold: one 48-frame dropout would then be charged to the
        device as tens of thousands of content defects, and charged in whichever
        direction the guess went wrong. Guessing is not available here -- a
        region is scored only if its offset was verified by content.

        The self-test proved this mattered: with a 64-step guard that simply
        stopped, the leftover of a 48-frame drop was scored under the new offset
        and reported as 40 content defects, and a 96-frame drop as 24. A dropped
        packet must be reported as the slip and nothing else.
        """
        # If either anchor fails, we cannot say where the offset changed, so the
        # WHOLE interval between the two probes is discarded: region N ends at lo,
        # region N+1 starts at hi. Returning (lo, lo) instead -- which an earlier
        # version did -- would leave [lo, hi) scored under the NEW offset when its
        # content is still the old one, manufacturing defects out of our own
        # inability to see. Unprovable means discard, never guess.
        if verdict_at(lo, old_d) is not True:
            return lo, hi, False
        if verdict_at(hi, new_d) is not True:
            return lo, hi, False
        left, right = lo, hi
        guard = 0
        # Each step halves the interval, so log2(PROBE_STRIDE) ~ 16 iterations
        # suffice; the guard is a tripwire, and tripping it costs discarded
        # frames -- which is honest -- never a wrong defect count.
        while right - left > 1 and guard < 64:
            guard += 1
            mid = (left + right) // 2
            if verdict_at(mid, old_d) is True:
                left = mid
                continue
            if verdict_at(mid, new_d) is True:
                right = mid
                continue
            # Matches neither offset (marker content, or a second fault here).
            # Shrink from this end rather than assume; if the interval never
            # closes, the leftover is discarded by construction.
            if verdict_at(mid + 1, new_d) is True:
                right = mid + 1
            elif verdict_at(mid - 1, old_d) is True:
                left = mid - 1
            else:
                break
        # The third value says whether BOTH sides of the slip were bracketed by
        # content -- i.e. whether a change of offset was actually observed, which
        # is what separates a real dropout from a boundary of our own probing.
        # Band width is NOT part of that test. The self-test caught the first
        # version demanding <= VERIFY_FRAMES, which failed its own injected
        # 48-frame drop (band 128) and would have VOIDED every genuine dropout on
        # the device, whose slips run 48..96 frames. The band is simply discarded
        # and reported: a wide band costs measurement coverage, never a verdict.
        return left, right, True

    # Walk forward at a fixed stride, closing a region whenever the offset moves.
    #
    # The first region starts PAST the head marker run for the same reason the
    # first probe does: the head is repeated marker content that is not part of
    # the steady schedule, so scoring it would charge the device for frames the
    # player itself held and replayed.
    regions = []
    slips = []
    uncertain = 0
    n_probe_calls = 1          # the first offset search
    cur_from = head_end + min_run
    cur_delta = delta
    last_good = cur_from
    prev_p = cur_from
    p = cur_from + PROBE_STRIDE
    while p < tail - VERIFY_FRAMES - 1:
        # Local search first: once the offset is known, the next one is within a
        # few frames, and a wide search at every probe would cost a schedule
        # expansion of 4M frames per 64K frames measured. Widen only on a miss,
        # and stop the region rather than guess across a gap we cannot see.
        n_probe_calls += 1
        d = probe_at(p, window, cur_delta + p)
        if d is None:
            n_probe_calls += 1
            d = probe_at(p, WIDE_WINDOW, cur_delta + p)
            if d is None:
                break
        if d != cur_delta:
            lo, hi, provable = refine_boundary(prev_p, p, cur_delta, d)
            slips.append({"at_capture": int(hi),
                          "frames_lost": int(d - cur_delta),
                          "direction": "gap" if d > cur_delta else "dup",
                          "verified": bool(provable),
                          "unresolved_frames": int(hi - lo)})
            regions.append((cur_from, lo, cur_delta))
            uncertain += hi - lo
            cur_from, cur_delta = hi, d
        last_good = p + VERIFY_FRAMES
        prev_p = p
        p += PROBE_STRIDE
    regions.append((cur_from, last_good, cur_delta))
    regions = [(a, b, d) for (a, b, d) in regions if b > a + VERIFY_FRAMES]
    if not regions:
        res["reason"] = "no region long enough to score"
        return res

    res["regions"] = regions
    res["slips"] = slips
    # Unverified offset changes are a measurement failure, not a defect. If the
    # whole capture is one clean stream except for boundaries we could not
    # bracket, the honest verdict is VOID, not FAIL with a defect count of 0.
    res["slips_unverified"] = sum(1 for s in slips if not s["verified"])
    res["slip_band_frames"] = int(sum(s.get("unresolved_frames", 0)
                                      for s in slips))
    res["probes"] = int(n_probe_calls)
    res["uncertain_discarded"] = int(uncertain)
    delta = regions[0][2]
    res["delta"] = int(delta)
    res["latency_frames_out_to_in"] = int(delta)
    res["latency_ms_out_to_in"] = 1000.0 * delta / rate

    # Step 3: what to keep. Only verified regions are scoreable; the head (the
    # marker block the player holds and repeats while capture opens) and the
    # tail after the last probe are DISCARDED, and their size is reported. A rig
    # that reaches a low error rate by throwing away most of the data must be
    # seen doing it.
    res["head_discarded"] = int(regions[0][0])
    res["tail_discarded"] = int(max(0, len(cap_L) - regions[-1][1]))
    scored = sum(b - a for (a, b, _d) in regions)
    res["aligned_frames"] = int(scored)
    res["aligned_fraction"] = scored / max(1, len(cap_L))
    if res["aligned_fraction"] < MIN_ALIGNED_FRACTION:
        res["reason"] = (f"only {res['aligned_fraction']:.2%} of captured frames "
                         f"fall inside a verified region; scoring a sliver is "
                         f"not a measurement")
        return res
    res["verdict"] = "ALIGNED"
    return res


def score(cap_L, cap_R, schedule_vec, regions, max_report=20):
    """Step 4, and only ever step 4.

    Strict per-frame comparison inside each verified region against THAT
    region's own offset. No re-anchoring: a mismatch is a defect, full stop.
    Run-length distribution is reported because one USB packet at 384 kHz is 48
    frames, so run length tells a packet drop apart from a scattered bit error.
    """
    out = {"checked": 0, "defects": 0, "reports": [], "zeros": 0,
           "run_histogram": []}
    all_bad = []
    for (first, last, delta) in regions:
        # Sliced for the same reason as the probe search: a 30-minute region is
        # 691 M frames, and two int64 schedules over it is ~11 GB.
        for s in range(first, last, _SCORE_SLICE):
            e = min(s + _SCORE_SLICE, last)
            n = e - s
            idx = np.arange(s + delta, e + delta, dtype=np.uint64)
            eL, eR = schedule_vec(idx)
            eL = eL.astype(np.int64)
            eR = eR.astype(np.int64)
            gotL = np.asarray(cap_L[s:e], dtype=np.int64)
            gotR = np.asarray(cap_R[s:e], dtype=np.int64)
            out["checked"] += int(n)
            # Zero fields, counted over the WHOLE region and not only over
            # defects: random noise is zero in all 24 bits about once in 16 M
            # frames, so a starved buffer emitting zeros by the packet is orders
            # of magnitude off the noise floor and the distinction is evidence.
            out["zeros"] += int((gotL == 0).sum() + (gotR == 0).sum())
            ok = (gotL == eL) & (gotR == eR)
            bad = np.nonzero(~ok)[0]
            out["defects"] += int(len(bad))
            if len(bad):
                all_bad.append((s, bad))
                room = max(0, max_report - len(out["reports"]))
                for j in bad[:room]:
                    out["reports"].append((int(s + j), int(gotL[j]),
                                           int(gotR[j]), int(eL[j]),
                                           int(eR[j])))
    if all_bad:
        # Run LENGTHS. An earlier version used bad[0]+1 for the first run, which
        # is its POSITION in the region, not its length -- so an injected 48-frame
        # packet at offset 1.6 M reported a "run" of 1656297 and the histogram
        # that exists to separate a packet drop from a scattered bit error was
        # meaningless. Runs are delimited by edges (where consecutive bad frames
        # stop being consecutive); each run is the span between delimiters.
        lengths = []
        for (_first, bad) in all_bad:
            if not len(bad):
                continue
            d = np.diff(bad)
            edges = np.nonzero(d != 1)[0]
            starts = np.concatenate(([0], edges + 1))
            ends = np.concatenate((edges, [len(bad) - 1]))
            lengths.extend((bad[ends] - bad[starts] + 1).tolist())
        vals, cnts = np.unique(np.array(lengths), return_counts=True)
        out["run_histogram"] = sorted(zip(vals.tolist(), cnts.tolist()),
                                      key=lambda t: -t[1])[:12]
        out["first_bad_capture_pos"] = int(all_bad[0][0] + all_bad[0][1][0])
    return out


# ================ self-test: does the aligner itself work? ==================
def self_test(schedule_vec, mk_block, marker_period, rate=384000,
              latency=803000, inject=None, window=None, slip=None):
    """Feed the aligner a SYNTHETIC capture with a KNOWN latency and KNOWN
    faults; require it to recover both. A tool that cannot reproduce a
    known-truth case cannot be trusted on an unknown one.

    Direction matters, and getting it backwards once made this test fail for a
    reason unrelated to the aligner: the host writes AHEAD of the device, so
    capture frame i is schedule frame (i + delta).
    """
    n_markers = 8
    n = (n_markers + 2) * marker_period
    top = n + latency
    sL, sR = schedule_vec(np.arange(top, dtype=np.uint64))
    sL = sL.astype(np.int64)
    sR = sR.astype(np.int64)
    for b in range(top // marker_period + 1):
        s = b * marker_period
        e = min(s + len(mk_block), top)
        sL[s:e], sR[s:e] = mk_block[:e - s, 0], mk_block[:e - s, 1]

    cap_L = sL[latency:latency + n].copy()
    cap_R = sR[latency:latency + n].copy()

    # The head: the player holds and REPEATS the marker block until capture is
    # reading, so that region is marker content that is not part of the steady
    # schedule. Reproduce it so the aligner has to discard it.
    head = min(latency, 3 * marker_period)
    for s in range(0, head, len(mk_block)):
        e = min(s + len(mk_block), head)
        cap_L[s:e], cap_R[s:e] = mk_block[:e - s, 0], mk_block[:e - s, 1]

    # A DROPPED packet is modelled by DELETING frames from the capture, which is
    # what the device actually does: the content after the gap arrives earlier
    # relative to the schedule, so the offset moves. Zeroing them instead (as
    # `inject` does) is a different fault -- corrupted content, same timing --
    # and conflating the two is why a slip used to be reported as 48 subst.
    if slip:
        spos, slen = slip
        if spos + slen > n:
            return False, "slip injection past end of synthetic"
        keep = np.ones(n, dtype=bool)
        keep[spos:spos + slen] = False
        cap_L = cap_L[keep].copy()
        cap_R = cap_R[keep].copy()

    injected = 0
    for name, (pos, ln) in (inject or {}).items():
        if pos + ln > n:
            return False, f"injection {name} past end of synthetic"
        cap_L[pos:pos + ln] = 0
        cap_R[pos:pos + ln] = 0
        injected += ln

    a = align(cap_L, cap_R, schedule_vec, mk_block[0, 0], mk_block[0, 1], rate,
              centre_guess=latency, window=window or marker_period * 4,
              marker_period=marker_period)
    if a["verdict"] != "ALIGNED":
        return False, f"align refused a valid stream: {a['reason']}"
    if a["delta"] != latency:
        return False, (f"latency recovered {a['delta']} != injected {latency} "
                       f"(off by {a['delta'] - latency})")
    sc = score(cap_L, cap_R, schedule_vec, a["regions"])
    if slip:
        spos, slen = slip
        if len(a["slips"]) != 1:
            return False, (f"injected a {slen}-frame drop, found "
                           f"{len(a['slips'])} slip(s): {a['slips'][:4]}")
        g = a["slips"][0]
        if abs(g["frames_lost"]) != slen:
            return False, (f"slip measured {g['frames_lost']} frames, "
                           f"injected {slen}")
        # A pure drop must be reported as the slip ITSELF, not as slen content
        # defects: the frames are gone, not wrong. Double-counting them as both
        # would make the drop look like two different failures.
        if sc["defects"]:
            return False, (f"pure drop scored {sc['defects']} content defects; "
                           f"a dropped packet is not corrupted content")
        return True, (f"delta={a['delta']}, slip={g}, regions={len(a['regions'])}, "
                      f"checked={sc['checked']:,}, defects=0")
    if injected:
        if sc["defects"] != injected:
            return False, (f"injected {injected} bad frames, scored "
                           f"{sc['defects']}")
        lens = [t[0] for t in sc["run_histogram"]]
        missing = [ln for (_, ln) in (inject or {}).values() if ln not in lens]
        if missing:
            return False, (f"run histogram {sc['run_histogram']} missed injected "
                           f"run lengths {missing}")
    elif sc["defects"]:
        return False, f"clean synthetic stream scored {sc['defects']} defects"
    return True, (f"delta={a['delta']} (injected {latency}), "
                  f"markers={a['markers']} spacing={a['spacing']['mode']}, "
                  f"aligned={a['aligned_fraction']:.1%}, "
                  f"checked={sc['checked']:,}, defects={sc['defects']}, "
                  f"injected={injected}")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import csd_signal as sig
    import bit_perfect_noise as B

    sig.RATE = 384000
    mp, ml = B.MARKER_PERIOD, B.MARKER_LEN
    mk = np.zeros((ml, 2), dtype=np.int64)
    mk[:, 0], mk[:, 1] = sig.SYNC_L, sig.SYNC_R
    rc = 0
    for label, kw in (
        ("clean stream, latency 803000", {}),
        ("clean stream, latency 1 frame", {"latency": 1}),
        ("clean stream, latency 1 period", {"latency": mp}),
        ("one 48-frame packet zeroed",
         {"inject": {"pkt": (mp * 3 + 100000, 48)}}),
        ("two zeroed runs (48 + 24)",
         {"inject": {"pkt": (mp * 3 + 100000, 48),
                     "short": (mp * 5 + 200000, 24)}}),
        ("one 48-frame packet DROPPED",
         {"slip": (mp * 3 + 100000, 48)}),
        ("one 96-frame slip (2 microframes)",
         {"slip": (mp * 4 + 50000, 96)}),
    ):
        ok, msg = self_test(B.schedule_vec, mk, mp, **kw)
        print(f"{'PASS' if ok else 'FAIL':4s}  {label:34s} {msg}")
        rc |= 0 if ok else 1
    sys.exit(rc)
