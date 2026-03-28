#!/usr/bin/env python3
"""my-nimbie — CLI controller for Nimbie USB Plus NB21 disc autoloader.

Controls the Nimbie loader/unloader mechanism via USB HID and orchestrates
batch disc processing with configurable commands (e.g. my-handbrake).
"""

# ---------------------------------------------------------------------------
# Venv bootstrap: ensure we run inside a venv with pyusb installed.
# If not, create the venv, install deps, and re-exec ourselves in it.
# ---------------------------------------------------------------------------
import os
import subprocess
import sys

VENV_DIR = os.path.expanduser("~/.python.venv/my-nimbie")
VENV_PYTHON = os.path.join(VENV_DIR, "bin", "python3")
VENV_DEPS = ["pyusb"]


def _venv_has_deps():
    """Check if venv exists and has required packages."""
    if not os.path.isfile(VENV_PYTHON):
        return False
    site_packages = os.path.join(VENV_DIR, "lib")
    if not os.path.isdir(site_packages):
        return False
    # Check for usb module (pyusb installs as 'usb')
    for d in os.listdir(site_packages):
        pkg_dir = os.path.join(site_packages, d, "site-packages", "usb")
        if os.path.isdir(pkg_dir):
            return True
    return False


def _bootstrap_venv():
    """Create venv, install deps, and re-exec."""
    print(f"\n >>> Creating python virtualenv '{VENV_DIR}'...\n", file=sys.stderr)

    rc = subprocess.call([sys.executable, "-m", "venv", VENV_DIR])
    if rc != 0:
        print(f"\n  ERROR: Failed to create venv at {VENV_DIR}\n", file=sys.stderr)
        sys.exit(1)

    pip = os.path.join(VENV_DIR, "bin", "pip")
    rc = subprocess.call([pip, "install"] + VENV_DEPS)
    if rc != 0:
        print(f"\n  ERROR: Failed to install dependencies: {', '.join(VENV_DEPS)}\n", file=sys.stderr)
        sys.exit(1)

    print(f"\n >>> DONE creating python virtualenv '{VENV_DIR}'.", file=sys.stderr)
    print("=" * 64, file=sys.stderr)

    # Re-exec with venv python, preserving all arguments
    os.execv(VENV_PYTHON, [VENV_PYTHON] + sys.argv)


# If we're not already running inside our venv, bootstrap it.
# Check sys.prefix — inside a venv it points to the venv dir, outside it points to the system python.
if os.path.realpath(sys.prefix) != os.path.realpath(VENV_DIR):
    if not _venv_has_deps():
        _bootstrap_venv()
    # Venv exists and has deps — re-exec inside it
    os.execv(VENV_PYTHON, [VENV_PYTHON] + sys.argv)

# ---------------------------------------------------------------------------
# From here on we are guaranteed to run inside the venv with pyusb available
# ---------------------------------------------------------------------------

import argparse
import configparser
import datetime
import re
import signal
import time

# ---------------------------------------------------------------------------
# USB constants for Nimbie NB21 (NT21 autoloader controller)
# ---------------------------------------------------------------------------
NIMBIE_VID = 0x1723
NIMBIE_PID = 0x0945

# Interrupt endpoints
EP_OUT = 0x02  # 8-byte max packet, interrupt OUT
EP_IN  = 0x81  # 64-byte max packet, interrupt IN

# Commands: sent as 8-byte packets with command in byte[2], param in byte[3]
CMD_GET_STATE  = (0x43,)        # query hardware state → "{xxxxxxxxx}"
CMD_PLACE_DISC = (0x52, 0x01)   # drop disc from hopper onto open tray
CMD_ACCEPT     = (0x52, 0x02)   # drop lifted disc into accept pile
CMD_REJECT     = (0x52, 0x03)   # drop lifted disc into reject pile
CMD_LIFT_DISC  = (0x47, 0x01)   # lift disc from open tray with gripper

# AT+ response codes
AT_OK          = "AT+O"         # operation success
AT_PLACED      = "AT+S07"       # disc placed on tray
AT_NO_DISC     = "AT+S00"       # no disc in tray
AT_DROPPER_ERR = "AT+S03"       # mechanism stuck or disc already lifted
AT_TRAY_WRONG  = "AT+S10"       # tray in wrong state
AT_TRAY_HAS    = "AT+S12"       # tray already has a disc
AT_HOPPER_EMPTY = "AT+S14"      # no disc in input queue
AT_HW_ERROR    = "AT+E09"       # hardware error

# State bit string positions (0-indexed within braces of "{xxxxxxxxx}")
STATE_BIT_DISC_AVAILABLE   = 1  # discs in input hopper
STATE_BIT_DISC_IN_TRAY     = 3  # disc sitting in ejected tray
STATE_BIT_DISC_LIFTED      = 4  # disc held by gripper
STATE_BIT_TRAY_OUT         = 5  # drive tray is ejected/open

# ---------------------------------------------------------------------------
# Batch flavors: maps CLI name → config key suffix
# ---------------------------------------------------------------------------
BATCH_FLAVORS = {
    "ripdvd":   "RIP_DVD",
    "ripaudio": "RIP_AUDIOCD",
    "readdvd":  "READ_DVD",
}

# ---------------------------------------------------------------------------
# Default config (built-in, used when no config file exists)
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "nimbie": {
        "vid": "0x1723",
        "pid": "0x0945",
        "mount_point": "/Volumes/DVD_VIDEO_RECORDER",
    },
    "commands": {
        "on_load_default":     '/LINKS/bin/my-handbrake dvd "$MOUNT_POINT" --all encode tvDVD',
        "on_load_rip_dvd":     '/LINKS/bin/my-handbrake dvd "$MOUNT_POINT" --all encode tvDVD',
        "on_load_rip_audiocd": 'mkdir -p "$DIR_NAME" && cd "$DIR_NAME" && cdparanoia -B -- -0 && flac --best *.wav && rm -f *.wav',
        "on_load_read_dvd":    'dvdbackup -i "$MOUNT_POINT" -o "$DIR_NAME" -M',
        "on_validate": "",
    },
    "target_dirs": {
        "default":     "",
        "rip_dvd":     "",
        "rip_audiocd": "",
        "read_dvd":    "",
    },
    "naming": {
        "name_prefix": "{INDEX}",
        "name":        " - {MEDIA_TYPE}",
        "name_postfix": "",
        "idx_padding":  "4",
        "idx_offset":   "0",
    },
    "batch": {
        "max_discs":       "0",
        "load_settle_time": "5",
        "mount_timeout":   "60",
        "poll_interval":   "2",
    },
}

CONFIG_SEARCH_PATHS = [
    os.path.expanduser("~/.my-nimbie.conf"),
    "/etc/my-nimbie.conf",
    "/LINKS/global/etc/my-nimbie",
    "/LINKS/default/my-nimbie",
]

