# my-nimbie

CLI controller for **Acronova Nimbie USB Plus NB21** disc autoloader.

Provides batch disc processing (rip, backup, encode) with automatic loading/unloading,
crash recovery, status tracking, and progress reporting.

## Requirements

- macOS (uses `drutil` for tray control)
- Python 3
- `pyusb` (`pip3 install --user --break-system-packages pyusb`)
- `libusb` (`brew install libusb`)

## Quick Start

```bash
# Process discs with a configured flavor
my-nimbie batch readdvd

# Process a single disc
my-nimbie next readdvd

# Check status of a running batch
my-nimbie status

# Eject current disc to accept/reject bin
my-nimbie accept
my-nimbie reject

# Recover from bootloader mode (after accidental 0x56)
my-nimbie reset                     # auto-detect and recover
my-nimbie reset --exit-bootloader   # exit Microchip PIC bootloader
my-nimbie reset --diagnostics       # show device counters and state
```

## Configuration

Config search order: `~/.my-nimbie.conf`, `/etc/my-nimbie.conf`, `/LINKS/default/my-nimbie`

See the config file for all options including batch flavors, target directories,
naming patterns, and timing settings.

---

## Nimbie NB21 (NT21) USB Protocol

The Nimbie NB21 uses USB HID interrupt endpoints for communication.
The protocol is **undocumented by Acronova** -- all knowledge below comes from
reverse-engineering open-source implementations and the BS Utility binary.

### Hardware

| Property | Value |
| --- | --- |
| Manufacturer | AUTO DUPLICATOR (Acronova Technology Inc.) |
| Product | NT21 |
| VID | 0x1723 |
| PID | 0x0945 |
| Interface | USB 2.0/3.0, class 0 (Reserved) |
| EP OUT | 0x02 Interrupt, 8 bytes max packet |
| EP IN | 0x81 Interrupt, 64 bytes max packet |

### Command Packet Format

8-byte packet sent to EP 0x02:

```
[0x00, 0x00, CMD, PARAM, 0x00, 0x00, 0x00, 0x00]
```

Responses are null-terminated ASCII strings read from EP 0x81.
Multiple responses may arrive for a single command (OK, state, AT code).

### Known Safe Commands

| CMD | PARAM | Name | Response |
| --- | --- | --- | --- |
| 0x43 | 0x00 | GET_STATE | OK, {state_bits}, AT+O |
| 0x47 | 0x01 | LIFT_DISC | AT+O / AT+S00 / AT+S03 |
| 0x49 | 0x00 | DIAGNOSTICS | OL-Timer, Supply-N/E, Pulley-E counters |
| 0x4A | 0x00 | COUNTERS | Pick-N/E, Release-N/E counters |
| 0x52 | 0x01 | PLACE_DISC | AT+S07 (placed) / AT+S14 (empty) / AT+S10 / AT+S12 |
| 0x52 | 0x02 | ACCEPT | AT+O (drop to done bin) |
| 0x52 | 0x03 | REJECT | AT+O (drop to reject bin) |

### AT Response Codes

| Code | Meaning |
| --- | --- |
| AT+O | Operation accepted / success |
| AT+S00 | No disc in tray |
| AT+S01 | Unknown (seen in BS Utility) |
| AT+S03 | Dropper / mechanism error |
| AT+S07 | Disc placed on tray |
| AT+S08 | Unknown (seen in BS Utility) |
| AT+S10 | Tray in wrong state |
| AT+S12 | Tray already has disc |
| AT+S14 | Hopper empty |
| AT+E09 | Hardware error / unknown command |
| AT+I00 | Initialization (seen in BS Utility) |
| AT+I99 | Reset? (seen in BS Utility) |

### State Bits

GET_STATE returns `{xxxxxxxxx}` -- a 9-character string of 0/1/x values.

| Bit | Meaning |
| --- | --- |
| 1 | disc_available -- discs in input hopper |
| 3 | disc_in_tray -- disc sitting on ejected tray |
| 4 | disc_lifted -- disc held by gripper mechanism |
| 5 | tray_out -- drive tray is ejected/open |
| 0, 2, 6, 7, 8 | Unknown purpose |

### Additional Commands (from BS Utility binary, NOT tested)

These were found in the command table at offset 0x091bb0 in `BSUtility_3_0_0_1.exe`.
**Do NOT send these without understanding what they do.**

