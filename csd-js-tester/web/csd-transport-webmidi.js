// csd-transport-webmidi.js -- Web MIDI transport for the ClearSoundDac.
//
// Two transports exist for this board because only the bench can say which an
// operator prefers, and they fail differently:
//
//   WebUSB   claims the USB-MIDI bulk endpoints (interface 4 on this board)
//            and does the USB-MIDI packetisation itself. Needs the kernel
//            driver unbound, and needs the "pair" to be chosen by class.
//   Web MIDI hands raw MIDI bytes to the browser and lets it packetise. No
//            driver fiddling, no interface claiming -- but it requires SYSEX
//            permission, and without that permission Chrome still reports the
//            port as connected and simply never delivers SysEx, so this file
//            treats missing sysex access as a hard error rather than letting a
//            query time out.
//
// A note on which Linux MIDI path works on this board, because three of them
// look interchangeable and are not:
//   * python-rtmidi / amidi -d / aseqdump talk to the ALSA *sequencer*. On
//     this device writes into the sequencer port succeed but no IN URB is ever
//     submitted, so every reply is lost -- measured, and it reads exactly like
//     a dead board.
//   * Chrome's Web MIDI uses the ALSA rawmidi API, which is the node that
//     round-trips first time (8 bytes out, 34 back). So Web MIDI is expected
//     to work; it is NOT the sequencer path above. That expectation is the
//     one thing here not yet confirmed on hardware from a browser -- if Web
//     MIDI times out where WebUSB answers, that is the finding, and the port
//     label + sysex state printed below is what tells you it is not a bug in
//     this file.
//
// Frame direction: Web MIDI delivers/parses complete SysEx as MIDIMessage.data,
// which already includes F0 and F7. It is re-emitted as a byte stream so the
// client's "raw" framing path (SysexCollector) does the reassembly, keeping one
// parsing code path shared with the rtmidi-style framing in the Python client.

import { VID, PID, withTimeout, PermissionTimeoutError } from "./csd-proto.js";

// Must stay generous: this is a prompt a human has to find and answer, so a
// short bound would break a legitimate connect that only took a moment too
// long. It is a chosen value, not a measured one. If the console ever gains a
// "waiting for permission" indicator with a Cancel button, that beats any
// fixed timeout here, because the operator then controls the wait.
export const DEFAULT_ACCESS_TIMEOUT_MS = 20000;

export class WebMidiTransport {
  constructor(inputPort, outputPort, { needle = "XMOS" } = {}) {
    this._in = inputPort;
    this._out = outputPort;
    this._needle = needle;
    this._queue = [];
    this._waiters = [];
    this._closed = false;
    this._onMessage = (ev) => {
      if (!ev || !ev.data || !ev.data.length) return;
      this._queue.push(Uint8Array.from(ev.data));
      this._wake();
    };
    this._in.onmidimessage = this._onMessage;
    this._in.onstatechange = () => {
      if (this._in.state !== "connected") this._wake();
    };
  }

  static async open({ needle = "XMOS", accessTimeoutMs = DEFAULT_ACCESS_TIMEOUT_MS } = {}) {
    if (!("requestMIDIAccess" in navigator)) {
      throw new Error("Web MIDI is not available in this browser");
    }

    // sysex:true is not optional for this protocol: every command and reply is
    // a SysEx message.
    //
    // The request is bounded because it stays pending until the operator
    // answers the permission prompt, and a pending promise never reaches a
    // catch -- an unbounded await would sit there with no message at all.
    // See DEFAULT_ACCESS_TIMEOUT_MS for why the bound must stay generous.
    let access;
    try {
      access = await withTimeout(navigator.requestMIDIAccess({ sysex: true }),
                                 accessTimeoutMs,
                                 "requestMIDIAccess");
    } catch (e) {
      if (e instanceof PermissionTimeoutError) {
        throw new Error(
          "Web MIDI permission was not answered within " + accessTimeoutMs +
          " ms, so the request is still pending. Look for a MIDI permission " +
          "prompt behind this window and allow it; if it was blocked before, " +
          "reset MIDI permission for this site and press Connect board again.");
      }
      throw new Error(`Web MIDI access denied: ${e.message}`);
    }
    if (!access.sysexEnabled) {
      throw new Error(
        "Web MIDI is available but SYSEX access was not granted, so SysEx " +
        "messages will never arrive. Revoke MIDI permission for this page " +
        "(padlock icon → MIDI devices → reset) and press Connect again, " +
        "then allow the prompt.");
    }

    const pick = (ports) => {
      const list = [...ports.values()];
      // Prefer a connected port whose label names the board; fall back to any
      // connected port so a renamed device still works rather than failing.
      const scored = list
        .filter((p) => p.state === "connected")
        .map((p) => ({
          p,
          score: (needle && p.name && p.name.toLowerCase().includes(needle.toLowerCase())) ? 0 : 1,
        }));
      scored.sort((a, b) => a.score - b.score);
      return scored.length ? scored[0].p : null;
    };

    const input = pick(access.inputs);
    const output = pick(access.outputs);
    if (!input || !output) {
      const names = (m) => [...m.values()].map((p) => `${p.name} [${p.state}]`).join(", ") || "none";
      throw new Error(
        `no connected MIDI ${!input ? "input" : "output"} port for ${JSON.stringify(needle)}.\n` +
        `inputs: ${names(access.inputs)}\noutputs: ${names(access.outputs)}\n` +
        "Check the board is enumerated (amidi -l) and not held by another " +
        "application; Web MIDI and a running ALSA client can contend.");
    }
    return new WebMidiTransport(input, output, { needle });
  }

  describe() {
    return `Web MIDI  in=${this._in.name}  out=${this._out.name}`;
  }

  async send(bytes) {
    if (this._closed) throw new Error("transport closed");
    if (this._out.state !== "connected") {
      throw new Error(`MIDI output port disconnected (${this._out.name})`);
    }
    // send() takes an iterable of raw MIDI bytes; the UA does USB-MIDI
    // packetisation, so no event framing is added here.
    this._out.send(Array.from(bytes));
  }

  async recv(_maxBytes, timeoutSec) {
    if (this._queue.length) return this._queue.shift();
    if (timeoutSec <= 0) return new Uint8Array(0);
    const got = await new Promise((resolve) => {
      let done = false;
      const timer = setTimeout(() => {
        if (done) return;
        done = true;
        const i = this._waiters.indexOf(finish);
        if (i >= 0) this._waiters.splice(i, 1);
        resolve(false);
      }, timeoutSec * 1000);
      const finish = () => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        resolve(true);
      };
      this._waiters.push(finish);
      if (this._queue.length) finish();
    });
    return this._queue.length ? this._queue.shift() : new Uint8Array(0);
  }

  _wake() {
    const w = this._waiters.shift();
    if (w) w();
  }

  async close() {
    this._closed = true;
    try { this._in.onmidimessage = null; } catch (_) {}
    this._wake();
  }
}

export { VID, PID };
