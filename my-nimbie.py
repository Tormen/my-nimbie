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
if "--create-config" not in sys.argv:
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
import shutil
import signal
import threading
import time

# ---------------------------------------------------------------------------
# Zsh completions: install/update on every run (fast no-op if unchanged)
# ---------------------------------------------------------------------------
def _install_zsh_completions():
    """Write zsh completion file and patch .zshrc fpath if needed."""
    completion_dir = os.path.expanduser("~/.zsh/completions")
    completion_file = os.path.join(completion_dir, "_my-nimbie")

    completion_content = r'''#compdef my-nimbie

_my-nimbie() {
    local curcontext="$curcontext" state line
    typeset -A opt_args

    local -a commands
    commands=(
        'load:Load next disc from hopper into drive'
        'eject:Eject current disc to accept (done) bin'
        'reject:Reject current disc to reject bin (or tell paused process to reject)'
        'status:Show Nimbie device state or batch progress'
        'next:Process exactly one disc (same setup as batch, for testing)'
        'batch:Batch mode — load, process, accept/reject, repeat'
        'accept:Tell paused batch/next to accept the disc'
        'retry:Tell paused batch/next to retry the command'
        'stop:Tell paused batch/next to reject and stop'
    )

    local -a global_opts
    global_opts=(
        '(-c --config)'{-c,--config}'[Config file path]:file:_files'
        '--create-config[Create example config file]::path:_files'
        '(-d --dry)'{-d,--dry}'[Dry run — print what would be done]'
        '(-v --verbose)'{-v,--verbose}'[Verbose output]'
        '(-D --debug)'{-D,--debug}'[Debug output; -DD for deep debug]'
        '(-h --help)'{-h,--help}'[Show help message]'
        '1:command:->cmd'
        '*::arg:->args'
    )

    _arguments -s : $global_opts

    case $state in
        cmd)
            _describe 'command' commands
            ;;
        args)
            case $line[1] in
                next|batch)
                    local -a flavors
                    flavors=(
                        'ripdvd:Encode DVD titles to MKV'
                        'ripaudio:Rip audio CD tracks to FLAC'
                        'readdvd:Full DVD backup via dvdbackup'
                    )
                    _arguments -s : \
                        '(-t --target-dir)'{-t,--target-dir}'[Base output directory]:dir:_directories' \
                        '--prefix[Directory name prefix]:str:' \
                        '--name[Directory name middle part]:str:' \
                        '--postfix[Directory name postfix]:str:' \
                        '--offset[Offset for disc index]:n:' \
                        '--padding[Zero-padding width for index]:n:' \
                        '--pause-on-err[Pause on command error, keep disc in drive]' \
                        '1:flavor:->flavor'
                    case $state in
                        flavor)
                            _describe 'flavor' flavors
                            ;;
                    esac
                    ;;
            esac
            ;;
    esac
}

_my-nimbie "$@"
'''

    os.makedirs(completion_dir, exist_ok=True)

    # Only write if content changed
    try:
        with open(completion_file, 'r') as f:
            if f.read() == completion_content:
                return  # unchanged, nothing to do
    except FileNotFoundError:
        pass

    with open(completion_file, 'w') as f:
        f.write(completion_content)

    # Patch .zshrc fpath if needed
    zshrc = os.path.expanduser("~/.zshrc")
    if os.path.islink(zshrc):
        zshrc_real = os.path.realpath(zshrc)
    elif os.path.isfile(zshrc):
        zshrc_real = zshrc
    else:
        return  # no .zshrc to patch

    try:
        with open(zshrc_real, 'r') as f:
            zshrc_content = f.read()
    except Exception:
        return

    if '.zsh/completions' in zshrc_content:
        return  # already configured (by my-plex or earlier run)


_install_zsh_completions()

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
        "on_load_default":     '',
        "on_load_rip_dvd":     '',
        "on_load_rip_audiocd": '',
        "on_load_read_dvd":    '',
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
        "idx_padding":  "3",
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
    "/LINKS/default/my-nimbie",
]

DEFAULT_CONFIG_PATH = CONFIG_SEARCH_PATHS[0]  # ~/.my-nimbie.conf

STATUS_FILE = "/tmp/my-nimbie.status"
PROGRESS_FILE = "/tmp/my-nimbie.progress"
COMMAND_FILE = "/tmp/my-nimbie.command"
RESULT_FILE = "/tmp/my-nimbie.results"

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
verbose = False
debug = False
deepdebug = False
dry_run = False
interrupted = False
batch_status = None  # set to BatchStatus instance during batch runs


def signal_handler(_signum, _frame):
    global interrupted
    if interrupted:
        # Second Ctrl+C — abort immediately
        print("\n  Aborting.", file=sys.stderr)
        sys.exit(130)
    interrupted = True
    if batch_status:
        print("\n  Interrupted — finishing current disc, then stopping...", file=sys.stderr)
    else:
        # Not in batch mode — exit immediately
        print("", file=sys.stderr)
        sys.exit(130)


def sigusr1_handler(_signum, _frame):
    """Print batch status summary to stderr on SIGUSR1."""
    if batch_status:
        batch_status.print_summary()