| CMD | PARAM | Possible meaning |
| --- | --- | --- |
| 0x3F | 0x00 | Query (ASCII '?') |
| 0x46 | 0x00 | Unknown (ASCII 'F') |
| 0x46 | 0x02 | Unknown |
| 0x47 | 0x04 | Alternate lift variant? |
| 0x47 | 0x99 | Reset/clear mechanism? |
| 0x49 | 0x08 | Extended diagnostics? |
| 0x49 | 0x99 | Reset/clear? (matches AT+I99 string) |
| 0x4D | 0x01 | Motor control? (ASCII 'M') |
| 0x4D | 0x02 | Motor control? |
| 0x50 | 0x00 | Unknown (ASCII 'P') |
| 0x50 | 0x01 | Unknown |
| 0x52 | 0x04 | Alternate reject bin? |
| 0x52 | 0x90 | Unknown |
| 0x52 | 0x99 | Reset/clear? |
| 0x54 | 0x00 | Tray control? (ASCII 'T') |
| 0x54 | 0x01 | Tray open? |
| 0x54 | 0x02 | Tray close? |

### DANGEROUS Commands -- DO NOT SEND

| CMD | PARAM | Effect |
| --- | --- | --- |
| **0x56** | **0x00** | **BRICKS THE DEVICE.** Writes persistent error to EEPROM. Device stops enumerating on USB. ERROR LED turns solid red. Survives power cycles. No known recovery without Acronova support. |
| 0x55 | 0x00 | Returns OK/AT+O. Unknown side effect. Was sent just before 0x56 during probing -- may have contributed to the error state. |

### LED Indicators

LEDs left to right: **ERROR**, **LINK**, **USB 3.0**, **READY**

Legend: solid = lit, (off) = dark, (flash) = blinking

| ERROR | LINK | USB 3.0 | READY | Meaning |
| --- | --- | --- | --- | --- |
| (off) | (flash) | (off) | (flash) | Initialization in progress |
| (off) | solid | solid | solid | USB 3.0 connected, discs in loader |
| (off) | solid | solid | (flash) | USB 3.0 connected, loader empty |
| (off) | solid | (off) | solid | USB 2.0 connected, discs in loader |
| (off) | solid | (off) | (flash) | USB 2.0 connected, loader empty |
| (off) | (off) | (off) | solid | USB not detected, discs in loader |
| (off) | (off) | (off) | (flash) | USB not detected, loader empty |
| (flash) | (off) | (off) | (off) | Hardware error (no USB) |
| (flash) | solid | solid | (off) | Hardware error (USB 3.0) |
| (flash) | solid | (off) | (off) | Hardware error (USB 2.0) |

---

## Incident Log: 0x56 Firmware Brick (2026-03-28)

### What happened

While reverse-engineering the USB protocol to find recovery commands for a stuck disc,
a probe script (`nimbie-probe.py`) scanned command bytes 0x40-0x60 sequentially.

Exact sequence of commands sent (each as 8-byte packet `00 00 CMD 00 00 00 00 00`):

```
TX: 00 00 40 00 00 00 00 00  → RX: AT+E09
TX: 00 00 41 00 00 00 00 00  → RX: AT+E09
TX: 00 00 42 00 00 00 00 00  → RX: AT+E09
TX: 00 00 43 00 00 00 00 00  → RX: OK | {0000001xx} | AT+O   (GET_STATE -- known)
TX: 00 00 44 00 00 00 00 00  → RX: AT+E09
TX: 00 00 45 00 00 00 00 00  → RX: AT+E09
TX: 00 00 46 00 00 00 00 00  → RX: AT+E09
     (0x47 skipped -- known mechanical command LIFT_DISC)
TX: 00 00 48 00 00 00 00 00  → RX: AT+E09
TX: 00 00 49 00 00 00 00 00  → RX: OK | OL-Timer | 00000309 | Supply-N | 00000011 | ...
TX: 00 00 4A 00 00 00 00 00  → RX: Pick-N | 00000011 | Pick-E | 00000000 | ...
TX: 00 00 4B 00 00 00 00 00  → RX: AT+E09 | AT+E09
TX: 00 00 4C 00 00 00 00 00  → RX: AT+E09
TX: 00 00 4D 00 00 00 00 00  → RX: AT+E09
TX: 00 00 4E 00 00 00 00 00  → RX: AT+E09
TX: 00 00 4F 00 00 00 00 00  → RX: AT+E09
TX: 00 00 50 00 00 00 00 00  → RX: AT+E09
TX: 00 00 51 00 00 00 00 00  → RX: AT+E09
     (0x52 skipped -- known mechanical command PLACE/ACCEPT/REJECT)
TX: 00 00 53 00 00 00 00 00  → RX: AT+E09
TX: 00 00 54 00 00 00 00 00  → RX: AT+E09
TX: 00 00 55 00 00 00 00 00  → RX: OK | AT+O         ← last successful response
TX: 00 00 56 00 00 00 00 00  → WRITE ERROR: [Errno 2] Entity not found  ← DEVICE DIED HERE
TX: 00 00 57 00 00 00 00 00  → WRITE ERROR: [Errno 19] No such device
TX: 00 00 58 00 00 00 00 00  → WRITE ERROR: [Errno 19] No such device
     ... all subsequent commands: [Errno 19] No such device
```

