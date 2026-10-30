// csd-proto.js -- ClearSoundDac SCPI-over-USB-MIDI protocol core.
//
// Ported 1:1 from csd_diag.py, which is verified against the real board
// (14/14 acceptance matrix) and against real libscpi through the native
// harness. Rules that hardware forced and this port MUST keep:
//
//   * byte0 of an event = (cable << 4) | CIN; cable 0 only.
//   * A REJECTED command is SILENT (SCPI_RES_ERR emits no SysEx). Observe it
//     through SYSTem:ERRor:COUNt? / SYSTem:ERRor? -- never by awaiting a reply.
//   * Register replies are RAW base64 (SCPI_ResultCharacters), no quotes.
//   * The final event's CIN is not trusted: the USB Audio spec and the
//     vendor's producer disagree on 1- and 2-byte tails, so the message end
//     is decided by LOCATING 0xF7 in the event payload.
//   * base64 validation is strict: only the canonical alphabet, mandatory
//     padding, padding only at the very end. Error text ('-151,...' with a
//     comma/minus) must fail validation, never decode as payload.

export const VID = 0x20b1;
export const PID = 0x0018;

export const SYSEX_START = 0xf0;
export const SYSEX_END = 0xf7;
export const MANUFACTURER_ID = 0x7e;
export const CABLE_NUMBER = 0;
export const USB_PACKET_SIZE = 64;

export const CIN_SYSEX_3 = 0x4;       // SysEx starts / continues: 3 payload bytes
export const CIN_SYSEX_END_2 = 0x5;   // ends, 2 payload bytes (spec Table 4-7)
export const CIN_SYSEX_END_1 = 0x6;   // ends, 1 payload byte  (spec Table 4-7)
export const CIN_SYSEX_FULL3 = 0x7;   // ends, 3 payload bytes

export const CSD_REG_MAX = 64;        // device cap: addresses per READ, bytes per WRITE / 2
export const DEFAULT_TIMEOUT = 2.0;

export class ProtoError extends Error {}
export class TimeoutError extends ProtoError {}

// ---- SysEx build / parse --------------------------------------------------

export function buildSysex(command) {
  // Wrap one SCPI line as F0 <id> <ascii> F7. CR/LF stripped; >0x7F rejected
  // client-side (the device parser would drop the message).
  const stripped = String(command).replace(/\r/g, "").replace(/\n/g, "");
  const bytes = new Uint8Array(stripped.length);
  for (let i = 0; i < stripped.length; i++) {
    const c = stripped.charCodeAt(i);
    if (c > 0x7f) throw new ProtoError("SCPI command must be 7-bit ASCII");
    bytes[i] = c;
  }
  const out = new Uint8Array(bytes.length + 3);
  out[0] = SYSEX_START;
  out[1] = MANUFACTURER_ID;
  out.set(bytes, 2);
  out[out.length - 1] = SYSEX_END;
  return out;
}

export function packEvents(sysex) {
  // Split a complete SysEx into 4-byte USB-MIDI events, spec final CINs
  // (mirrors csd_diag.pack_events, which the board accepts).
  const events = [];
  let i = 0;
  const n = sysex.length;
  while (i < n) {
    const rem = n - i;
    let chunk, cin;
    if (rem > 3) {
      chunk = sysex.subarray(i, i + 3);
      cin = CIN_SYSEX_3;
      i += 3;
    } else {
      chunk = sysex.subarray(i);
      cin = { 1: CIN_SYSEX_END_1, 2: CIN_SYSEX_END_2, 3: CIN_SYSEX_FULL3 }[rem];
      i = n;
    }
    const ev = new Uint8Array(4);
    ev[0] = (CABLE_NUMBER << 4) | cin;
    ev.set(chunk, 1); // zero pad beyond chunk
    events.push(ev);
  }
  return events;
}

