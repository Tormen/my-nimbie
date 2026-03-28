#!/usr/bin/env python3
"""Nimbie NB21 Microchip PIC HID Bootloader recovery tool.

After command 0x56, the Nimbie enters Microchip PIC HID bootloader mode:
  VID: 0x04D8  PID: 0x000B  (Microchip Technology Inc.)

This script communicates with the bootloader to:
  1. Query device info
  2. Read flash/EEPROM to check firmware integrity
  3. Exit bootloader and return to normal Nimbie firmware

Microchip HID Bootloader Protocol (AN1388):
  64-byte packets via Bulk EP 0x01 OUT / EP 0x81 IN
  Commands: QUERY(0x00), UNLOCK_CONFIG(0x01), ERASE(0x02), PROGRAM(0x03),
            PROGRAM_COMPLETE(0x04), GET_DATA(0x05), RESET_DEVICE(0x06),
            SIGN_FLASH(0x07)

Usage:
  python3 nimbie-bootloader.py --query        # Query bootloader info
  python3 nimbie-bootloader.py --read-flash   # Read flash to check firmware
  python3 nimbie-bootloader.py --exit         # Try to exit bootloader
  python3 nimbie-bootloader.py --scan         # Map all bootloader commands
  python3 nimbie-bootloader.py --raw 06       # Send raw command byte
"""

import sys
import time
import struct
import argparse

BL_VID = 0x04D8
BL_PID = 0x000B
EP_OUT = 0x01
EP_IN  = 0x81


def connect():
    """Connect to Microchip HID bootloader device."""
    import usb.core
    import usb.util

    dev = usb.core.find(idVendor=BL_VID, idProduct=BL_PID)
    if dev is None:
        # Also check if normal Nimbie is back
        nimbie = usb.core.find(idVendor=0x1723, idProduct=0x0945)
        if nimbie:
            print("SUCCESS: Normal Nimbie found (VID 0x1723:0x0945)!")
            print("Device has exited bootloader mode.")
            sys.exit(0)
        print("ERROR: Neither bootloader (0x04D8:0x000B) nor Nimbie (0x1723:0x0945) found")
        sys.exit(1)

    print(f"Found bootloader: VID={dev.idVendor:#06x} PID={dev.idProduct:#06x}")
    try:
        print(f"  Manufacturer: {dev.manufacturer}")
        print(f"  Product: {dev.product}")
    except Exception:
        pass

    # Show configuration details
    try:
        cfg = dev.get_active_configuration()
        print(f"  Configuration: {cfg.bConfigurationValue}")
        for intf in cfg:
            print(f"  Interface {intf.bInterfaceNumber}: class={intf.bInterfaceClass:#04x}")
            for ep in intf:
                direction = "IN" if ep.bEndpointAddress & 0x80 else "OUT"
                ep_type = {1: "Isoc", 2: "Bulk", 3: "Interrupt"}.get(ep.bmAttributes & 3, "?")
                print(f"    EP {ep.bEndpointAddress:#04x} {direction} {ep_type} maxpacket={ep.wMaxPacketSize}")
    except Exception as e:
        print(f"  Config read: {e}")

    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
            print("  Detached kernel driver")
    except Exception:
        pass

    try:
        dev.set_configuration()
    except Exception as e:
        print(f"  set_configuration: {e}")

    try:
        import usb.util
        usb.util.claim_interface(dev, 0)
    except Exception as e:
        print(f"  WARNING: claim_interface: {e}")

    # Drain stale data
    for _ in range(5):
        try:
            dev.read(EP_IN, 64, timeout=100)
        except Exception:
            break

    print("  Connected.\n")
    return dev


def send_raw(dev, data, label="", timeout=3000):
    """Send raw bytes (padded to 64), read response."""
    pkt = bytearray(64)
    for i, b in enumerate(data):
        if i >= 64:
            break
        pkt[i] = b

    if label:
        print(f"  TX [{label}]: {bytes(data).hex()}")
    else:
        print(f"  TX: {bytes(data).hex()}")

    try:
        dev.write(EP_OUT, pkt, timeout=5000)
    except Exception as e:
        print(f"  WRITE ERROR: {e}")
        return None

    time.sleep(0.2)

    try:
        resp = dev.read(EP_IN, 64, timeout=timeout)
        raw = bytes(resp)
        # Show non-zero portion
        trimmed = raw.rstrip(b'\x00')
        if trimmed:
            print(f"  RX: {trimmed.hex()} ({len(raw)} bytes, {len(trimmed)} non-zero)")
            # Also show ASCII if printable
            ascii_str = trimmed.decode('ascii', errors='replace')
            if all(32 <= b < 127 for b in trimmed):
                print(f"  RX (ASCII): {ascii_str}")
        else:
            print(f"  RX: (all zeros, {len(raw)} bytes)")
        return raw
    except Exception as e:
        print(f"  READ: {e} (no response)")
        return None


