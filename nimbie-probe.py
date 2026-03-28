#!/usr/bin/env python3
"""Nimbie NB21 USB command probe — reverse-engineer the full command set.

This script systematically sends command bytes to the Nimbie and records
all responses, to discover undocumented commands beyond the 5 known ones:
  0x43       GET_STATE
  0x47,0x01  LIFT_DISC
  0x52,0x01  PLACE_DISC
  0x52,0x02  ACCEPT (drop to done pile)
  0x52,0x03  REJECT (drop to reject pile)

Usage:
  python3 nimbie-probe.py                  # scan command bytes 0x00-0xFF
  python3 nimbie-probe.py --range 0x40-0x60  # scan a specific range
  python3 nimbie-probe.py --cmd 0x52 --params  # scan param bytes for cmd 0x52
  python3 nimbie-probe.py --reset          # USB device reset + check state
  python3 nimbie-probe.py --state          # just query current state
  python3 nimbie-probe.py --raw 43         # send raw hex bytes (2-16 hex chars)

CAUTION: Unknown commands may cause mechanical actions. Watch the device!
"""

import sys
import time
import argparse

EP_OUT = 0x02
EP_IN  = 0x81

def connect():
    """Connect to Nimbie, return (dev, was_kernel_detached)."""
    import usb.core
    import usb.util

    dev = usb.core.find(idVendor=0x1723, idProduct=0x0945)
    if dev is None:
        print("ERROR: Nimbie not found")
        sys.exit(1)

    print(f"Found: {dev.manufacturer} {dev.product}")

    kernel_detached = False
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
            kernel_detached = True
            print("Detached kernel driver")
    except Exception as e:
        print(f"Kernel driver check: {e}")

    try:
        dev.set_configuration()
    except Exception as e:
        print(f"set_configuration: {e}")

    try:
        usb.util.claim_interface(dev, 0)
    except Exception as e:
        print(f"ERROR: Cannot claim interface: {e}")
        sys.exit(1)

    # Drain stale data
    for _ in range(10):
        try:
            data = dev.read(EP_IN, 64, timeout=100)
            if bytes(data) == b"\x00" * len(data):
                break
        except Exception:
            break

    print("Connected and ready.\n")
    return dev, kernel_detached


def send_and_read(dev, pkt_bytes, label="", timeout=3000, max_reads=15):
    """Send packet, read all responses. Returns list of decoded strings."""
    pkt = bytearray(8)
    for i, b in enumerate(pkt_bytes):
        if i >= 8:
            break
        pkt[i] = b

    hex_str = pkt.hex()
    if label:
        print(f"  TX [{label}]: {hex_str}")
    else:
        print(f"  TX: {hex_str}")

    try:
        dev.write(EP_OUT, pkt, timeout=5000)
    except Exception as e:
        print(f"  WRITE ERROR: {e}")
        return []

    time.sleep(0.3)

    responses = []
    empty_count = 0
    for _ in range(max_reads):
        try:
            data = dev.read(EP_IN, 64, timeout=timeout)
            raw = bytes(data)
            if len(raw) == 0 or raw == b"\x00" * len(raw):
                empty_count += 1
                if empty_count >= 2:
                    break
                continue
            empty_count = 0
            text = raw.rstrip(b"\x00").decode("ascii", errors="replace")
            if text:
                responses.append(text)
        except Exception:
            break

    return responses


def get_state(dev):
    """Query state, return state dict."""
    responses = send_and_read(dev, [0x00, 0x00, 0x43], "GET_STATE")
    for r in responses:
        if r.startswith("{") and r.endswith("}"):
            bits = r[1:-1]
            print(f"  State: {bits}")
            def bit(pos):
                return pos < len(bits) and bits[pos] == "1"
            state = {
                "raw": bits,
                "disc_available": bit(1),
                "disc_in_tray": bit(3),
                "disc_lifted": bit(4),
                "tray_out": bit(5),
            }
            flags = [k for k, v in state.items() if k != "raw" and v]
            print(f"  Flags: {', '.join(flags) if flags else '(none)'}")
            return state
    print(f"  No state in responses: {responses}")
    return None


def scan_commands(dev, start, end):
    """Scan command bytes at position [2], with param=0x00."""
    known = {0x43: "GET_STATE", 0x47: "LIFT_DISC(param=0x01)", 0x52: "PLACE/ACCEPT/REJECT(param varies)"}
    results = {}

    print(f"Scanning command bytes 0x{start:02X} - 0x{end:02X} (param=0x00)")
    print(f"Known commands: {', '.join(f'0x{k:02X}={v}' for k, v in known.items())}")
    print()

    for cmd in range(start, end + 1):
        tag = known.get(cmd, "")
        label = f"cmd=0x{cmd:02X}" + (f" ({tag})" if tag else "")

        # Skip known mechanical commands to avoid unwanted actions
        if cmd in (0x47, 0x52):
            print(f"  SKIP {label} — known mechanical command")
            results[cmd] = "SKIPPED"
            continue

        responses = send_and_read(dev, [0x00, 0x00, cmd, 0x00], label, timeout=2000, max_reads=10)

        if responses:
            resp_str = " | ".join(responses)
            print(f"  RX: {resp_str}")
            results[cmd] = resp_str
        else:
            results[cmd] = "(no response)"

        time.sleep(0.2)

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY — commands that gave a response:")
    print("=" * 60)
    for cmd, resp in sorted(results.items()):
        if resp not in ("(no response)", "SKIPPED"):
            print(f"  0x{cmd:02X}: {resp}")

    skipped = [cmd for cmd, resp in results.items() if resp == "SKIPPED"]
    if skipped:
        print(f"\n  Skipped (mechanical): {', '.join(f'0x{c:02X}' for c in skipped)}")

    no_resp = [cmd for cmd, resp in results.items() if resp == "(no response)"]
    print(f"  No response: {len(no_resp)} commands")