export function packPackets(sysex, maxPacket = USB_PACKET_SIZE) {
  const per = Math.max(1, Math.floor(maxPacket / 4));
  const events = packEvents(sysex);
  const out = [];
  for (let i = 0; i < events.length; i += per) {
    const group = events.slice(i, i + per);
    const joined = new Uint8Array(group.length * 4);
    group.forEach((e, k) => joined.set(e, k * 4));
    out.push(joined);
  }
  return out;
}

export function parseSysex(msg) {
  if (!msg || msg.length < 4 || msg[0] !== SYSEX_START ||
      msg[msg.length - 1] !== SYSEX_END) {
    throw new ProtoError("not a SysEx message");
  }
  if (msg[1] !== MANUFACTURER_ID) {
    throw new ProtoError(`unexpected manufacturer ID: 0x${msg[1].toString(16)}`);
  }
  let text = "";
  for (let i = 2; i < msg.length - 1; i++) text += String.fromCharCode(msg[i]);
  return text;
}

// ---- base64 (strict, matching the device decoder's contract) --------------

const B64_ALPHABET =
  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

export function encodeB64(bytes) {
  let out = "";
  for (let i = 0; i < bytes.length; i += 3) {
    const a = bytes[i];
    const b = i + 1 < bytes.length ? bytes[i + 1] : 0;
    const c = i + 2 < bytes.length ? bytes[i + 2] : 0;
    const acc = (a << 16) | (b << 8) | c;
    out += B64_ALPHABET[(acc >> 18) & 0x3f];
    out += B64_ALPHABET[(acc >> 12) & 0x3f];
    out += i + 1 < bytes.length ? B64_ALPHABET[(acc >> 6) & 0x3f] : "=";
    out += i + 2 < bytes.length ? B64_ALPHABET[acc & 0x3f] : "=";
  }
  return out;
}

export function decodeB64Strict(text) {
  // Same acceptance rule as the device's b64_decode: length % 4 == 0,
  // canonical alphabet, '=' only in the trailing positions of the FINAL
  // group. This is deliberate strictness, not redundancy: reply text must be
  // a payload, and error-shaped text ('-151,Invalid string data') has to be
  // rejected here so a rejected command can never decode as data.
  if (typeof text !== "string" || text.length === 0 || text.length % 4 !== 0) {
    throw new ProtoError("base64: empty or length not a multiple of 4");
  }
  let pad = 0;
  if (text[text.length - 1] === "=") {
    pad++;
    if (text[text.length - 2] === "=") pad++;
  }
  const val = (c) => {
    const k = B64_ALPHABET.indexOf(c);
    return k < 0 ? -1 : k;
  };
  const out = new Uint8Array(((text.length / 4) * 3) - pad);
  let n = 0;
  for (let i = 0; i < text.length; i += 4) {
    const last = i + 4 === text.length;
    let acc = 0;
    for (let k = 0; k < 4; k++) {
      const c = text[i + k];
      if (c === "=") {
        if (!last || k < 4 - pad) throw new ProtoError("base64: misplaced padding");
        acc <<= 6;
        continue;
      }
      const v = val(c);
      if (v < 0) throw new ProtoError(`base64: non-alphabet character ${JSON.stringify(c)}`);
      acc = (acc << 6) | v;
    }
    const emit = last ? 3 - pad : 3;
    for (let k = 0; k < emit; k++) out[n++] = (acc >> (16 - 8 * k)) & 0xff;
  }
  return out;
}

// ---- USB-MIDI event parsing (reply direction) ------------------------------

export class UsbMidiEventParser {
  // Tolerates events split across arbitrary read boundaries and accepts any
  // final CIN 0x5/0x6/0x7, locating F7 in the payload to end the message --
  // the spec and vendor conventions disagree for short tails and the device
  // uses the vendor one (payload bytes counting F7 = CIN - 4).
  constructor() {
    this._pending = [];   // bytes not yet forming a full 4-byte event
    this._msg = [];       // SysEx payload accumulating
    this._inSysex = false;
  }