def query_device(dev):
    """Send QUERY_DEVICE (0x00) — returns bootloader info."""
    print("=== QUERY_DEVICE (0x00) ===")
    resp = send_raw(dev, [0x00], "QUERY_DEVICE")
    if resp:
        trimmed = resp.rstrip(b'\x00')
        if len(trimmed) >= 2 and trimmed[0] == 0x00:
            print(f"  Bootloader version/info: {trimmed[1:].hex()}")
            # Parse AN1388 format if applicable
            if len(trimmed) >= 5:
                print(f"    Byte 1: {trimmed[1]:#04x}")
                print(f"    Byte 2: {trimmed[2]:#04x}")
                print(f"    Byte 3: {trimmed[3]:#04x}")
                if len(trimmed) >= 5:
                    print(f"    Byte 4: {trimmed[4]:#04x}")
    return resp


def get_data(dev, addr, length):
    """Send GET_DATA (0x05) — read flash/EEPROM at address."""
    # AN1388 format: [0x05, addr_low, addr_high, addr_upper, len_low, len_high]
    addr_bytes = struct.pack('<I', addr)[:3]  # 3-byte little-endian address
    len_bytes = struct.pack('<H', length)      # 2-byte little-endian length
    pkt = [0x05] + list(addr_bytes) + list(len_bytes)
    label = f"GET_DATA addr={addr:#08x} len={length}"
    print(f"\n=== {label} ===")
    resp = send_raw(dev, pkt, label)
    return resp


def program_complete(dev):
    """Send PROGRAM_COMPLETE (0x04)."""
    print("\n=== PROGRAM_COMPLETE (0x04) ===")
    resp = send_raw(dev, [0x04], "PROGRAM_COMPLETE")
    return resp


def sign_flash(dev):
    """Send SIGN_FLASH (0x07)."""
    print("\n=== SIGN_FLASH (0x07) ===")
    resp = send_raw(dev, [0x07], "SIGN_FLASH")
    return resp


def reset_device(dev):
    """Send RESET_DEVICE (0x06) — should restart the PIC."""
    print("\n=== RESET_DEVICE (0x06) ===")
    resp = send_raw(dev, [0x06], "RESET_DEVICE")
    return resp


def scan_commands(dev):
    """Scan bootloader commands 0x00-0x0F to map the full command set."""
    print("=== SCANNING BOOTLOADER COMMANDS 0x00-0x0F ===\n")

    # Known Microchip HID bootloader commands
    names = {
        0x00: "QUERY_DEVICE",
        0x01: "UNLOCK_CONFIG",
        0x02: "ERASE_FLASH",
        0x03: "PROGRAM_FLASH",
        0x04: "PROGRAM_COMPLETE",
        0x05: "GET_DATA",
        0x06: "RESET_DEVICE",
        0x07: "SIGN_FLASH",
    }

    results = {}
    for cmd in range(0x10):
        name = names.get(cmd, "UNKNOWN")

        # Skip ERASE and PROGRAM — these are destructive
        if cmd in (0x01, 0x02, 0x03):
            print(f"  SKIP cmd=0x{cmd:02X} ({name}) — potentially destructive")
            results[cmd] = "SKIPPED (destructive)"
            continue

        resp = send_raw(dev, [cmd], f"cmd=0x{cmd:02X} ({name})", timeout=2000)
        if resp:
            trimmed = resp.rstrip(b'\x00')
            results[cmd] = trimmed.hex() if trimmed else "(all zeros)"
        else:
            results[cmd] = "(no response)"

        time.sleep(0.3)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for cmd in range(0x10):
        name = names.get(cmd, "")
        resp = results[cmd]
        status = ""
        if resp == "(no response)":
            status = " [no response]"
        elif "SKIPPED" in resp:
            status = " [skipped]"
        else:
            status = f" → {resp}"
        print(f"  0x{cmd:02X} {name:20s}{status}")