DEFAULT_CONFIG_PATH = CONFIG_SEARCH_PATHS[0]  # ~/.my-nimbie.conf

STATUS_FILE = "/tmp/my-nimbie.status"

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
verbose = False
debug = False
dry_run = False
interrupted = False
batch_status = None  # set to BatchStatus instance during batch runs


def signal_handler(_signum, _frame):
    global interrupted
    interrupted = True
    print("\n  Interrupted — finishing current operation...", file=sys.stderr)


def sigusr1_handler(_signum, _frame):
    """Print batch status summary to stderr on SIGUSR1."""
    if batch_status:
        batch_status.print_summary()


# ---------------------------------------------------------------------------
# Batch status tracking
# ---------------------------------------------------------------------------
class BatchStatus:
    """Tracks batch progress, writes status file, handles SIGUSR1."""

    def __init__(self, flavor, mount_point, target_dir):
        self.flavor = flavor or "default"
        self.mount_point = mount_point
        self.target_dir = target_dir
        self.disc_nr = 0
        self.accepted = 0
        self.rejected = 0
        self.current = "starting"
        self.started = datetime.datetime.now()
        self.last_update = self.started

    def update(self, current, disc_nr=None):
        """Update state and rewrite status file."""
        self.current = current
        if disc_nr is not None:
            self.disc_nr = disc_nr
        self.last_update = datetime.datetime.now()
        self._write_file()

    def record_accept(self):
        self.accepted += 1
        self.update("accepting")

    def record_reject(self):
        self.rejected += 1
        self.update("rejecting")

    def finish(self, final_state="finished"):
        self.update(final_state)

    def _format_elapsed(self, now=None):
        if now is None:
            now = datetime.datetime.now()
        elapsed = now - self.started
        total_secs = int(elapsed.total_seconds())
        hours, remainder = divmod(total_secs, 3600)
        mins, secs = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {mins:02d}m {secs:02d}s"
        return f"{mins}m {secs:02d}s"

    def _ts(self, dt):
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _write_file(self):
        """Write machine-readable status file."""
        try:
            lines = [
                f"state={self.current}",
                f"flavor={self.flavor}",
                f"disc_nr={self.disc_nr}",
                f"accepted={self.accepted}",
                f"rejected={self.rejected}",
                f"current={self.current}",
                f"started={self._ts(self.started)}",
                f"last_update={self._ts(self.last_update)}",
                f"mount_point={self.mount_point}",
            ]
            if self.target_dir:
                lines.append(f"target_dir={self.target_dir}")

            with open(STATUS_FILE, "w") as f:
                f.write("\n".join(lines) + "\n")
        except OSError as e:
            dbg(f"Failed to write status file: {e}")

    def print_summary(self):
        """Print human-readable status to stderr (for SIGUSR1)."""
        now = datetime.datetime.now()
        lines = [
            "",
            "--- my-nimbie batch status ---",
            f"  Flavor:      {self.flavor}",
            f"  Running:     disc #{self.disc_nr} ({self.current})",
            f"  Accepted:    {self.accepted}",
            f"  Rejected:    {self.rejected}",
            f"  Started:     {self._ts(self.started)}",
            f"  Elapsed:     {self._format_elapsed(now)}",
            f"  Last update: {self._ts(self.last_update)}",
            "---------------------------------",
            "",
        ]
        print("\n".join(lines), file=sys.stderr)

    @staticmethod
    def remove_file():
        """Remove status file."""
        try:
            os.unlink(STATUS_FILE)
        except FileNotFoundError:
            pass
        except OSError as e:
            dbg(f"Failed to remove status file: {e}")

    @staticmethod
    def read_file():
        """Read and parse status file. Returns dict or None."""
        try:
            with open(STATUS_FILE) as f:
                data = {}
                for line in f:
                    line = line.strip()
                    if "=" in line:
                        key, _, value = line.partition("=")
                        data[key] = value
                return data if data else None
        except FileNotFoundError:
            return None
        except OSError:
            return None


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
def msg(text):
    print(text)

def vrb(text):
    if verbose:
        print(text)

def dbg(text):
    if debug:
        print(f"  DBG: {text}", file=sys.stderr)

def err(text, code=1):
    print(f"\n  ERROR: {text}\n", file=sys.stderr)
    sys.exit(code)