  feed(data) {
    for (const b of data) this._pending.push(b);
    const out = [];
    while (this._pending.length >= 4) {
      const ev = this._pending.splice(0, 4);
      const msg = this._feedEvent(ev);
      if (msg) out.push(msg);
    }
    return out;
  }

  _feedEvent(ev) {
    const cable = (ev[0] >> 4) & 0x0f;
    const cin = ev[0] & 0x0f;
    if (cable !== CABLE_NUMBER ||
        ![CIN_SYSEX_3, CIN_SYSEX_END_1, CIN_SYSEX_END_2, CIN_SYSEX_FULL3].includes(cin)) {
      return null;
    }
    const data = ev.slice(1);
    if (cin === CIN_SYSEX_3) {
      if (!this._inSysex) {
        if (data[0] !== SYSEX_START) return null;  // continuation without start
        this._msg = [];
        this._inSysex = true;
      }
      this._msg.push(...data);
      return null;
    }
    if (!this._inSysex) return null;
    const f7 = data.indexOf(SYSEX_END);
    if (f7 >= 0) {
      this._msg.push(...data.slice(0, f7 + 1));
    } else {
      // malformed final event: take the bytes before zero padding, stay open
      let end = data.length;
      while (end > 0 && data[end - 1] === 0x00) end--;
      this._msg.push(...data.slice(0, end));
    }
    // complete at the first F7 (a stray F0 later restarts the message)
    for (let idx = 0; idx < this._msg.length; idx++) {
      const b = this._msg[idx];
      if (b === SYSEX_END) {
        const msg = Uint8Array.from(this._msg.slice(0, idx + 1));
        this._msg = [];
        this._inSysex = false;
        return msg;
      }
      if (b === SYSEX_START && idx > 0) {
        this._msg = this._msg.slice(idx);   // restart, keep new header
        break;
      }
    }
    return null;
  }
}

export class SysexCollector {
  // Reassembles raw MIDI SysEx from a byte stream -- the kernel rawmidi path,
  // where the USB-MIDI framing has already been stripped. Real-time bytes
  // (>= 0xF8) may legally interleave and are skipped; a stray F0 restarts.
  constructor() { this._buf = null; }

  feed(data) {
    const out = [];
    for (const b of data) {
      if (b === SYSEX_START) {
        this._buf = [b];                       // restart on a stray F0
      } else if (this._buf !== null) {
        if (b === SYSEX_END) {
          this._buf.push(b);
          out.push(Uint8Array.from(this._buf));
          this._buf = null;
        } else if (b >= 0xf8) {
          /* realtime: interleaved, skip */
        } else {
          // Inside SysEx every other byte is payload, including 0x80..0xEF
          // such as the manufacturer ID.
          this._buf.push(b);
          if (this._buf.length > 1024) this._buf = null;   // runaway guard
        }
      }
    }
    return out;
  }

  flush() {
    // The kernel's rawmidi node hands over one SysEx per read batch and DROPS
    // the terminating F7 (observed directly: a write of F0 7E '*IDN?' F7 came
    // back as 34 bytes ending '0d 0a' with no F7). So for this transport
    // end-of-read means end-of-message, and waiting for F7 would hang until
    // the timeout. F7 is restored here so parseSysex still validates the frame.
    const out = [];
    if (this._buf !== null && this._buf.length > 1) {
      this._buf.push(SYSEX_END);
      out.push(Uint8Array.from(this._buf));
    }
    this._buf = null;
    return out;
  }

  pending() { return this._buf !== null && this._buf.length > 1; }
}

// ---- error-reply classification --------------------------------------------

export function isErrorReply(text) {
  return /^-\d+,/.test(text) || text.startsWith("ERROR");
}

// ---- client core (transport-agnostic; mirrors CsdDiag) ----------------------

