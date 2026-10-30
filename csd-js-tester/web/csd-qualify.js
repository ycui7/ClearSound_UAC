// csd-qualify.js -- one-run qualification routine for a new hardware build.
//
// This is a port of hw_acceptance.py, which passes 14/14 against the board,
// including the checks that only hardware can answer. Two rules carried over
// deliberately, because both were learned the expensive way:
//
//  1. A check must never pass by receiving an error. An earlier version
//     accepted any reply "that looked like hex OR started with -", so every
//     register check passed while the device was rejecting the command. Each
//     check here states the exact expected shape, and a rejection is a FAIL.
//  2. A rejected command is SILENT. Anything expected to be refused is
//     verified through the error queue (queuedError), never by awaiting a
//     reply -- awaiting one is indistinguishable from a dead transport.

import { CSD_REG_MAX, decodeB64Strict, encodeB64, ProtoError } from "./csd-proto.js";

export class Qualify {
  constructor(client, { log = () => {} } = {}) {
    this.c = client;
    this.log = log;
  }

  async check(name, actual, okFn) {
    let passed, shown;
    try {
      passed = await okFn(actual);
    } catch (e) {
      passed = false;
      actual = `${e.name}: ${e.message}`;
    }
    shown = typeof actual === "string" ? actual : JSON.stringify(actual);
    if (shown && shown.length > 70) shown = shown.slice(0, 67) + "...";
    this.log({ name, passed, value: shown });
    return passed;
  }

