// csd-transport-webusb.js -- WebUSB bulk transport for the ClearSoundDac
// MIDI data interface (VID 0x20b1 PID 0x0018).
//
// Why WebUSB and not Web MIDI: this board's replies arrive only on the raw
// bulk endpoints. The ALSA sequencer path (which is what Web MIDI uses on
// Linux) accepts writes and returns zero bytes -- measured with amidi,
// aseqdump and python-rtmidi alike -- so a Web MIDI app would time out on
// every query and look like a dead board. The bulk path is the one verified
// against hardware (14/14 acceptance matrix).
//
// Bench requirement: the kernel driver must not hold the MIDI interface. On
// Linux snd-usb-audio claims it; unbind first:
//   echo -n '2-1' | sudo tee /sys/bus/usb/drivers/usb/unbind   (then rebind)
// or boot with the quirk. Chrome reports "interface busy" / claim failure
// otherwise, and that message is surfaced verbatim rather than hidden.

import { VID, PID } from "./csd-proto.js";

export class WebUsbTransport {
  constructor(device, { iface, out, in: epIn, maxPacket = 512 } = {}) {
    this._d = device;
    this._iface = iface;
    this._epOut = out;
    this._epIn = epIn;
    // Request the endpoint's full max packet, not 64. The board's MIDI
    // endpoints declare wMaxPacketSize 512 (verified with
    // tools/dump_midi_desc.py); the firmware sends 4-byte events, so 64 has
    // always sufficed in practice, but a short request buffer risks truncating
    // a reply on a device that packs several events into one packet. Reading
    // more is always safe: UsbMidiEventParser tolerates any byte boundary.
    this._maxPacket = maxPacket;
    this._queue = [];
    this._waiters = [];
    this._pump = true;
    this._closed = false;
  }

  static async open({ vendorId = VID, productId = PID } = {}) {
    if (!("usb" in navigator)) {
      throw new Error("WebUSB is not available in this browser (needs Chromium-based over http(s) or localhost)");
    }
    let device;
    const existing = await navigator.usb.getDevices();
    device = existing.find((d) => d.vendorId === vendorId && d.productId === productId);
    if (!device) {
      device = await navigator.usb.requestDevice({
        filters: [{ vendorId, productId }],
      });
    }
    await device.open();
    if (device.configuration === null) await device.selectConfiguration(1);

    // Locate the MIDI bulk pair by class+subclass (see findMidiInterface).
    // Not by CS_ENDPOINT subtype: measured on the board, the audio isochronous
    // endpoints carry CS_ENDPOINT subtype 0x01 too, so subtype alone cannot
    // tell MIDI from audio.
    const { number: iface, out: epOut, in: epIn } = findMidiInterface(device);

    try {
      await device.claimInterface(iface);
    } catch (e) {
      await device.close();
      throw new Error(
        `cannot claim interface ${iface}: ${e.message}\n` +
        "The kernel driver almost certainly holds it. Unbind snd-usb-audio " +
        "from this device first (see the header comment in " +
        "csd-transport-webusb.js), then reconnect.");
    }
    const t = new WebUsbTransport(device, { iface, out: epOut, in: epIn });
    t._startPump();
    return t;
  }

  static async connectExisting() {
    // No user gesture: only devices already granted to this origin.
    const devs = await navigator.usb.getDevices();
    return devs.find((d) => d.vendorId === VID && d.productId === PID) || null;
  }

  _startPump() {
    // One continuous transferIn loop feeding a queue. Doing the read per-recv
    // would need cancellation on timeout, and a cancelled bulk transfer on
    // this device has previously produced spurious transport errors.
    const loop = async () => {
      while (this._pump && !this._closed) {
        try {
          const r = await this._d.transferIn(this._epIn, this._maxPacket);
          if (r.status === "ok" && r.data && r.data.byteLength) {
            this._queue.push(new Uint8Array(r.data.buffer.slice(0)));
            this._wake();
          } else if (r.status === "stall") {
            await this._d.clearHalt("in", this._epIn);
          }
        } catch (e) {
          if (this._closed) return;
          await new Promise((r) => setTimeout(r, 20));
        }
      }
    };
    loop();
  }

  _wake() {
    const w = this._waiters.shift();
    if (w) w();
  }

  async send(bytes) {
    if (this._closed) throw new Error("transport closed");
    // USB-MIDI events are 4-byte aligned; pad a short tail so the device
    // never sees a partial event.
    let data = bytes;
    if (data.length % 4 !== 0) {
      data = new Uint8Array(Math.ceil(data.length / 4) * 4);
      data.set(bytes);
    }
    const r = await this._d.transferOut(this._epOut, data);
    if (r.status !== "ok") throw new Error(`transferOut status ${r.status}`);
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
    if (!got) return new Uint8Array(0);
    return this._queue.length ? this._queue.shift() : new Uint8Array(0);
  }

  async close() {
    this._closed = true;
    this._pump = false;
    this._wake();
    try { await this._d.releaseInterface(this._iface); } catch (_) {}
    try { await this._d.close(); } catch (_) {}
  }
}

function findMidiInterface(device) {
  // Verified against the live board with tools/dump_midi_desc.py (raw
  // /sys/bus/usb/devices/*/descriptors walk):
  //   interface 4, class 0x01 subclass 0x03 (MIDI Streaming), bulk
  //   0x02 OUT + 0x83 IN @512 B, each followed by CS_ENDPOINT (bType 0x25)
  //   subtype 0x01 (MS_GENERAL): 05 25 01 01 01 / 05 25 01 01 03.
  //   The audio interfaces (1 and 2, subclass 0x02) expose 0x01/0x82/0x81 as
  //   ISOC @392 B, not bulk -- and they carry CS_ENDPOINT subtype 0x01 as well
  //   (08 25 01 00 00 00 00 00). Subtype therefore cannot separate MIDI from
  //   audio; class+subclass can, which is why it is the rule used here.
  //
  // Keying on class+subclass is the authoritative test for MIDI Streaming and
  // cannot be confused with AudioStreaming. The earlier version also accepted
  // "any interface with a bulk in+out pair" as a fallback; that is removed,
  // because a guessed interface means writing SysEx into whatever pipe we
  // happened to claim -- a wrong claim is far more expensive to diagnose than
  // a refusal that names the missing subclass.
  const AUDIO_CLASS = 0x01;
  const MIDI_SUBCLASS = 0x03;
  for (const cfg of device.configurations) {
    for (const itf of cfg.interfaces) {
      if (itf.interfaceClass !== AUDIO_CLASS || itf.interfaceSubclass !== MIDI_SUBCLASS) continue;
      let out = null, inEp = null;
      for (const alt of itf.alternates) {
        for (const ep of alt.endpoints) {
          if (ep.type !== "bulk") continue;
          if (ep.direction === "out") out = ep.endpointNumber;
          else inEp = ep.endpointNumber;
        }
      }
      if (out !== null && inEp !== null) {
        return { number: itf.interfaceNumber, out, in: inEp };
      }
    }
  }
  throw new Error(
    "no USB-MIDI Streaming interface (class 01 subclass 03) with a bulk " +
    "endpoint pair found; refusing to claim another interface instead");
}