// Bound a browser permission promise that may never settle.
//
// Measured in Chromium on a plain http page: navigator.requestMIDIAccess
// neither resolves nor rejects -- it stays pending indefinitely, with
// sysex:true and sysex:false alike. navigator.usb.requestDevice has the same
// shape (it waits on a user-chosen picker). A pending promise never reaches a
// catch, so a bare await freezes the console with no message at all: every
// browser permission call needs a bound.
export class PermissionTimeoutError extends Error {}

export function withTimeout(promise, ms, what) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(
        () => reject(new PermissionTimeoutError(
          `${what} did not settle within ${ms} ms`)), ms);
    }),
  ]).finally(() => clearTimeout(timer));
}

export class CsdDiag {
  // transport: { send(bytes) -> Promise, recv(maxBytes, timeoutSec) ->
  //              Promise<Uint8Array> (may be empty), close() }
  // framing: "usb-midi" (raw bulk endpoints, 4-byte USB-MIDI events) or
  //          "raw" (kernel rawmidi node / Web MIDI, plain MIDI bytes).
  // The two need different reassembly AND different end-of-message rules, so
  // picking the wrong one looks like a dead board.
  constructor(transport, framing = "usb-midi") {
    this._t = transport;
    this._framing = framing;
    this._parser = framing === "raw" ? new SysexCollector() : new UsbMidiEventParser();
  }

  async _flush() {
    // Drop stale bytes from a previous exchange. Swallows errors on purpose:
    // a cancelled bulk read can report a transport error rather than a
    // timeout, and the first query of a session must not die here.
    for (;;) {
      let chunk;
      try {
        chunk = await this._t.recv(4096, 0);
      } catch (_) {
        return;
      }
      if (!chunk || chunk.length === 0) return;
    }
  }

  async sendCommand(command) {
    const sysex = buildSysex(command);
    if (this._framing === "raw") {
      await this._t.send(sysex);               // the node already frames events
      return;
    }
    for (const p of packPackets(sysex)) await this._t.send(p);
  }

  async readReply(timeout = DEFAULT_TIMEOUT, raiseError = true) {
    // The raw path needs a quiesce window: rawmidi delivers one SysEx per read
    // batch and omits the F7, so "no more bytes for a moment" is what closes a
    // message there. Kept separate from the overall timeout so a slow device
    // still gets its full budget on the first chunk.
    const QUIESCE = 0.05;
    const deadline = Date.now() + timeout * 1000;
    const take = (msg) => {
      let text;
      try {
        text = parseSysex(msg);
      } catch (_) {
        return undefined;           // ignore foreign SysEx
      }
      if (raiseError && isErrorReply(text)) {
        const err = new ProtoError(text);
        err.isDeviceError = true;
        throw err;
      }
      return text;
    };
    for (;;) {
      const remaining = (deadline - Date.now()) / 1000;
      if (remaining <= 0) throw new TimeoutError("no reply within timeout");
      let chunk;
      try {
        chunk = await this._t.recv(this._framing === "raw" ? 256 : USB_PACKET_SIZE,
                                   remaining);
      } catch (e) {
        // Treat transport hiccups as "nothing yet" until the deadline, as the
        // Python client does: an empty read is not "no reply".
        await new Promise((r) => setTimeout(r, 2));
        continue;
      }
      for (const msg of this._parser.feed(chunk || new Uint8Array())) {
        const t = take(msg);
        if (t !== undefined) return t;
      }
      if (chunk && this._framing === "raw" && this._parser.pending()) {
        // Drain until quiet, then close the message.
        for (;;) {
          const r = await this._t.recv(256, QUIESCE);
          if (!r || !r.length) break;
          for (const msg of this._parser.feed(r)) {
            const t = take(msg);
            if (t !== undefined) return t;
          }
        }
        for (const msg of this._parser.flush()) {
          const t = take(msg);
          if (t !== undefined) return t;
        }
      }
    }
  }

  async query(command, timeout = DEFAULT_TIMEOUT, raiseError = true) {
    await this._flush();
    await this.sendCommand(command);
    return this.readReply(timeout, raiseError);
  }

