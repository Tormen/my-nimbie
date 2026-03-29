# my-nimbie

CLI controller for the **Acronova Nimbie USB Plus NB21** disc autoloader on macOS.

Drives the Nimbie loader/unloader mechanism via USB HID and orchestrates batch disc
processing with configurable commands (e.g. DVD ripping, audio CD extraction, full
disc backup).

---

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [Commands](#commands)
- [Batch Flavors](#batch-flavors)
- [Configuration](#configuration)
- [Per-Disc Directory Naming](#per-disc-directory-naming)
- [Monitoring](#monitoring)
- [Error Recovery](#error-recovery)
- [Nimbie NB21 USB Protocol Reference](#nimbie-nb21-usb-protocol-reference)
  - [Device Identification](#device-identification)
  - [Endpoint Configuration](#endpoint-configuration)
  - [Command Packet Format](#command-packet-format)
  - [Command Table](#command-table)
  - [AT+ Response Codes](#at-response-codes)
  - [State Bit String Format](#state-bit-string-format)
  - [Response Sequence](#response-sequence)
  - [Timing Requirements](#timing-requirements)
  - [Mechanical Operation Sequence](#mechanical-operation-sequence)
- [Bootloader Mode](#bootloader-mode)
  - [How It Happens](#how-it-happens)
  - [Bootloader Device Identity](#bootloader-device-identity)
  - [Bootloader Commands](#bootloader-commands)
  - [Recovery Procedure](#recovery-procedure)
- [Critical Implementation Notes](#critical-implementation-notes)
  - [USB Read Timeout](#usb-read-timeout)
  - [PLACE_DISC and USB Bus Death](#place_disc-and-usb-bus-death)
  - [Dropper Retraction Delay](#dropper-retraction-delay)
  - [USB Recovery via dev.reset()](#usb-recovery-via-devreset)
  - [Disc in Closed Drive Detection](#disc-in-closed-drive-detection)
- [Diagnostic Commands](#diagnostic-commands)
- [Reference Implementations](#reference-implementations)
- [BS Utility (Windows) — Reverse Engineering](#bs-utility-windows--reverse-engineering)
  - [Binary Identity](#binary-identity)
  - [Runtime Platform: Delphi](#runtime-platform-delphi)
  - [AT+ Protocol Evidence](#at-protocol-evidence)
  - [Error Message Strings](#error-message-strings)
  - [Operational Step Labels](#operational-step-labels)
  - [Device Model Support](#device-model-support)
  - [USB Enumeration Strings](#usb-enumeration-strings)
  - [Undocumented Commands: AT+I00 and AT+I99](#undocumented-commands-ati00-and-ati99)
- [Known Issues / Crash Log](#known-issues--crash-log)
  - [LIFT_DISC AT+E09 — Batch Crash 2026-03-29](#lift_disc-ate09--batch-crash-2026-03-29)

---

## Installation

No manual installation needed. On first run, my-nimbie automatically creates a
Python virtualenv at `~/.python.venv/my-nimbie` and installs its only dependency
(`pyusb`). You need `libusb` on the system:

```bash
brew install libusb
```

Create a symlink so it is on your PATH:

```bash
ln -s /path/to/my-nimbie.py /usr/local/bin/my-nimbie
chmod +x /path/to/my-nimbie.py
```

The first run bootstraps the venv automatically. The `--create-config` command does
NOT require the venv and can be run immediately.

### Prerequisites

- macOS (uses `drutil` for tray control, `diskutil` for mount/unmount)
- Python 3.9+
- `libusb` (via Homebrew)
- An optical drive connected to the Nimbie

### USB Permissions

macOS may block new USB accessories. If my-nimbie cannot find the device, check:

**System Settings > Privacy & Security > Accessories** -- set to "Automatically When Unlocked"

---

## Quick Start

```bash
# 1. Create a config file
my-nimbie --create-config

# 2. Edit the config file to set your mount point and commands
vim ~/.my-nimbie.conf

# 3. Check device status
my-nimbie status

# 4. Process one disc (test your setup)
my-nimbie next readdvd

# 5. Batch process all discs in the hopper
my-nimbie batch readdvd --target-dir /Volumes/ext-data/
```

---

## Commands

| Command | Description |
|---------|-------------|
| `load` | Load the next disc from the hopper into the drive |
| `eject` | Eject the current disc to the accept (done) bin |
| `reject` | Eject the current disc to the reject bin |
| `unmount` | Unmount the disc from the optical drive (no USB needed) |
| `status` | Show device state, or batch progress if a batch is running |
| `next <flavor>` | Process exactly one disc (same setup as batch, for testing) |
| `batch <flavor>` | Batch mode: load, process, accept/reject, repeat until hopper empty |
| `accept` | Tell a paused batch/next to accept the current disc |
| `retry` | Tell a paused batch/next to retry the command |
| `reject` | (also) Tell a paused batch/next to reject the current disc |
| `stop` | Tell a paused batch/next to reject the current disc and stop |
| `cancel` | Cancel a running batch/next (sends SIGINT to the process) |
| `reset` | Recover from error states (bootloader mode, stuck disc) |

### Global Options

| Option | Description |
|--------|-------------|
| `-c, --config FILE` | Use a specific config file |
| `--create-config [PATH]` | Create example config (default: `~/.my-nimbie.conf`) |
| `-d, --dry` | Dry run: print what would be done without executing |
| `-v, --verbose` | Verbose output |
| `-D, --debug` | Debug output; `-DD` or `--deepdbg` for deep debug |
| `--test` | Run unit tests (no hardware needed) |

### Batch/Next Options

| Option | Description |
|--------|-------------|
| `-t, --target-dir DIR` | Base output directory (overrides config) |
| `--prefix STR` | Directory name prefix (default: `{INDEX}`) |
| `--name STR` | Directory name middle part (default: `" - {MEDIA_TYPE}"`) |
| `--postfix STR` | Directory name postfix (default: empty) |
| `--offset N` | Offset added to disc number for `{INDEX}` |
| `--padding N` | Zero-padding width for `{INDEX}` (default: 3) |
| `--pause-on-err` | On command error, keep disc in drive, wait for user action |
| `--use-loaded` | Process a disc already in the drive (skip loading from hopper) |
| `--max N` | Max discs to process (batch only, 0 = unlimited) |

### Reset Options

| Option | Description |
|--------|-------------|
| `--exit-bootloader` | Exit Microchip PIC bootloader mode (requires power cycle) |
| `--diagnostics` | Show device diagnostics (counters, timers, state, bootloader info) |

---

## Batch Flavors

Each flavor maps to a command in the config file:

| CLI Flavor | Config Key | Description |
|------------|------------|-------------|
| `ripdvd` | `on_load_RIP_DVD` | Encode DVD titles to MKV |
| `ripaudio` | `on_load_RIP_AUDIOCD` | Rip audio CD tracks to FLAC |
| `readdvd` | `on_load_READ_DVD` | Full DVD backup via dvdbackup |
| (none) | `on_load_DEFAULT` | Default command or synonym for another flavor |

The special command value `pause` loads the disc but runs no command. The disc stays
mounted and my-nimbie waits for an interactive command (`accept`, `reject`, `retry`,
`stop`) from another terminal.

---

## Configuration

Config file search order:

1. `~/.my-nimbie.conf`
2. `/etc/my-nimbie.conf`
3. `/LINKS/default/my-nimbie`

Or specify explicitly: `my-nimbie --config /path/to/config <command>`

Create an example config with: `my-nimbie --create-config`

### Config Sections

**`[nimbie]`** -- device identification and mount point:

```ini
[nimbie]
    vid = 0x1723                # USB Vendor ID
    pid = 0x0945                # USB Product ID
    mount_point = /Volumes/DVD_VIDEO_RECORDER   # MANDATORY
```

**`[commands]`** -- shell commands executed for each batch flavor:

```ini
[commands]
    on_load_RIP_DVD = /LINKS/bin/my-handbrake dvd "$MOUNT_POINT" --all encode tvDVD
    on_load_RIP_AUDIOCD = mkdir -p "$DIR_NAME" && cd "$DIR_NAME" && cdparanoia -B -- -0 && flac --best *.wav && rm -f *.wav
    on_load_READ_DVD = mkdir -p "$DIR_NAME" && dvdbackup -i "$MOUNT_POINT" -o "$DIR_NAME" -M -v
    # on_load_DEFAULT = ripdvd          # synonym for an existing flavor
    on_validate =                        # optional validation command
```

Available variables in commands (`$VAR` or `${VAR}` syntax):

| Variable | Description |
|----------|-------------|
| `$MOUNT_POINT` | Where the disc is mounted (from config) |
| `$TARGET_DIR` | Base output directory |
| `$DIR_NAME` | Full per-disc output path: `TARGET_DIR/<generated name>` |
| `$DISC_NR` | Sequential disc number (1, 2, 3, ...) |
| `$DEVICE` | Raw device path (e.g. `/dev/disk4`), resolved from mount_point |

**`[target_dirs]`** -- base output directories per flavor:

```ini
[target_dirs]
    rip_dvd = /Volumes/ext-data/
    rip_audiocd = /Volumes/ext-data/
    read_dvd = /Volumes/ext-data/
```

**`[naming]`** -- per-disc subdirectory naming:

```ini
[naming]
    name_prefix = {INDEX}
    name = " - {MEDIA_TYPE}"
    name_postfix =
    idx_padding = 3
    idx_offset = 0
```

**`[batch]`** -- timing and limits:

```ini
[batch]
    max_discs = 0               # 0 = unlimited
    load_settle_time = 5        # seconds after loading before mount check
    mount_timeout = 60          # seconds to wait for disc to mount
    poll_interval = 2           # seconds between mount-point polls
    result_file = /tmp/my-nimbie.result
```

---

## Per-Disc Directory Naming

The per-disc subdirectory is assembled as: `{NAME_PREFIX}{NAME}{NAME_POSTFIX}`

Supported `{VARIABLE}` placeholders (case-insensitive):

| Variable | Description |
|----------|-------------|
| `{INDEX}` | Disc index: `DISC_NR + idx_offset`, zero-padded to `idx_padding` digits |
| `{DISC_NR}` | Raw disc number (1, 2, 3, ...) without padding or offset |
| `{MEDIA_TYPE}` | `"DVD"` or `"CD"` (auto-detected from disc content) |
| `{DVD_TITLE}` | Volume name of the disc (lazy: only read when referenced) |
| `{FLAVOR}` | Batch flavor name |
| `{DATE}` | Current date as `YYYY-MM-DD` |

### Examples

```bash
# Default naming: "001 - DVD", "002 - DVD", ...
my-nimbie batch readdvd

# Named by disc title: "LOTR_DISC_1 (001)", "LOTR_DISC_2 (002)", ...
my-nimbie batch --prefix "{DVD_TITLE}" --name " ({INDEX})" readdvd

# Continue numbering from a previous batch of 50 discs
my-nimbie batch --offset 50 readdvd
# First disc: 051, then 052, 053, ...

# Date-stamped: "2026-03-28_001 - LOTR_DISC_1"
my-nimbie batch --prefix "{DATE}_{INDEX}" --name " - {DVD_TITLE}" readdvd
```

---

## Monitoring

While a batch is running:

```bash
# From another terminal
my-nimbie status

# Send SIGUSR1 for live status dump to stderr
kill -USR1 <pid>

# Machine-readable status file
cat /tmp/my-nimbie.status

# Copy progress (updated in real time)
cat /tmp/my-nimbie.progress

# Per-disc result log
cat /tmp/my-nimbie.result
```

The status file contains key=value pairs: `state`, `mode`, `pid`, `flavor`,
`disc_nr`, `accepted`, `rejected`, `started`, `last_update`, etc.

---

## Error Recovery

### Disc Stuck in Drive (after crash)

```bash
my-nimbie status              # Detects crashed process, shows disc location
my-nimbie eject               # Eject disc to accept bin
my-nimbie reject              # Eject disc to reject bin
my-nimbie next --use-loaded   # Process the disc that is already loaded
```

### Nimbie in Bootloader Mode

```bash
my-nimbie reset                     # Auto-detect and recover
my-nimbie reset --exit-bootloader   # Explicit bootloader exit
# Then: power switch OFF, wait 5s, switch ON
my-nimbie status                    # Verify recovery
```

### Device Diagnostics

```bash
my-nimbie reset --diagnostics     # Show state bits, counters, timers, LED status
my-nimbie -v status               # Verbose status with hardware diagnostics
```

---

## Nimbie NB21 USB Protocol Reference

This section documents the reverse-engineered USB protocol for the Acronova Nimbie
USB Plus NB21 (internally identified as "NT21"). The protocol is completely
undocumented by Acronova. All information below was determined through USB packet
analysis, probing, and study of the reference `nimbiestatemachine` Python library
and Acronova's BS Utility Windows application.

### Device Identification

| Property | Value |
|----------|-------|
| Manufacturer string | `AUTO DUPLICATOR` |
| Product string | `NT21` |
| Vendor ID (VID) | `0x1723` (Acronova Technology Inc.) |
| Product ID (PID) | `0x0945` |
| USB standard | USB 2.0 / 3.0 |
| Interface class | 0 (Reserved / vendor-specific) |

### Endpoint Configuration

The device uses interrupt transfer endpoints (not control transfers):

| Endpoint | Direction | Type | Max Packet Size |
|----------|-----------|------|-----------------|
| `0x02` | OUT (host to device) | Interrupt | 8 bytes |
| `0x81` | IN (device to host) | Interrupt | 64 bytes |

There is only one USB configuration and one interface `(0, 0)`.

### Command Packet Format

Commands are sent as **8-byte interrupt OUT packets**. The command byte is placed at
offset 2, with an optional parameter byte at offset 3. All other bytes are zero:

```
Byte:  [0]   [1]   [2]    [3]     [4]   [5]   [6]   [7]
       0x00  0x00  CMD    PARAM   0x00  0x00  0x00  0x00
```

Up to 6 command/parameter bytes can be placed at offsets [2] through [7], though no
known command uses more than 2.

### Command Table

#### Known Safe Commands

| CMD | PARAM | Name | Description | Response |
|-----|-------|------|-------------|----------|
| `0x43` | `0x00` | `GET_STATE` | Query hardware state bits | `"OK"`, `"{xxxxxxxxx}"`, `"AT+O"` |
| `0x47` | `0x01` | `LIFT_DISC` | Lift disc from open tray with gripper | `"AT+O"` / `"AT+S00"` / `"AT+S03"` |
| `0x49` | `0x00` | `DIAGNOSTICS` | Read internal timers and counters | `"OK"`, name/value pairs, `"AT+O"` |
| `0x4A` | `0x00` | `COUNTERS` | Read pick/release counters | name/value pairs or `"AT+E09"` |
| `0x52` | `0x01` | `PLACE_DISC` | Drop disc from hopper onto open tray | `"AT+O"`, then `"AT+S07"` |
| `0x52` | `0x02` | `ACCEPT` | Drop lifted disc into accept (done) pile | `"AT+O"` |
| `0x52` | `0x03` | `REJECT` | Drop lifted disc into reject pile | `"AT+O"` |

#### Dangerous Commands (Do Not Send)

| CMD | PARAM | Effect |
|-----|-------|--------|
| `0x56` | `0x00` | **JUMPS TO BOOTLOADER** -- device re-enumerates as Microchip PIC HID bootloader (`0x04D8:0x000B`). Recoverable but requires power cycle. |
| `0x55` | `0x00` | Returns `"OK"` / `"AT+O"` -- unknown side effect, avoid. |

All other command bytes in the range `0x00`-`0xFF` either return no response or
return `"AT+E09"` (unknown command error). Sending untested commands is strongly
discouraged as it may trigger mechanical actions or enter bootloader mode.

### AT+ Response Codes

All status responses are ASCII strings prefixed with `"AT+"`:

| Code | Meaning |
|------|---------|
| `AT+O` | Operation accepted / success (generic OK) |
| `AT+S00` | No disc in tray (cannot lift) |
| `AT+S03` | Dropper/mechanism error (disc already lifted, or mechanism stuck) |
| `AT+S07` | Disc successfully placed on tray |
| `AT+S10` | Tray is in the wrong state (e.g. tray closed when place_disc needs it open) |
| `AT+S12` | Tray already has a disc |
| `AT+S14` | Hopper is empty (no disc in input queue) |
| `AT+E09` | Hardware error / unknown command |

**Important:** `AT+O` means "command accepted" -- it is the initial acknowledgment.
For mechanical operations (`PLACE_DISC`, `LIFT_DISC`, `ACCEPT`, `REJECT`), the
actual result arrives as a separate `AT+Sxx` or `AT+Exx` response after the
mechanism completes. For `GET_STATE`, `AT+O` follows the state string.

### State Bit String Format

The `GET_STATE` command (`0x43`) returns a string in the format `{xxxxxxxxx}` where
each `x` is `'0'` or `'1'` (or `'x'` for unused trailing positions). The string is
typically 9 characters long inside the braces.

| Bit Position | Name | Description | Confirmed |
|-------------|------|-------------|-----------|
| 0 | (unknown) | Purpose unknown | No |
| 1 | `disc_available` | `1` when discs are in the input hopper, `0` when empty | Yes |
| 2 | (unknown) | Purpose unknown | No |
| 3 | `disc_in_tray` | `1` when a disc is sitting on the ejected (open) tray | Yes |
| 4 | `disc_lifted` | `1` when a disc is held by the gripper mechanism | Yes |
| 5 | `tray_out` | `1` when the drive tray is ejected/open, `0` when closed | Yes |
| 6 | (unknown) | Often `1` when device is idle | No |
| 7-8 | (unknown) | Usually `x` (literal character, not binary) | No |

**State string examples:**

| State | Bits | Meaning |
|-------|------|---------|
| `{000000100}` | Idle, no discs | Hopper empty, tray closed, nothing happening |
| `{010000100}` | Ready | Discs in hopper, tray closed |
| `{010001100}` | Tray open, discs available | Tray ejected, discs in hopper |
| `{000101100}` | Disc on open tray | Disc placed, tray still open |
| `{000010100}` | Disc lifted | Gripper holding a disc |

**Critical limitation:** The Nimbie state bits **cannot detect a disc inside a closed
drive**. When the tray is closed with a disc loaded, `disc_in_tray` reads `0`. This
is why my-nimbie uses `drutil status` as a secondary check for disc presence in a
closed drive.

**Historical note:** The original `nimbie-py` driver (archived as `nimbie-driver.py`)
used different bit position offsets (shifted by +1 from the AT+ response prefix),
interpreting position 2 as `disk_available`, 4 as `disk_in_open_tray`, 5 as
`disk_lifted`, and 6 as `tray_out`. The nimbiestatemachine project corrected these
to the positions documented above through systematic hardware testing.

### Response Sequence

When a command is sent, the device returns multiple interrupt IN packets:

1. **Empty/null packets** -- zero-filled 64-byte packets (idle interrupt traffic)
2. **`"OK"` string** -- null-terminated ASCII (present for `GET_STATE` and
   `DIAGNOSTICS`, not always for mechanical commands)
3. **Payload** -- the state string `{xxxxxxxxx}`, diagnostic name/value pairs, etc.
4. **`AT+xxx` status code** -- the final status response
5. **More empty packets** -- indicating end of response

Responses are null-terminated ASCII strings within 64-byte interrupt IN packets.
The reader must accumulate packets until it receives an AT+ code (or times out).

For `DIAGNOSTICS` (`0x49`), the response contains interleaved name/value pairs:

```
"OK", "OL-Timer", "00000009", "Supply-N", "00000123", "Supply-E", "00000456",
"Pulley-E", "00000789", "Pick-N", "00001234", "Pick-E", "00000012",
"Release-N", "00005678", "Release-E", "00000034", "AT+O"
```

### Timing Requirements

| Parameter | Value | Reason |
|-----------|-------|--------|
| USB IN read timeout | **20,000 ms** (20 seconds) | Required. Shorter timeouts (e.g. 3s) cause `[Errno 60]` Operation timed out, which cascades to `[Errno 5]` I/O Error, killing the USB bus entirely. |
| Post-command delay before first read | **300 ms** | Gives the microcontroller time to prepare its response buffer. |
| Tray open/close time | **0.5-1.0 s** | Mechanical tray motion. Poll `tray_out` state bit. |
| Disc placement time | **2-5 s** | Dropper picks up a disc and drops it on tray. Poll `disc_in_tray`. |
| **Dropper retraction delay** | **0.8 s** after disc placed | Critical: the dropper mechanism must retract before the tray closes. Without this delay, the tray closes on the dropper arm. |
| Disc lift time | **~2 s** | Gripper picks disc from open tray. Poll `disc_lifted`. |
| Accept/reject drop time | **~1 s** | Drop disc from gripper to bin. Poll `disc_lifted` becoming `0`. |
| USB re-enumeration after reset | **3 s** | After `dev.reset()`, wait for the device to re-appear on the bus. |
| Polling interval (state queries) | **0.1-0.5 s** | Balance between responsiveness and USB traffic. |

### Mechanical Operation Sequence

A full disc processing cycle:

```
1. OPEN TRAY     drutil tray eject     Poll: tray_out = 1        (~0.5s)
2. PLACE DISC    CMD 0x52, 0x01        Poll: disc_in_tray = 1    (~2-5s)
   [wait 0.8s for dropper retraction]
3. CLOSE TRAY    drutil tray close     Poll: tray_out = 0        (~0.5s)
4. [disc is in drive -- process it]
5. OPEN TRAY     drutil tray eject     Poll: tray_out = 1        (~0.5s)
6. LIFT DISC     CMD 0x47, 0x01        Poll: disc_lifted = 1     (~2s)
7. CLOSE TRAY    drutil tray close     Poll: tray_out = 0        (~0.5s)
8. ACCEPT/REJECT CMD 0x52, 0x02/0x03   Poll: disc_lifted = 0     (~1s)
```

Note that tray control is done via the operating system (`drutil` on macOS, `eject`
on Linux), not via USB commands to the Nimbie. The Nimbie only controls the
loader/unloader mechanism (dropper, gripper, bins). The drive tray is controlled
by the optical drive itself.

---

## Bootloader Mode

### How It Happens

Sending USB command byte `0x56` to the Nimbie causes the PIC microcontroller to
jump into its Microchip HID Bootloader. This was discovered accidentally during
USB command space probing on 2026-03-28. The device is NOT bricked -- it is
recoverable.

**Root cause:** Command `0x56` invalidates the application validity signature in
NVM. On every subsequent power-on, the bootloader checks that signature, finds it
invalid, and stays in bootloader mode instead of running the firmware. A plain power
cycle does NOT fix this -- the signature must be re-written before power cycling.

### Bootloader Device Identity

| Property | Value |
|----------|-------|
| VID | `0x04D8` (Microchip Technology Inc.) |
| PID | `0x000B` |
| Interface class | `0x00` (not HID despite "HID Bootloader" name) |
| Endpoints | EP `0x01` OUT (Bulk, 64 bytes), EP `0x81` IN (Bulk, 64 bytes) |
| Protocol | Raw single-byte commands (see Protocol section below) |
| Device family | `0x07` (non-standard; standard PIC32 family = `0x03`) |

LED pattern in bootloader mode: **ERROR: RED (solid), LINK: GREEN (solid), USB3: GREEN (solid), READY: GREEN (solid)**

**macOS note:** When the device re-enumerates with the bootloader VID/PID, macOS may
block it as an unknown accessory. Check **System Settings > Privacy & Security >
Accessories** and set to "Automatically When Unlocked".

### Bootloader Protocol — Raw Only (Framed AN1388 NOT Supported)

The Nimbie NB21 bootloader uses a **raw single-byte command protocol**, not the
framed AN1388 protocol described in Microchip application note AN1388B.pdf.

**Sources:** Microchip AN1388B.pdf (`,archive/bootloader/AN1388B.pdf`),
AN1388 Harmony variant (`,archive/bootloader/AN1388_harmony_protocol.html`),
pic32prog source (`adapter-an1388.c`).

The framed protocol (AN1388B / Harmony) uses:

```text
<SOH=0x01> [DLE-escaped data] <CRC16_low> <CRC16_high> <EOT=0x04>
```

**This does NOT work on the Nimbie bootloader.** When a 64-byte framed packet is
sent, the bootloader echoes back the first 5 bytes of the packet unchanged and
ignores the framed command. Confirmed by sending the correct framed Jump-to-App
packet (`01 05 A5 50 04`) and comparing the response -- it was an echo, not a
jump-to-application action.

Raw commands are 64-byte bulk packets with the command byte at offset 0 (all other
bytes zero). The bootloader responds with an echo of the command byte.

### Bootloader Commands

Full command space scan (`0x00-0xFF`) performed 2026-03-29. Only these responded:

| CMD | Name | Response | Description | Safe? |
|-----|------|----------|-------------|-------|
| `0x00` | `QUERY_DEVICE` | `00 00 00 07` | Query bootloader info: bytes/pkt=0, family=0x07 | Yes |
| `0x04` | `PROGRAM_COMPLETE` | `04` | Signal programming is done | Yes |
| `0x05` | `GET_DATA` | `05` | Read flash at address (read-protected, returns echo only) | Yes |
| `0x06` | `RESET_DEVICE` | `06` then disconnect | Soft reset; stays in BL without valid app signature | Yes |
| `0x07` | `SIGN_FLASH` | `07` | Writes application validity signature to NVM | Yes |
| `0x32` | UNKNOWN | `32` | Echoes back `0x32`; meaning unknown | Yes |

Standard AN1388 commands that were NOT probed (destructive -- would brick the device):

| CMD | Name | Description |
|-----|------|-------------|
| `0x01` | `UNLOCK_CONFIG` | Unlock config bits |
| `0x02` | `ERASE_FLASH` | **Erases entire application flash → BRICK** |
| `0x03` | `PROGRAM_FLASH` | **Writes to flash → BRICK if wrong data** |

**RESET_DEVICE behavior:** Soft reset that maintains the USB connection briefly
before going offline. Unlike a power cycle, the device stays in bootloader mode
if the application validity signature has not been written.

Flash is read-protected on the Nimbie NB21: `GET_DATA` returns only the command echo
byte with no actual flash data.

### Recovery Procedure

**The confirmed working procedure** (verified after multiple failed attempts):

```bash
my-nimbie reset --sign-and-reset
# Then: hardware switch OFF, wait 10 seconds, switch ON
my-nimbie status                    # Verify device is back to normal mode
```

What this does:

1. `PROGRAM_COMPLETE` (0x04) — signals to the bootloader that firmware programming
   is complete and a valid application should be present
2. `SIGN_FLASH` (0x07) — writes the application validity CRC signature to NVM
3. `SIGN_FLASH` (0x07) again — second write ensures the NVM write fully commits
4. Wait 10 seconds before power cycle — critical: NVM write must complete
5. Power cycle — bootloader re-checks the application CRC, finds it valid, runs firmware

**What did NOT work:**

- `RESET_DEVICE` (0x06) alone + power cycle — signature not written, bootloader stays
- Framed Jump-to-App (0x05) — not processed; bootloader echoes the packet
- Single `SIGN_FLASH` + immediate power cycle — NVM write may not have committed

**Disc behavior after power cycle:** When the Nimbie powers on with a disc in an
unknown state (disc was present during bootloader mode), it automatically rejects
the disc to the reject bin rather than the accept bin. This is a safety behavior --
the Nimbie cannot determine if the disc was successfully read without a completed
`ACCEPT` command. The disc is not lost; check the reject bin.

---

## Critical Implementation Notes

### USB Read Timeout

The interrupt IN read timeout **must be at least 20 seconds** (20,000 ms). The
reference `nimbiestatemachine` code uses `in_ep.read(IN_SIZE, 20000)`.

With shorter timeouts (e.g. the common 3-second default), reads during mechanical
operations fail with `[Errno 60] Operation timed out`. This timeout error corrupts
the USB bus state, causing subsequent operations to fail with `[Errno 5] Input/output
error`. Once this cascade starts, the only recovery is `dev.reset()` followed by
device re-enumeration.

### PLACE_DISC and USB Bus Death

The `PLACE_DISC` command (`0x52, 0x01`) is the most dangerous command from a USB
stability perspective. After sending it, the device is mechanically busy for 2-5
seconds (the dropper picks up a disc and drops it on the tray). During this time,
the microcontroller is unresponsive to USB reads.

**The problem:** If you attempt to read responses from the IN endpoint during the
mechanical operation, the reads time out. With a short timeout, this triggers
`[Errno 60]` which cascades to `[Errno 5]`, killing the USB bus.

**The solution in my-nimbie:** After sending `PLACE_DISC`, do NOT read from the
IN endpoint. Instead:

1. Send the command (write only, no read)
2. Sleep 5 seconds for the mechanism to complete
3. Poll `GET_STATE` to confirm `disc_in_tray = 1`

This approach is simpler and more reliable than the reference code's approach of
using a 20-second read timeout and waiting for the AT+ response.

### Dropper Retraction Delay

After a disc is placed on the tray (`PLACE_DISC` completes, `disc_in_tray = 1`),
you **must wait 0.8 seconds** before closing the tray. This gives the dropper
mechanism time to retract upward. Without this delay, the tray closes while the
dropper arm is still in position, which can jam the mechanism.

This timing was determined through hardware testing and confirmed across multiple
Nimbie units.

### USB Recovery via dev.reset()

When USB communication breaks down (timeout cascades, I/O errors), the recovery
procedure is:

1. Release the USB interface: `usb.util.release_interface(dev, 0)`
2. Send USB bus reset: `dev.reset()`
3. Wait 3 seconds for device re-enumeration
4. Re-find the device: `usb.core.find(idVendor=0x1723, idProduct=0x0945)`
5. Re-configure and re-claim the interface

my-nimbie implements automatic USB recovery: on the first I/O error, it attempts a
clean reconnect; after 3 consecutive failures during state polling, it performs a
full USB reset.

### Disc in Closed Drive Detection

The Nimbie state bits **cannot detect** whether a disc is inside a closed optical
drive. When the tray closes with a disc, `disc_in_tray` becomes `0` (the Nimbie only
tracks disc on an *open* tray). This creates a blind spot for crash recovery.

my-nimbie works around this by also querying `drutil status` on macOS. If `drutil`
reports media present and the Nimbie says tray is closed with no disc on tray, then
a disc is in the closed drive.

---

## Diagnostic Commands

The `DIAGNOSTICS` command (`0x49`) returns internal counters as interleaved
name/value pairs:

| Counter | Description |
|---------|-------------|
| `OL-Timer` | Overload timer value |
| `Supply-N` | Supply normal counter |
| `Supply-E` | Supply error counter |
| `Pulley-E` | Pulley error counter |
| `Pick-N` | Pick (grab disc) normal counter |
| `Pick-E` | Pick error counter |
| `Release-N` | Release (drop disc) normal counter |
| `Release-E` | Release error counter |

The `COUNTERS` command (`0x4A`) returns a subset (Pick/Release counters) on some
units, or `AT+E09` (not supported) on others.

Access via: `my-nimbie reset --diagnostics` or `my-nimbie -v status`

---

## Reference Implementations

This project was informed by two reference implementations:

### nimbie-py (original)

The original Python driver by Matt Soulanille:
[github.com/mattsoulanille/nimbie-py](https://github.com/mattsoulanille/nimbie-py)

Simple, synchronous driver using `pyusb`. Uses 20-second read timeouts. Original
state bit interpretation was offset by +1 (later corrected in nimbiestatemachine).

### nimbiestatemachine

Extended driver by Ben Roeder:
[github.com/benroeder/nimbiestatemachine](https://github.com/benroeder/nimbiestatemachine)

Adds a state machine layer with polling (no hardcoded sleeps), multi-device support
via USB port path identification, pure state machine variant, hierarchical states,
and comprehensive error recovery. This was the primary reference for my-nimbie's USB
protocol implementation.

### BS Utility (Windows)

Acronova's official Windows application for the Nimbie NB21. See the full reverse
engineering section below: [BS Utility (Windows) — Reverse Engineering](#bs-utility-windows--reverse-engineering).

---

## BS Utility (Windows) — Reverse Engineering

**Source:** Downloaded from Acronova's product page for the Nimbie USB NB21.

| Property | Value |
|----------|-------|
| Product page | <https://disc.acronova.com/download/product/auto-blu-ray-duplicator-publisher-ripper-nimbie-usb-nb21/9.html> |
| Direct download | <https://disc.acronova.com/file/22/download.html> |
| Filename | `BSUtility_3_0_0_1.exe` |
| File size | 3.4 MB |
| Version | 3.0.0.1 |
| Dates | 2017-10-20 / 2020-09-15 |
| Stored at | `,archive/BSUtility/BSUtility_3_0_0_1.exe` |
| Format | PE32 executable (GUI) Intel 80386, stripped to external PDB, for MS Windows |

### Binary Identity

```text
file BSUtility_3_0_0_1.exe
BSUtility_3_0_0_1.exe: PE32 executable (GUI) Intel 80386 (stripped to external PDB), for MS Windows
```

Window title string found in binary: `"BS Autoloader Utility"`

Version string format: `"Test program version : %s"`

### Runtime Platform: Delphi

The BS Utility is a **Borland Delphi (Object Pascal)** application, not C or C++.
This is confirmed by the presence of Delphi-specific exception class names in the
binary strings:

- `EStreamError`
- `EOSError`
- `EListError`
- `TWindowState`
- `TConversion`
- `TConversionFormat`

It also uses `ImmGetConversionStatus` / `ImmSetConversionStatus` (Windows IME API),
indicating Japanese language input support was included.

**Implication for protocol analysis:** Delphi string handling and USB I/O library
calls differ from C/C++, but the wire protocol is identical. The Delphi runtime does
not affect the byte-level USB HID packets or the AT+ command/response encoding.

### AT+ Protocol Evidence

The following AT+ codes appear verbatim in the binary, confirming the protocol used
by my-nimbie's Python driver:

| String | Role |
| ------ | ---- |
| `AT+O` | Success / operation accepted |
| `AT+E` | Error prefix |
| `AT+S01` | Status code 01 |
| `AT+S07` | Status code 07 (disc placed on tray) |
| `AT+S08` | Status code 08 |
| `AT+S12` | Status code 12 (tray already has disc) |
| `AT+I00` | Device info query (see below) |
| `AT+I99` | Device info query (see below) |

Format string found in binary:

```text
"AT+%C%02X = %s"
```

This confirms that AT+ command opcodes consist of **one ASCII letter** (`%C`) followed
by a **two-digit hex number** (`%02X`), matching the `AT+S07`, `AT+E09`, etc. pattern
throughout the protocol.

The model-tagged variants found in strings:

```text
"(NK50Y-NB11) AT+I00"
"(NK50Y-NB11) AT+I99"
"(NK50Y-NK50) AT+I00"
"(NK50Y-NK50) AT+I99"
"AT+%C%02X = %s"
"AT+? = %s"
```

### Error Message Strings

All error strings found verbatim in `BSUtility_3_0_0_1.exe` via `strings` analysis.
These are the user-visible error descriptions shown by the Windows utility when the
device returns an error code.

```text
An error occurs when autoloader picks up a disc from the loading area.
An error occurs when autoloader picks up a disc from the tray.
An error occurs when autoloader unloads a disc in the output area.
An error occurs when autoloader moves to the waiting area.
An error occurs when a disc is flipped to the lower drive.
An error occurs when a disc is unloaded to the output bin from the lower drive.
An error occurs when a disc is unloaded to the tray.
An error occurs when discs are unloaded to the output bin from the dual drives.
An error occurs when one tray opens. The other tray is not existing.
An error occurs when pick up disc.
An error occurs when the dual drives pick up discs.
An error occurs when the dual trays close up.
An error occurs when the dual trays open.
An error occurs when the lower drive closes up.
An error occurs when the lower drive picks up a disc.
An error occurs when the lower tray already has disc on it.
An error occurs when the lower tray opens.
An error occurs when the printer ejects the tray.
An error occurs when the tray already has disc on it.
An error occurs when the tray closes up.
An error occurs when the tray opens.
An error occurs when the tray picks up a disc.
An error occurs when the trays already have discs.
An error occurs when the upper drive picks up a disc.
An error occurs when the upper tray already has disc on it.
An error occurs when the upper tray closes up.
An error occurs when the upper tray opens.
An error occurs when tray backward.
An error occurs when tray closes up.
An error occurs when tray moves to the loading area.
An error occurs when tray moves to the printing area.
An error occurs when tray moves to the reject area.
An error occurs when tray opens.
An error occurs when trays open.
An error occurs while loading a disc.
An error occurs while picking up a disc.
An error occurs while switching to CD/DVD mode.
An error occurs while switching to paper mode.
An error occurs while unloading a disc to the output bin.
An error occurs while unloading a disc.
An error occurs with DupliQ
An error occurs with NB11 of NK50Y.
An error occurs with NK50V.
An error occurs with NK50Y.
An error occurs with the motor.
An error ocurrs with motor.
An error with NB11 port.
An error with the eject ramp
An error with the motor.
An error with the printer port.
The output bin is not attached properly.
The output ramp is not open.
```

Note: `"An error ocurrs with motor."` contains a typo (`ocurrs`) — reproduced
verbatim from the binary.

Several of these messages refer to hardware features not present on the NB21 (dual
drives, lower/upper trays, printer, paper mode, flip mechanism). This confirms the
utility supports multiple Acronova product lines using a shared codebase, and the
NB21 only exercises a subset of the error conditions.

### Operational Step Labels

These strings appear to be progress/step labels shown in the utility's UI during
autoloader operations:

```text
Autoloader moves to the ready area
Check disc in the input bin
Check eject ramp
Check if tray opens
Check if trays open
Check output bin
Check output ramp
Load disc to lower tray
Load disc to the lower tray
Load disc to the tray
Load disc to the upper tray
Lower tray close
Lower tray open
Pick up disc
Pick up disc from input bin
Pick up disc from lower tray
Pick up disc from the tray
Pick up disc from upper tray
Pick up discs
Supply the loader mechanism with discs.
Supply the loading area with discs.
Tray moves to loading area
Tray moves to reject area
Tray moves to the loading area
Unload disc
Unload disc to output bin
Unload discs to output bin
Upper tray close
Upper tray open
Accept
Input/output error
```

### Device Model Support

The BS Utility supports multiple Acronova product lines from a single binary. Models
identified from string analysis:

| Model | Description |
| ----- | ----------- |
| `NB21` | The device this project controls (Nimbie USB Plus NB21) |
| `NK50Y` | Different product line; referenced repeatedly in model-tagged AT+I strings |
| `NK50V` | Referenced in error string `"An error occurs with NK50V."` |
| `NB11` | Referenced as `"(NK50Y-NB11)"` — appears to be an NB11 sub-module of NK50Y |
| `NK50` | Referenced as `"(NK50Y-NK50)"` — NK50 drive module within NK50Y |
| `DupliQ` | Duplicator product; referenced in `"An error occurs with DupliQ"` |

The presence of printer-related strings (`"An error occurs when the printer ejects the
tray."`, `"An error occurs while switching to paper mode."`, `"An error when tray moves
to the printing area."`) indicates the utility also supports Acronova's disc-printing
product variants.

### USB Enumeration Strings

Verbatim from binary:

```text
Device : USB#VID_%04X&PID_%04X#%d  %s
---------- Find USB 3.0 HUB / Device ----------
---------- End Find USB 3.0 HUB / Device ----------
```

The format string `USB#VID_%04X&PID_%04X#%d  %s` is the Windows device instance path
format (e.g. `USB#VID_1723&PID_0945#1  AUTO DUPLICATOR`). The utility enumerates USB
devices using Windows device manager APIs, not raw HID calls — consistent with Delphi
Windows application patterns.

The presence of dedicated USB 3.0 hub detection (`"---------- Find USB 3.0 HUB / Device
----------"`) suggests the utility has specific USB 3.0 detection logic, likely to
identify devices connected through USB 3.0 hubs versus direct connections.

### Undocumented Commands: AT+I00 and AT+I99

The strings `AT+I00` and `AT+I99` appear paired with model identifiers:

```text
(NK50Y-NB11) AT+I00
(NK50Y-NB11) AT+I99
(NK50Y-NK50) AT+I00
(NK50Y-NK50) AT+I99
```

These are device information query commands not documented in any public Acronova
material, and not implemented in either reference Python driver. Based on context:

- `AT+I00` — likely "device info start" or "firmware version query"
- `AT+I99` — likely "device info end" or a second info variant

The `I` prefix (letter `I` + two-digit hex) matches the `AT+%C%02X` format string
exactly: `AT+` + letter `I` + hex `00` or hex `99`.

These commands have not been probed on the NB21. They may not be supported on the
NB21 (which may use different command bytes), or they may return device identification
strings. Use caution if attempting to probe these on live hardware.

---

## Known Issues / Crash Log

### LIFT_DISC AT+E09 — Batch Crash 2026-03-29

**Date:** 2026-03-29
**Disc:** #205 in a running batch (readdvd flavor)
**Phase:** LIFT_DISC, after successful dvdbackup

#### What happened

During batch processing of disc #205, `dvdbackup` completed successfully (exit
code 0). The disc data was fully written to disk before any crash occurred.

The subsequent LIFT_DISC command (`0x47, 0x01`) returned `AT+E09` instead of
the expected `AT+O` / `AT+S` sequence.

#### Old behavior (pre-fix)

The old code called `err()` directly on any `AT+E09` response from LIFT_DISC:

```python
# old code -- crash path
if response == "AT+E09":
    err(f"LIFT_DISC returned AT+E09")   # -> sys.exit(1)
```

This caused an immediate `sys.exit(1)`, leaving the disc in the open tray and
the batch process dead. The drive tray remained open after the crash.

#### Observed consequence: "No disc in drive"

After the crash, running `drutil status` reported no disc present. This is
expected: `drutil` queries the optical drive, and the drive tray was open
(the disc was sitting on the open tray, not inside the drive). An open tray
appears as "no media" to the OS. The disc data was safe — the crash happened
after the backup completed.

#### Fix applied

A 3-retry loop with a 3-second delay between attempts was added for `AT+E09`
responses from LIFT_DISC:

```python
# current code -- retry loop
for attempt in range(3):
    response = send_lift_disc()
    if response != "AT+E09":
        break
    if attempt < 2:
        log(f"LIFT_DISC returned AT+E09, retrying in 3s (attempt {attempt+1}/3)")
        time.sleep(3)
else:
    err(f"LIFT_DISC returned AT+E09 after 3 attempts")
```

`AT+E09` from LIFT_DISC can be transient (hardware not yet ready, mechanical
state unclear after a long drive operation). A short delay and retry resolves
it in practice without requiring a full USB reset or user intervention.

#### Recovery after this crash

```bash
my-nimbie status         # confirm disc #205 data already written, tray open
my-nimbie eject          # lift disc from open tray and drop to accept bin
                         # (or: my-nimbie next --use-loaded  to re-process)
my-nimbie batch readdvd --offset 205 --use-loaded   # continue from disc #206
```

Disc #205 did not need to be re-ripped.

---

### macOS Display Sleep Causes DVD Mount Failure → Bad Reject (2026-03-29)

**Date:** 2026-03-29
**Affected discs:** #215, #216, #217
**Symptom:** Three consecutive good discs rejected without being read; `wait_for_mount()` timed out (60 s) with the disc never appearing in `/Volumes/`.

#### Root cause: macOS Disk Arbitration vs. display sleep

macOS uses a subsystem called **Disk Arbitration** (`diskarbitrationd`) to detect
removable media and automatically mount them. When the display goes to sleep,
macOS aggressively throttles background I/O and Disk Arbitration events to save
power. The optical drive still spins and reads the disc, but the **"disk arrived"
notification** that triggers auto-mount is suppressed — macOS never calls mount
for the new disc, so it never appears in `/Volumes/`.

This only affects optical media (DVD/CD/BD). Hard drives and SSDs are already
mounted and not affected. Each disc insertion is a new Disk Arbitration event,
so every disc in a batch is vulnerable if the display sleeps between discs.

The display had stayed on during discs #213 and #214 (mounted in ~8 s each),
then slept during the ~3-minute inter-disc gap, causing #215–#217 to never mount.

#### Fix: caffeinate + manual-mount fallback

Two defenses were added to `cmd_batch()`:

1. **`caffeinate -d -i -w <pid>`** is launched at batch start and runs for the
   entire batch duration:
   - `-d` prevents display sleep
   - `-i` prevents system idle sleep
   - `-w <pid>` exits automatically when the batch process exits
   With the display kept on, Disk Arbitration operates normally and DVDs mount
   within seconds.

2. **`_force_mount_optical()`** as a safety net: if `wait_for_mount()` reaches
   half of its timeout without seeing the disc in `/Volumes/`, it calls
   `diskutil mount /dev/<disc>` explicitly, bypassing Disk Arbitration entirely.
   This covers edge cases where caffeinate fails to start or is killed.

#### Unmount dissent (same batch run, disc #212)

On the same day, `diskutil unmount` failed with:

```text
Unmount failed for /Volumes/...: dissented by PID 190 (loginwindow)
```

macOS Finder (via loginwindow) holds optical volumes and can veto unmount
requests. The batch crashed mid-accept sequence.

**Fix:** `unmount_disc()` now retries with `diskutil unmount force <path>` when
the error contains "dissented" or "failed to unmount". Force unmount overrides
the loginwindow veto. Confirmed working on subsequent discs.
