"""Differential check: does the JS port put the SAME bytes on the wire as the
Python client, and parse the same replies?

The Python client is verified against the board (14/14 acceptance matrix), so
it is the oracle here. A port that merely "looks right" is the failure mode
that hid every earlier bug in this project -- the simulator agreed with the
tests while the device rejected everything. So this compares actual bytes.

Run: python3 tools/compare_js_client.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), "web")
sys.path.insert(0, os.path.dirname(HERE))

import csd_diag as C  # noqa: E402

CASES = [
    "*IDN?", "*OPC?", "SYSTem:ERRor:COUNt?", "SYSTem:ERRor?",
    "SYSTem:ERRor:NEXT?", "DIAG:DSP:PRESet?", "DIAG:DSP:PRESet 0",
    "DIAG:DSP:PRESet 2", "DIAG:DSP:PRESet 7", "CONFigure:CURRent?",
    'REGister:READ? "AA=="', 'REGister:READ? "Aw=="',
    'REGister:READ? "AEA="', 'REGister:READ? "AAECAw=="',
    'REGister:READ? "AAECAwQFBgcICQoL"',
    'REGister:WRITE "Ayo="', 'REGister:WRITE "AAECAwQF"',
    'REGister:READ? "Aw"',            # malformed: same bytes, device refuses
    'REGister:READ? ""',
    "REGister:READ? 0x03",            # dead legacy syntax
]


def python_wire(cmd):
    """Exact bytes the verified Python client would send."""
    sysex = C.build_sysex(cmd)
    return [bytes(p) for p in C.pack_packets(sysex)]


JS_DRIVER = """
import { buildSysex, packPackets, packEvents, parseSysex, decodeB64Strict,
         encodeB64, UsbMidiEventParser, isErrorReply } from %s;
const cases = JSON.parse(process.argv[2]);
const out = { wire: {}, reply: {}, b64: {}, err: {} };
for (const c of cases) {
  out.wire[c] = packPackets(buildSysex(c)).map(p => Array.from(p));
}
// reply parsing: feed the same SysEx the device emits, in awkward chunks
const replyTexts = ["ClearSound,CSD-22,00000000,1.0", "0", "AAECAwQF",
                    "-113,Undefined header", "-151,Invalid string data"];
for (const t of replyTexts) {
  const evs = packEvents(buildSysex(t));
  const flat = [].concat(...evs.map(e => Array.from(e)));
  // split at 3 and at 5 bytes: events must survive arbitrary boundaries
  const p1 = new UsbMidiEventParser();
  const a = [];
  for (let i = 0; i < flat.length; i += 3) a.push(...p1.feed(flat.slice(i, i+3)));
  const p2 = new UsbMidiEventParser();
  const b = [];
  for (let i = 0; i < flat.length; i += 5) b.push(...p2.feed(flat.slice(i, i+5)));
  out.reply[t] = { chunk3: a.map(m => parseSysex(m)), chunk5: b.map(m => parseSysex(m)),
                   isError: isErrorReply(t) };
}
// base64: every payload length the protocol carries, plus reject cases
out.b64.enc = {};
for (let n = 1; n <= 64; n++) {
  const b = new Uint8Array(n);
  for (let i = 0; i < n; i++) b[i] = (i * 37 + n) & 0xff;
  out.b64.enc[n] = encodeB64(b);
}
out.b64.reject = ["-151,Invalid string data", "Aw", "A===", "AA=A", "", "a b"].map(s => {
  try { decodeB64Strict(s); return "ACCEPTED"; } catch (e) { return "rejected"; }
});
console.log(JSON.stringify(out));
"""


def js_wire():
    # The driver must sit in web/ next to csd-proto.js: an ES module's
    # relative import resolves against the FILE's own directory, not the
    # process cwd, so a driver in /tmp looks for csd-proto.js in /tmp.
    driver = os.path.join(WEB, "csd_js_driver.mjs")
    with open(driver, "w") as f:
        f.write(JS_DRIVER % json.dumps("./csd-proto.js"))
    try:
        r = subprocess.run(["node", driver, json.dumps(CASES)], cwd=WEB,
                           capture_output=True, text=True)
    finally:
        os.remove(driver)      # generated artifact, never left in the tree
    if r.returncode != 0:
        raise SystemExit("node driver failed:\n%s\n%s" % (r.stdout, r.stderr))
    return json.loads(r.stdout)


def main():
    js = js_wire()
    fails = []

    # 1. wire bytes must be identical to the verified Python client
    for cmd in CASES:
        want = [list(p) for p in python_wire(cmd)]
        got = js["wire"].get(cmd)
        if got != want:
            fails.append("WIRE %r\n   python=%s\n   js    =%s" % (cmd, want, got))

    # 2. reply reassembly: chunked feeds must yield exactly one message and
    #    the original text, at two different chunk boundaries
    for text, info in js["reply"].items():
        for key in ("chunk3", "chunk5"):
            msgs = info[key]
            if len(msgs) != 1 or msgs[0] != text:
                fails.append("REPLY %r %s -> %r (want [%r])" % (text, key, msgs, text))
        want_err = bool(__import__("re").match(r"^-\d+,", text))
        if info["isError"] != want_err:
            fails.append("ISERROR %r -> %s (want %s)" % (text, info["isError"], want_err))

    # 3. base64 encode must match Python's stdlib exactly for every length
    import base64
    for n, enc in js["b64"]["enc"].items():
        b = bytes(((i * 37 + int(n)) & 0xff) for i in range(int(n)))
        if enc != base64.b64encode(b).decode():
            fails.append("B64ENC n=%s js=%s python=%s"
                         % (n, enc, base64.b64encode(b).decode()))

    # 4. error-shaped and malformed text must be rejected, never decoded
    for s, verdict in zip(["-151,Invalid string data", "Aw", "A===", "AA=A", "", "a b"],
                          js["b64"]["reject"]):
        # stdlib agrees on these: unpadded, misplaced padding, empty, space
        try:
            base64.b64decode(s, validate=True)
            accepted_by_python = True
        except Exception:
            accepted_by_python = False
        if accepted_by_python:
            continue     # not a case both sides must refuse
        if verdict != "rejected":
            fails.append("B64REJECT %r -> %s (must reject)" % (s, verdict))

    total = len(CASES) * 1 + len(js["reply"]) * 3 + len(js["b64"]["enc"]) + 6
    if fails:
        print("FAIL %d/%d" % (len(fails), total))
        for f in fails[:20]:
            print("  " + f)
        return 1
    print("PASS %d checks: wire bytes identical to the verified Python client "
          "for %d commands, replies reassembled at 2 chunk boundaries, "
          "64 base64 payload lengths match stdlib, malformed input rejected"
          % (total, len(CASES)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