# ---------------------------------------------------------------------------
# Batch status tracking
# ---------------------------------------------------------------------------
class BatchStatus:
    """Tracks batch progress, writes status file, handles SIGUSR1."""

    def __init__(self, flavor, mount_point, target_dir, idx_offset=None, mode="next"):
        self.flavor = flavor or "default"
        self.mount_point = mount_point
        self.target_dir = target_dir
        self.idx_offset = idx_offset
        self.mode = mode  # "next" or "batch"
        self.disc_nr = 0
        self.accepted = 0
        self.rejected = 0
        self.command = ""
        self.current = "starting"
        self.started = datetime.datetime.now()
        self.last_update = self.started
        self.disc_load_start = None  # when current disc loading began (for total time)
        self.disc_results = []  # list of dicts: {index, dir_name, size, elapsed, total_elapsed, rc, result}
        self.last_disc = None   # last completed disc result dict

    def update(self, current, disc_nr=None, command=None):
        """Update state and rewrite status file."""
        self.current = current
        if disc_nr is not None:
            self.disc_nr = disc_nr
        if command is not None:
            self.command = command
        self.last_update = datetime.datetime.now()
        self._write_file()

    def start_disc_timer(self):
        """Mark the start of disc processing (including load time)."""
        self.disc_load_start = time.time()

    def get_disc_total_elapsed(self):
        """Get total elapsed time since disc loading started."""
        if self.disc_load_start is None:
            return 0.0
        return time.time() - self.disc_load_start

    def record_disc(self, index, dir_name, source_size, elapsed, rc, result, total_elapsed=None):
        """Record a completed disc result.

        elapsed:       command runtime only (seconds)
        total_elapsed: total processing time including loading (seconds), defaults to elapsed
        """
        entry = {
            "index": index,
            "dir_name": dir_name,
            "size": source_size,
            "elapsed": elapsed,
            "total_elapsed": total_elapsed if total_elapsed is not None else elapsed,
            "rc": rc,
            "result": result,
            "timestamp": self._ts(datetime.datetime.now()),
        }
        self.disc_results.append(entry)
        self.last_disc = entry
        self._append_result_file(entry)
        self._write_file()

    def record_accept(self, index=None, dir_name=None, source_size=0, elapsed=0.0, total_elapsed=None):
        self.accepted += 1
        if index is not None:
            self.record_disc(index, dir_name, source_size, elapsed, 0, "accepted", total_elapsed=total_elapsed)
        self.update("accepted")

    def record_reject(self, index=None, dir_name=None, source_size=0, elapsed=0.0, rc=1, total_elapsed=None):
        self.rejected += 1
        if index is not None:
            self.record_disc(index, dir_name, source_size, elapsed, rc, "rejected", total_elapsed=total_elapsed)
        self.update("rejected")

    def finish(self, final_state="finished"):
        self.update(final_state)
        self._write_summary()

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

    @staticmethod
    def _fmt_elapsed(secs):
        """Format seconds as human-readable elapsed time."""
        secs = int(secs)
        hours, remainder = divmod(secs, 3600)
        mins, s = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {mins:02d}m {s:02d}s"
        return f"{mins}m {s:02d}s"

    def _append_result_file(self, entry):
        """Append a disc result line to the result file."""
        try:
            size_str = _format_size(entry["size"]) if entry["size"] else "?"
            cmd_str = self._fmt_elapsed(entry["elapsed"])
            total_str = self._fmt_elapsed(entry["total_elapsed"])
            line = (f"{entry['timestamp']}  "
                    f"#{entry['index']:>3}  "
                    f"{entry['result']:<8}  "
                    f"rc={entry['rc']}  "
                    f"cmd={cmd_str:>10}  "
                    f"total={total_str:>10}  "
                    f"{size_str:>8}  "
                    f"{entry['dir_name']}")
            with open(RESULT_FILE, "a") as f:
                f.write(line + "\n")
        except OSError as e:
            dbg(f"Failed to write result file: {e}")

    def _write_summary(self):
        """Print detailed summary at end of batch/next."""
        total_elapsed = (datetime.datetime.now() - self.started).total_seconds()
        msg(f"\n{'=' * 64}")
        msg(f"  Summary: {self.flavor}")
        msg(f"  Total time:  {self._fmt_elapsed(total_elapsed)}")
        msg(f"  Accepted:    {self.accepted}")
        msg(f"  Rejected:    {self.rejected}")
        msg(f"  Total discs: {self.accepted + self.rejected}")
        if self.disc_results:
            msg(f"\n  {'#':>3}  {'Result':<8}  {'RC':>3}  {'Command':>10}  {'Total':>10}  {'Size':>8}  Dir")
            msg(f"  {'-' * 72}")
            for e in self.disc_results:
                size_str = _format_size(e["size"]) if e["size"] else "?"
                cmd_str = self._fmt_elapsed(e["elapsed"])
                total_str = self._fmt_elapsed(e["total_elapsed"])
                msg(f"  {e['index']:>3}  {e['result']:<8}  {e['rc']:>3}  "
                    f"{cmd_str:>10}  {total_str:>10}  {size_str:>8}  "
                    f"{os.path.basename(e['dir_name'])}")
        msg(f"{'=' * 64}")
        if self.disc_results:
            msg(f"  Results saved to: {RESULT_FILE}")

    def _write_file(self):
        """Write machine-readable status file."""
        try:
            lines = [
                f"state={self.current}",
                f"mode={self.mode}",
                f"pid={os.getpid()}",
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
            if self.idx_offset is not None:
                lines.append(f"idx_offset={self.idx_offset}")
            if self.command:
                lines.append(f"command={self.command}")
            if self.last_disc:
                d = self.last_disc
                size_str = _format_size(d["size"]) if d["size"] else "?"
                cmd_str = self._fmt_elapsed(d["elapsed"])
                total_str = self._fmt_elapsed(d["total_elapsed"])
                lines.append(f"last_disc=#{d['index']} {d['result']} rc={d['rc']} "
                             f"cmd={cmd_str} total={total_str} {size_str}")

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
# Pause command helpers (for --pause-on-err)
# ---------------------------------------------------------------------------
def _send_pause_command(cmd):
    """Write a command to COMMAND_FILE for a paused process to pick up."""
    with open(COMMAND_FILE, "w") as f:
        f.write(cmd + "\n")
    msg(f"  Sent '{cmd}' to paused process via {COMMAND_FILE}")


def _wait_for_pause_command(status):
    """Sleep-loop until a command appears in COMMAND_FILE. Returns the command string."""
    # Clear any stale command file
    try:
        os.unlink(COMMAND_FILE)
    except FileNotFoundError:
        pass

    status.update("PAUSED — command failed, waiting for: accept / reject / retry")
    while True:
        time.sleep(0.5)
        try:
            with open(COMMAND_FILE) as f:
                cmd = f.read().strip().lower()
            if cmd:
                os.unlink(COMMAND_FILE)
                dbg(f"_wait_for_pause_command: received '{cmd}'")
                return cmd
        except FileNotFoundError:
            continue
        except OSError:
            continue


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
def msg(text):
    print(text)

def vrb(text):
    if verbose:
        print(text)

def _ts():
    now = datetime.datetime.now()
    return now.strftime("%Y-%m-%d_%H%M.") + f"{now.microsecond // 1000:03d}"

def dbg(text):
    if debug:
        print(f"{_ts()}|  DBG: {text}", file=sys.stderr)

def ddbg(text):
    if deepdebug:
        print(f"{_ts()}| DDBG: {text}", file=sys.stderr)

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

    search_list = "\n".join(f"    - {p}" for p in CONFIG_SEARCH_PATHS)
    err(f"No config file found.\n\n"
        f"  Searched:\n{search_list}\n\n"
        f"  Create one with:\n"
        f"    my-nimbie --create-config                  → {DEFAULT_CONFIG_PATH}\n"
        f"    my-nimbie --create-config /custom/path")


def load_config(config_path):
    """Load config from file, with built-in defaults for missing values."""
    config = configparser.ConfigParser(inline_comment_prefixes=("#",))

    # Set defaults for any values not specified in the file
    for section, values in DEFAULT_CONFIG.items():
        config[section] = values

    config.read(config_path)
    vrb(f"  Config loaded from: {config_path}")

    config.config_path = config_path  # store for status display
    return config


def generate_example_config():
    """Return the example config file content as a string."""
    return """\
# my-nimbie configuration
#
# Search order: ~/.my-nimbie.conf, /etc/my-nimbie.conf, /LINKS/default/my-nimbie
# Or specify explicitly: my-nimbie --config /path/to/config <command>

[nimbie]
    # USB device identifiers (use system_profiler SPUSBDataType to verify)
    vid = 0x1723                # default: 0x1723 (Nimbie NB21)
    pid = 0x0945                # default: 0x0945 (Nimbie NB21)

    # MANDATORY: where the optical disc mounts on macOS
    mount_point = /Volumes/DVD_VIDEO_RECORDER

[commands]
    # Commands executed by "batch" for each disc flavor.
    #
    # Available variables (use $VAR or ${VAR} syntax):
    #   $MOUNT_POINT  — where the disc is mounted (from [nimbie] mount_point)
    #   $TARGET_DIR   — base output directory (from [target_dirs] or --target-dir)
    #   $DIR_NAME     — full output path: TARGET_DIR / <generated dir name from [naming]>
    #   $DISC_NR      — sequential disc number (1, 2, 3, ...)
    #   $DEVICE       — raw device path (e.g. /dev/disk4), resolved from mount_point
    #                   Use this for tools like dvdbackup that need the device, not mount point
    #
    # "batch <flavor>"    → runs the matching on_load_<FLAVOR> command
    # "batch" (no flavor) → runs on_load_DEFAULT (if set)
    #
    # If no flavor is given and on_load_DEFAULT is not set, my-nimbie lists
    # all available flavors and their commands.
    #
    # Special value "pause": loads the disc but runs no command. The disc stays
    # mounted and my-nimbie waits for a command from another terminal:
    #   my-nimbie accept / reject / retry / stop
    # Useful for testing or manually tweaking commands while the disc is loaded.
    # Example:  on_load_TEST = pause

    # RIPDVD: encode DVD titles to MKV via my-handbrake
    on_load_RIP_DVD = /LINKS/bin/my-handbrake dvd "$MOUNT_POINT" --all encode tvDVD

    # RIPAUDIO: rip audio CD tracks to FLAC (max quality) via cdparanoia + flac
    on_load_RIP_AUDIOCD = mkdir -p "$DIR_NAME" && cd "$DIR_NAME" && cdparanoia -B -- -0 && flac --best *.wav && rm -f *.wav

    # READDVD: full DVD backup (all content, mirror mode) via dvdbackup
    # -M = mirror entire disc, -v = verbose
    # my-nimbie auto-adds: -n <volume_name> (required for DVDs with empty title)
    # my-nimbie auto-removes: -p (causes incomplete copies on macOS)
    # mkdir -p ensures the output directory exists before dvdbackup runs
    on_load_READ_DVD = mkdir -p "$DIR_NAME" && dvdbackup -i "$MOUNT_POINT" -o "$DIR_NAME" -M -v

    # Optional: set a default for "batch" without a flavor.
    # Can be a full command or a flavor name (synonym):
    # on_load_DEFAULT = ripdvd
    # on_load_DEFAULT = /LINKS/bin/my-handbrake dvd "$MOUNT_POINT" --all encode tvDVD

    # Optional: validation command run AFTER on_load (exit 0 = accept disc, non-zero = reject).
    # If empty (default), the on_load exit code determines accept/reject.
    on_validate =

[target_dirs]
    # Base output directories for each batch flavor.
    # REQUIRED: either set here or pass --target-dir on the command line.
    # Without a target directory, batch will abort with an error.
    #
    # Can be overridden per invocation with: my-nimbie batch --target-dir /path ripdvd
    # The per-disc subdirectory name is built from [naming] settings below.
    #
    # "default" is used when on_load_DEFAULT is set to a direct command.
    # When on_load_DEFAULT is a synonym (e.g. "ripdvd"), the synonym's
    # target_dir is used instead (e.g. rip_dvd).
    # default =
    rip_dvd = /Volumes/ext-data/
    rip_audiocd = /Volumes/ext-data/
    read_dvd = /Volumes/ext-data/

[naming]
    # Per-disc subdirectory naming within TARGET_DIR.
    #
    # The directory name is assembled as:  {NAME_PREFIX}{NAME}{NAME_POSTFIX}
    #
    # Supported {VARIABLE} placeholders (case-insensitive):
    #   {INDEX}       — disc index (see idx_offset below), zero-padded to idx_padding digits
    #   {DISC_NR}     — raw disc number (1, 2, 3, ...) without padding or offset
    #   {MEDIA_TYPE}  — "DVD" or "CD" (auto-detected from disc content)
    #   {DVD_TITLE}   — volume name of the disc (read only when needed, e.g. "LOTR_DISC_1")
    #   {FLAVOR}      — batch flavor name ("default", "ripdvd", "ripaudio", "readdvd")
    #   {DATE}        — current date as YYYY-MM-DD
    #
    # Examples:
    #   name_prefix = {INDEX}                 → "001"
    #   name        =  - {MEDIA_TYPE}         → " - DVD"
    #   name_postfix =                        → ""
    #   Result: "001 - DVD"
    #
    #   name_prefix = {DVD_TITLE}             → "LOTR_DISC_1"
    #   name        =  ({INDEX})              → " (001)"
    #   Result: "LOTR_DISC_1 (001)"
    #
    #   name_prefix = {DATE}_{INDEX}          → "2026-03-28_0005"
    #   name        =  - {DVD_TITLE}          → " - LOTR_DISC_1"
    #   Result: "2026-03-28_0005 - LOTR_DISC_1"

    name_prefix = {INDEX}       # default: {INDEX}
    name = " - {MEDIA_TYPE}"    # default: " - {MEDIA_TYPE}"
    name_postfix =              # default: (empty)

    # Zero-padding width for {INDEX} (e.g. 3 → "001", 2 → "01")
    idx_padding = 3             # default: 3

    # Offset added to disc number for {INDEX}.
    # {INDEX} = DISC_NR + offset, where DISC_NR starts at 1.
    # With the default (0), numbering starts at 1: 001, 002, 003, ...
    # To continue from a previous batch of 50 discs: idx_offset = 50
    #   → first disc gets {INDEX} = 051, then 052, 053, ...
    idx_offset = 0              # default: 0

[batch]
    # Max discs to process (0 = unlimited, process until hopper is empty)
    max_discs = 0               # default: 0

    # Seconds to wait after loading before checking if disc mounted
    load_settle_time = 5        # default: 5

    # Seconds to wait for disc to mount before giving up and rejecting
    mount_timeout = 60          # default: 60

    # Seconds between mount-point polling checks
    poll_interval = 2           # default: 2
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
    try:
        with open(path, "w") as f:
            f.write(content)
    except PermissionError:
        err(f"Permission denied: {path}\n\n"
            f"  Try one of:\n"
            f"    sudo my-nimbie --create-config {path}\n"
            f"    my-nimbie --create-config {DEFAULT_CONFIG_PATH}")

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
    def _strip_quotes(s):
        """Strip surrounding quotes from config values (preserves inner whitespace)."""
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
            return s[1:-1]
        return s

    # Read naming settings: CLI overrides > config
    name_prefix  = cli_naming.get("prefix")  if cli_naming.get("prefix")  is not None else _strip_quotes(config.get("naming", "name_prefix",  fallback="{INDEX}"))
    name         = cli_naming.get("name")    if cli_naming.get("name")    is not None else _strip_quotes(config.get("naming", "name",         fallback=" - {MEDIA_TYPE}"))
    name_postfix = cli_naming.get("postfix") if cli_naming.get("postfix") is not None else _strip_quotes(config.get("naming", "name_postfix", fallback=""))
    idx_padding  = cli_naming.get("idx_padding") if cli_naming.get("idx_padding") is not None else config.getint("naming", "idx_padding", fallback=3)
    idx_offset   = cli_naming.get("idx_offset")  if cli_naming.get("idx_offset")  is not None else config.getint("naming", "idx_offset",  fallback=0)

    idx_padding = int(idx_padding)
    idx_offset = int(idx_offset)

    index_val = disc_nr + idx_offset
    index_str = str(index_val).zfill(idx_padding)

    dbg(f"build_dir_name: disc_nr={disc_nr}, idx_offset={idx_offset} "
        f"(INDEX = disc_nr + offset = {disc_nr}+{idx_offset} = {index_val}), "
        f"idx_padding={idx_padding} → index_str='{index_str}'")
    ddbg(f"build_dir_name: name_prefix='{name_prefix}', name='{name}', name_postfix='{name_postfix}'")
    ddbg(f"build_dir_name: sources — prefix:{'CLI' if cli_naming.get('prefix') is not None else 'config'}, "
         f"name:{'CLI' if cli_naming.get('name') is not None else 'config'}, "
         f"postfix:{'CLI' if cli_naming.get('postfix') is not None else 'config'}, "
         f"offset:{'CLI' if cli_naming.get('idx_offset') is not None else 'config'}, "
         f"padding:{'CLI' if cli_naming.get('idx_padding') is not None else 'config'}")

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

    dbg(f"build_dir_name: result='{dir_name}'")
    ddbg(f"build_dir_name: variables={variables}")
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
        if self.dev is not None:
            ddbg(f"USB device found: {self.dev}")
            try:
                ddbg(f"  manufacturer={self.dev.manufacturer}, product={self.dev.product}")
            except (ValueError, usb.core.USBError) as e:
                ddbg(f"  (cannot read device strings: {e})")
            ddbg(f"  bDeviceClass={self.dev.bDeviceClass}, bNumConfigurations={self.dev.bNumConfigurations}")
            try:
                cfg = self.dev.get_active_configuration()
            except usb.core.USBError:
                cfg = None
            if cfg:
                for intf in cfg:
                    ddbg(f"  Interface {intf.bInterfaceNumber}: class={intf.bInterfaceClass}")
                    for ep in intf:
                        ddbg(f"    EP {ep.bEndpointAddress:#04x}: type={usb.util.endpoint_type(ep.bmAttributes)}"
                             f" maxPacket={ep.wMaxPacketSize}")
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

        try:
            self.dev.set_configuration()
            dbg("set_configuration OK")
        except usb.core.USBError as e:
            dbg(f"set_configuration: {e} — may already be configured")

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

        # Drain any stale data from the IN endpoint (max 10 reads)
        ddbg("Draining stale IN endpoint data...")
        drain_count = 0
        for _ in range(10):
            try:
                data = self.dev.read(EP_IN, 64, timeout=100)
                raw = bytes(data)
                if raw == b"\x00" * len(raw):
                    break  # all-zero = idle interrupt packet, nothing to drain
                drain_count += 1
                ddbg(f"  Drained: {raw.hex()}")
            except Exception:
                break
        ddbg(f"  Drained {drain_count} stale packet(s)")

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
            written = self.dev.write(EP_OUT, pkt, timeout=5000)
            ddbg(f"  Write OK: {written} bytes to EP {EP_OUT:#04x}")
        except Exception as e:
            err(f"USB write failed ({description}): {e}\n\n"
                f"  Possible reasons:\n"
                f"    - Device disconnected or not powered on\n"
                f"    - USB interface not properly claimed\n"
                f"    - macOS blocking USB access — check:\n"
                f"      System Settings → Privacy & Security → USB\n"
                f"    - Try: unplug device, wait 5s, reconnect")

    def _read_responses(self, timeout=3000, max_reads=20, wait_for_at=False):
        """Read all pending responses from interrupt IN endpoint.

        Returns a list of ASCII strings. Reads until timeout or all-zero idle packet.
        If wait_for_at=True, keeps reading through empty packets until an AT+S/AT+E
        status response arrives or max_reads is exhausted — used for mechanical
        operations where AT+O (accepted) comes first, then AT+Sxx (result) later.
        """
        responses = []
        empty_count = 0
        got_status = False  # True when AT+S* or AT+E* received
        for i in range(max_reads):
            try:
                data = self.dev.read(EP_IN, 64, timeout=timeout)
                raw = bytes(data)
                ddbg(f"  Raw read ({len(raw)} bytes): {raw.hex()}")
                if len(raw) == 0 or raw == b"\x00" * len(raw):
                    empty_count += 1
                    if not wait_for_at or got_status:
                        if empty_count >= 2:
                            ddbg("  Two consecutive empty reads — done")
                            break
                    else:
                        ddbg(f"  Empty packet #{empty_count} (waiting for AT+S/AT+E status...)")
                    continue
                empty_count = 0
                text = raw.rstrip(b"\x00").decode("ascii", errors="replace")
                if text:
                    responses.append(text)
                    dbg(f"  Received: \"{text}\"")
                    if text.startswith("AT+S") or text.startswith("AT+E"):
                        got_status = True
            except Exception as e:
                dbg(f"  Read ended: {e}")
                break
        if wait_for_at and not got_status:
            dbg(f"  WARNING: no AT+S/AT+E status received after {max_reads} reads "
                f"(got: {responses})")
        return responses

    def _send_and_read(self, cmd_tuple, description="", timeout=3000,
                       max_reads=20, wait_for_at=False):
        """Send command and collect all responses."""
        self._send_command(cmd_tuple, description)
        time.sleep(0.3)
        return self._read_responses(timeout=timeout, max_reads=max_reads,
                                    wait_for_at=wait_for_at)

    def _find_at_response(self, responses):
        """Find the most relevant AT+ response in a list of responses.

        Prefers AT+S/AT+E status codes over AT+O (generic OK).
        AT+O means "command accepted" — the actual result comes as AT+Sxx or AT+Exx.
        """
        # First pass: look for status/error codes (AT+S*, AT+E*)
        for r in responses:
            if r.startswith("AT+S") or r.startswith("AT+E"):
                return r
        # Fallback: return AT+O if that's all we got
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
        # Mechanical operation: hopper picks up disc and drops it on tray.
        # This can take 10-15 seconds, so use a generous read timeout and
        # keep reading through idle packets until the AT+S status arrives.
        responses = self._send_and_read(CMD_PLACE_DISC, "PLACE_DISC",
                                        timeout=20000, max_reads=100,
                                        wait_for_at=True)
        at = self._find_at_response(responses)

        if at == AT_HOPPER_EMPTY:
            return False
        elif at == AT_PLACED:
            # Wait for disc to settle in tray + dropper retract
            time.sleep(0.8)
            return True
        elif at == AT_OK:
            # AT+O without AT+S07 — disc likely placed successfully
            dbg("place_disc: got AT+O without AT+S07, assuming success")
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
        responses = self._send_and_read(CMD_LIFT_DISC, "LIFT_DISC",
                                        timeout=15000, max_reads=100,
                                        wait_for_at=True)
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
        responses = self._send_and_read(CMD_ACCEPT, "ACCEPT_DISC",
                                        timeout=15000, max_reads=100,
                                        wait_for_at=True)
        at = self._find_at_response(responses)

        if at == AT_OK:
            # Poll until disc is no longer lifted
            self._poll_state(lambda s: not s["disc_lifted"], "disc to drop to accept", timeout=10)
        else:
            err(f"Unexpected response from ACCEPT_DISC: {at}\n"
                f"  All responses: {responses}")

    def reject_disc(self):
        """Drop a lifted disc into the reject pile."""
        responses = self._send_and_read(CMD_REJECT, "REJECT_DISC",
                                        timeout=15000, max_reads=100,
                                        wait_for_at=True)
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
    dbg(f"wait_for_mount: mount_point={mount_point}, timeout={timeout}s, poll={poll_interval}s")
    start = time.time()

    while time.time() - start < timeout:
        if interrupted:
            dbg("wait_for_mount: interrupted")
            return False
        is_mount = os.path.ismount(mount_point)
        has_video_ts = os.path.isdir(os.path.join(mount_point, "VIDEO_TS"))
        ddbg(f"wait_for_mount: is_mount={is_mount}, has_video_ts={has_video_ts}, "
             f"elapsed={time.time() - start:.1f}s")
        if is_mount or has_video_ts:
            dbg(f"wait_for_mount: mounted after {time.time() - start:.1f}s")
            vrb(f"  Disc mounted at {mount_point}")
            return True
        time.sleep(poll_interval)

    dbg(f"wait_for_mount: timed out after {timeout}s")
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
def _mount_to_device(mount_point):
    """Resolve a mount point to its raw device path (e.g. /dev/disk4).

    Used by dvdbackup which needs the raw device, not the mount point.
    Returns the device path, or the mount_point itself if resolution fails.
    """
    try:
        result = subprocess.run(
            ["diskutil", "info", "-plist", mount_point],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            # Parse plist for DeviceNode
            import plistlib
            plist = plistlib.loads(result.stdout.encode())
            device = plist.get("DeviceNode", "")
            if device:
                dbg(f"_mount_to_device: {mount_point} → {device}")
                return device
    except Exception as e:
        dbg(f"_mount_to_device: failed: {e}")
    return mount_point


def expand_command(cmd_template, mount_point, disc_nr, target_dir, dir_name):
    """Expand $VAR and ${VAR} variables in command template."""
    # Lazily resolve $DEVICE only if referenced
    device = None
    if "$DEVICE" in cmd_template or "${DEVICE}" in cmd_template:
        device = _mount_to_device(mount_point)

    cmd = cmd_template
    vars_list = [("MOUNT_POINT", mount_point), ("TARGET_DIR", target_dir),
                 ("DIR_NAME", dir_name), ("DISC_NR", str(disc_nr))]
    if device is not None:
        vars_list.append(("DEVICE", device))
    for var, val in vars_list:
        cmd = cmd.replace(f"${{{var}}}", val)
        cmd = cmd.replace(f"${var}", val)
    ddbg(f"expand_command: '{cmd_template}' → '{cmd}'")
    ddbg(f"expand_command: MOUNT_POINT={mount_point}, TARGET_DIR={target_dir}, "
         f"DIR_NAME={dir_name}, DISC_NR={disc_nr}"
         + (f", DEVICE={device}" if device else ""))
    return cmd


def _ensure_dvdbackup_flags(cmd, mount_point):
    """Transparently fix dvdbackup command for reliable operation.

    Adds -n <volume_name> if not present (required when DVD title is empty).
    """
    parts = cmd.split("&&")
    modified = []
    for part in parts:
        stripped = part.strip()
        if stripped.startswith("dvdbackup ") or stripped.startswith("dvdbackup\t"):
            if " -n " not in stripped and " --name " not in stripped:
                vol_name = os.path.basename(mount_point)
                stripped = stripped.replace("dvdbackup ", f'dvdbackup -n "{vol_name}" ', 1)
                dbg(f"_ensure_dvdbackup_flags: auto-added -n \"{vol_name}\"")
            modified.append(stripped)
        else:
            modified.append(part)
    return " && ".join(modified)


def _get_dir_size(path):
    """Get total size of a directory tree in bytes."""
    total = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _get_mount_size(mount_point):
    """Get total used size of a mounted disc in bytes.

    Works for DVDs (mounted filesystem) and audio CDs (via diskutil).
    """
    # Try filesystem stats first (works for mounted DVDs)
    try:
        st = os.statvfs(mount_point)
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        if used > 0:
            return used
    except OSError:
        pass
    # Fallback: walk the directory (works for any mounted disc)
    size = _get_dir_size(mount_point)
    if size > 0:
        return size
    # Last resort: try diskutil for unmounted/audio discs
    try:
        import plistlib
        result = subprocess.run(
            ["diskutil", "info", "-plist", mount_point],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            plist = plistlib.loads(result.stdout.encode())
            return plist.get("TotalSize", 0) or plist.get("Size", 0)
    except Exception:
        pass
    return 0


def _format_size(size_bytes):
    """Format bytes as human-readable string."""
    if size_bytes >= 1073741824:
        return f"{size_bytes / 1073741824:.1f} GB"
    if size_bytes >= 1048576:
        return f"{size_bytes / 1048576:.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


class _ProgressMonitor:
    """Background thread that monitors copy progress by comparing dir sizes."""

    def __init__(self, source_size, target_dir, interval=0.5):
        self.source_size = source_size
        self.target_dir = target_dir
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._last_progress = ""
        self._start_time = time.time()

    def start(self):
        if self.source_size <= 0:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        dbg(f"_ProgressMonitor: started (source={_format_size(self.source_size)}, "
            f"target={self.target_dir}, interval={self.interval}s)")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        # Write final progress (don't delete — status may still read it)
        if self.source_size > 0:
            copied = _get_dir_size(self.target_dir)
            pct = min(100.0, (copied / self.source_size) * 100)
            elapsed = time.time() - self._start_time
            elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60):02d}s"
            final = f"{pct:5.1f}%  {_format_size(copied)} / {_format_size(self.source_size)}  [{elapsed_str}] done"
            self._last_progress = final
            try:
                with open(PROGRESS_FILE, "w") as f:
                    f.write(final + "\n")
            except OSError:
                pass

    def _run(self):
        while not self._stop.is_set():
            self._stop.wait(self.interval)
            if self._stop.is_set():
                break
            copied = _get_dir_size(self.target_dir)
            pct = min(100.0, (copied / self.source_size) * 100) if self.source_size > 0 else 0
            elapsed = time.time() - self._start_time
            elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60):02d}s"
            progress = (f"{pct:5.1f}%  {_format_size(copied)} / {_format_size(self.source_size)}"
                        f"  [{elapsed_str}]")
            self._last_progress = progress
            # Write to progress file for 'my-nimbie status'
            try:
                with open(PROGRESS_FILE, "w") as f:
                    f.write(progress + "\n")
            except OSError:
                pass
            # Print progress on CLI (overwrite line)
            print(f"\r  Progress: {progress}  ", end="", flush=True)

    @property
    def last_progress(self):
        return self._last_progress


def run_command(cmd_template, mount_point, disc_nr, target_dir, dir_name, status=None):
    """Run a shell command with variable expansion. Returns (exit_code, elapsed_secs).

    Streams output to the user in real time. For dvdbackup, transparently
    adds -n (name) for reliable operation on macOS.
    Monitors copy progress by comparing source disc size vs target dir size.
    """
    cmd = expand_command(cmd_template, mount_point, disc_nr, target_dir, dir_name)
    cmd = _ensure_dvdbackup_flags(cmd, mount_point)

    msg(f"\n  Running: {cmd}")
    if status is not None:
        status.update("running command", command=cmd)
    dbg(f"run_command: template='{cmd_template}'")
    dbg(f"run_command: expanded='{cmd}'")

    if dry_run:
        msg("  [DRY-RUN] Would execute above command")
        return 0, 0.0

    # Start progress monitor (measures source disc size, polls target dir)
    source_size = _get_mount_size(mount_point)
    dbg(f"run_command: source disc size = {_format_size(source_size)}")
    monitor = _ProgressMonitor(source_size, dir_name)
    monitor.start()

    start_time = time.time()
    try:
        dbg("run_command: executing via shell (streaming output)...")
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                bufsize=1)
        for line in proc.stdout:
            line = line.rstrip("\n")
            # Clear progress line, print command output, then let monitor redraw
            print(f"\r    {line:<80}")
        proc.wait()
        elapsed = time.time() - start_time
        monitor.stop()
        # Print final newline after progress line
        print()
        dbg(f"run_command: exit code {proc.returncode}, elapsed {elapsed:.1f}s")
        vrb(f"  Command exited with code {proc.returncode} in {int(elapsed // 60)}m {int(elapsed % 60):02d}s")
        return proc.returncode, elapsed
    except KeyboardInterrupt:
        monitor.stop()
        elapsed = time.time() - start_time
        warn("Command interrupted")
        return 130, elapsed


# ---------------------------------------------------------------------------
# Resolve batch flavor → on_load key + target_dir
# ---------------------------------------------------------------------------
def _format_flavor_list(config):
    """Build a formatted list of available flavors and their commands."""
    lines = []
    # Check DEFAULT
    default_val = config.get("commands", "on_load_default", fallback="")
    if default_val:
        if default_val in BATCH_FLAVORS:
            lines.append(f"    (none)      on_load_DEFAULT      → synonym for '{default_val}'")
        else:
            lines.append(f"    (none)      on_load_DEFAULT      = {default_val}")
    # Named flavors
    for name, suffix in BATCH_FLAVORS.items():
        cmd = config.get("commands", f"on_load_{suffix}".lower(), fallback="")
        if cmd:
            lines.append(f"    {name:10s}  on_load_{suffix:14s} = {cmd}")
        else:
            lines.append(f"    {name:10s}  on_load_{suffix:14s}   (not configured)")
    return "\n".join(lines)


def resolve_batch_flavor(config, flavor, cli_target_dir):
    """Resolve flavor to (on_load_command, target_dir). Exits on error."""
    dbg(f"resolve_batch_flavor: flavor={flavor}, cli_target_dir={cli_target_dir}")
    if flavor is None:
        # No flavor specified — check if on_load_DEFAULT is set
        default_val = config.get("commands", "on_load_default", fallback="")
        if not default_val:
            err(f"No flavor specified and no on_load_DEFAULT configured.\n\n"
                f"  Available flavors:\n"
                f"{_format_flavor_list(config)}\n\n"
                f"  Usage:\n"
                f"    my-nimbie batch <flavor>\n"
                f"    my-nimbie batch              (requires on_load_DEFAULT in config)")
        # Check if DEFAULT is a synonym for an existing flavor
        if default_val in BATCH_FLAVORS:
            dbg(f"on_load_DEFAULT is synonym for flavor '{default_val}'")
            return resolve_batch_flavor(config, default_val, cli_target_dir)
        config_suffix = "DEFAULT"
        target_key = "default"
    else:
        config_suffix = BATCH_FLAVORS.get(flavor)
        if config_suffix is None:
            err(f"Unknown batch flavor: '{flavor}'\n\n"
                f"  Available flavors:\n"
                f"{_format_flavor_list(config)}")
        target_key = config_suffix.lower()

    on_load_key = f"on_load_{config_suffix}".lower()
    on_load = config.get("commands", on_load_key, fallback="")
    dbg(f"resolve_batch_flavor: config_suffix={config_suffix}, on_load_key={on_load_key}, on_load='{on_load}'")

    if not on_load:
        err(f"No command configured for [commands] on_load_{config_suffix}\n\n"
            f"  Available flavors:\n"
            f"{_format_flavor_list(config)}\n\n"
            f"  Set it in your config file or create one with:\n"
            f"    my-nimbie --create-config")

    # target_dir: CLI param overrides config
    if cli_target_dir:
        target_dir = cli_target_dir
        dbg(f"resolve_batch_flavor: target_dir from CLI: {target_dir}")
    else:
        target_dir = config.get("target_dirs", target_key, fallback="")
        dbg(f"resolve_batch_flavor: target_dir from config [{target_key}]: '{target_dir}'")

    if not target_dir:
        err(f"No target directory configured for flavor '{target_key}'.\n\n"
            f"  Set it in one of:\n"
            f"    config file: [target_dirs] {target_key} = /path/to/output\n"
            f"    command line: my-nimbie batch --target-dir /path {flavor or ''}")

    if not os.path.isdir(target_dir):
        err(f"Target directory does not exist: {target_dir}\n\n"
            f"  Create it first:\n"
            f"    mkdir -p {target_dir}")
    if not os.access(target_dir, os.W_OK):
        err(f"Target directory is not writable: {target_dir}\n\n"
            f"  Check permissions:\n"
            f"    ls -ld {target_dir}")

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


def cmd_status(nimbie, config, _args):
    config_path = getattr(config, "config_path", None)
    if verbose:
        msg(f"Config: {config_path or '(built-in defaults)'}")
        msg("")

    # Check if a batch is running (status file exists with non-finished state)
    sf = BatchStatus.read_file()
    is_batch = sf.get("mode") == "batch" if sf else False

    # Detect stale status file: process died without cleaning up
    process_crashed = False
    if sf and sf.get("state") not in ("finished", "interrupted"):
        pid_str = sf.get("pid")
        process_alive = False
        if pid_str:
            try:
                os.kill(int(pid_str), 0)  # signal 0 = check if alive
                process_alive = True
            except (OSError, ValueError):
                pass
        if not process_alive:
            process_crashed = True

    if process_crashed:
        # Process died — show CRASHED status with disc info, then clean up and
        # fall through to show hardware state so the user knows what to do
        mode_label = "Batch" if is_batch else "Next"
        disc_nr = sf.get('disc_nr', '?')
        idx_offset = sf.get('idx_offset')
        index_str = ""
        if idx_offset is not None:
            try:
                index_str = f", index {int(disc_nr) + int(idx_offset)}"
            except (ValueError, TypeError):
                pass

        msg(f"CRASHED — {mode_label} process (pid {sf.get('pid', '?')}) died during '{sf.get('state', '?')}'")
        msg(f"  Flavor:      {sf.get('flavor', '?')}")
        msg(f"  Disc:        #{disc_nr}{index_str}")
        if sf.get("target_dir"):
            msg(f"  Target dir:  {sf['target_dir']}")
        if sf.get("command"):
            msg(f"  Command:     {sf['command']}")
        if is_batch:
            msg(f"  Accepted:    {sf.get('accepted', '?')}")
            msg(f"  Rejected:    {sf.get('rejected', '?')}")
        if is_batch and sf.get("last_disc"):
            msg(f"  Last disc:   {sf['last_disc']}")
        msg(f"\n  The disc may still be in the drive.")
        msg(f"    my-nimbie eject    — eject disc to accept bin")
        msg(f"    my-nimbie reject   — eject disc to reject bin")

        # Clean up stale files
        for stale_f in (STATUS_FILE, PROGRESS_FILE):
            try:
                os.unlink(stale_f)
            except FileNotFoundError:
                pass
        msg("")
        # Fall through to show hardware state below

    if sf and sf.get("state") not in ("finished", "interrupted"):
        mode_label = "Batch" if is_batch else "Next"
        msg(f"{mode_label} in progress (from status file {STATUS_FILE}):")
        msg(f"  Flavor:      {sf.get('flavor', '?')}")

        # Build "Running:" line with disc number, index, state, and elapsed
        disc_nr = sf.get('disc_nr', '?')
        idx_offset = sf.get('idx_offset')
        running_parts = [f"disc #{disc_nr}"]
        if idx_offset is not None:
            try:
                index_val = int(disc_nr) + int(idx_offset)
                running_parts[0] = f"disc #{disc_nr}, index {index_val}"
            except (ValueError, TypeError):
                pass
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
                running_parts.append(elapsed_str)
            except ValueError:
                pass
        current = sf.get('current', '?')
        msg(f"  Running:     {', '.join(running_parts)} — {current}")

        msg(f"  Accepted:    {sf.get('accepted', '?')}")
        msg(f"  Rejected:    {sf.get('rejected', '?')}")
        if verbose:
            if sf.get("started"):
                msg(f"  Started:     {sf['started']}")
            if sf.get("last_update"):
                msg(f"  Last update: {sf['last_update']}")
        if sf.get("target_dir"):
            msg(f"  Target dir:  {sf['target_dir']}")
        if sf.get("command"):
            msg(f"  Command:     {sf['command']}")
        # Show copy progress if available
        try:
            with open(PROGRESS_FILE) as f:
                progress = f.read().strip()
            if progress:
                msg(f"  Progress:    {progress}")
        except (FileNotFoundError, OSError):
            pass
        # Show last completed disc (batch only)
        if is_batch and sf.get("last_disc"):
            msg(f"  Last disc:   {sf['last_disc']}")
        return

    # Show last run result if available (even after finished)
    if sf and sf.get("state") in ("finished", "interrupted"):
        mode_label = "batch" if is_batch else "next"
        msg(f"Last {mode_label}: {sf.get('flavor', '?')} — {sf.get('state')}")
        msg(f"  Accepted: {sf.get('accepted', '?')}, Rejected: {sf.get('rejected', '?')}")
        if is_batch and sf.get("last_disc"):
            msg(f"  Last disc: {sf['last_disc']}")
        msg("")

    # No batch running — query USB device directly
    msg("Querying Nimbie status...")
    state = nimbie.get_state()
    msg(f"  Disc available: {state['disc_available']}")
    msg(f"  Disc in tray:   {state['disc_in_tray']}")
    msg(f"  Disc lifted:    {state['disc_lifted']}")
    msg(f"  Tray out:       {state['tray_out']}")

    # Determine current stage
    avail = state["disc_available"]
    in_tray = state["disc_in_tray"]
    lifted = state["disc_lifted"]
    tray_out = state["tray_out"]

    if lifted:
        stage = "5/5  Disc grabbed by gripper — waiting for accept/reject drop"
    elif in_tray and not tray_out:
        stage = "3/5  Disc in drive (tray closed) — ready for read/rip"
    elif in_tray and tray_out:
        stage = "2/5  Disc on open tray — waiting for tray close"
    elif tray_out and not in_tray:
        stage = "1/5  Tray open — waiting for disc placement from hopper"
    elif not tray_out and not in_tray and not lifted:
        stage = "0/5  Idle — no disc in drive"
    else:
        stage = "?    Unknown state combination"

    msg(f"\n  Stage: {stage}")
    if avail:
        msg(f"         Hopper has discs available")
    else:
        msg(f"         Hopper is EMPTY")

    # Show command progress if available (from next or batch)
    try:
        with open(PROGRESS_FILE) as f:
            progress = f.read().strip()
        if progress:
            msg(f"\n  Command progress: {progress}")
    except (FileNotFoundError, OSError):
        pass

    msg(f"""
  Mechanism stages:
    1. Open tray
    2. Place disc from hopper onto tray
    3. Close tray — drive reads/rips the disc
    4. Open tray, lift disc (gripper picks it up)
    5. Drop disc to accept (done) or reject bin""")


def cmd_next(nimbie, config, args):
    """Process exactly one disc: load → run command → accept/reject."""
    dbg("cmd_next: starting")
    mount_point = config.get("nimbie", "mount_point")
    on_validate = config.get("commands", "on_validate")
    mount_timeout = config.getfloat("batch", "mount_timeout")
    poll_interval = config.getfloat("batch", "poll_interval")
    settle_time = config.getfloat("batch", "load_settle_time")

    dbg(f"cmd_next: mount_point={mount_point}, mount_timeout={mount_timeout}s, "
        f"poll_interval={poll_interval}s, settle_time={settle_time}s")
    dbg(f"cmd_next: on_validate={'(none)' if not on_validate else on_validate}")

    flavor = args.flavor
    cli_target_dir = args.target_dir
    dbg(f"cmd_next: flavor={flavor}, cli_target_dir={cli_target_dir}")
    on_load, target_dir = resolve_batch_flavor(config, flavor, cli_target_dir)
    dbg(f"cmd_next: resolved on_load={on_load}")
    dbg(f"cmd_next: resolved target_dir={target_dir}")

    cli_naming = {
        "prefix":      args.prefix,
        "name":        args.name,
        "postfix":     args.postfix,
        "idx_padding": args.padding,
        "idx_offset":  args.offset,
    }
    dbg(f"cmd_next: cli_naming={cli_naming}")

    flavor_label = flavor or "default"

    # Pre-flight checks (same as batch)
    dbg("cmd_next: pre-flight checks starting")
    cmd_binary = on_load.split()[0] if on_load else ""
    if cmd_binary and not cmd_binary.startswith("$"):
        cmd_binary_expanded = os.path.expanduser(os.path.expandvars(cmd_binary))
        resolved = shutil.which(cmd_binary_expanded)
        dbg(f"cmd_next: command binary '{cmd_binary}' → which: {resolved}")
        if not resolved:
            err(f"Command not found: {cmd_binary}\n\n"
                f"  The on_load command's binary does not exist or is not executable.\n"
                f"  Check your config: [commands] on_load_{BATCH_FLAVORS.get(flavor, 'DEFAULT')}")

    if on_validate:
        val_binary = on_validate.split()[0]
        if val_binary and not val_binary.startswith("$"):
            val_binary_expanded = os.path.expanduser(os.path.expandvars(val_binary))
            resolved = shutil.which(val_binary_expanded)
            dbg(f"cmd_next: validate binary '{val_binary}' → which: {resolved}")
            if not resolved:
                err(f"Validation command not found: {val_binary}\n\n"
                    f"  Check your config: [commands] on_validate")

    mount_parent = os.path.dirname(mount_point)
    dbg(f"cmd_next: mount parent '{mount_parent}' exists: {os.path.isdir(mount_parent)}")
    if not os.path.isdir(mount_parent):
        err(f"Mount point parent directory does not exist: {mount_parent}\n\n"
            f"  The mount_point is set to: {mount_point}\n"
            f"  Check your config: [nimbie] mount_point")

    dbg("cmd_next: pre-flight checks passed")

    disc_nr = 1
    dbg(f"cmd_next: disc_nr={disc_nr}")
    dir_name_part = build_dir_name(config, disc_nr, mount_point, flavor, cli_naming)
    dir_name = os.path.join(target_dir, dir_name_part) if target_dir else dir_name_part

    # Track status for 'my-nimbie status' queries
    # Resolve effective idx_offset for status display
    effective_offset = cli_naming.get("idx_offset")
    if effective_offset is None:
        effective_offset = config.getint("naming", "idx_offset", fallback=0)
    status = BatchStatus(flavor, mount_point, target_dir, idx_offset=effective_offset, mode="next")
    status.update("starting", disc_nr=disc_nr)

    msg(f"Processing next disc (flavor: {flavor_label})")
    msg(f"  Command:    {on_load}")
    msg(f"  Mount:      {mount_point}")
    msg(f"  Target dir: {target_dir}")
    msg(f"  Dir name:   {dir_name}")

    # Check if there's already a disc in the drive
    if not dry_run:
        state = nimbie.get_state()
        dbg(f"cmd_next: device state before load: {state}")
        if state["disc_in_tray"]:
            msg("\n  Disc already in drive — ejecting to accept bin first...")
            unmount_disc(mount_point)
            nimbie.eject_accept()
            msg("  Previous disc accepted.")

    # Load
    status.start_disc_timer()
    status.update("loading")
    msg("\n  Loading disc from hopper...")
    dbg("cmd_next: calling nimbie.load_disc()")
    has_more = nimbie.load_disc()
    dbg(f"cmd_next: load_disc returned has_more={has_more}")

    if not dry_run:
        vrb(f"  Waiting {settle_time}s for disc to settle...")
        time.sleep(settle_time)

        dbg(f"cmd_next: waiting for mount at {mount_point}")
        if not wait_for_mount(mount_point, mount_timeout, poll_interval):
            warn(f"Disc did not mount within {mount_timeout}s — rejecting")
            dbg("cmd_next: mount timeout, rejecting disc")
            nimbie.eject_reject()
            return
        dbg("cmd_next: disc mounted successfully")

    # Clear DVD title cache for new disc
    _dvd_title_cache.clear()
    dbg("cmd_next: DVD title cache cleared, rebuilding dir_name with mounted disc")

    # Rebuild dir_name now that disc is mounted (DVD_TITLE may be available)
    dir_name_part = build_dir_name(config, disc_nr, mount_point, flavor, cli_naming)
    dir_name = os.path.join(target_dir, dir_name_part) if target_dir else dir_name_part
    msg(f"  Dir name:   {dir_name}")

    # Get source size before running command (for result tracking)
    source_size = _get_mount_size(mount_point)

    # Check if command is "pause" — skip command, go straight to pause loop
    if on_load.strip().lower() == "pause":
        msg(f"\n  PAUSED — command is 'pause', disc loaded and mounted")
        msg(f"  Disc is mounted at: {mount_point}")
        msg(f"  Output directory: {dir_name}")
        rc = 1  # trigger pause loop
        elapsed = 0.0
    else:
        # Run command
        dbg(f"cmd_next: running on_load command")
        rc, elapsed = run_command(on_load, mount_point, disc_nr, target_dir, dir_name, status=status)
        dbg(f"cmd_next: on_load returned rc={rc}, elapsed={elapsed:.1f}s")

        # Validate
        if on_validate:
            dbg(f"cmd_next: running on_validate command")
            rc, _ = run_command(on_validate, mount_point, disc_nr, target_dir, dir_name)
            dbg(f"cmd_next: on_validate returned rc={rc}")
        else:
            dbg("cmd_next: no on_validate configured, skipping")

    # Pause-on-error (or "pause" command): keep disc in drive, wait for command
    is_pause_cmd = on_load.strip().lower() == "pause"
    while rc != 0 and (getattr(args, "pause_on_err", False) or is_pause_cmd):
        msg(f"\n  COMMAND FAILED (exit code {rc}) — disc kept in drive (--pause-on-err)")
        msg(f"  Disc is still mounted at: {mount_point}")
        msg(f"  Output directory: {dir_name}")
        msg(f"\n  Waiting for command from another terminal:")
        msg(f"    my-nimbie retry    — re-run the command")
        msg(f"    my-nimbie accept   — accept the disc anyway")
        msg(f"    my-nimbie reject   — reject the disc")
        answer = _wait_for_pause_command(status)
        if answer == "accept":
            msg("  Received ACCEPT")
            rc = 0
        elif answer == "retry":
            msg("  Received RETRY — re-running command...")
            rc, elapsed = run_command(on_load, mount_point, disc_nr, target_dir, dir_name, status=status)
            dbg(f"cmd_next: retry on_load returned rc={rc}, elapsed={elapsed:.1f}s")
        elif answer == "reject":
            msg("  Received REJECT")
            break
        else:
            warn(f"Unknown pause command: '{answer}' (expected: accept/reject/retry)")
            continue

    dbg(f"cmd_next: unmounting disc before eject")
    unmount_disc(mount_point)

    index_val = disc_nr + (effective_offset or 0)
    total_elapsed = status.get_disc_total_elapsed()
    if rc == 0:
        cmd_time = f"{int(elapsed // 60)}m {int(elapsed % 60):02d}s"
        total_time = f"{int(total_elapsed // 60)}m {int(total_elapsed % 60):02d}s"
        msg(f"  ACCEPTING disc #{index_val} (command succeeded, {_format_size(source_size)}, "
            f"cmd={cmd_time}, total={total_time})")
        status.update("ejecting — accepting")
        nimbie.eject_accept()
        status.record_accept(index=index_val, dir_name=dir_name, source_size=source_size,
                             elapsed=elapsed, total_elapsed=total_elapsed)
    else:
        msg(f"  REJECTING disc #{index_val} (command failed with exit code {rc})")
        status.update("ejecting — rejecting")
        nimbie.eject_reject()
        status.record_reject(index=index_val, dir_name=dir_name, source_size=source_size,
                             elapsed=elapsed, rc=rc, total_elapsed=total_elapsed)

    if has_more:
        msg("  Hopper has more discs available.")
    else:
        msg("  Hopper is empty — that was the last disc.")

    status.finish()
    try:
        os.unlink(PROGRESS_FILE)
    except OSError:
        pass
    dbg("cmd_next: done")


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
        "idx_padding": args.padding,
        "idx_offset":  args.offset,
    }

    flavor_label = flavor or "default"

    # -- Pre-flight checks --
    # Catch problems BEFORE the first disc loads, not halfway through a stack.
    # (target_dir existence + writability is already checked in resolve_batch_flavor)

    # 1. Command binary must exist (check first word of the command)
    cmd_binary = on_load.split()[0] if on_load else ""
    # Expand ~ and env vars but skip if it contains $VAR (runtime variable)
    if cmd_binary and not cmd_binary.startswith("$"):
        cmd_binary_expanded = os.path.expanduser(os.path.expandvars(cmd_binary))
        if not shutil.which(cmd_binary_expanded):
            err(f"Command not found: {cmd_binary}\n\n"
                f"  The on_load command's binary does not exist or is not executable.\n"
                f"  Check your config: [commands] on_load_{BATCH_FLAVORS.get(flavor, 'DEFAULT')}")

    # 3. Validate command if set
    if on_validate:
        val_binary = on_validate.split()[0]
        if val_binary and not val_binary.startswith("$"):
            val_binary_expanded = os.path.expanduser(os.path.expandvars(val_binary))
            if not shutil.which(val_binary_expanded):
                err(f"Validation command not found: {val_binary}\n\n"
                    f"  Check your config: [commands] on_validate")

    # 4. Mount point path must be a valid parent directory
    mount_parent = os.path.dirname(mount_point)
    if not os.path.isdir(mount_parent):
        err(f"Mount point parent directory does not exist: {mount_parent}\n\n"
            f"  The mount_point is set to: {mount_point}\n"
            f"  Check your config: [nimbie] mount_point")

    # Initialize status tracking
    effective_offset = cli_naming.get("idx_offset")
    if effective_offset is None:
        effective_offset = config.getint("naming", "idx_offset", fallback=0)
    status = BatchStatus(flavor, mount_point, target_dir, idx_offset=effective_offset, mode="batch")
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
            status.start_disc_timer()
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

            # Get source size + index for result tracking
            source_size = _get_mount_size(mount_point)
            index_val = status.disc_nr + (effective_offset or 0)

            # Check if command is "pause"
            is_pause_cmd = on_load.strip().lower() == "pause"
            if is_pause_cmd:
                msg(f"\n  PAUSED — command is 'pause', disc loaded and mounted")
                rc = 1
                elapsed = 0.0
            else:
                # Run command
                rc, elapsed = run_command(on_load, mount_point, status.disc_nr, target_dir, dir_name, status=status)

                # Validate
                if on_validate:
                    rc, _ = run_command(on_validate, mount_point, status.disc_nr, target_dir, dir_name)

            # Pause-on-error (or "pause" command): keep disc in drive, wait for command
            stop_batch = False
            while rc != 0 and (args.pause_on_err or is_pause_cmd):
                msg(f"\n  COMMAND FAILED (exit code {rc}) — disc kept in drive (--pause-on-err)")
                msg(f"  Disc #{status.disc_nr} is still mounted at: {mount_point}")
                msg(f"  Output directory: {dir_name}")
                msg(f"\n  Waiting for command from another terminal:")
                msg(f"    my-nimbie retry    — re-run the command")
                msg(f"    my-nimbie accept   — accept the disc anyway")
                msg(f"    my-nimbie reject   — reject the disc and continue batch")
                msg(f"    my-nimbie stop     — reject the disc and stop the batch")
                answer = _wait_for_pause_command(status)
                if answer == "accept":
                    msg("  Received ACCEPT")
                    rc = 0
                elif answer == "retry":
                    msg("  Received RETRY — re-running command...")
                    rc, elapsed = run_command(on_load, mount_point, status.disc_nr, target_dir, dir_name, status=status)
                elif answer == "stop":
                    msg("  Received STOP")
                    unmount_disc(mount_point)
                    nimbie.eject_reject()
                    total_elapsed = status.get_disc_total_elapsed()
                    status.record_reject(index=index_val, dir_name=dir_name, source_size=source_size,
                                         elapsed=elapsed, rc=rc, total_elapsed=total_elapsed)
                    stop_batch = True
                    break
                elif answer == "reject":
                    msg("  Received REJECT")
                    break
                else:
                    warn(f"Unknown pause command: '{answer}' (expected: accept/reject/retry/stop)")
                    continue
            if stop_batch:
                break

            unmount_disc(mount_point)

            total_elapsed = status.get_disc_total_elapsed()
            if rc == 0:
                cmd_time = BatchStatus._fmt_elapsed(elapsed)
                total_time = BatchStatus._fmt_elapsed(total_elapsed)
                msg(f"  Disc #{status.disc_nr}: ACCEPTING (command succeeded, "
                    f"{_format_size(source_size)}, cmd={cmd_time}, total={total_time})")
                nimbie.eject_accept()
                status.record_accept(index=index_val, dir_name=dir_name, source_size=source_size,
                                     elapsed=elapsed, total_elapsed=total_elapsed)
            else:
                msg(f"  Disc #{status.disc_nr}: REJECTING (command failed with exit code {rc})")
                nimbie.eject_reject()
                status.record_reject(index=index_val, dir_name=dir_name, source_size=source_size,
                                     elapsed=elapsed, rc=rc, total_elapsed=total_elapsed)

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
  {INDEX}       — disc index: DISC_NR + idx_offset (default offset 0 → starts at 1)
                  E.g. --offset 50 → first disc is 051, then 052, ...
  {DISC_NR}     — raw disc number (1, 2, 3, ...) without padding or offset
  {MEDIA_TYPE}  — "DVD" or "CD" (auto-detected from disc content)
  {DVD_TITLE}   — volume name of the disc (lazy: only read when referenced)
  {FLAVOR}      — batch flavor name ("default", "ripdvd", "ripaudio", "readdvd")
  {DATE}        — current date as YYYY-MM-DD"""


class _HelpfulParser(argparse.ArgumentParser):
    """ArgumentParser that adds guidance hints to common errors."""

    HINTS = {
        "--offset":  "  --offset requires a number, e.g.: --offset 50\n"
                     "  {INDEX} = DISC_NR + offset, where DISC_NR starts at 1.\n"
                     "  With --offset 50, numbering starts at 051, 052, 053, ...\n"
                     "  With --offset 209, numbering starts at 210, 211, 212, ...",
        "--padding": "  --padding requires a number, e.g.: --padding 3\n"
                     "  Controls zero-padding width for {INDEX}: 3 → 001, 4 → 0001",
    }

    def error(self, message):
        self.print_usage(sys.stderr)
        hint = ""
        for flag, text in self.HINTS.items():
            if flag in message:
                hint = f"\n{text}\n"
                break
        self.exit(2, f"\n  ERROR: {self.prog}: {message}\n{hint}")


def build_parser():
    parser = _HelpfulParser(
        prog="my-nimbie",
        description="CLI controller for Nimbie USB Plus NB21 disc autoloader.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""\
Examples:
  my-nimbie load                          Load next disc from hopper
  my-nimbie eject                         Eject disc to accept (done) bin
  my-nimbie reject                        Eject disc to reject bin
  my-nimbie status                        Show device state or batch progress

  my-nimbie batch                         List available flavors (or run DEFAULT if set)
  my-nimbie batch ripdvd                  Batch process using on_load_RIP_DVD command
  my-nimbie batch ripaudio                Batch process using on_load_RIP_AUDIOCD command
  my-nimbie batch readdvd                 Batch process using on_load_READ_DVD command
  my-nimbie batch --target-dir /out ripdvd   Override output directory for this run

  my-nimbie batch --prefix "{{DVD_TITLE}}" --name "" ripdvd
  my-nimbie batch --offset 50 ripaudio     Continue numbering from 51 (after 50 previous discs)
  my-nimbie batch --name " - {{DVD_TITLE}} ({{MEDIA_TYPE}})" readdvd

  my-nimbie --dry batch ripdvd            Dry-run: show what would happen
  my-nimbie --config ~/test.conf batch    Use a specific config file
  my-nimbie --create-config               Create example config at ~/.my-nimbie.conf
  my-nimbie --create-config /LINKS/default/my-nimbie   Create example config at given path

Config file search order:
  1. ~/.my-nimbie.conf
  2. /etc/my-nimbie.conf
  3. /LINKS/default/my-nimbie

Batch flavors and their config keys:
  ripdvd      → [commands] on_load_RIP_DVD      [target_dirs] rip_dvd
  ripaudio    → [commands] on_load_RIP_AUDIOCD   [target_dirs] rip_audiocd
  readdvd     → [commands] on_load_READ_DVD     [target_dirs] read_dvd

  Each flavor runs a different command from the config file.
  Optional: set on_load_DEFAULT to allow "batch" without a flavor.
  The target directory can be set per flavor in [target_dirs] or overridden
  with --target-dir on the CLI.

Available variables in [commands] ($VAR or ${{VAR}} syntax):
  $MOUNT_POINT  — disc mount point (from [nimbie] mount_point)
  $TARGET_DIR   — base output directory (from [target_dirs] or --target-dir)
  $DIR_NAME     — full per-disc output path: TARGET_DIR / <name from [naming]>
  $DISC_NR      — sequential disc number (1, 2, 3, ...)
  $DEVICE       — raw device path (e.g. /dev/disk4), resolved from mount_point

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
    parser.add_argument("--debug", "-D", action="count", default=0,
                        help="debug output; -DD for deep debug (implies --verbose)")

    sub = parser.add_subparsers(dest="command", parser_class=_HelpfulParser)
    sub.add_parser("load",   help="Load next disc from hopper into drive")
    sub.add_parser("eject",  help="Eject current disc to accept (done) bin")
    sub.add_parser("reject", help="Reject current disc to reject bin (or: tell paused batch to reject)")
    sub.add_parser("status", help="Show Nimbie device state (or batch progress if running)")
    sub.add_parser("accept", help="Tell a paused batch/next to accept the disc")
    sub.add_parser("retry",  help="Tell a paused batch/next to retry the command")
    sub.add_parser("stop",   help="Tell a paused batch/next to reject and stop")

    # --- next: process exactly one disc (same setup as batch, for testing) ---
    next_parser = sub.add_parser("next", help="Process exactly one disc (same setup as batch, for testing)",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 description=f"""\
Process exactly one disc from the Nimbie hopper.

Uses the same pre-flight checks, flavors, and naming as "batch", but stops
after a single disc. This is useful for testing your batch setup before
committing to a full run.

Loads a disc, runs the configured command, accepts or rejects the disc
based on the command's exit code (0 = accept, non-zero = reject), and
reports whether the hopper has more discs.

Flavors select which command from the config file to run:
  ripdvd      run [commands] on_load_RIP_DVD     (e.g. my-handbrake encode)
  ripaudio    run [commands] on_load_RIP_AUDIOCD  (e.g. cdparanoia + flac)
  readdvd     run [commands] on_load_READ_DVD    (e.g. dvdbackup mirror)
  (none)      run [commands] on_load_DEFAULT     (optional, allows "next" without flavor)

Per-disc directory naming:
  DIR_NAME = TARGET_DIR / {{NAME_PREFIX}}{{NAME}}{{NAME_POSTFIX}}
  Defaults: --prefix "{{INDEX}}" --name " - {{MEDIA_TYPE}}" --postfix ""
  Example result: "/out/001 - DVD"

  Supported {{VARIABLE}} placeholders:
{NAMING_VARS_HELP}""")
    next_parser.add_argument("flavor", nargs="?", default=None,
                             choices=list(BATCH_FLAVORS.keys()),
                             help="processing flavor (omit to list available or use on_load_DEFAULT)")
    next_parser.add_argument("--target-dir", "-t", metavar="DIR",
                             help="base output directory (overrides [target_dirs] from config)")
    next_parser.add_argument("--prefix", metavar="STR",
                             help="directory name prefix (default: \"{INDEX}\", overrides [naming] name_prefix)")
    next_parser.add_argument("--name", metavar="STR",
                             help="directory name middle part (default: \" - {MEDIA_TYPE}\", overrides [naming] name)")
    next_parser.add_argument("--postfix", metavar="STR",
                             help="directory name postfix (default: \"\", overrides [naming] name_postfix)")
    next_parser.add_argument("--offset", metavar="N", type=int,
                             help="offset added to disc number for {INDEX} (default: 0). "
                             "INDEX = DISC_NR + offset, DISC_NR starts at 1. "
                             "E.g. --offset 209 → first disc is 210, then 211, ...")
    next_parser.add_argument("--padding", metavar="N", type=int,
                             help="zero-padding width for {INDEX} (default: 3, e.g. 3 → \"001\")")
    next_parser.add_argument("--pause-on-err", action="store_true",
                             help="pause on command error (keep disc in drive, wait for user)")

    # --- batch: process all discs in hopper ---
    batch_parser = sub.add_parser("batch", help="Batch mode: load → process → accept/reject → repeat",
                                  formatter_class=argparse.RawDescriptionHelpFormatter,
                                  description=f"""\
Batch-process discs from the Nimbie hopper.

Loads a disc, runs the configured command, and accepts or rejects the disc
based on the command's exit code (0 = accept, non-zero = reject). Repeats
until the hopper is empty or max_discs is reached.

Flavors select which command from the config file to run:
  ripdvd      run [commands] on_load_RIP_DVD     (e.g. my-handbrake encode)
  ripaudio    run [commands] on_load_RIP_AUDIOCD  (e.g. cdparanoia + flac)
  readdvd     run [commands] on_load_READ_DVD    (e.g. dvdbackup mirror)
  (none)      run [commands] on_load_DEFAULT     (optional, allows "batch" without flavor)

Per-disc directory naming:
  DIR_NAME = TARGET_DIR / {{NAME_PREFIX}}{{NAME}}{{NAME_POSTFIX}}
  Defaults: --prefix "{{INDEX}}" --name " - {{MEDIA_TYPE}}" --postfix ""
  Example result: "/out/001 - DVD"

  Supported {{VARIABLE}} placeholders:
{NAMING_VARS_HELP}

Progress is tracked in /tmp/my-nimbie.status and can be queried:
  my-nimbie status              from another terminal
  kill -USR1 <pid>              prints live status to stderr of the batch""")
    batch_parser.add_argument("flavor", nargs="?", default=None,
                              choices=list(BATCH_FLAVORS.keys()),
                              help="processing flavor (omit to list available or use on_load_DEFAULT)")
    batch_parser.add_argument("--target-dir", "-t", metavar="DIR",
                              help="base output directory (overrides [target_dirs] from config)")
    batch_parser.add_argument("--prefix", metavar="STR",
                              help="directory name prefix (default: \"{INDEX}\", overrides [naming] name_prefix)")
    batch_parser.add_argument("--name", metavar="STR",
                              help="directory name middle part (default: \" - {MEDIA_TYPE}\", overrides [naming] name)")
    batch_parser.add_argument("--postfix", metavar="STR",
                              help="directory name postfix (default: \"\", overrides [naming] name_postfix)")
    batch_parser.add_argument("--offset", metavar="N", type=int,
                              help="starting index for {INDEX} (default: 1). "
                              "--offset N means the first disc IS N. "
                              "E.g. --offset 50 → 050, 051, 052, ...")
    batch_parser.add_argument("--padding", metavar="N", type=int,
                              help="zero-padding width for {INDEX} (default: 3, e.g. 3 → \"001\")")
    batch_parser.add_argument("--pause-on-err", action="store_true",
                              help="pause batch on command error (keep disc in drive, wait for user)")

    return parser


def main():
    global verbose, debug, deepdebug, dry_run

    signal.signal(signal.SIGINT, signal_handler)

    # Pre-process argv: expand -DD → -D -D and hoist global flags before subcommand
    argv = sys.argv[1:]
    expanded = []
    for arg in argv:
        if re.match(r'^-D{2,}$', arg):
            expanded.extend(["-D"] * len(arg[1:]))
        else:
            expanded.append(arg)
    # Hoist global flags (-D, -v, -d, --debug, --verbose, --dry) before the subcommand
    subcommands = {"load", "eject", "reject", "status", "next", "batch"}
    global_flags = {"-D", "-v", "-d", "--debug", "--verbose", "--dry"}
    hoisted = []
    rest = []
    found_subcmd = False
    for arg in expanded:
        if not found_subcmd and arg in subcommands:
            found_subcmd = True
            rest.append(arg)
        elif found_subcmd and arg in global_flags:
            hoisted.append(arg)
        else:
            rest.append(arg)
    final_argv = hoisted + rest

    parser = build_parser()
    args = parser.parse_args(final_argv)

    debug = args.debug >= 1
    deepdebug = args.debug >= 2
    verbose = args.verbose or debug
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

    # Pause control commands: send command to paused batch/next process, no USB needed
    if args.command in ("accept", "retry", "stop"):
        sf = BatchStatus.read_file()
        if sf and "PAUSED" in sf.get("state", ""):
            _send_pause_command(args.command)
        else:
            err(f"No paused batch/next process found.\n\n"
                f"  '{args.command}' only works when a batch or next process is paused (--pause-on-err).\n"
                f"  Current state: {sf.get('state', 'no status file') if sf else 'no status file'}")
        return

    # 'reject' doubles as pause command when a process is paused
    if args.command == "reject":
        sf = BatchStatus.read_file()
        if sf and "PAUSED" in sf.get("state", ""):
            _send_pause_command("reject")
            return

    # Status command: try reading status file first, only connect if no batch running
    if args.command == "status":
        sf = BatchStatus.read_file()
        if sf and sf.get("state") not in ("finished", "interrupted"):
            # Batch/next is running — show status from file, don't grab USB
            cmd_status(None, config, args)
            return

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
            "next":   cmd_next,
            "batch":  cmd_batch,
        }
        commands[args.command](nimbie, config, args)
    finally:
        nimbie.disconnect()


if __name__ == "__main__":
    main()
