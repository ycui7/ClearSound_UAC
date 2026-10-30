// Drives WebMidiTransport against a fake Web MIDI stack, including the
// permission failure that looks like a dead board.
import { WebMidiTransport } from "./csd-transport-webmidi.js";
import { CsdDiag, buildSysex, SYSEX_START, SYSEX_END, MANUFACTURER_ID } from "./csd-proto.js";

let bad = 0;
const ok = (cond, name, extra = "") => {
  console.log(`  ${cond ? "PASS" : "FAIL"}  ${name}${extra ? "  " + extra : ""}`);
  if (!cond) bad++;
};

function setNavigator(v) {
  Object.defineProperty(globalThis, "navigator", {
    value: v, writable: true, configurable: true, enumerable: true,
  });
}

function makePort(name, state = "connected") {
  return { name, state, sent: [], onmidimessage: null, onstatechange: null,
           send(data) { this.sent.push(Array.from(data)); } };
}

function makeAccess({ inputs, outputs, sysex = true }) {
  const map = (m) => { const mm = new Map(); for (const [k, v] of Object.entries(m)) mm.set(k, v); return mm; };
  return { sysexEnabled: sysex, inputs: map(inputs), outputs: map(outputs) };
}

// ---- happy path -----------------------------------------------------------
{
  const input = makePort("XMOS (UAC2.0) MIDI 1");
  const output = makePort("XMOS (UAC2.0) MIDI 1");
  setNavigator({ requestMIDIAccess: async () =>
    makeAccess({ inputs: { "a": input }, outputs: { "b": output } }) });

  const t = await WebMidiTransport.open({ needle: "XMOS" });
  ok(true, "opens with a connected pair");
  ok(/XMOS/.test(t.describe()), "describe() names the port", t.describe());

  // send: raw MIDI bytes, NOT USB-MIDI events -- the UA does the packetising.
  await t.send(buildSysex("*IDN?"));
  const sent = output.sent[0];
  ok(sent[0] === SYSEX_START && sent[1] === MANUFACTURER_ID,
     "writes raw SysEx (F0 7E ...), no cable/CIN byte",
     sent.slice(0, 4).map((b) => b.toString(16)).join(" "));
  ok(sent[sent.length - 1] === SYSEX_END, "terminates with F7");
  ok(!sent.some((b, i) => i > 0 && i < sent.length - 1 && b === 0x04 && sent[i + 1] === SYSEX_START),
     "no USB-MIDI event header injected into the payload");

  // recv: an inbound SysEx message surfaces as bytes for the raw collector
  const reply = Array.from(buildSysex("ClearSound,CSD-MOCK,00000000,1.0"));
  input.onmidimessage({ data: Uint8Array.from(reply) });
  const got = await t.recv(256, 1);
  ok(got.length === reply.length, "recv returns the delivered message");
  ok((await t.recv(256, 0.05)).length === 0, "message is not delivered twice");

  // A full client round trip over the raw framing path, with a device stub
  // that answers like the board: replies as a complete SysEx message.
  const dev = {
    handle(text) {
      if (text === "*IDN?") return "ClearSound,CSD-MOCK,00000000,1.0";
      if (text === "DIAG:DSP:PRESet?") return "2";
      return null;                            // silent rejection
    },
  };
  const out2 = makePort("XMOS MIDI");
  const in2 = makePort("XMOS MIDI");
  in2.replyFor = null;
  const t2 = new WebMidiTransport(in2, out2);
  const origSend = out2.send.bind(out2);
  out2.send = (d) => {
    origSend(d);
    const b = Array.from(d);
    const f7 = b.indexOf(SYSEX_END);
    // Payload is indices 2..(f7-1) inclusive, i.e. i < f7. Excluding one more
    // byte drops the trailing '?', the stub stops matching, and the client
    // correctly times out on a command nothing answered -- which is exactly
    // how a silent rejection looks, so an off-by-one here reads as a bug in
    // the client rather than in the harness.
    let text = "";
    for (let i = 2; i < (f7 >= 0 ? f7 : b.length); i++) text += String.fromCharCode(b[i]);
    const r = dev.handle(text);
    if (r !== null) setImmediate(() => in2.onmidimessage({ data: Uint8Array.from(buildSysex(r)) }));
  };
  const c = new CsdDiag(t2, "raw");
  const idn = await c.query("*IDN?");
  ok(idn.trim() === "ClearSound,CSD-MOCK,00000000,1.0", "raw framing round trip works", idn);
  const p = await c.query("DIAG:DSP:PRESet?");
  ok(p.trim() === "2", "second query on the same port works", p);
  // silent rejection must time out, not return error text
  let timedOut = false;
  try { await c.query("BOGUS:CMD?", 0.4); } catch (e) { timedOut = /timeout/i.test(e.message); }
  ok(timedOut, "rejected command times out on the raw path too");
  await c.close();
}