def warn(text):
    print(f"  WARNING: {text}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def find_config_file(explicit_path=None):
    """Find config file. Explicit path > search paths."""
    if explicit_path:
        if not os.path.isfile(explicit_path):
            err(f"Config file not found: {explicit_path}")
        return explicit_path

    for path in CONFIG_SEARCH_PATHS:
        if os.path.isfile(path):
            dbg(f"Using config: {path}")
            return path

    dbg("No config file found, using built-in defaults")
    return None


def load_config(config_path=None):
    """Load config from file, falling back to defaults."""
    config = configparser.ConfigParser()

    # Set defaults
    for section, values in DEFAULT_CONFIG.items():
        config[section] = values

    if config_path:
        config.read(config_path)
        vrb(f"  Config loaded from: {config_path}")

    return config


def generate_example_config():
    """Return the example config file content as a string."""
    return """\
# my-nimbie configuration
#
# Search order: ~/.my-nimbie.conf, /etc/my-nimbie.conf, /LINKS/global/etc/my-nimbie, /LINKS/default/my-nimbie
# Or specify explicitly: my-nimbie --config /path/to/config <command>

[nimbie]
# USB device identifiers (use system_profiler SPUSBDataType to verify)
vid = 0x1723
pid = 0x0945

# Where the optical disc mounts on macOS
mount_point = /Volumes/DVD_VIDEO_RECORDER

[commands]
# Commands executed by "batch" for each disc flavor.
#
# Available variables (use $VAR or ${VAR} syntax):
#   $MOUNT_POINT  — where the disc is mounted (from [nimbie] mount_point)
#   $TARGET_DIR   — base output directory (from [target_dirs] or --target-dir)
#   $DIR_NAME     — full output path: TARGET_DIR / <generated dir name from [naming]>
#   $DISC_NR      — sequential disc number (1, 2, 3, ...)
#
# "batch" (no flavor)  → on_load_DEFAULT
# "batch ripdvd"       → on_load_RIP_DVD
# "batch ripaudio"     → on_load_RIP_AUDIOCD
# "batch readdvd"      → on_load_READ_DVD

# DEFAULT: encode DVD titles to MKV via my-handbrake
on_load_DEFAULT = /LINKS/bin/my-handbrake dvd "$MOUNT_POINT" --all encode tvDVD

# RIPDVD: encode DVD titles to MKV via my-handbrake (same as default, customize as needed)
on_load_RIP_DVD = /LINKS/bin/my-handbrake dvd "$MOUNT_POINT" --all encode tvDVD

# RIPAUDIO: rip audio CD tracks to FLAC (max quality) via cdparanoia + flac
on_load_RIP_AUDIOCD = mkdir -p "$DIR_NAME" && cd "$DIR_NAME" && cdparanoia -B -- -0 && flac --best *.wav && rm -f *.wav

# READDVD: full DVD backup (all content, mirror mode) via dvdbackup
on_load_READ_DVD = dvdbackup -i "$MOUNT_POINT" -o "$DIR_NAME" -M

# Optional: validation command run AFTER on_load (exit 0 = accept disc, non-zero = reject).
# If empty, the on_load exit code determines accept/reject.
on_validate =

[target_dirs]
# Base output directories for each batch flavor.
# Can be overridden per invocation with: my-nimbie batch --target-dir /path ripdvd
# The per-disc subdirectory name is built from [naming] settings below.
default =
rip_dvd =
rip_audiocd =
read_dvd =

[naming]
# Per-disc subdirectory naming within TARGET_DIR.
#
# The directory name is assembled as:  {NAME_PREFIX}{NAME}{NAME_POSTFIX}
#
# Supported {VARIABLE} placeholders (case-insensitive):
#   {INDEX}       — disc index: DISC_NR + idx_offset, zero-padded to idx_padding digits
#   {DISC_NR}     — raw disc number (1, 2, 3, ...) without padding or offset
#   {MEDIA_TYPE}  — "DVD" or "CD" (auto-detected from disc content)
#   {DVD_TITLE}   — volume name of the disc (read only when needed, e.g. "LOTR_DISC_1")
#   {FLAVOR}      — batch flavor name ("default", "ripdvd", "ripaudio", "readdvd")
#   {DATE}        — current date as YYYY-MM-DD
#
# Examples:
#   name_prefix = {INDEX}                 → "0001"
#   name        =  - {MEDIA_TYPE}         → " - DVD"
#   name_postfix =                        → ""
#   Result: "0001 - DVD"
#
#   name_prefix = {DVD_TITLE}             → "LOTR_DISC_1"
#   name        =  ({INDEX})              → " (0001)"
#   Result: "LOTR_DISC_1 (0001)"
#
#   name_prefix = {DATE}_{INDEX}          → "2026-03-28_0005"
#   name        =  - {DVD_TITLE}          → " - LOTR_DISC_1"
#   Result: "2026-03-28_0005 - LOTR_DISC_1"

name_prefix = {INDEX}
name = " - {MEDIA_TYPE}"
name_postfix =

# Zero-padding width for {INDEX} (e.g. 4 → "0001", 2 → "01")
idx_padding = 4

# Offset added to DISC_NR for {INDEX} (can be negative)
# Useful to continue numbering from a previous batch: --idx-offset 50
idx_offset = 0

[batch]
# Max discs to process (0 = unlimited, process until hopper is empty)
max_discs = 0

# Seconds to wait after loading before checking if disc mounted
load_settle_time = 5

# Seconds to wait for disc to mount before giving up and rejecting
mount_timeout = 60

# Seconds between mount-point polling checks
poll_interval = 2
"""


def cmd_create_config(args):
    """Create an example config file."""
    path = args.create_config_path or DEFAULT_CONFIG_PATH

    if os.path.exists(path):
        err(f"Config file already exists: {path}\n"
            f"  Remove it first or choose a different path:\n"
            f"    my-nimbie --create-config /other/path")

    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        err(f"Parent directory does not exist: {parent}")

    content = generate_example_config()
    with open(path, "w") as f:
        f.write(content)

    msg(f"Example config written to: {path}")
    msg(f"  Edit it to match your setup, then run: my-nimbie batch")


# ---------------------------------------------------------------------------
# Media type detection and DVD title reading
# ---------------------------------------------------------------------------
def detect_media_type(mount_point):
    """Detect whether the mounted disc is a DVD or CD.
    Returns "DVD" if VIDEO_TS or AUDIO_TS exists, otherwise "CD"."""
    for subdir in ("VIDEO_TS", "video_ts", "AUDIO_TS", "audio_ts"):
        if os.path.isdir(os.path.join(mount_point, subdir)):
            return "DVD"
    return "CD"


def read_dvd_title(mount_point):
    """Read the volume name of the mounted disc. Returns the volume label or ""."""
    # On macOS, the mount point basename IS the volume name
    if os.path.ismount(mount_point):
        title = os.path.basename(mount_point)
        if title:
            return title

    # Fallback: try diskutil
    try:
        result = subprocess.run(
            ["/usr/sbin/diskutil", "info", mount_point],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "Volume Name:" in line:
                    return line.split(":", 1)[1].strip()
    except Exception as e:
        dbg(f"read_dvd_title diskutil failed: {e}")

    return ""


# ---------------------------------------------------------------------------
# Directory naming
# ---------------------------------------------------------------------------
_dvd_title_cache = {}  # mount_point → title (lazy, read once per disc)


def expand_naming_vars(template, variables):
    """Expand {VARIABLE} placeholders in a naming template. Case-insensitive."""
    def replacer(m):
        key = m.group(1).upper()
        if key in variables:
            return variables[key]
        # Unknown variable — leave as-is
        return m.group(0)
    return re.sub(r"\{([^}]+)\}", replacer, template)


def build_dir_name(config, disc_nr, mount_point, flavor, cli_naming):
    """Build the per-disc subdirectory name from [naming] config + CLI overrides.

    cli_naming is a dict with optional keys: prefix, name, postfix, idx_offset, idx_padding.
    """
    # Read naming settings: CLI overrides > config
    name_prefix  = cli_naming.get("prefix")  if cli_naming.get("prefix")  is not None else config.get("naming", "name_prefix",  fallback="{INDEX}")
    name         = cli_naming.get("name")    if cli_naming.get("name")    is not None else config.get("naming", "name",         fallback=" - {MEDIA_TYPE}")
    name_postfix = cli_naming.get("postfix") if cli_naming.get("postfix") is not None else config.get("naming", "name_postfix", fallback="")
    idx_padding  = cli_naming.get("idx_padding") if cli_naming.get("idx_padding") is not None else config.getint("naming", "idx_padding", fallback=4)
    idx_offset   = cli_naming.get("idx_offset")  if cli_naming.get("idx_offset")  is not None else config.getint("naming", "idx_offset",  fallback=0)

    idx_padding = int(idx_padding)
    idx_offset = int(idx_offset)

    index_val = disc_nr + idx_offset
    index_str = str(index_val).zfill(idx_padding)

    # Build variables dict — lazy-evaluate DVD_TITLE only if referenced
    full_template = name_prefix + name + name_postfix
    needs_dvd_title = "{DVD_TITLE}" in full_template.upper()
    needs_media_type = "{MEDIA_TYPE}" in full_template.upper()

    media_type = ""
    if needs_media_type or needs_dvd_title:
        if dry_run:
            media_type = "DVD"
        else:
            media_type = detect_media_type(mount_point)

    dvd_title = ""
    if needs_dvd_title:
        if dry_run:
            dvd_title = "DISC_TITLE"
        elif mount_point in _dvd_title_cache:
            dvd_title = _dvd_title_cache[mount_point]
        else:
            dvd_title = read_dvd_title(mount_point)
            _dvd_title_cache[mount_point] = dvd_title
            vrb(f"  DVD title: {dvd_title}")

    variables = {
        "INDEX":      index_str,
        "DISC_NR":    str(disc_nr),
        "MEDIA_TYPE": media_type,
        "DVD_TITLE":  dvd_title,
        "FLAVOR":     flavor or "default",
        "DATE":       datetime.date.today().isoformat(),
    }

    dir_name = expand_naming_vars(name_prefix, variables) + \
               expand_naming_vars(name, variables) + \
               expand_naming_vars(name_postfix, variables)

    return dir_name


# ---------------------------------------------------------------------------
# USB HID communication
# ---------------------------------------------------------------------------
class NimbieDevice:
    """Direct USB interface to Nimbie NB21 (NT21 autoloader controller).

    Communication uses interrupt endpoints (not control transfers):
      EP 0x02 OUT (8 bytes) — send commands
      EP 0x81 IN (64 bytes) — read ASCII responses

    Command format: 8-byte packet, command in byte[2], param in byte[3].
    Responses: null-terminated ASCII strings ("OK", "{state}", "AT+code").
    """

    def __init__(self, vid=NIMBIE_VID, pid=NIMBIE_PID):
        self.vid = vid
        self.pid = pid
        self.dev = None
        self._kernel_detached = False
        self._drutil_drive_nr = None  # cached drutil drive number

    def connect(self):
        """Find and claim the Nimbie USB device."""
        try:
            import usb.core
            import usb.util
        except ImportError:
            err("pyusb not installed. Install with: pip3 install pyusb\n"
                "  Also ensure libusb is available: brew install libusb")

        self.dev = usb.core.find(idVendor=self.vid, idProduct=self.pid)
        if self.dev is None:
            err(f"Nimbie device not found (VID={self.vid:#06x}, PID={self.pid:#06x}).\n\n"
                f"  Possible reasons:\n"
                f"    - Device not connected via USB\n"
                f"    - Device not powered on\n"
                f"    - Wrong VID/PID in config (check with: system_profiler SPUSBDataType)\n"
                f"    - libusb not installed (brew install libusb)")

        # Detach kernel driver if active
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
                self._kernel_detached = True
                dbg("Detached kernel driver from interface 0")
        except Exception as e:
            dbg(f"Kernel driver check: {e}")

        # Reset device to ensure clean state — required on macOS for
        # non-root users to get proper endpoint access via libusb
        try:
            self.dev.reset()
            dbg("USB device reset OK")
        except Exception as e:
            dbg(f"USB reset: {e}")

        try:
            self.dev.set_configuration()
            dbg("set_configuration OK")
        except usb.core.USBError as e:
            warn(f"set_configuration failed: {e} — trying to continue")

        try:
            usb.util.claim_interface(self.dev, 0)
            dbg("claim_interface OK")
        except usb.core.USBError as e:
            err(f"Cannot claim Nimbie USB interface: {e}\n\n"
                f"  Possible reasons:\n"
                f"    - Another program is using the device\n"
                f"    - macOS security is blocking USB access\n"
                f"      Check: System Settings → Privacy & Security → USB\n"
                f"    - Try unplugging and reconnecting the device")

        # Drain any stale data from the IN endpoint
        try:
            while True:
                self.dev.read(EP_IN, 64, timeout=200)
        except Exception:
            pass

        # Wake-up handshake: send a GET_STATE and discard the response.
        # After a USB reset, the first command often gets no reply.
        try:
            pkt = bytearray(8)
            pkt[2] = CMD_GET_STATE[0]
            self.dev.write(EP_OUT, pkt, timeout=5000)
            time.sleep(0.5)
            while True:
                self.dev.read(EP_IN, 64, timeout=1000)
        except Exception:
            pass

        vrb(f"  Nimbie connected (VID={self.vid:#06x}, PID={self.pid:#06x})")

    def disconnect(self):
        """Release the USB device."""
        if self.dev is None:
            return
        try:
            import usb.util
            usb.util.release_interface(self.dev, 0)
            if self._kernel_detached:
                self.dev.attach_kernel_driver(0)
        except Exception as e:
            dbg(f"disconnect: {e}")
        self.dev = None

    # -- Low-level USB I/O --

    def _send_command(self, cmd_tuple, description=""):
        """Send a command via interrupt OUT endpoint.

        cmd_tuple: tuple of command bytes, placed at byte[2] onward in an 8-byte packet.
        """
        if self.dev is None:
            err("Not connected to Nimbie device")

        pkt = bytearray(8)
        for i, b in enumerate(cmd_tuple):
            pkt[2 + i] = b

        dbg(f"Sending {description}: {pkt.hex()}")

        try:
            self.dev.write(EP_OUT, pkt, timeout=5000)
        except Exception as e:
            err(f"USB write failed ({description}): {e}\n\n"
                f"  Possible reasons:\n"
                f"    - Device disconnected or not powered on\n"
                f"    - USB interface not properly claimed\n"
                f"    - macOS blocking USB access — check:\n"
                f"      System Settings → Privacy & Security → USB\n"
                f"    - Try: unplug device, wait 5s, reconnect")

    def _read_responses(self, timeout=3000, max_reads=20):
        """Read all pending responses from interrupt IN endpoint.

        Returns a list of ASCII strings. Reads until an empty response or timeout.
        """
        responses = []
        for _ in range(max_reads):
            try:
                data = self.dev.read(EP_IN, 64, timeout=timeout)
                if len(data) == 0:
                    break
                text = bytes(data).rstrip(b"\x00").decode("ascii", errors="replace")
                if text:
                    responses.append(text)
                    dbg(f"  Received: \"{text}\"")
            except Exception as e:
                dbg(f"  Read ended: {e}")
                break
        return responses

    def _send_and_read(self, cmd_tuple, description=""):
        """Send command and collect all responses."""
        self._send_command(cmd_tuple, description)
        time.sleep(0.3)
        return self._read_responses()

    def _find_at_response(self, responses):
        """Find the AT+ response code in a list of responses."""
        for r in responses:
            if r.startswith("AT+"):
                return r
        return None

    def _find_state_string(self, responses):
        """Find the {xxxxxxxxx} state string in responses."""
        for r in responses:
            if r.startswith("{") and r.endswith("}"):
                return r[1:-1]  # strip braces
        return None

    # -- State query --

    def get_state(self):
        """Query and return device state as a dict."""
        responses = self._send_and_read(CMD_GET_STATE, "GET_STATE")
        bits = self._find_state_string(responses)

        if bits is None:
            err(f"No state response from Nimbie device.\n"
                f"  Responses received: {responses}\n\n"
                f"  Possible reasons:\n"
                f"    - Device not responding\n"
                f"    - USB communication error")

        dbg(f"State bits: {bits}")

        def bit(pos):
            return pos < len(bits) and bits[pos] == "1"

        return {
            "raw":            bits,
            "disc_available":  bit(STATE_BIT_DISC_AVAILABLE),
            "disc_in_tray":    bit(STATE_BIT_DISC_IN_TRAY),
            "disc_lifted":     bit(STATE_BIT_DISC_LIFTED),
            "tray_out":        bit(STATE_BIT_TRAY_OUT),
        }

    def _poll_state(self, condition_fn, description, timeout=30, interval=0.5):
        """Poll get_state() until condition_fn(state) returns True."""
        start = time.time()
        while True:
            if time.time() - start > timeout:
                err(f"Timeout waiting for: {description} (after {timeout}s)")
            state = self.get_state()
            if condition_fn(state):
                return state
            time.sleep(interval)

    # -- Tray control via drutil (macOS) --

    def _find_drutil_drive(self):
        """Find the drutil drive number for the Nimbie's optical drive."""
        if self._drutil_drive_nr is not None:
            return self._drutil_drive_nr

        try:
            result = subprocess.run(["drutil", "list"], capture_output=True, text=True, timeout=10)
            # drutil list output: lines like "  1  VENDOR  MODEL  FW"
            for line in result.stdout.splitlines():
                parts = line.split()
                if parts and parts[0].isdigit():
                    # Take the first (or only) optical drive
                    self._drutil_drive_nr = parts[0]
                    dbg(f"drutil drive: {self._drutil_drive_nr}")
                    return self._drutil_drive_nr
        except Exception as e:
            dbg(f"drutil list failed: {e}")

        # Fallback to drive 1
        self._drutil_drive_nr = "1"
        return self._drutil_drive_nr

    def open_tray(self):
        """Open the optical drive tray via drutil."""
        drive = self._find_drutil_drive()
        vrb(f"  Opening tray (drutil drive {drive})...")
        try:
            subprocess.run(["drutil", "-drive", drive, "tray", "eject"],
                           capture_output=True, timeout=15)
        except Exception as e:
            warn(f"drutil tray eject failed: {e}")

        # Poll until tray is actually out
        self._poll_state(lambda s: s["tray_out"], "tray to open", timeout=15)

    def close_tray(self):
        """Close the optical drive tray via drutil."""
        drive = self._find_drutil_drive()
        vrb(f"  Closing tray (drutil drive {drive})...")
        try:
            subprocess.run(["drutil", "-drive", drive, "tray", "close"],
                           capture_output=True, timeout=15)
        except Exception as e:
            warn(f"drutil tray close failed: {e}")

        # Poll until tray is closed
        self._poll_state(lambda s: not s["tray_out"], "tray to close", timeout=15)

    # -- Autoloader mechanism commands --

    def place_disc(self):
        """Place a disc from the hopper onto the open tray.

        Returns True if there are more discs in the hopper, False if this was the last one.
        """
        responses = self._send_and_read(CMD_PLACE_DISC, "PLACE_DISC")
        at = self._find_at_response(responses)

        if at == AT_HOPPER_EMPTY:
            return False
        elif at == AT_PLACED:
            # Wait for disc to settle in tray + dropper retract
            time.sleep(0.8)
            return True
        elif at == AT_TRAY_WRONG:
            err("Cannot place disc: tray is not open.\n"
                "  Open the tray first with: my-nimbie load")
        elif at == AT_TRAY_HAS:
            err("Cannot place disc: tray already has a disc.")
        else:
            err(f"Unexpected response from PLACE_DISC: {at}\n"
                f"  All responses: {responses}")

    def lift_disc(self):
        """Lift disc from open tray with the gripper mechanism."""
        responses = self._send_and_read(CMD_LIFT_DISC, "LIFT_DISC")
        at = self._find_at_response(responses)

        if at == AT_OK:
            # Poll until disc is lifted
            self._poll_state(lambda s: s["disc_lifted"], "disc to be lifted", timeout=10)
        elif at == AT_NO_DISC:
            warn("No disc in tray to lift")
        elif at == AT_DROPPER_ERR:
            err("Lift mechanism error (disc may already be lifted or mechanism stuck)")
        else:
            err(f"Unexpected response from LIFT_DISC: {at}\n"
                f"  All responses: {responses}")

    def accept_disc(self):
        """Drop a lifted disc into the accept (done) pile."""
        responses = self._send_and_read(CMD_ACCEPT, "ACCEPT_DISC")
        at = self._find_at_response(responses)

        if at == AT_OK:
            # Poll until disc is no longer lifted
            self._poll_state(lambda s: not s["disc_lifted"], "disc to drop to accept", timeout=10)
        else:
            err(f"Unexpected response from ACCEPT_DISC: {at}\n"
                f"  All responses: {responses}")

    def reject_disc(self):
        """Drop a lifted disc into the reject pile."""
        responses = self._send_and_read(CMD_REJECT, "REJECT_DISC")
        at = self._find_at_response(responses)

        if at == AT_OK:
            self._poll_state(lambda s: not s["disc_lifted"], "disc to drop to reject", timeout=10)
        else:
            err(f"Unexpected response from REJECT_DISC: {at}\n"
                f"  All responses: {responses}")

    # -- High-level operations --

    def load_disc(self):
        """Load next disc from hopper into drive. Returns False if hopper empty."""
        vrb("  Opening tray...")
        self.open_tray()

        vrb("  Placing disc from hopper...")
        has_more = self.place_disc()

        vrb("  Closing tray...")
        self.close_tray()

        if not has_more:
            warn("Hopper is empty — this was the last disc")
        return has_more

    def eject_accept(self):
        """Eject current disc to the accept (done) bin."""
        vrb("  Opening tray...")
        self.open_tray()

        vrb("  Lifting disc...")
        self.lift_disc()

        vrb("  Closing tray...")
        self.close_tray()

        vrb("  Dropping to accept bin...")
        self.accept_disc()

    def eject_reject(self):
        """Eject current disc to the reject bin."""
        vrb("  Opening tray...")
        self.open_tray()

        vrb("  Lifting disc...")
        self.lift_disc()

        vrb("  Closing tray...")
        self.close_tray()

        vrb("  Dropping to reject bin...")
        self.reject_disc()


# ---------------------------------------------------------------------------
# Dry-run stub
# ---------------------------------------------------------------------------
class NimbieDeviceDryRun:
    """Stub that prints operations instead of executing them."""

    def __init__(self):
        self._load_count = 0
        self._max_demo_discs = 3

    def connect(self):
        msg("  [DRY-RUN] Would connect to Nimbie device")

    def disconnect(self):
        msg("  [DRY-RUN] Would disconnect from Nimbie device")

    def get_state(self):
        msg("  [DRY-RUN] Would query device state")
        empty = self._load_count >= self._max_demo_discs
        return {
            "disc_available": not empty,
            "disc_in_tray": self._load_count > 0,
            "disc_lifted": False,
            "tray_out": False,
        }

    def load_disc(self):
        self._load_count += 1
        has_more = self._load_count < self._max_demo_discs
        msg(f"  [DRY-RUN] Would load next disc from hopper (demo disc {self._load_count}/{self._max_demo_discs})")
        return has_more

    def eject_accept(self):
        msg("  [DRY-RUN] Would eject disc to accept (done) bin")

    def eject_reject(self):
        msg("  [DRY-RUN] Would eject disc to reject bin")


# ---------------------------------------------------------------------------
# Disc mount detection
# ---------------------------------------------------------------------------
def wait_for_mount(mount_point, timeout, poll_interval):
    """Wait for a disc to appear at mount_point. Returns True if mounted."""
    vrb(f"  Waiting for disc to mount at {mount_point} (timeout: {timeout}s)...")
    start = time.time()

    while time.time() - start < timeout:
        if interrupted:
            return False
        if os.path.ismount(mount_point) or os.path.isdir(os.path.join(mount_point, "VIDEO_TS")):
            vrb(f"  Disc mounted at {mount_point}")
            return True
        time.sleep(poll_interval)

    return False


def unmount_disc(mount_point):
    """Unmount the disc before mechanical eject."""
    if not os.path.ismount(mount_point):
        dbg(f"Not mounted: {mount_point}")
        return True

    vrb(f"  Unmounting {mount_point}...")
    try:
        result = subprocess.run(
            ["/usr/sbin/diskutil", "unmount", mount_point],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            vrb(f"  Unmounted successfully")
            return True
        else:
            warn(f"Unmount failed: {result.stderr.strip()}")
            return False
    except subprocess.TimeoutExpired:
        warn(f"Unmount timed out for {mount_point}")
        return False


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------
def expand_command(cmd_template, mount_point, disc_nr, target_dir, dir_name):
    """Expand $VAR and ${VAR} variables in command template."""
    cmd = cmd_template
    for var, val in [("MOUNT_POINT", mount_point), ("TARGET_DIR", target_dir),
                     ("DIR_NAME", dir_name), ("DISC_NR", str(disc_nr))]:
        cmd = cmd.replace(f"${{{var}}}", val)
        cmd = cmd.replace(f"${var}", val)
    return cmd


def run_command(cmd_template, mount_point, disc_nr, target_dir, dir_name):
    """Run a shell command with variable expansion. Returns exit code."""
    cmd = expand_command(cmd_template, mount_point, disc_nr, target_dir, dir_name)

    msg(f"\n  Running: {cmd}")

    if dry_run:
        msg("  [DRY-RUN] Would execute above command")
        return 0

    try:
        result = subprocess.run(cmd, shell=True)
        vrb(f"  Command exited with code: {result.returncode}")
        return result.returncode
    except KeyboardInterrupt:
        warn("Command interrupted")
        return 130


# ---------------------------------------------------------------------------
# Resolve batch flavor → on_load key + target_dir
# ---------------------------------------------------------------------------
def resolve_batch_flavor(config, flavor, cli_target_dir):
    """Resolve flavor to (on_load_command, target_dir). Exits on error."""
    if flavor is None:
        config_suffix = "DEFAULT"
        target_key = "default"
    else:
        config_suffix = BATCH_FLAVORS.get(flavor)
        if config_suffix is None:
            err(f"Unknown batch flavor: '{flavor}'\n\n"
                f"  Available flavors:\n"
                f"    (none)     — uses on_load_DEFAULT\n" +
                "".join(f"    {name:10s} — uses on_load_{BATCH_FLAVORS[name]}\n" for name in BATCH_FLAVORS))
        target_key = config_suffix.lower()

    on_load_key = f"on_load_{config_suffix}".lower()
    on_load = config.get("commands", on_load_key, fallback="")

    if not on_load:
        err(f"No command configured for [commands] on_load_{config_suffix}\n\n"
            f"  Set it in your config file or create one with:\n"
            f"    my-nimbie --create-config")

    # target_dir: CLI param overrides config
    if cli_target_dir:
        target_dir = cli_target_dir
    else:
        target_dir = config.get("target_dirs", target_key, fallback="")

    return on_load, target_dir


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_load(nimbie, config, _args):
    mount_point = config.get("nimbie", "mount_point")
    mount_timeout = config.getfloat("batch", "mount_timeout")
    poll_interval = config.getfloat("batch", "poll_interval")
    settle_time = config.getfloat("batch", "load_settle_time")

    msg("Loading next disc...")
    has_more = nimbie.load_disc()

    if not dry_run:
        vrb(f"  Waiting {settle_time}s for disc to settle...")
        time.sleep(settle_time)

        if not wait_for_mount(mount_point, mount_timeout, poll_interval):
            warn(f"Disc did not mount at {mount_point} within {mount_timeout}s")
            return False

    msg("  Disc loaded and mounted.")
    return has_more


def cmd_eject(nimbie, config, _args):
    mount_point = config.get("nimbie", "mount_point")
    msg("Accepting disc (eject to done bin)...")
    unmount_disc(mount_point)
    nimbie.eject_accept()
    msg("  Disc accepted.")


def cmd_reject(nimbie, config, _args):
    mount_point = config.get("nimbie", "mount_point")
    msg("Rejecting disc (eject to reject bin)...")
    unmount_disc(mount_point)
    nimbie.eject_reject()
    msg("  Disc rejected.")


def cmd_status(nimbie, _config, _args):
    # Check if a batch is running (status file exists with non-finished state)
    sf = BatchStatus.read_file()
    if sf and sf.get("state") not in ("finished", "interrupted"):
        msg(f"Batch in progress (from status file {STATUS_FILE}):")
        msg(f"  Flavor:      {sf.get('flavor', '?')}")
        msg(f"  Running:     disc #{sf.get('disc_nr', '?')} ({sf.get('current', '?')})")
        msg(f"  Accepted:    {sf.get('accepted', '?')}")
        msg(f"  Rejected:    {sf.get('rejected', '?')}")
        msg(f"  Started:     {sf.get('started', '?')}")
        started_str = sf.get("started")
        if started_str:
            try:
                started = datetime.datetime.strptime(started_str, "%Y-%m-%d %H:%M:%S")
                elapsed = datetime.datetime.now() - started
                total_secs = int(elapsed.total_seconds())
                hours, remainder = divmod(total_secs, 3600)
                mins, secs = divmod(remainder, 60)
                if hours > 0:
                    elapsed_str = f"{hours}h {mins:02d}m {secs:02d}s"
                else:
                    elapsed_str = f"{mins}m {secs:02d}s"
                msg(f"  Elapsed:     {elapsed_str}")
            except ValueError:
                pass
        msg(f"  Last update: {sf.get('last_update', '?')}")
        if sf.get("target_dir"):
            msg(f"  Target dir:  {sf['target_dir']}")
        msg(f"\n  Tip: send SIGUSR1 to the batch process for a live status dump:")
        msg(f"    kill -USR1 <pid>")
        return

    # No batch running — query USB device directly
    msg("Querying Nimbie status...")
    state = nimbie.get_state()
    msg(f"  Disc available: {state['disc_available']}")
    msg(f"  Disc in tray:   {state['disc_in_tray']}")
    msg(f"  Disc lifted:    {state['disc_lifted']}")
    msg(f"  Tray out:       {state['tray_out']}")


def cmd_batch(nimbie, config, args):
    global batch_status

    mount_point = config.get("nimbie", "mount_point")
    on_validate = config.get("commands", "on_validate")
    max_discs = config.getint("batch", "max_discs")
    mount_timeout = config.getfloat("batch", "mount_timeout")
    poll_interval = config.getfloat("batch", "poll_interval")
    settle_time = config.getfloat("batch", "load_settle_time")

    flavor = args.flavor
    cli_target_dir = args.target_dir
    on_load, target_dir = resolve_batch_flavor(config, flavor, cli_target_dir)

    # Collect CLI naming overrides
    cli_naming = {
        "prefix":      args.prefix,
        "name":        args.name,
        "postfix":     args.postfix,
        "idx_padding": args.idx_padding,
        "idx_offset":  args.idx_offset,
    }

    flavor_label = flavor or "default"

    # Initialize status tracking
    status = BatchStatus(flavor, mount_point, target_dir)
    batch_status = status

    # Install SIGUSR1 handler for live status queries
    signal.signal(signal.SIGUSR1, sigusr1_handler)

    msg(f"Batch mode started (flavor: {flavor_label})")
    msg(f"  Command:    {on_load}")
    msg(f"  Mount:      {mount_point}")
    if target_dir:
        msg(f"  Target dir: {target_dir}")
    if max_discs > 0:
        msg(f"  Max discs:  {max_discs}")
    msg(f"  Status:     {STATUS_FILE}")
    msg(f"  Live query: kill -USR1 {os.getpid()}")

    # Show naming preview
    preview_dir_name = build_dir_name(config, 1, mount_point, flavor, cli_naming)
    msg(f"  Dir naming: {preview_dir_name}  (preview for disc #1)")

    status.update("running")

    try:
        while not interrupted:
            status.disc_nr += 1

            if max_discs > 0 and status.disc_nr > max_discs:
                msg(f"\n  Reached max_discs limit ({max_discs}). Stopping.")
                break

            msg(f"\n{'=' * 60}")
            msg(f"  Disc #{status.disc_nr}")
            msg(f"{'=' * 60}")

            # Load
            status.update("loading", status.disc_nr)
            msg("  Loading disc from hopper...")
            has_more = nimbie.load_disc()

            if not dry_run:
                vrb(f"  Waiting {settle_time}s for disc to settle...")
                time.sleep(settle_time)

                if not wait_for_mount(mount_point, mount_timeout, poll_interval):
                    warn(f"Disc did not mount within {mount_timeout}s — rejecting")
                    nimbie.eject_reject()
                    status.record_reject()
                    if not has_more:
                        msg("  Hopper empty. Stopping.")
                        break
                    continue

            # Clear DVD title cache for new disc
            _dvd_title_cache.clear()

            # Build per-disc directory name
            dir_name_part = build_dir_name(config, status.disc_nr, mount_point, flavor, cli_naming)
            if target_dir:
                dir_name = os.path.join(target_dir, dir_name_part)
            else:
                dir_name = dir_name_part
            msg(f"  Dir name:   {dir_name}")

            # Run command
            status.update("processing")
            rc = run_command(on_load, mount_point, status.disc_nr, target_dir, dir_name)

            # Validate
            if on_validate:
                rc = run_command(on_validate, mount_point, status.disc_nr, target_dir, dir_name)

            # Accept or reject
            unmount_disc(mount_point)

            if rc == 0:
                msg(f"  Disc #{status.disc_nr}: ACCEPTING (command succeeded)")
                nimbie.eject_accept()
                status.record_accept()
            else:
                msg(f"  Disc #{status.disc_nr}: REJECTING (command failed with exit code {rc})")
                nimbie.eject_reject()
                status.record_reject()

            if not has_more:
                msg("  Hopper empty. Stopping.")
                break

        # Summary
        final_state = "interrupted" if interrupted else "finished"
        status.finish(final_state)

        msg(f"\n{'=' * 60}")
        msg(f"  Batch {final_state} (flavor: {flavor_label})")
        msg(f"    Processed: {status.disc_nr}")
        msg(f"    Accepted:  {status.accepted}")
        msg(f"    Rejected:  {status.rejected}")
        msg(f"    Elapsed:   {status._format_elapsed()}")
        msg(f"{'=' * 60}")

    finally:
        batch_status = None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
NAMING_VARS_HELP = """\
  {INDEX}       — disc index (DISC_NR + idx_offset), zero-padded to idx_padding digits
  {DISC_NR}     — raw disc number (1, 2, 3, ...) without padding or offset
  {MEDIA_TYPE}  — "DVD" or "CD" (auto-detected from disc content)
  {DVD_TITLE}   — volume name of the disc (lazy: only read when referenced)
  {FLAVOR}      — batch flavor name ("default", "ripdvd", "ripaudio", "readdvd")
  {DATE}        — current date as YYYY-MM-DD"""


def build_parser():
    parser = argparse.ArgumentParser(
        prog="my-nimbie",
        description="CLI controller for Nimbie USB Plus NB21 disc autoloader.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""\
Examples:
  my-nimbie load                          Load next disc from hopper
  my-nimbie eject                         Eject disc to accept (done) bin
  my-nimbie reject                        Eject disc to reject bin
  my-nimbie status                        Show device state or batch progress

  my-nimbie batch                         Batch process using on_load_DEFAULT command
  my-nimbie batch ripdvd                  Batch process using on_load_RIP_DVD command
  my-nimbie batch ripaudio                Batch process using on_load_RIP_AUDIOCD command
  my-nimbie batch readdvd                 Batch process using on_load_READ_DVD command
  my-nimbie batch --target-dir /out ripdvd   Override output directory for this run

  my-nimbie batch --prefix "{{DVD_TITLE}}" --name "" ripdvd
  my-nimbie batch --idx-offset 50 --idx-padding 3 ripaudio
  my-nimbie batch --name " - {{DVD_TITLE}} ({{MEDIA_TYPE}})" readdvd

  my-nimbie --dry batch ripdvd            Dry-run: show what would happen
  my-nimbie --config ~/test.conf batch    Use a specific config file
  my-nimbie --create-config               Create example config at ~/.my-nimbie.conf
  my-nimbie --create-config /LINKS/default/my-nimbie   Create example config at given path

Config file search order:
  1. ~/.my-nimbie.conf
  2. /etc/my-nimbie.conf
  3. /LINKS/global/etc/my-nimbie
  4. /LINKS/default/my-nimbie

Batch flavors and their config keys:
  (none)      → [commands] on_load_DEFAULT      [target_dirs] default
  ripdvd      → [commands] on_load_RIP_DVD      [target_dirs] rip_dvd
  ripaudio    → [commands] on_load_RIP_AUDIOCD   [target_dirs] rip_audiocd
  readdvd     → [commands] on_load_READ_DVD     [target_dirs] read_dvd

  Each flavor runs a different command from the config file.
  The target directory can be set per flavor in [target_dirs] or overridden
  with --target-dir on the CLI.

Available variables in [commands] ($VAR or ${{VAR}} syntax):
  $MOUNT_POINT  — disc mount point (from [nimbie] mount_point)
  $TARGET_DIR   — base output directory (from [target_dirs] or --target-dir)
  $DIR_NAME     — full per-disc output path: TARGET_DIR / <name from [naming]>
  $DISC_NR      — sequential disc number (1, 2, 3, ...)

Per-disc directory naming ({{VAR}} syntax in --prefix, --name, --postfix):
  DIR_NAME = TARGET_DIR / {{NAME_PREFIX}}{{NAME}}{{NAME_POSTFIX}}
  Defaults: --prefix "{{INDEX}}" --name " - {{MEDIA_TYPE}}" --postfix ""
  Supported variables:
{NAMING_VARS_HELP}

Monitoring a running batch:
  my-nimbie status              Show batch progress (reads /tmp/my-nimbie.status)
  kill -USR1 <pid>              Print live status to the batch process stderr
  cat /tmp/my-nimbie.status     Machine-readable status file
""",
    )

    parser.add_argument("--config", "-c", metavar="FILE",
                        help="config file path (overrides search order)")
    parser.add_argument("--create-config", nargs="?", const="", metavar="PATH",
                        dest="create_config_path",
                        help="create example config file (default: ~/.my-nimbie.conf, or specify PATH)")
    parser.add_argument("--dry", "-d", action="store_true",
                        help="dry run — print what would be done without executing")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="verbose output")
    parser.add_argument("--debug", "-D", action="store_true",
                        help="debug output (implies --verbose)")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("load",   help="Load next disc from hopper into drive")
    sub.add_parser("eject",  help="Eject current disc to accept (done) bin")
    sub.add_parser("reject", help="Reject current disc to reject bin")
    sub.add_parser("status", help="Show Nimbie device state (or batch progress if running)")

    batch_parser = sub.add_parser("batch", help="Batch mode: load → process → accept/reject → repeat",
                                  formatter_class=argparse.RawDescriptionHelpFormatter,
                                  description=f"""\
Batch-process discs from the Nimbie hopper.

Loads a disc, runs the configured command, and accepts or rejects the disc
based on the command's exit code (0 = accept, non-zero = reject). Repeats
until the hopper is empty or max_discs is reached.

Flavors select which command from the config file to run:
  (none)      run [commands] on_load_DEFAULT     (e.g. my-handbrake encode)
  ripdvd      run [commands] on_load_RIP_DVD     (e.g. my-handbrake encode)
  ripaudio    run [commands] on_load_RIP_AUDIOCD  (e.g. cdparanoia + flac)
  readdvd     run [commands] on_load_READ_DVD    (e.g. dvdbackup mirror)

Per-disc directory naming:
  DIR_NAME = TARGET_DIR / {{NAME_PREFIX}}{{NAME}}{{NAME_POSTFIX}}
  Defaults: --prefix "{{INDEX}}" --name " - {{MEDIA_TYPE}}" --postfix ""
  Example result: "/out/0001 - DVD"

  Supported {{VARIABLE}} placeholders:
{NAMING_VARS_HELP}

Progress is tracked in /tmp/my-nimbie.status and can be queried:
  my-nimbie status              from another terminal
  kill -USR1 <pid>              prints live status to stderr of the batch""")
    batch_parser.add_argument("flavor", nargs="?", default=None,
                              choices=list(BATCH_FLAVORS.keys()),
                              help="processing flavor (default: use on_load_DEFAULT)")
    batch_parser.add_argument("--target-dir", "-t", metavar="DIR",
                              help="base output directory (overrides [target_dirs] from config)")
    batch_parser.add_argument("--prefix", metavar="STR",
                              help="directory name prefix (default: \"{INDEX}\", overrides [naming] name_prefix)")
    batch_parser.add_argument("--name", metavar="STR",
                              help="directory name middle part (default: \" - {MEDIA_TYPE}\", overrides [naming] name)")
    batch_parser.add_argument("--postfix", metavar="STR",
                              help="directory name postfix (default: \"\", overrides [naming] name_postfix)")
    batch_parser.add_argument("--idx-offset", metavar="N", type=int,
                              help="offset added to disc number for {INDEX} (default: 0, can be negative)")
    batch_parser.add_argument("--idx-padding", metavar="N", type=int,
                              help="zero-padding width for {INDEX} (default: 4, e.g. 4 → \"0001\")")

    return parser


def main():
    global verbose, debug, dry_run

    signal.signal(signal.SIGINT, signal_handler)

    parser = build_parser()
    args = parser.parse_args()

    verbose = args.verbose or args.debug
    debug = args.debug
    dry_run = args.dry

    # --create-config: special action, no device needed
    if args.create_config_path is not None:
        # nargs="?" with const="" means: --create-config without value → "", with value → value
        if args.create_config_path == "":
            args.create_config_path = None  # use default path
        cmd_create_config(args)
        return

    if not args.command:
        parser.print_help()
        sys.exit(1)

    config_path = find_config_file(args.config)
    config = load_config(config_path)

    # Create device
    if dry_run:
        nimbie = NimbieDeviceDryRun()
    else:
        vid = int(config.get("nimbie", "vid"), 16)
        pid = int(config.get("nimbie", "pid"), 16)
        nimbie = NimbieDevice(vid, pid)

    # Connect
    nimbie.connect()

    try:
        commands = {
            "load":   cmd_load,
            "eject":  cmd_eject,
            "reject": cmd_reject,
            "status": cmd_status,
            "batch":  cmd_batch,
        }
        commands[args.command](nimbie, config, args)
    finally:
        nimbie.disconnect()


if __name__ == "__main__":
    main()
