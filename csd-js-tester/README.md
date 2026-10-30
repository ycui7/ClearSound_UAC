# csd-diag — SCPI-over-USB-MIDI diagnostics for the ClearSound device

Host-side Python client (`csd_diag.py`) plus in-process fake device
(`firmware_sim.py`) and unit tests (`test_csd_diag.py`) for the SCPI
diagnostic interface carried inside class-compliant USB-MIDI SysEx.

The device exposes the owner-approved diagnostic command set:
`*IDN?`, `SYSTem:ERRor[:NEXT]?`, `SYSTem:ERRor:COUNt?`,
`DIAG:DSP:PRESet[ <0|1|2>]`, `REGister:READ? <hex>`,
`REGister:WRITE <hex>,<hex>`. Everything else answers −201.

## Quick start (venv already provisioned)

```bash
cd <tree>   # this folder; the .venv lives here
.venv/bin/python csd_diag.py --list                 # MIDI ports / USB devices
.venv/bin/python csd_diag.py --sim '*IDN?'           # against the simulator
.venv/bin/python csd_diag.py --sim --repl            # interactive REPL
.venv/bin/python csd_diag.py --sim cmds.txt          # batch file ('?'-lines reply)
# real hardware, once it enumerates:
.venv/bin/python csd_diag.py --backend rtmidi --port XMOS '*IDN?'
.venv/bin/python csd_diag.py --backend pyusb --pid 0x0002 '*IDN?'
.venv/bin/python -m unittest -v test_csd_diag.py      # 52 tests
```

## Wire format (firmware must match this exactly)

* One SCPI line per SysEx message:
  `F0 7E <ASCII bytes, no CR/LF> F7` — single ID byte **0x7E**
  (MMA "Educational & Research"). **All bytes between F0 and F7 are
  7-bit data bytes (< 0x80)**; the vendor parser `midi_in_parse`
  (lib_xua/src/midi/midiinparse.xc) drops SysEx on any >=0x80 byte.
  The client rejects non-ASCII commands before they hit the wire.
* USB-MIDI framing (chunking starts at the F0 itself). Event byte0 =
  `(cable<<4)|CIN` — the **HIGH nibble is the CABLE NUMBER** (0), the
  **LOW nibble is the CODE INDEX NUMBER** (USB Audio 1.0 Table 4-6).
  Fixed by the vendor source, not interpretation:
  `lib_xua/src/midi/midiinparse.xc` `makeEvent()` builds
  `event = (cable_number << 28) | (codeIndexNumber << 24) | (midi0<<16)|(midi1<<8)|midi2`,
  and `lib_xua/src/midi/usb_midi.xc` (host->device path) does
  `int event = byterev(datum);` on the little-endian word read from the
  USB buffer — together: wire byte0 high nibble = cable, low nibble = CIN.
  * Host **sends** the USB Audio 1.0 Table 4-7 spec convention:
    CIN `0x4` SysEx start/continue (3 payload bytes); final event
    CIN `0x5` ends with 2 data bytes, CIN `0x6` ends with 1 data byte,
    CIN `0x7` ends with 3 data bytes (last byte = F7). Zero-padded.
  * The host **receiver accepts either convention**: any final CIN
    0x5/0x6/0x7 is tolerated and the payload length is decided by
    locating F7 in the event's bytes 1..3, not by trusting the CIN.
    This is required because the firmware reply producer uses the
    vendor's own `codeIndexNumber = received + 0x4` convention
    (`midiinparse.xc`) — i.e. 0x4+k for a k-byte tail — which
    disagrees with the spec for k=1 and k=2.
* Replies: same shape, CR/LF stripped, may span multiple 64-byte
  bulk-IN packets. Set-commands (no trailing `?`) get **no reply**.

## Backed vs stubbed commands (honesty rule)

Source of truth: `firmware_sim.BACKING`. Stubs return `-201,Undefined
header` — never an invented value; tests assert this.

| Command | Status | Notes |
|---|---|---|
| `*IDN?` | backed | `ClearSound,CSD-AN22,<serial>,<fw>` |
| `*OPC?` | backed | always `1` (no long ops) |
| `SYSTem:ERRor[:NEXT]?` / `:COUNt?` | backed | error queue |
| `DIAG:DSP:PRESet[ <0\|1\|2>]` | backed | query returns slot; set is silent; invalid value → silent −222 queued |
| `REGister:READ? "<b64>"` | backed | 8-bit register file; any address reads (unpopulated → `0x00`); bad/empty/over-64-byte payload → −222 |
| `REGister:WRITE "<b64>"` | backed | `(addr,val)` pairs, applied in order, silent; undecodable/empty/odd/over-cap payload → −222 (nothing written) |

Register payloads are **base64 in a quoted string**, one parameter, matching the
BATMCU convention (`DIAGnostic:SPI0:CS1:WRB64? "<base64>"`). Hex arguments were
measured dead on the real parser (`0x03`/`0X03` → `−101 Invalid character`),
which is why the syntax moved. Use the client helpers instead of hand-encoding:
`client.reg_read([0x00, 0x03])` and `client.reg_write({0x03: 0x2A})`.
| `REGister:WRITE <hex>,<hex>` | backed | silent; out-of-range/unknown → −223 queued, bad char → −222 queued |
| `MEASure:*` | **stub** | −201 (outside approved command set) |
| `SOURce:*` | **stub** | −201 (outside approved command set) |
| `CONFigure:*` | **stub** | −201 (outside approved command set) |
| anything else (incl. `*RST`, `*CLS`) | **stub** | −201 |

## Transports

* **rtmidi** (default when a matching ALSA port exists): kernel
  snd-usb-audio does USB-MIDI framing, client speaks raw MIDI bytes.
* **pyusb**: driverless bulk OUT/IN MIDI endpoints (descriptor walk
  finds bulk endpoints followed by CS_ENDPOINT/MS_GENERAL); this module
  does the 4-byte event framing itself.
* **sim**: `SimTransport` over `FakeDevice` pipes — the acceptance path.

## Files

* `csd_diag.py` — library (wire primitives, parsers, transports, client) + CLI
* `firmware_sim.py` — fake CSD-AN22 device + `SimTransport` + `BACKING` table
* `test_csd_diag.py` — 52 unittest cases (wire vectors, parsers, backed
  commands, honesty, CLI)
* `.venv/` — python-rtmidi + pyusb installed