  async run() {
    const r = [];
    const add = async (name, actual, ok) => r.push(await this.check(name, actual, ok));
    const c = this.c;

    // 1. *IDN? -- vendor, product, serial, version, comma separated.
    let idn;
    try { idn = (await c.query("*IDN?")).trim(); } catch (e) { idn = `ERR ${e.message}`; }
    await add("*IDN? format", idn, (s) => {
      const parts = s.split(",");
      return parts.length === 4 && parts[0].length > 0 && parts[3].length > 0;
    });

    // 2. DSP preset round-trip through all three slots, in an order that
    //    would expose a value cached from the first write.
    for (const want of [0, 2, 1, 0]) {
      await c.command(`DIAG:DSP:PRESet ${want}`);
      const back = await c.query("DIAG:DSP:PRESet?");
      await add(`PRESet ${want} readback`, back.trim(), (s) => s === String(want));
    }

    // 3. Out-of-range preset must be refused. Silent, so read the queue.
    {
      const { depth, text } = await c.queuedError("DIAG:DSP:PRESet 7");
      await add("PRESet 7 queued an error", depth, (s) => s !== "0");
      await add("PRESet 7 rejected with -224", text, (s) => s.startsWith("-224"));
      const back = await c.query("DIAG:DSP:PRESet?");
      await add("rejected preset left state unchanged", back.trim(), (s) => s === "0");
    }

    // 4. Error queue drains to empty.
    await c.drainErrors();
    await add("ERR:COUNt drained", await c.errorCount(), (s) => s === "0");

    // 5. Unknown header: queued -113, no reply.
    {
      const { depth, text } = await c.queuedError("CONFigure:CURRent?");
      await add("unknown cmd queued an error", depth, (s) => s !== "0");
      await add("unknown cmd is -113 undefined header", text, (s) => s.startsWith("-113"));
    }

    // 6. Register proxy: a range in ONE command, positionally correct.
    {
      let v;
      try { v = Array.from(await c.regRead([0x00, 0x03])); }
      catch (e) { v = `ERR ${e.message}`; }
      await add("REG READ [0x00,0x03] returns 2 bytes", v,
                (x) => Array.isArray(x) && x.length === 2);
    }

    // 7. Write then read must AGREE, not merely reply. The old test accepted
    //    any reply including an error, so a silently failing write passed.
    {
      await c.regWrite({ 0x03: 0x00 });
      const back = Array.from(await c.regRead(0x03));
      await add("REG WRITE 0x03=0 then READ agrees", back,
                (x) => x.length === 1 && x[0] === 0x00);
    }

    // 8. Multi-address read in one round trip (the reason payloads are lists).
    {
      const rng = Array.from(await c.regRead([0x00, 0x01, 0x02, 0x03]));
      await add("REG READ 4-address range returns 4 bytes", rng,
                (x) => x.length === 4);
    }

    // 9. Full 64-address dump: widest reply the protocol emits (88 base64
    //    chars, more than one 64-byte packet).
    {
      const addrs = [];
      for (let a = 0; a < CSD_REG_MAX; a++) addrs.push(a);
      const v = Array.from(await c.regRead(addrs));
      await add("REG READ 64-address dump returns 64 bytes", v, (x) => x.length === 64);
    }

    // 10. Write/read agreement across a writable span, positional. Address
    //     0x00 is excluded: it is the ES9219Q chip id and is read-only, so
    //     asserting writability there tests a property the codec lacks. Its
    //     value is recorded instead.
    {
      const chip = Array.from(await c.regRead(0x00));
      this.log({ name: "reg 0x00 (read-only chip id)", passed: true, value: `0x${chip[0].toString(16)}` });
      const span = [];
      for (let a = 0x01; a <= 0x10; a++) span.push(a);
      const pairs = {};
      for (const a of span) pairs[a] = (a * 7 + 1) & 0xff;
      await c.regWrite(pairs);
      const back = Array.from(await c.regRead(span));
      const want = span.map((a) => pairs[a]);
      const bad = span.filter((a, i) => back[i] !== want[i])
                      .map((a, i) => `0x${a.toString(16)}:got 0x${back[i].toString(16)}`);
      await add("REG pattern round-trip 0x01..0x10 positional", bad,
                (x) => Array.isArray(x) && x.length === 0);
    }

    // 11. Malformed payload must be refused with nothing written (the device
    //     decoder rejects before any I2C). Silent + queued -151.
    {
      const { depth, text } = await c.queuedError('REGister:READ? "Aw"');   // unpadded
      await add("unpadded base64 queued an error", depth, (s) => s !== "0");
      await add("rejection is -151 invalid string data", text, (s) => s.startsWith("-151"));
    }

    // 12. Over-cap payload refused (device caps at CSD_REG_MAX, refuses
    //     rather than truncating -- a truncated dump would decode as a
    //     plausible map of a codec we did not read).
    {
      const big = encodeB64(new Uint8Array(CSD_REG_MAX + 1));
      const { depth, text } = await c.queuedError(`REGister:READ? "${big}"`);
      await add(`over-cap read (${CSD_REG_MAX + 1} addrs) queued`, depth, (s) => s !== "0");
      await add("over-cap read is -151", text, (s) => s.startsWith("-151"));
    }

    // 13. The legacy hex syntax must stay dead. It is what the real lexer
    //     rejected (measured: 0x03/0X03 -> -101 Invalid character), and a
    //     simulator that answered it let 52/52 pass over a dead device.
    {
      const { depth } = await c.queuedError("REGister:READ? 0x03");
      await add("legacy hex form is not answered", depth, (s) => s !== "0");
    }

    // 14. Repeated *IDN? must be stable. This is the stale-accumulator
    //     regression check: one truncated reply used to poison every later
    //     command, which reads as an intermittent transport rather than a bug.
    {
      const got = [];
      for (let i = 0; i < 5; i++) {
        try { got.push((await c.query("*IDN?")).trim()); }
        catch (e) { got.push(`ERR ${e.message}`); }
      }
      await add("5x IDN stable", got,
                (g) => new Set(g).size === 1 && g[0].length > 0 && !g[0].startsWith("ERR"));
    }

    // 15. Repeated register reads back to back: catches reply-accumulator
    //     state carry-over on the widest reply shape.
    {
      const addrs = [];
      for (let a = 0x00; a <= 0x07; a++) addrs.push(a);
      const snaps = [];
      for (let i = 0; i < 4; i++) snaps.push(Array.from(await c.regRead(addrs)).join(","));
      await add("4x register dump stable", snaps, (s) => new Set(s).size === 1);
    }

    const failed = r.filter((x) => !x);
    return { total: r.length, passed: r.length - failed.length, failed: failed.length };
  }
}

// ---- pure helpers, unit-testable without a board ---------------------------

export function roundTripCheck(bytes) {
  // base64 encode/decode must be an exact involution over every payload the
  // protocol can carry. Runs in-browser before connecting: a browser whose
  // b64 is broken will produce register failures that look like device bugs.
  const out = [];
  for (let n = 1; n <= CSD_REG_MAX; n++) {
    const b = new Uint8Array(n);
    for (let i = 0; i < n; i++) b[i] = (i * 37 + n) & 0xff;
    const enc = encodeB64(b);
    let dec;
    try { dec = decodeB64Strict(enc); } catch (e) { return { ok: false, at: n, why: e.message }; }
    if (dec.length !== n) return { ok: false, at: n, why: `length ${dec.length} != ${n}` };
    for (let i = 0; i < n; i++) {
      if (dec[i] !== b[i]) return { ok: false, at: n, why: `byte ${i}: ${dec[i]} != ${b[i]}` };
    }
    out.push(enc);
  }
  return { ok: true, cases: out.length };
}

export { ProtoError };
