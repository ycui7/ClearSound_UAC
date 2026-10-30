import { buildSysex, packEvents, packPackets, SYSEX_END, CSD_REG_MAX,
         SYSEX_START, MANUFACTURER_ID } from "./csd-proto.js";

// The device model shared by the web tests. Its behaviour is transcribed from
// the firmware contract that the 14/14 hardware acceptance matrix pins:
// register replies are RAW base64, a rejected command is SILENT and only
// queued, -113 unknown header, -151 undecodable payload, -224 bad preset,
// writes applied in order with no rollback.
//
// It is a stand-in for the wire only. It cannot validate the WebUSB/Web MIDI
// transports and is not a substitute for running index.html on the board.
function quotedArg(up, cmd, header) {
  // Returns [payload] when `up` starts with `header` followed by a quoted
  // string, else null. The payload comes from `cmd`, not `up`, so its base64
  // case is preserved.
  const u = up.trim();
  if (!u.startsWith(header)) return null;
  const rest = cmd.trim().slice(header.length).trim();
  const m = /^"([A-Za-z0-9+/=]*)"$/.exec(rest);
  // Index 1, not 0: the call sites read m[1] exactly as they did for the
  // regex match this replaces, so the shape has to match or the payload
  // silently becomes undefined and every register call dies in the decoder.
  return m ? [m[0], m[1]] : null;
}

export class MockDevice {
  constructor() {
    this.regs = new Map([[0x00, 0x5a], [0x03, 0x30]]);
    this.preset = 0;
    this.errors = [];
    this.commands = [];
  }

  _b64encode(bytes) {
    const A = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let out = "";
    for (let i = 0; i < bytes.length; i += 3) {
      const a = bytes[i], b = bytes[i + 1] ?? 0, c = bytes[i + 2] ?? 0;
      const acc = (a << 16) | (b << 8) | c;
      out += A[(acc >> 18) & 63] + A[(acc >> 12) & 63];
      out += i + 1 < bytes.length ? A[(acc >> 6) & 63] : "=";
      out += i + 2 < bytes.length ? A[acc & 63] : "=";
    }
    return out;
  }

  _b64decodeStrict(t) {
    // Mirrors b64_decode in csd_diag_scpi.c: length % 4 == 0, canonical
    // alphabet, '=' only in the trailing positions of the FINAL group.
    const A = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    if (!t.length || t.length % 4 !== 0) return null;
    let pad = 0;
    if (t[t.length - 1] === "=") { pad++; if (t[t.length - 2] === "=") pad++; }
    const out = [];
    for (let i = 0; i < t.length; i += 4) {
      const last = i + 4 === t.length;
      let acc = 0;
      for (let k = 0; k < 4; k++) {
        const c = t[i + k];
        if (c === "=") { if (!last || k < 4 - pad) return null; acc <<= 6; continue; }
        const v = A.indexOf(c);
        if (v < 0) return null;
        acc = (acc << 6) | v;
      }
      const emit = last ? 3 - pad : 3;
      for (let k = 0; k < emit; k++) out.push((acc >> (16 - 8 * k)) & 0xff);
    }
    return out;
  }

  push(code, msg) { this.errors.push(`-${code},"${msg}"`); }

  // Returns reply text, or null for SILENT (which is what a rejection does).
  handle(cmd) {
    this.commands.push(cmd);
    const up = cmd.toUpperCase().trim();

    if (up === "*IDN?") return "ClearSound,CSD-MOCK,00000000,1.0";
    if (up === "*OPC?") return "1";

    if (up === "SYSTEM:ERROR?" || up === "SYSTEM:ERROR:NEXT?") {
      return this.errors.length ? this.errors.shift() : '0,"No error"';
    }
    if (up === "SYSTEM:ERROR:COUNT?") return String(this.errors.length);

    let m = /^DIAG:DSP:PRESET\?*(?:\s+(\S+))?$/.exec(up);
    if (m) {
      if (m[1] === undefined) return String(this.preset);
      const v = Number(m[1]);
      if (!Number.isInteger(v) || v > 2) { this.push(224, "Illegal parameter value"); return null; }
      this.preset = v;
      return null;                      // set-command: silent
    }

    // Header compared case-insensitively, payload taken from the ORIGINAL
    // command: base64 is case-sensitive, so it must never come from `up`.
    m = quotedArg(up, cmd, "REGISTER:READ?");
    if (m) {
      const addrs = this._b64decodeStrict(m[1]);
      if (!addrs || addrs.length > CSD_REG_MAX) {
        this.push(151, "Invalid string data");
        return null;                    // silent, per reg_read_q
      }
      return this._b64encode(addrs.map((a) => this.regs.get(a) ?? 0));
    }

    m = quotedArg(up, cmd, "REGISTER:WRITE");
    if (m) {
      const buf = this._b64decodeStrict(m[1]);
      if (!buf || buf.length % 2 || buf.length > 2 * CSD_REG_MAX) {
        this.push(151, "Invalid string data");
        return null;
      }
      for (let i = 0; i < buf.length; i += 2) this.regs.set(buf[i], buf[i + 1]);
      return null;                      // set-command: silent
    }

    this.push(113, "Undefined header");   // libscpi's UNDEFINED_HEADER
    return null;                          // ...and no reply, like the device
  }
}