def try_exit_bootloader(dev):
    """Try multiple approaches to exit bootloader mode."""
    import usb.core

    print("=" * 60)
    print("ATTEMPTING TO EXIT BOOTLOADER MODE")
    print("=" * 60)

    # Approach 1: Just RESET_DEVICE
    print("\n--- Approach 1: RESET_DEVICE ---")
    reset_device(dev)
    time.sleep(3)

    # Check if normal Nimbie appeared
    nimbie = usb.core.find(idVendor=0x1723, idProduct=0x0945)
    if nimbie:
        print("\nSUCCESS! Normal Nimbie is back!")
        return True

    bl = usb.core.find(idVendor=BL_VID, idProduct=BL_PID)
    if bl:
        print("Still in bootloader. Reconnecting...")
        dev = connect()
    else:
        print("Device not found — waiting 5s for re-enumeration...")
        time.sleep(5)
        nimbie = usb.core.find(idVendor=0x1723, idProduct=0x0945)
        if nimbie:
            print("\nSUCCESS! Normal Nimbie is back!")
            return True
        dev = connect()

    # Approach 2: SIGN_FLASH then RESET
    print("\n--- Approach 2: SIGN_FLASH + RESET ---")
    sign_flash(dev)
    time.sleep(1)
    reset_device(dev)
    time.sleep(3)

    nimbie = usb.core.find(idVendor=0x1723, idProduct=0x0945)
    if nimbie:
        print("\nSUCCESS! Normal Nimbie is back!")
        return True

    bl = usb.core.find(idVendor=BL_VID, idProduct=BL_PID)
    if bl:
        print("Still in bootloader. Reconnecting...")
        dev = connect()
    else:
        time.sleep(5)
        nimbie = usb.core.find(idVendor=0x1723, idProduct=0x0945)
        if nimbie:
            print("\nSUCCESS! Normal Nimbie is back!")
            return True
        dev = connect()

    # Approach 3: PROGRAM_COMPLETE + SIGN_FLASH + RESET
    print("\n--- Approach 3: PROGRAM_COMPLETE + SIGN_FLASH + RESET ---")
    program_complete(dev)
    time.sleep(0.5)
    sign_flash(dev)
    time.sleep(0.5)
    reset_device(dev)
    time.sleep(3)

    nimbie = usb.core.find(idVendor=0x1723, idProduct=0x0945)
    if nimbie:
        print("\nSUCCESS! Normal Nimbie is back!")
        return True

    bl = usb.core.find(idVendor=BL_VID, idProduct=BL_PID)
    if bl:
        print("Still in bootloader. Reconnecting...")
        dev = connect()
    else:
        time.sleep(5)
        nimbie = usb.core.find(idVendor=0x1723, idProduct=0x0945)
        if nimbie:
            print("\nSUCCESS! Normal Nimbie is back!")
            return True
        dev = connect()

    # Approach 4: Read flash at reset vector to verify firmware present
    print("\n--- Approach 4: Check reset vector ---")
    print("Reading flash at address 0x0000 (reset vector)...")
    resp = get_data(dev, 0x0000, 64)
    if resp:
        trimmed = resp.rstrip(b'\x00')
        if trimmed and len(trimmed) > 1:
            # If reset vector is all 0xFF, flash is erased — firmware is gone
            data_part = trimmed[1:]  # Skip command echo byte
            if all(b == 0xFF for b in data_part):
                print("  WARNING: Flash appears ERASED (all 0xFF) — firmware may be missing!")
                print("  The device needs firmware re-flashing.")
            elif all(b == 0x00 for b in data_part):
                print("  Flash reads all zeros — may be read-protected or empty")
            else:
                print(f"  Flash has data: {data_part[:32].hex()}")
                print("  Firmware appears present — bootloader should be able to exit")
        else:
            print("  Empty/null response from GET_DATA")

    # Approach 5: Try USB device-level reset (not bootloader RESET command)
    print("\n--- Approach 5: USB bus reset ---")
    try:
        dev.reset()
        print("  USB dev.reset() OK")
    except Exception as e:
        print(f"  USB dev.reset(): {e}")

    time.sleep(3)
    nimbie = usb.core.find(idVendor=0x1723, idProduct=0x0945)
    if nimbie:
        print("\nSUCCESS! Normal Nimbie is back!")
        return True

    # Approach 6: Try AN1388 framed protocol (SOH + data + CRC + EOT)
    print("\n--- Approach 6: AN1388 framed RESET ---")
    print("Trying framed protocol: SOH(0x01) + CMD + CRC16 + EOT(0x04)")
    bl = usb.core.find(idVendor=BL_VID, idProduct=BL_PID)
    if bl:
        dev = connect()

    # AN1388 frame: SOH(0x01) + command_data + CRC16_LE + EOT(0x04)
    # For RESET_DEVICE: command_data = [0x09] (AN1388 uses different cmd numbers)
    # Actually AN1388 cmd numbers: READ_BOOT_INFO=1, ERASE_FLASH=2, PROGRAM_FLASH=3,
    #   READ_FLASH=4, PROGRAM_COMPLETE=5, GET_DATA=6, RESET_DEVICE=9
    for reset_cmd in [0x06, 0x09]:
        # CRC16 of just the command byte
        crc = crc16([reset_cmd])
        frame = [0x01, reset_cmd, crc & 0xFF, (crc >> 8) & 0xFF, 0x04]
        label = f"AN1388 framed RESET (cmd=0x{reset_cmd:02X})"
        resp = send_raw(dev, frame, label, timeout=2000)
        time.sleep(2)

        nimbie = usb.core.find(idVendor=0x1723, idProduct=0x0945)
        if nimbie:
            print(f"\nSUCCESS with framed cmd 0x{reset_cmd:02X}!")
            return True

    print("\n" + "=" * 60)
    print("All approaches exhausted. Device remains in bootloader mode.")
    print("=" * 60)
    print("\nPossible next steps:")
    print("  1. Re-flash firmware using Microchip's mphidflash tool")
    print("  2. Use Acronova's BS Utility on a Windows machine")
    print("  3. Contact Acronova support: 732-422-1868 / support@acronova.com")
    print("  4. Check if power cycling (hardware switch) after RESET helps")
    return False


