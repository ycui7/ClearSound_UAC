"""Differential test: the FIRMWARE's base64 decoder vs Python's stdlib.

Why this exists
---------------
b64_decode in src/extensions/csd_diag_scpi.c is hand-rolled (no malloc, no
libc on that core), and for a long time the only tests covering base64
register payloads ran through the PYTHON simulator, which calls stdlib
base64. That is two implementations agreeing with each other, not with the
device -- and it hid a real defect: the device's decoder zeroed its `pad`
counter after the first group, so any payload with more than one 4-char group
that ended in '=' returned -1. On hardware, 1-, 4- and 5-address register
reads timed out (the -1 makes the SCPI caller push an error and answer
nothing) while 2-, 3-, 6- and 12-address reads worked. Failure correlated
with command LENGTH only because 4- and 8-char payloads are what 1 and 4..5
addresses encode to, which sent two rounds of debugging down the USB-MIDI
framing and buffer-size paths instead.

This test closes that hole by compiling the decoder out of the firmware
source and checking it against stdlib over the whole payload space the
protocol can produce.

Extraction is by regex from the shipped .c file, so this cannot drift from
what the device runs. If the regex stops matching, the test fails loudly --
which is the point.

The one intentional deviation (empty input) is asserted, not tolerated.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

FIRMWARE_SRC = os.path.expanduser(
    "~/sw-audio-sc/sw_usb_audio/clearsound_uac/src/extensions/csd_diag_scpi.c")

# The device refuses an empty payload; stdlib accepts it as zero bytes. In
# this protocol a zero-address read is a malformed command, not a valid empty
# dump, so the device is right and the oracle must be told to expect it.
ORACLE_EXCEPTIONS = {"": 0}


def _extract(src_text, signature):
    m = re.search(r"static\s+\w+\s+" + signature + r"\(.*?\n\}\n", src_text, re.S)
    if not m:
        raise AssertionError(
            "could not extract %s() from %s -- the function was renamed or "
            "reformatted, so this test is no longer covering the shipped code"
            % (signature, FIRMWARE_SRC))
    return m.group(0)


def _build_decoder(cases, cc="gcc"):
    """Compile the firmware's b64_decode verbatim; return its per-case output."""
    with open(FIRMWARE_SRC) as f:
        src = f.read()
    body = [
        "#include <stdio.h>",
        "#include <string.h>",
        "/* b64_val / b64_decode copied verbatim from the firmware source */",
        _extract(src, "b64_val"),
        _extract(src, "b64_decode"),
        "static const char *cases[] = {",
    ]
    for c in cases:
        assert '"' not in c and "\\" not in c, "case would break generated C: %r" % c
        body.append('    "%s",' % c)
    body.append("};")
    body.append(
        "int main(void) {\n"
        "    unsigned char out[256];\n"
        "    for (size_t i = 0; i < sizeof cases / sizeof cases[0]; i++) {\n"
        "        memset(out, 0, sizeof out);\n"
        "        int n = b64_decode(cases[i], strlen(cases[i]), out, sizeof out);\n"
        "        printf(\"%zu|%d|\", i, n);\n"
        "        for (int k = 0; k < (n > 0 ? n : 0); k++) printf(\"%02x\", out[k]);\n"
        "        printf(\"\\n\");\n"
        "    }\n"
        "    return 0;\n}\n")

    tmp = tempfile.mkdtemp(prefix="csd_b64_")
    cpath = os.path.join(tmp, "b64_test.c")
    xpath = os.path.join(tmp, "b64_test")
    with open(cpath, "w") as f:
        f.write("\n".join(body))
    try:
        subprocess.run([cc, "-O1", "-o", xpath, cpath], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise unittest.SkipTest("%s not available; cannot test the device decoder" % cc)
    except subprocess.CalledProcessError as e:
        raise AssertionError("extracted decoder does not compile:\n%s"
                             % e.stderr.decode(errors="replace"))
    out = subprocess.run([xpath], check=True, stdout=subprocess.PIPE)
    return out.stdout.decode().strip().split("\n")


class B64DecodeAgainstStdlib(unittest.TestCase):
    """The shipped decoder must agree with stdlib on every payload shape the
    register protocol can emit, and must be no more permissive than stdlib."""

    @classmethod
    def setUpClass(cls):
        import base64
        cls.cases = []
        # every address-count the protocol allows (CSD_REG_MAX == 64), in both
        # a constant-byte and a distinct-byte shape, so padding position varies
        for n in range(1, 65):
            cls.cases.append(base64.b64encode(bytes([n]) * n).decode())
            cls.cases.append(base64.b64encode(bytes(range(n))).decode())
        # hand-picked shapes: single group, multi group, each padding width,
        # and inputs that must be refused (bad alphabet, misplaced padding)
        cls.cases += ["Aw==", "AA==", "WgAAMA==", "A", "Aw", "A===", "====",
                      "=A==", "AA=A", "A=A=", "Aw=A", "AAAAAw==", "Aw==AAAA",
                      "A!ECAw==", "++//", "++//++//", "////", "Aw==Aw==", ""]
        import random
        r = random.Random(20261030)          # pinned: failures must reproduce
        for _ in range(200):
            n = r.randint(1, 64)
            cls.cases.append(base64.b64encode(
                bytes(r.randrange(256) for _ in range(n))).decode())
        cls.got = _build_decoder(cls.cases)

    def _oracle(self, case):
        import base64
        if case in ORACLE_EXCEPTIONS:
            return "%d|-1|" % self.cases.index(case)   # asserted separately
        try:
            d = base64.b64decode(case, validate=True)
        except Exception:
            return None
        return d

    def test_decoder_compiles_and_covers_every_case(self):
        self.assertEqual(len(self.got), len(self.cases))

    def test_agrees_with_stdlib_on_all_valid_payloads(self):
        import base64
        bad = []
        for i, case in enumerate(self.cases):
            want = self._oracle(case)
            if isinstance(want, str):        # documented exception
                continue
            if want is None:
                # stdlib refused: the device must refuse too. Its rule is
                # stricter (canonical alphabet, mandatory padding), so a
                # stdlib rejection has to be a device rejection.
                got_n = int(self.got[i].split("|")[1])
                if got_n != -1:
                    bad.append((case, "stdlib refuses", "device got %d bytes" % got_n))
                continue
            got_n = int(self.got[i].split("|")[1])
            got_hex = self.got[i].split("|")[2] if got_n > 0 else ""
            if got_n != len(want) or got_hex != want.hex():
                bad.append((case, "%d bytes %s" % (len(want), want.hex()),
                            "%d bytes %s" % (got_n, got_hex)))
        self.assertEqual(bad, [], "device decoder disagrees with stdlib:\n%s"
                         % "\n".join(repr(b) for b in bad[:15]))

    def test_empty_payload_is_refused_not_accepted(self):
        # The single place where the device is deliberately stricter than
        # stdlib. Zero requested registers is a command error: accepting it
        # would let a host bug (empty address list) read as a successful
        # empty dump and hide a broken harness.
        i = self.cases.index("")
        self.assertEqual(int(self.got[i].split("|")[1]), -1)

    def test_multi_group_padded_payloads_decode(self):
        # The class that shipped broken: >1 group ending in '='. Regression
        # guard for the bug whose hardware symptom was a transport timeout.
        # Restricted to inputs stdlib ACCEPTS -- the hand-picked invalid ones
        # (A===, A!ECAw==) are supposed to return -1 and belong to the
        # strictness assertion above, not here.
        import base64
        victims = []
        for c in self.cases:
            if len(c) <= 4 or not c.endswith("="):
                continue
            try:
                base64.b64decode(c, validate=True)
            except Exception:
                continue
            victims.append(c)
        self.assertGreater(len(victims), 50, "test lost its coverage")
        failed = [c for c in victims
                  if int(self.got[self.cases.index(c)].split("|")[1]) == -1]
        self.assertEqual(failed, [],
                         "%d multi-group padded payloads rejected, e.g. %r"
                         % (len(failed), failed[:5]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