export class MockTransport {
  constructor(dev) {
    this.dev = dev;
    this.queue = [];
    this._pending = [];              // bytes not yet forming a full 4-byte event
    this._rxCmd = [];                // SysEx payload bytes of the command in flight
  }

  async send(bytes) {
    this._pending.push(...bytes);
    while (this._pending.length >= 4) {
      const ev = this._pending.splice(0, 4);
      const cin = ev[0] & 0x0f;
      if (cin >= 4 && cin <= 7) {
        this._rxCmd.push(...ev.slice(1));
        if (ev.slice(1).includes(SYSEX_END)) { this._complete(); }
      }
    }
  }

  _complete() {
    const b = this._rxCmd;
    const f7 = b.indexOf(SYSEX_END);
    const body = b.slice(0, f7 >= 0 ? f7 + 1 : b.length);
    this._rxCmd = [];
    if (body[0] !== SYSEX_START || body[1] !== MANUFACTURER_ID) return;
    let text = "";
    for (let i = 2; i < body.length - 1; i++) text += String.fromCharCode(body[i]);
    const reply = this.dev.handle(text);
    if (reply === null) return;                // silent: nothing queued back
    // Queue SYNCHRONOUSLY. A real WebUSB transferIn pump owns its own timing
    // and is independent of the client's pre-send flush; deferring the reply
    // here put it behind the drain in _flush(), so every query timed out for
    // a reason that had nothing to do with the client.
    for (const ev of packEvents(buildSysex(reply))) this.queue.push(ev);
  }

  async recv(_n, timeout) {
    if (this.queue.length) {
      const evs = this.queue.splice(0, this.queue.length);
      const flat = new Uint8Array(evs.length * 4);
      evs.forEach((e, k) => flat.set(e, k * 4));
      return flat;
    }
    if (timeout <= 0) return new Uint8Array(0);
    // Breathe instead of spinning the client's deadline loop at full CPU
    // while a silent set-command correctly produces no reply.
    await new Promise((r) => setTimeout(r, 5));
    return new Uint8Array(0);
  }

  async close() {}
}


// transport factory: same device, two framings. "raw" exchanges plain MIDI
// bytes (what Web MIDI / rawmidi hand over); "usb-midi" exchanges 4-byte
// USB-MIDI events (what the bulk endpoints carry).
// (buildSysex/packEvents/packPackets/SYSEX_END/CSD_REG_MAX come from the
// single import at the top of this file.)
export function makeRawLikeTransport(dev, framing = "usb-midi") {
  const t = { queue: [], _pending: [], _rxCmd: [] };
  t.send = async (bytes) => {
    if (framing === "raw") {
      const b = Array.from(bytes);
      const f7 = b.indexOf(SYSEX_END);
      let text = "";
      for (let i = 2; i < (f7 >= 0 ? f7 : b.length); i++) text += String.fromCharCode(b[i]);
      const r = dev.handle(text);
      if (r !== null) t.queue.push(Array.from(buildSysex(r)));
      return;
    }
    t._pending.push(...bytes);
    while (t._pending.length >= 4) {
      const ev = t._pending.splice(0, 4);
      const cin = ev[0] & 0x0f;
      if (cin >= 4 && cin <= 7) {
        t._rxCmd.push(...ev.slice(1));
        if (ev.slice(1).includes(SYSEX_END)) {
          const b = t._rxCmd;
          const f7 = b.indexOf(SYSEX_END);
          const body = b.slice(0, f7 >= 0 ? f7 + 1 : b.length);
          t._rxCmd = [];
          let text = "";
          for (let i = 2; i < body.length - 1; i++) text += String.fromCharCode(body[i]);
          const r = dev.handle(text);
          if (r !== null) {
            for (const e of packEvents(buildSysex(r))) t.queue.push(e);
          }
        }
      }
    }
  };
  t.recv = async (_n, timeout) => {
    if (t.queue.length) {
      const items = t.queue.splice(0, t.queue.length);
      if (framing === "raw") return Uint8Array.from([].concat(...items));
      const flat = new Uint8Array(items.length * 4);
      items.forEach((e, k) => flat.set(e, k * 4));
      return flat;
    }
    if (timeout <= 0) return new Uint8Array(0);
    await new Promise((r) => setTimeout(r, 5));
    return new Uint8Array(0);
  };
  t.close = async () => {};
  return t;
}