def crc16(data):
    """CRC-16/CCITT for AN1388 framed protocol."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


def read_flash_range(dev, start_addr, total_bytes, chunk_size=32):
    """Read a range of flash memory and display it."""
    print(f"\n=== Reading flash: {start_addr:#08x} - {start_addr + total_bytes:#08x} ({total_bytes} bytes) ===\n")

    all_data = bytearray()
    offset = 0
    while offset < total_bytes:
        sz = min(chunk_size, total_bytes - offset)
        addr = start_addr + offset

        addr_bytes = struct.pack('<I', addr)[:3]
        len_bytes = struct.pack('<H', sz)
        pkt = [0x05] + list(addr_bytes) + list(len_bytes)

        resp = send_raw(dev, pkt, f"GET_DATA {addr:#08x}+{sz}", timeout=2000)
        if resp:
            trimmed = resp.rstrip(b'\x00')
            if trimmed and len(trimmed) > 1:
                data_part = trimmed[1:]  # Skip command echo
                all_data.extend(data_part)
            else:
                all_data.extend(b'\x00' * sz)
        else:
            print(f"  No response at {addr:#08x}")
            all_data.extend(b'\x00' * sz)

        offset += sz
        time.sleep(0.1)

    # Hex dump
    print(f"\n--- Hex dump ---")
    for i in range(0, len(all_data), 16):
        addr = start_addr + i
        hex_part = ' '.join(f'{b:02x}' for b in all_data[i:i+16])
        ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in all_data[i:i+16])
        print(f"  {addr:08x}: {hex_part:<48s}  {ascii_part}")

    # Analysis
    if all(b == 0xFF for b in all_data):
        print("\n  *** ALL 0xFF — flash is ERASED ***")
    elif all(b == 0x00 for b in all_data):
        print("\n  *** ALL 0x00 — flash may be read-protected ***")
    else:
        non_ff = sum(1 for b in all_data if b != 0xFF)
        print(f"\n  {non_ff}/{len(all_data)} bytes contain data (non-0xFF)")

    return all_data


def main():
    p = argparse.ArgumentParser(description="Nimbie NB21 bootloader recovery")
    p.add_argument("--query", action="store_true", help="Query bootloader info")
    p.add_argument("--scan", action="store_true", help="Scan bootloader commands 0x00-0x0F")
    p.add_argument("--exit", action="store_true", help="Try to exit bootloader mode")
    p.add_argument("--read-flash", action="store_true", help="Read flash to check firmware")
    p.add_argument("--read-addr", help="Read specific flash address (hex, e.g. 0x0000)")
    p.add_argument("--read-len", type=int, default=256, help="Bytes to read (default 256)")
    p.add_argument("--raw", help="Send raw hex command byte(s)")
    p.add_argument("--reset", action="store_true", help="Send RESET_DEVICE command only")
    args = p.parse_args()

    dev = connect()

    try:
        if args.query:
            query_device(dev)

        elif args.scan:
            scan_commands(dev)

        elif args.exit:
            try_exit_bootloader(dev)

        elif args.read_flash:
            # Read reset vector area
            read_flash_range(dev, 0x0000, 256)
            # Read bootloader config area (typical PIC locations)
            print()
            read_flash_range(dev, 0x1FC00, 64)

        elif args.read_addr:
            addr = int(args.read_addr, 0)
            read_flash_range(dev, addr, args.read_len)

        elif args.raw:
            hex_str = args.raw.replace(" ", "")
            raw_bytes = list(bytes.fromhex(hex_str))
            send_raw(dev, raw_bytes, "RAW", timeout=5000)

        elif args.reset:
            reset_device(dev)

        else:
            # Default: query + scan
            query_device(dev)
            print()
            scan_commands(dev)

    finally:
        try:
            import usb.util
            usb.util.release_interface(dev, 0)
        except Exception:
            pass

    print("\nDone.")


if __name__ == "__main__":
    main()