  async command(command) {
    await this._flush();
    await this.sendCommand(command);   // set-commands are silent: no wait
  }

  async close() {
    // Release the transport. The caller opened it, but leaving no way back
    // means a reconnect in the same page session cannot releaseInterface
    // cleanly, which on WebUSB strands the board until the tab closes.
    if (this._t && typeof this._t.close === "function") await this._t.close();
  }

  // ---- error queue (the ONLY observable for a rejected command) ----

  async errorCount() {
    return (await this.query("SYSTem:ERRor:COUNt?", DEFAULT_TIMEOUT, false)).trim();
  }

  async errorNext() {
    // Positional discipline matters: query(command, timeout, raiseError).
    // Passing `false` where the Python original used the keyword
    // raise_error=False put a boolean into the TIMEOUT slot, which made
    // every error-queue read time out instantly. The queue is the only
    // observable for a rejected command, so that single slip fails every
    // rejection check in the routine -- and would have looked like a dead
    // board on the bench.
    return (await this.query("SYSTem:ERRor:NEXT?", DEFAULT_TIMEOUT, false)).trim();
  }

  async drainErrors() {
    for (;;) {
      const t = await this.errorNext();
      if (t.startsWith("0,")) return;
    }
  }

  async queuedError(cmd) {
    // Send a command that must be REJECTED and read back what the device
    // queued. The rejection itself is silent; awaiting a reply for it is the
    // classic trap -- it looks identical to a dead transport.
    await this.errorNext();            // drain
    await this.command(cmd);
    const depth = await this.errorCount();
    const text = await this.errorNext();
    return { depth, text };
  }

  // ---- codec registers ----

  async regRead(addrs, timeout = DEFAULT_TIMEOUT) {
    const list = typeof addrs === "number" ? [addrs] : Array.from(addrs);
    for (const a of list) {
      if (!(a >= 0 && a <= 0xff)) {
        throw new ProtoError(`register address 0x${a.toString(16)} out of range`);
      }
    }
    if (list.length === 0 || list.length > CSD_REG_MAX) {
      throw new ProtoError(`bad address count ${list.length} (1..${CSD_REG_MAX})`);
    }
    const payload = encodeB64(Uint8Array.from(list));
    const reply = await this.query(`REGister:READ? "${payload}"`, timeout);
    let data;
    try {
      data = decodeB64Strict(reply.trim());
    } catch (e) {
      throw new ProtoError(`register reply is not base64: ${e.message} (${JSON.stringify(reply)})`);
    }
    if (data.length !== list.length) {
      throw new ProtoError(`register reply short: asked ${list.length}, got ${data.length}`);
    }
    return data;
  }

  async regWrite(pairs) {
    const items = pairs instanceof Map ? [...pairs.entries()]
      : Array.isArray(pairs) ? pairs
      : Object.entries(pairs).map(([k, v]) => [Number(k), Number(v)]);
    const buf = [];
    for (const [a, v] of items) {
      if (!(a >= 0 && a <= 0xff)) throw new ProtoError(`address 0x${a.toString(16)} out of range`);
      if (!(v >= 0 && v <= 0xff)) throw new ProtoError(`value 0x${v.toString(16)} out of range`);
      buf.push(a, v);
    }
    if (buf.length === 0 || buf.length > 2 * CSD_REG_MAX) {
      throw new ProtoError(`payload ${buf.length} bytes out of range`);
    }
    await this.command(`REGister:WRITE "${encodeB64(Uint8Array.from(buf))}"`);
  }

  async regReadMap(lo, hi) {
    if (!(lo >= 0 && lo <= hi && hi <= 0xff)) throw new ProtoError("bad range");
    const addrs = [];
    for (let a = lo; a <= hi; a++) addrs.push(a);
    const vals = await this.regRead(addrs);
    const map = new Map();
    addrs.forEach((a, i) => map.set(a, vals[i]));
    return map;
  }
}