def scan_params(dev, cmd_byte, start=0x00, end=0xFF):
    """Scan param bytes for a given command byte."""
    known_params = {
        0x52: {0x01: "PLACE_DISC", 0x02: "ACCEPT", 0x03: "REJECT"},
        0x47: {0x01: "LIFT_DISC"},
    }
    param_names = known_params.get(cmd_byte, {})
    results = {}

    print(f"Scanning params 0x{start:02X}-0x{end:02X} for command 0x{cmd_byte:02X}")
    print()

    for param in range(start, end + 1):
        tag = param_names.get(param, "")
        label = f"cmd=0x{cmd_byte:02X} param=0x{param:02X}" + (f" ({tag})" if tag else "")

        # Skip known mechanical params
        if cmd_byte == 0x52 and param in (0x01, 0x02, 0x03):
            print(f"  SKIP {label} — known mechanical")
            results[param] = "SKIPPED"
            continue
        if cmd_byte == 0x47 and param == 0x01:
            print(f"  SKIP {label} — known mechanical")
            results[param] = "SKIPPED"
            continue

        responses = send_and_read(dev, [0x00, 0x00, cmd_byte, param], label, timeout=2000, max_reads=10)

        if responses:
            resp_str = " | ".join(responses)
            print(f"  RX: {resp_str}")
            results[param] = resp_str

            # Safety: if we get an AT+S response indicating mechanical action, stop
            for r in responses:
                if r.startswith("AT+S") and r not in ("AT+S00", "AT+S03", "AT+S10", "AT+S12"):
                    print(f"  *** UNEXPECTED MECHANICAL RESPONSE — pausing for safety ***")
                    print(f"  Check device state before continuing!")
                    get_state(dev)
                    input("  Press Enter to continue or Ctrl-C to abort...")
        else:
            results[param] = "(no response)"

        time.sleep(0.2)

    # Summary
    print("\n" + "=" * 60)
    print(f"SUMMARY — params for cmd 0x{cmd_byte:02X} that gave a response:")
    print("=" * 60)
    for param, resp in sorted(results.items()):
        if resp not in ("(no response)", "SKIPPED"):
            print(f"  0x{param:02X}: {resp}")


def usb_reset(dev):
    """Perform USB device reset and reconnect."""
    print("Performing USB device reset...")
    try:
        dev.reset()
        print("  dev.reset() OK")
    except Exception as e:
        print(f"  dev.reset() error: {e}")

    time.sleep(2)
    print("Reconnecting...")
    return connect()


def main():
    p = argparse.ArgumentParser(description="Nimbie NB21 USB command probe")
    p.add_argument("--range", help="Command byte range to scan (e.g. 0x40-0x60)")
    p.add_argument("--cmd", help="Command byte for param scanning (e.g. 0x52)")
    p.add_argument("--params", action="store_true", help="Scan param bytes for --cmd")
    p.add_argument("--param-range", help="Param byte range (e.g. 0x00-0x10)")
    p.add_argument("--reset", action="store_true", help="USB device reset then check state")
    p.add_argument("--state", action="store_true", help="Just query current state")
    p.add_argument("--raw", help="Send raw hex bytes (e.g. '0052010000000000')")
    p.add_argument("--skip-mechanical", action="store_true", default=True,
                   help="Skip known mechanical commands during scan (default: true)")
    p.add_argument("--include-mechanical", action="store_true",
                   help="Include known mechanical commands (DANGER!)")
    args = p.parse_args()

    dev, kd = connect()

    try:
        if args.state:
            get_state(dev)

        elif args.reset:
            print("State BEFORE reset:")
            get_state(dev)
            dev, kd = usb_reset(dev)
            print("\nState AFTER reset:")
            get_state(dev)

        elif args.raw:
            hex_str = args.raw.replace(" ", "")
            raw_bytes = list(bytes.fromhex(hex_str))
            print(f"Sending raw bytes: {bytes(raw_bytes).hex()}")
            responses = send_and_read(dev, raw_bytes, "RAW", timeout=5000, max_reads=20)
            if responses:
                for r in responses:
                    print(f"  RX: {r}")
            else:
                print("  (no response)")

        elif args.cmd and args.params:
            cmd_byte = int(args.cmd, 0)
            ps, pe = 0x00, 0xFF
            if args.param_range:
                parts = args.param_range.split("-")
                ps = int(parts[0], 0)
                pe = int(parts[1], 0) if len(parts) > 1 else ps
            scan_params(dev, cmd_byte, ps, pe)

        else:
            # Default: scan command bytes
            start, end = 0x00, 0xFF
            if args.range:
                parts = args.range.split("-")
                start = int(parts[0], 0)
                end = int(parts[1], 0) if len(parts) > 1 else start
            scan_commands(dev, start, end)

    finally:
        try:
            import usb.util
            usb.util.release_interface(dev, 0)
            if kd:
                dev.attach_kernel_driver(0)
        except Exception:
            pass

    print("\nDone.")


if __name__ == "__main__":
    main()
