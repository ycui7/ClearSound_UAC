// Runs the whole JS client + qualification routine in Node against a device
// model whose behaviour is transcribed from the firmware contract that the
// 14/14 hardware acceptance matrix pins: register replies RAW base64, a
// rejected command is SILENT and only queued, -113 for unknown headers, -151
// for undecodable payloads, -224 for a bad preset, writes applied in order.
//
// What this proves: the client's send/receive plumbing, the SysEx reassembly,
// the error-queue contract, timeout behaviour, the set-command silence rule,
// and every Qualify check. It does NOT prove the WebUSB transport -- there is
// no USB here -- and it is not a substitute for running index.html on the
// board. The wire bytes themselves are covered separately by
// tools/compare_js_client.py against the verified Python client.

import { CsdDiag, buildSysex, packEvents, SYSEX_START, SYSEX_END,
         MANUFACTURER_ID, CSD_REG_MAX } from "./csd-proto.js";
import { Qualify, roundTripCheck } from "./csd-qualify.js";
import { MockDevice, MockTransport } from "./_mock_device.mjs";


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
async function main() {
  let bad = 0;

  // 0. pure helper, no transport
  const rt = roundTripCheck();
  console.log(`  ${rt.ok ? "ok  " : "FAIL"}  base64 round-trip ${rt.cases ?? rt.why}`);
  if (!rt.ok) bad++;

  // 1. full qualification routine
  const dev = new MockDevice();
  const client = new CsdDiag(new MockTransport(dev));
  const rows = [];
  const q = new Qualify(client, { log: (r) => {
    rows.push(r);
    console.log(`  ${r.passed ? "PASS" : "FAIL"}  ${r.name}  ${r.value ?? ""}`);
  } });
  let res;
  try { res = await q.run(); }
  catch (e) {
    console.log("  ABORT " + (e.name || "") + ": " + e.message);
    console.log("  mock saw commands: " + JSON.stringify(dev.commands.slice(-6)));
    console.log("  mock error queue   : " + JSON.stringify(dev.errors));
    console.log("  mock pending events: " + dev._pendingEv + " rxCmd=" + JSON.stringify(dev._rxLen));
    res = { passed: rows.filter(r => r.passed).length, total: rows.length, failed: 1 };
  }
  bad += res.failed;

  // 2. the behaviour the routine cannot assert by itself: a rejected command
  //    must emit NO bytes at all. If the device ever answered with the error
  //    text, tests written against a reply would go green on a dead board.
  client._t.queue = [];
  await client.command("NONSENSE:CMD?");
  const silence = client._t.queue.length === 0;
  console.log(`  ${silence ? "PASS" : "FAIL"}  rejected command produced no bytes`);
  if (!silence) bad++;

  // 3. set-commands are silent: awaiting one must time out, not hang forever
  const t0 = Date.now();
  let timedOut = false;
  try { await client.query("DIAG:DSP:PRESet 1", 0.3); }
  catch (e) { timedOut = e.name === "TimeoutError" || /timeout/i.test(e.message); }
  const quick = Date.now() - t0 < 2000;
  console.log(`  ${timedOut && quick ? "PASS" : "FAIL"}  set-command times out promptly (${Date.now() - t0} ms)`);
  if (!(timedOut && quick)) bad++;

  // 4. read-back after write, and the read-only chip id
  await client.regWrite({ 0x05: 0xa5 });
  const back = await client.regRead(0x05);
  console.log(`  ${back[0] === 0xa5 ? "PASS" : "FAIL"}  write/read agreement 0x05=0xa5`);
  if (back[0] !== 0xa5) bad++;
  const chip = await client.regRead(0x00);
  console.log(`  ${chip[0] === 0x5a ? "PASS" : "FAIL"}  chip id reg reads 0x5a`);
  if (chip[0] !== 0x5a) bad++;

  // 5. 64-address dump spans several 64-byte replies and stays aligned
  const addrs = []; for (let a = 0; a < 64; a++) addrs.push(a);
  const dump = await client.regRead(addrs);
  console.log(`  ${dump.length === 64 ? "PASS" : "FAIL"}  64-register dump returns 64 bytes`);
  if (dump.length !== 64) bad++;

  client.close();
  console.log(bad ? `\nFAILED ${bad}` : "\nALL PASS");
  process.exit(bad ? 1 : 0);
}

main();
