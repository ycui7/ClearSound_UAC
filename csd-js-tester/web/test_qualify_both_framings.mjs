
// The qualification routine must pass over BOTH framings. The default UI path
// is Web MIDI, which is framing "raw" -- a different receive path from the
// 4-byte USB-MIDI events -- so a routine tested only on "usb-midi" leaves the
// path the operator actually uses unexercised.
import { CsdDiag, encodeB64 } from "./csd-proto.js";
import { Qualify } from "./csd-qualify.js";
import { MockDevice, makeRawLikeTransport } from "./_mock_device.mjs";

let bad = 0;
for (const framing of ["usb-midi", "raw"]) {
  const dev = new MockDevice();
  const t = makeRawLikeTransport(dev, framing);
  const rows = [];
  const q = new Qualify(new CsdDiag(t, framing), { log: (r) => rows.push(r) });
  const res = await q.run();
  const failed = rows.filter((r) => !r.passed);
  console.log(`  ${res.failed === 0 ? "PASS" : "FAIL"}  framing=${framing}: ${res.passed}/${res.total}`);
  for (const f of failed) console.log(`        FAIL ${f.name}  ${f.value}`);
  bad += res.failed;

  // Framing must not change what the device sees: the same command under both
  // framings has to arrive as the same text. A framing bug that mangled the
  // payload would show up here as a wrong register value.
  const d2 = new MockDevice();
  const c2 = new CsdDiag(makeRawLikeTransport(d2, framing), framing);
  await c2.query("*IDN?");
  await c2.regWrite({ 0x07: 0x5c });
  const back = await c2.regRead(0x07);
  // Expected command text derived, never hardcoded: an earlier version of this
  // check asserted a literal copied from a different address/value pair, so it
  // failed while the client was actually correct.
  const expectCmd = `REGister:WRITE "${encodeB64(Uint8Array.from([0x07, 0x5c]))}"`;
  const sawWrite = d2.commands.includes(expectCmd);
  const okFraming = back[0] === 0x5c && sawWrite;
  console.log(`  ${okFraming ? "PASS" : "FAIL"}  framing=${framing}: command text reaches the device intact` +
              `  (read-back 0x${back[0].toString(16)})`);
  if (!okFraming) bad++;
  await c2.close();
}
console.log(bad ? `\nFAILED ${bad}` : "\nALL PASS");
process.exit(bad ? 1 : 0);