// ---- F7 stripped by the host (the rawmidi quirk) ---------------------------
{
  // Some hosts deliver one SysEx per batch and drop the terminating F7. The
  // raw collector must close the message at end-of-read rather than hang.
  const input = makePort("XMOS MIDI");
  const output = makePort("XMOS MIDI");
  const t = new WebMidiTransport(input, output);
  const body = Array.from(buildSysex("0")).slice(0, -1);   // no F7
  input.onmidimessage({ data: Uint8Array.from(body) });
  const c = new CsdDiag(t, "raw");
  let text = null;
  try { text = await c.readReply(1.0, false); } catch (e) { text = "ERR " + e.message; }
  ok(text === "0", "message without F7 still closes (end-of-read rule)", String(text));
  await c.close();
}

// ---- permission denial: must be loud, not a silent timeout ----------------
{
  setNavigator({ requestMIDIAccess: async () =>
    makeAccess({ inputs: { a: makePort("XMOS MIDI") },
                 outputs: { b: makePort("XMOS MIDI") }, sysex: false }) });
  let msg = "";
  try { await WebMidiTransport.open({ needle: "XMOS" }); } catch (e) { msg = e.message; }
  ok(/SYSEX access was not granted/i.test(msg),
     "missing sysex grant is a hard error with remediation", msg.split(",")[0]);
}

// no ports / wrong name ------------------------------------------------------
{
  setNavigator({ requestMIDIAccess: async () =>
    makeAccess({ inputs: { a: makePort("Focusrite USB MIDI", "disconnected") },
                 outputs: { b: makePort("Focusrite USB MIDI", "disconnected") } }) });
  let msg = "";
  try { await WebMidiTransport.open({ needle: "XMOS" }); } catch (e) { msg = e.message; }
  ok(/no connected MIDI/i.test(msg) && /Focusrite/.test(msg),
     "no matching port lists what was found", msg.split("\n")[0]);
}

// selection prefers the named board over an unrelated one -------------------
{
  const xm = makePort("XMOS (UAC2.0) MIDI 1");
  const other = makePort("Focusrite USB MIDI");
  setNavigator({ requestMIDIAccess: async () =>
    makeAccess({ inputs: { z: other, a: xm }, outputs: { z: other, a: xm } }) });
  const t = await WebMidiTransport.open({ needle: "XMOS" });
  ok(/XMOS/.test(t.describe()), "picks the board by label, not list order", t.describe());
}

// no Web MIDI at all ---------------------------------------------------------
{
  setNavigator({});
  let msg = "";
  try { await WebMidiTransport.open({}); } catch (e) { msg = e.message; }
  ok(/Web MIDI is not available/i.test(msg), "missing Web MIDI reported clearly", msg);
}

// An unanswered permission prompt must REPORT, not hang.
//
// requestMIDIAccess stays pending until the operator answers the prompt, and a
// pending promise never reaches a catch -- so without a bound this test would
// simply never finish. The bound is the behaviour under test; the short
// timeout is here so the test is fast, not to assert anything about real
// prompt latency.
{
  let called = 0;
  setNavigator({ requestMIDIAccess: () => { called++; return new Promise(() => {}); } });
  let msg = "", threw = false;
  const t0 = Date.now();
  try { await WebMidiTransport.open({ needle: "XMOS", accessTimeoutMs: 50 }); }
  catch (e) { threw = true; msg = e.message; }
  const dt = Date.now() - t0;
  ok(threw, "unanswered permission prompt rejects instead of hanging");
  ok(/not answered within 50 ms/i.test(msg), "says the request went unanswered", msg.slice(0, 70));
  ok(/permission/i.test(msg) && /Connect board again/i.test(msg),
     "tells the operator what to do", msg.slice(-60));
  ok(called === 1 && dt >= 45 && dt < 4000, "bounded by the timeout, not by luck", `${dt}ms`);
}

console.log(bad ? `\nFAILED ${bad}` : "\nALL PASS");
process.exit(bad ? 1 : 0);