After this:
1. ERROR LED turned solid red, READY LED changed from blinking to solid green
2. Device never re-enumerated on USB despite: power cycles, USB cable changes,
   different USB-C ports, different adapters, 30+ second power drain, 5+ minute
   power drain with button held, macOS USB security setting changes

### State before incident

- A DVD was physically loaded inside the Nimbie drive (tray closed)
- LEDs: OFF GREEN GREEN GREEN-BLINKING (normal operating state)
- State bits: `0000001xx` (tray closed, no disc detected by sensor)

### State after incident

- LEDs: RED GREEN GREEN GREEN (solid) -- undocumented LED pattern
- USB: device does not enumerate at all (not visible to macOS)
- Disc still physically inside the drive

### Recovery — SOLVED (2026-03-28)

Command 0x56 does NOT brick the device. It jumps to the **Microchip PIC HID Bootloader**:
- Normal mode: VID 0x1723, PID 0x0945 (Acronova NT21)
- Bootloader mode: VID 0x04D8, PID 0x000B (Microchip Technology Inc.)
- Bootloader uses Bulk endpoints EP 0x01 OUT / EP 0x81 IN, 64-byte packets
- Commands: QUERY(0x00), PROGRAM_COMPLETE(0x04), GET_DATA(0x05), RESET_DEVICE(0x06), SIGN_FLASH(0x07)

**Recovery procedure:**
1. Send RESET_DEVICE (0x06) to the bootloader
2. **Immediately** turn OFF the Nimbie hardware power switch
3. Wait 5 seconds
4. Turn it back ON
5. Device returns to normal Nimbie firmware (VID 0x1723:0x0945)

**Automated recovery:**
```bash
my-nimbie reset                     # auto-detect bootloader mode and recover
my-nimbie reset --exit-bootloader   # explicitly exit bootloader
my-nimbie reset --diagnostics       # show device state and counters
```

After `reset`, the command instructs you to power cycle the device.

**Why the initial confusion:** After 0x56, the device appeared "bricked" because:
- macOS Privacy & Security → Accessories was set to "Ask" and blocked the new
  bootloader USB device (different VID/PID) from connecting
- The bootloader has no Manufacturer/Product USB strings, so it looked dead
- Changing Accessories to "Automatically When Unlocked" made it visible

### Lesson learned

**Never send untested USB commands to hardware devices.** Even scanning "safe-looking"
byte ranges can hit firmware commands that jump to bootloader mode. The probe
script should have only tested commands found in documented sources (BS Utility binary
analysis), not blind sequential scans.

---

## Acronova Support

- Phone: 732-422-1868
- Email: support@acronova.com
- Downloads: <https://disc.acronova.com/download/product/auto-blu-ray-duplicator-publisher-ripper-nimbie-usb-nb21/9.html>
- BS Utility: Windows diagnostic tool (v3.0.0.1, 511 KB)

## References

- [Acronova NB21 Operation Manual](https://www.manualslib.com/manual/1603690/Acronova-Technology-Nb21-Series.html)
- [benroeder/nimbiestatemachine](https://github.com/benroeder/nimbiestatemachine) -- Python driver
- [nuxx.net USB 3.0 workaround](https://nuxx.net/blog/2016/01/13/workaround-for-acronova-nimbie-usb-plus-qqgettray-and-os-x-10-11-el-capitan-failure-with-usb-3-0-cable/)
