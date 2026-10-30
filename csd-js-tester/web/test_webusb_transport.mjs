// Drives WebUsbTransport against a fake navigator.usb, so the code I wrote
// (endpoint discovery, the transferIn pump, the recv queue, send padding,
// close) is actually executed. What this cannot prove: that Chrome opens this
// specific device -- only a browser on the machine the board is plugged into
// can answer that. It is deliberately NOT a substitute for the bench run.

import { WebUsbTransport } from "./csd-transport-webusb.js";
import { buildSysex, packPackets, VID, PID, SYSEX_START, SYSEX_END } from "./csd-proto.js";

let bad = 0;
const ok = (cond, name, extra = "") => {
  console.log(`  ${cond ? "PASS" : "FAIL"}  ${name}${extra ? "  " + extra : ""}`);
  if (!cond) bad++;
};

// ---- fake USB stack -------------------------------------------------------
function makeFakeDevice({ midiIface = 4, epOut = 0x02, epIn = 0x83,
                            log = [], audioBulk = false } = {}) {
  const state = { opened: false, claimed: [], released: [], closed: false,
                  out: [], inQueue: [], clearHalt: 0, inRequestSizes: [] };
  const endpoints = (nOut, nIn) => ([
    { type: "bulk", direction: "out", endpointNumber: nOut },
    { type: "bulk", direction: "in", endpointNumber: nIn },
  ]);
  // The board's audio endpoints are ISOC (verified), so a bulk pair there is
  // a synthetic case: discovery must still ignore it.
  const isocEndpoints = (nOut, nIn) => ([
    { type: "isochronous", direction: "out", endpointNumber: nOut },
    { type: "isochronous", direction: "in", endpointNumber: nIn },
  ]);
  const bulkEndpoints = endpoints;
  const dev = {
    vendorId: VID, productId: PID, configuration: null, _state: state, _log: log,
    configurations: [
      // decoy: a bulk pair on an interface that is NOT MIDI streaming, to
      // prove the class-aware pass wins rather than the first pair found
      { interfaces: [
        { interfaceNumber: 1, interfaceClass: 0x01, interfaceSubclass: 0x02,
          alternates: [{ endpoints: audioBulk ? bulkEndpoints(0x01, 0x82)
                                              : isocEndpoints(0x01, 0x82) }] },
        { interfaceNumber: midiIface, interfaceClass: 0x01, interfaceSubclass: 0x03,
          alternates: [{ endpoints: endpoints(epOut, epIn) }] },
      ] },
    ],
    async open() { state.opened = true; },
    async selectConfiguration(c) { dev.configuration = c; },
    async claimInterface(i) {
      if (log.includes("claimThrows")) throw new Error("interface busy (kernel driver holds it)");
      state.claimed.push(i);
    },
    async releaseInterface(i) { state.released.push(i); },
    async close() { state.closed = true; },
    async transferOut(ep, data) {
      if (ep !== epOut) throw new Error(`transferOut to wrong endpoint 0x${ep.toString(16)}`);
      state.out.push(new Uint8Array(data));
      return { status: "ok" };
    },
    async transferIn(ep, len) {
      state.inRequestSizes.push(len);
      if (ep !== epIn) throw new Error(`transferIn from wrong endpoint 0x${ep.toString(16)}`);
      if (state.inQueue.length) return { status: "ok", data: state.inQueue.shift() };
      await new Promise((r) => setTimeout(r, 5));
      return { status: "ok", data: new Uint8Array(0) };
    },
    async clearHalt() { state.clearHalt++; return { status: "ok" }; },
    _push(bytes) { state.inQueue.push(new Uint8Array(bytes)); },
  };
  return dev;
}

// Node >=21 ships a read-only `navigator` global, so plain assignment throws
// ("Cannot set property navigator ... which has only a getter"). defineProperty
// replaces the accessor for the duration of the test.
function setNavigator(value) {
  Object.defineProperty(globalThis, "navigator", {
    value, writable: true, configurable: true, enumerable: true,
  });
}

function installNavigator(dev, { getDevices = true } = {}) {
  setNavigator({
    usb: {
      getDevices: async () => (getDevices ? [dev] : []),
      requestDevice: async () => dev,
    },
  });
}

// ---- tests ---------------------------------------------------------------
{
  const dev = makeFakeDevice({});
  installNavigator(dev);
  const t = await WebUsbTransport.open({});
  ok(dev._state.opened, "device opened");
  ok(dev.configuration === 1, "configuration selected");
  ok(dev._state.claimed.includes(4),
     "claims the MIDI-streaming interface 4 (subclass 0x03), never the audio interface",
     `claimed=${JSON.stringify(dev._state.claimed)}`);
  await new Promise((r) => setTimeout(r, 30));   // let the pump issue a transfer
  ok(dev._state.inRequestSizes.some((n) => n >= 512),
     "pump requests the endpoint's 512-byte max packet, not a 64-byte guess",
     `sizes=${JSON.stringify(dev._state.inRequestSizes)}`);

  // send: a command must reach the bulk OUT endpoint as 4-byte-aligned events
  await t.send(packPackets(buildSysex("*IDN?"))[0]);
  const sent = dev._state.out[0];
  ok(!!sent && sent.length % 4 === 0, "send() emits whole 4-byte events",
     `len=${sent ? sent.length : "none"}`);
  ok(sent[0] === 0x04, "first event is cable 0 / CIN 4 (SysEx continue)",
     `byte0=0x${sent[0].toString(16)}`);
  ok(sent[1] === SYSEX_START, "payload opens with F0");

  // send: an unaligned tail must be zero-padded, never sent as a partial event
  await t.send(new Uint8Array([1, 2, 3]));
  const padded = dev._state.out[1];
  ok(padded.length === 4 && padded[3] === 0, "short write padded to a whole event");

  // recv: pump-delivered bytes surface exactly once, in order
  const reply = packPackets(buildSysex("0")).reduce((a, p) => {
    const j = new Uint8Array(a.length + p.length); j.set(a); j.set(p, a.length); return j;
  }, new Uint8Array(0));
  dev._push(reply);
  await new Promise((r) => setTimeout(r, 30));        // let the pump run
  const got = await t.recv(64, 1);
  ok(got.length === reply.length, "recv returns the queued packet");
  const again = await t.recv(64, 0.1);
  ok(again.length === 0, "a packet is not delivered twice");

  // recv with nothing queued returns empty rather than hanging past its budget
  const t0 = Date.now();
  const empty = await t.recv(64, 0.2);
  ok(empty.length === 0 && Date.now() - t0 < 1500, "empty recv respects its timeout");

  await t.close();
  ok(dev._state.released.includes(4) && dev._state.closed,
     "close() releases the interface and closes the device");
}

// endpoint discovery must fail loudly when the descriptors carry no bulk pair
{
  const dev = makeFakeDevice({});
  dev.configurations = [{ interfaces: [{ interfaceNumber: 0, interfaceClass: 1,
                                        interfaceSubclass: 1, alternates: [{ endpoints: [] }] }] }];
  installNavigator(dev);
  let msg = "";
  try { await WebUsbTransport.open({}); } catch (e) { msg = e.message; }
  ok(/no USB-MIDI Streaming interface/i.test(msg),
     "refuses a device with no MIDI Streaming interface", msg);
}

// a claim failure must surface the kernel-driver guidance, not a bare error
{
  const dev = makeFakeDevice({ log: ["claimThrows"] });
  installNavigator(dev);
  let msg = "";
  try { await WebUsbTransport.open({}); } catch (e) { msg = e.message; }
  ok(/kernel driver|unbind/i.test(msg),
     "claim failure explains the snd-usb-audio unbind fix", msg.split("\n")[0]);
  ok(dev._state.closed, "device closed after a failed claim (no leaked handle)");
}

// no WebUSB at all (Firefox/Safari) must say so rather than throw deep inside
{
  globalThis.navigator = {};
  let msg = "";
  try { await WebUsbTransport.open({}); } catch (e) { msg = e.message; }
  ok(/WebUSB is not available/i.test(msg), "missing WebUSB reported clearly", msg);
}

// An AudioStreaming interface that happens to expose a bulk in+out pair must
// still be refused: only the MIDI Streaming subclass may be claimed. This is
// the regression the removed "any bulk pair" fallback would have permitted.
{
  const dev = makeFakeDevice({ audioBulk: true });
  installNavigator(dev);
  const t = await WebUsbTransport.open({});
  ok(dev._state.claimed.length === 1 && dev._state.claimed[0] === 4,
     "audio interface with BULK endpoints is still not claimed",
     `claimed=${JSON.stringify(dev._state.claimed)}`);
  await t.close();
}

console.log(bad ? `\nFAILED ${bad}` : "\nALL PASS");
process.exit(bad ? 1 : 0);
