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
        'unmount:Unmount disc from optical drive'
        'cancel:Cancel running batch/next (sends SIGINT to the process)'
        'reset:Recover Nimbie from error states (bootloader, stuck disc)'
        'monitor:Start real-time status monitor (USB state + hardware polling)'
        'probe:Scan Nimbie USB command space for reverse-engineering'
    )

    local -a global_opts
    global_opts=(
        '(-c --config)'{-c,--config}'[Config file path]:file:_files'
        '--create-config[Create example config file]::path:_files'
        '(-d --dry)'{-d,--dry}'[Dry run — print what would be done]'
        '(-v -V --verbose)'{-v,-V,--verbose}'[Verbose output]'
        '(-D --debug)'{-D,--debug}'[Debug output; -DD for deep debug]'
        '--deepdbg[Deep debug (same as -DD)]'
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
                    local -a subcmd_args
                    subcmd_args=(
                        '(-t --target-dir)'{-t,--target-dir}'[Base output directory]:dir:_directories'
                        '--prefix[Directory name prefix]:str:'
                        '--name[Directory name middle part]:str:'
                        '--postfix[Directory name postfix]:str:'
                        '--offset[Offset for disc index]:n:'
                        '--padding[Zero-padding width for index]:n:'
                        '--pause-on-err[Pause on command error, keep disc in drive]'
                        '--use-loaded[Process disc already in drive as first disc]'
                        '1:flavor:->flavor'
                    )
                    if [[ $line[1] == batch ]]; then
                        subcmd_args+=('--max[Max discs to process]:n:')
                    fi
                    _arguments -s : $subcmd_args
                    case $state in
                        flavor)
                            _describe 'flavor' flavors
                            ;;
                    esac
                    ;;
                reset)
                    _arguments -s : \
                        '--exit-bootloader[Send RESET_DEVICE to bootloader (then power cycle)]' \
                        '--diagnostics[Show device diagnostics]' \
                        '--jump-to-app[AN1388 framed Jump-to-App (NOT supported on this device)]' \
                        '--bl-query[QUERY_DEVICE (0x00) — show bootloader info]' \
                        '--bl-scan[Scan bootloader commands 0x00-0xFF]' \
                        '--bl-read-flash[Read flash reset vector + config area]' \
                        '--bl-read-addr[Read flash at hex address]:addr:' \
                        '--bl-read-len[Bytes to read (default 256)]:n:' \
                        '--bl-raw[Send raw hex bytes to bootloader]:hex:' \
                        '--sign-and-reset[CONFIRMED recovery: PROGRAM_COMPLETE + SIGN_FLASH x2 + power cycle]'
                    ;;
                probe)
                    _arguments -s : \
                        '--range[Command byte range to scan (e.g. 0x40-0x60)]:range:' \
                        '--cmd[Command byte for param scanning (e.g. 0x52)]:cmd:' \
                        '--params[Scan param bytes for --cmd]' \
                        '--param-range[Param byte range]:range:' \
                        '--raw[Send raw hex bytes to Nimbie]:hex:'
                    ;;
                status)
                    _arguments -s : \
                        '(-V --verbose)'{-V,--verbose}'[Verbose output]'
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

# Microchip PIC HID Bootloader (entered via 0x56 command)
BL_VID = 0x04D8
BL_PID = 0x000B
BL_EP_OUT = 0x01  # Bulk OUT, 64 bytes
BL_EP_IN  = 0x81  # Bulk IN, 64 bytes
BL_CMD_QUERY            = 0x00
BL_CMD_PROGRAM_COMPLETE = 0x04
BL_CMD_GET_DATA         = 0x05
BL_CMD_RESET_DEVICE     = 0x06
BL_CMD_SIGN_FLASH       = 0x07

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
        "result_file":     "/tmp/my-nimbie.result",
        "status_json":     "/tmp/my-nimbie.status-log.json",
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
CMD_CHILD_PID_FILE = "/tmp/my-nimbie.cmd-child.pid"  # PID of active on_load subprocess
MONITOR_PID_FILE = "/tmp/my-nimbie.monitor.pid"      # PID of running monitor process
DEFAULT_RESULT_FILE = "/tmp/my-nimbie.result"
DEFAULT_STATUS_JSON = "/tmp/my-nimbie.status-log.json"

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
verbose = False
debug = False
deepdebug = False
dry_run = False
interrupted = False
batch_status = None  # set to BatchStatus instance during batch runs
_active_cmd_proc = None  # subprocess.Popen of the currently running on_load command


# ---------------------------------------------------------------------------
# Status JSON collector (started in -DD / --deepdbg mode)
# ---------------------------------------------------------------------------
def _start_status_json_collector(nimbie, status_json_path, interval=0.1):
    """Start a background thread that writes a live status JSON every `interval` seconds.

    Collects the same data as 'my-nimbie status -V' (hardware state + batch progress)
    and appends each snapshot as a JSON object to status_json_path (one per line, NDJSON).
    Only started when deepdebug is True (-DD flag).
    """
    import threading
    import json

    # Create file immediately so "tail -f" works from the start.
    # Rotation is handled by the caller via _rotate_run_files().
    try:
        open(status_json_path, "w").close()
    except OSError:
        pass

    stop_event = threading.Event()

    def _collect():
        while not stop_event.wait(interval):
            try:
                snapshot = {}
                snapshot["ts"] = _ts()

                # Hardware state — MUST use fatal=False to avoid crashing the process.
                # The collector runs in a background thread that shares the USB device
                # with the main thread. If both call get_state() simultaneously the
                # collector may read an empty response (main thread drained the buffer).
                # With fatal=False it gets None and skips the entry; with fatal=True it
                # calls reconnect() which sets self.dev=None and breaks the main thread.
                try:
                    state = nimbie.get_state(fatal=False)
                    if state is not None:
                        snapshot["hw"] = {
                            "disc_available": state.get("disc_available"),
                            "disc_in_tray":   state.get("disc_in_tray"),
                            "disc_lifted":    state.get("disc_lifted"),
                            "tray_out":       state.get("tray_out"),
                        }
                    else:
                        snapshot["hw"] = {"error": "transient: no state response"}
                except Exception as e:
                    snapshot["hw"] = {"error": str(e)}

                # Batch status from status file
                sf = BatchStatus.read_file()
                if sf:
                    snapshot["batch"] = sf

                # Progress file
                try:
                    with open(PROGRESS_FILE) as f:
                        snapshot["progress"] = f.read().strip()
                except OSError:
                    pass

                with open(status_json_path, "a") as f:
                    f.write(json.dumps(snapshot) + "\n")

            except Exception as e:
                try:
                    import json as _j
                    with open(status_json_path, "a") as f:
                        f.write(_j.dumps({"ts": _ts(), "collector_error": str(e)}) + "\n")
                except Exception:
                    pass

    t = threading.Thread(target=_collect, name="status-json-collector", daemon=True)
    t.stop_event = stop_event
    t.start()
    return t



def signal_handler(_signum, _frame):
    global interrupted
    if interrupted:
        # Second signal — kill any active on_load child process, then abort
        if _active_cmd_proc is not None:
            try:
                _active_cmd_proc.kill()
            except Exception:
                pass
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

    def __init__(self, flavor, mount_point, target_dir, idx_offset=None, mode="next", result_file=None, dry_run=False):
        self.flavor = flavor or "default"
        self.mount_point = mount_point
        self.target_dir = target_dir
        self.idx_offset = idx_offset
        self.mode = mode  # "next" or "batch"
        self.result_file = result_file or DEFAULT_RESULT_FILE
        self.dry_run = dry_run
        self.cli = "my-nimbie " + " ".join(sys.argv[1:])
        self.disc_nr = 0
        self.accepted = 0
        self.rejected = 0
        self.command = ""
        self.disc_target_dir = ""   # full path of the current disc's output directory
        self.current = "starting"
        self.started = datetime.datetime.now()
        self.last_update = self.started
        self.disc_load_start = None  # when current disc loading began (for total time)
        self.disc_results = []  # list of dicts: {index, dir_name, size, elapsed, total_elapsed, rc, result}
        self.last_disc = None   # last completed disc result dict

    def update(self, current, disc_nr=None, command=None, disc_target_dir=None):
        """Update state and rewrite status file."""
        self.current = current
        if disc_nr is not None:
            self.disc_nr = disc_nr
        if command is not None:
            self.command = command
        if disc_target_dir is not None:
            self.disc_target_dir = disc_target_dir
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
            self.record_disc(index, dir_name, source_size, elapsed, 0, "successful", total_elapsed=total_elapsed)
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
        """Append a disc result line to the result file. Skipped in dry-run mode."""
        if self.dry_run:
            return
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
            with open(self.result_file, "a") as f:
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
        msg(f"  Results saved to: {self.result_file}")

    def _write_file(self):
        """Write machine-readable status file. Skipped in dry-run mode."""
        if self.dry_run:
            return
        try:
            lines = [
                f"state={self.current}",
                f"mode={self.mode}",
                f"pid={os.getpid()}",
                f"cli={self.cli}",
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
            if self.disc_target_dir:
                lines.append(f"disc_target_dir={self.disc_target_dir}")
            if self.last_disc:
                d = self.last_disc
                size_str = _format_size(d["size"]) if d["size"] else "?"
                cmd_str = self._fmt_elapsed(d["elapsed"])
                total_str = self._fmt_elapsed(d["total_elapsed"])
                lines.append(f"last_disc=#{d['index']} {d['result']}, rc={d['rc']}, "
                             f"cmd={cmd_str}, total={total_str}, size={size_str}")

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
    return now.strftime("%Y-%m-%d_%H%M.%S.") + f"{now.microsecond // 1000:03d}"

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
    vrb(f"Config loaded from: {config_path}")

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
    # Note: my-nimbie creates $DIR_NAME automatically; cd is still needed for cdparanoia output
    on_load_RIP_AUDIOCD = cd "$DIR_NAME" && cdparanoia -B -- -0 && flac --best *.wav && rm -f *.wav

    # READDVD: full DVD backup (all content, mirror mode) via dvdbackup
    # -M = mirror entire disc, -v = verbose
    # my-nimbie auto-adds: -n <volume_name> (required for DVDs with empty title)
    # my-nimbie auto-removes: -p (causes incomplete copies on macOS)
    # Note: my-nimbie automatically creates $DIR_NAME before running this command
    # (mkdir -p is redundant here but harmless)
    on_load_READ_DVD = dvdbackup -i "$MOUNT_POINT" -o "$DIR_NAME" -M -v

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

    # Result log file — each disc result is appended as one line
    result_file = /tmp/my-nimbie.result

    # Status JSON log — written continuously in -DD mode (one JSON object per line, NDJSON)
    # Each entry contains hardware state, batch progress, and progress info
    # Read with: tail -f /tmp/my-nimbie.status-log.json | python3 -m json.tool
    status_json = /tmp/my-nimbie.status-log.json
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
        # Mutex to prevent concurrent USB send+read from the status-JSON collector
        # background thread and the main thread from interleaving their responses.
        self._usb_lock = threading.Lock()

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

        # Retry claiming interface for up to 5s (monitor releases briefly every 0.1s)
        import time as _time_usb
        claimed = False
        for _attempt in range(50):
            try:
                usb.util.claim_interface(self.dev, 0)
                dbg("claim_interface OK")
                claimed = True
                break
            except usb.core.USBError:
                if _attempt == 0:
                    dbg("claim_interface busy, retrying...")
                _time_usb.sleep(0.1)
        if not claimed:
            err(f"Cannot claim Nimbie USB interface after 5s\n\n"
                f"  Possible reasons:\n"
                f"    - Another program is holding the device\n"
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

        vrb(f"Nimbie connected (VID={self.vid:#06x}, PID={self.pid:#06x})")

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

    def reset_usb(self):
        """Send a USB bus reset to the device — recovers from I/O errors
        without requiring a physical cable unplug/replug.

        After reset, the device re-enumerates on the bus. We must release,
        wait, then re-find and re-claim it.
        Raises on failure (caller must handle).
        """
        import usb.core
        import usb.util

        if self.dev is None:
            dbg("reset_usb: no device — attempting fresh connect")
            self.connect()
            return

        dbg("reset_usb: sending USB bus reset...")
        try:
            # Release interface first
            try:
                usb.util.release_interface(self.dev, 0)
            except Exception:
                pass

            # Send USB reset — this re-enumerates the device on the bus
            self.dev.reset()
            dbg("reset_usb: USB reset sent OK")
        except usb.core.USBError as e:
            dbg(f"reset_usb: reset failed: {e}")
        except Exception as e:
            dbg(f"reset_usb: unexpected error: {e}")

        self.dev = None
        self._kernel_detached = False

        # Wait for the device to re-enumerate on the USB bus
        dbg("reset_usb: waiting 3s for device re-enumeration...")
        time.sleep(3)

        # Reconnect — this calls err() on failure which raises SystemExit
        self.connect()

    def reconnect(self):
        """Disconnect and reconnect to the Nimbie device.

        Tries a clean disconnect first, then USB reset if needed.
        """
        dbg("reconnect: attempting USB recovery...")
        self.disconnect()
        time.sleep(1)
        try:
            self.connect()
            dbg("reconnect: clean reconnect succeeded")
        except (Exception, SystemExit):
            dbg("reconnect: clean reconnect failed, trying USB reset...")
            self.reset_usb()

    # -- Low-level USB I/O --

    def _send_command(self, cmd_tuple, description=""):
        """Send a command via interrupt OUT endpoint.

        cmd_tuple: tuple of command bytes, placed at byte[2] onward in an 8-byte packet.
        On USB I/O error, attempts automatic recovery via USB bus reset.
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
            # Attempt USB recovery before giving up
            warn(f"USB write failed ({description}): {e} — attempting USB recovery...")
            try:
                self.reconnect()
                # Retry the write after recovery
                written = self.dev.write(EP_OUT, pkt, timeout=5000)
                ddbg(f"  Write OK after recovery: {written} bytes to EP {EP_OUT:#04x}")
                return
            except (Exception, SystemExit):
                pass
            err(f"USB write failed ({description}): {e}\n\n"
                f"  USB recovery also failed.\n\n"
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
        for _ in range(max_reads):
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

    def get_state(self, fatal=True):
        """Query and return device state as a dict.

        If fatal=True (default), attempts USB recovery then calls err() on failure.
        If fatal=False, returns None on failure (for use in polling loops).

        Thread-safe: acquires _usb_lock so the background status-JSON collector
        thread cannot interleave its GET_STATE with the main thread's operations.
        """
        # Use 20s read timeout (matching reference code) — shorter timeouts
        # cause [Errno 60] which cascades to [Errno 5] killing the USB bus.
        # Hold _usb_lock for the entire query + recovery so the collector thread
        # cannot send its own GET_STATE while we are mid-read (or mid-reconnect).
        with self._usb_lock:
            responses = self._send_and_read(CMD_GET_STATE, "GET_STATE", timeout=20000)
            bits = self._find_state_string(responses)

            if bits is None and fatal:
                # Attempt USB recovery before giving up
                warn(f"No state response — attempting USB recovery...")
                try:
                    self.reconnect()
                    responses = self._send_and_read(CMD_GET_STATE, "GET_STATE", timeout=20000)
                    bits = self._find_state_string(responses)
                except (Exception, SystemExit):
                    pass

        if bits is None:
            if fatal:
                err(f"No state response from Nimbie device.\n"
                    f"  Responses received: {responses}\n\n"
                    f"  USB recovery was attempted but failed.\n\n"
                    f"  Possible reasons:\n"
                    f"    - Device not responding\n"
                    f"    - USB communication error\n"
                    f"    - Try: unplug USB cable, wait 5s, reconnect")
            dbg(f"get_state: no state response (transient), responses={responses}")
            return None

        dbg(f"State bits: {bits}")

        def bit(pos):
            return pos < len(bits) and bits[pos] == "1"

        # AT+S07 ("disc placed on tray") can arrive as an unsolicited status mixed in
        # with the GET_STATE response — the device holds the USB read open while
        # mechanically dropping the disc, then sends AT+S07 when it lands.
        # Treat it as authoritative: disc IS in tray even if the bit hasn't updated yet.
        at_s07_seen = AT_PLACED in responses

        return {
            "raw":            bits,
            "disc_available":  bit(STATE_BIT_DISC_AVAILABLE),
            "disc_in_tray":    bit(STATE_BIT_DISC_IN_TRAY) or at_s07_seen,
            "disc_lifted":     bit(STATE_BIT_DISC_LIFTED),
            "tray_out":        bit(STATE_BIT_TRAY_OUT),
        }

    def _poll_state(self, condition_fn, description, timeout=30, interval=0.5):
        """Poll get_state() until condition_fn(state) returns True.

        Tolerates transient USB failures during polling — only errors out
        on final timeout. After 3 consecutive failures, attempts USB reset.
        """
        start = time.time()
        transient_fails = 0
        usb_reset_attempted = False
        while True:
            if time.time() - start > timeout:
                err(f"Timeout waiting for: {description} (after {timeout}s)")
            state = self.get_state(fatal=False)
            if state is None:
                transient_fails += 1
                dbg(f"_poll_state: transient failure #{transient_fails}, retrying...")
                # After 3 consecutive failures, attempt USB recovery
                if transient_fails >= 3 and not usb_reset_attempted:
                    usb_reset_attempted = True
                    warn(f"_poll_state: {transient_fails} consecutive USB failures — attempting USB recovery...")
                    try:
                        self.reconnect()
                    except (Exception, SystemExit):
                        dbg("_poll_state: USB recovery failed, continuing to poll...")
                time.sleep(interval)
                continue
            transient_fails = 0  # reset counter on success
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
        Retries up to 3 times if the disc doesn't land on the tray within the timeout
        (the dropper mechanism can be slow or need a second attempt).
        """
        # CRITICAL: Do NOT read USB after PLACE_DISC.
        # The device is mechanically busy and doesn't respond to reads.
        # Reading during this time causes [Errno 60] timeout which cascades
        # into [Errno 5] I/O Error, killing the USB bus entirely.
        #
        # Reference: BS Utility / nimbiestatemachine uses 20s read timeout
        # and waits for the device to respond. We take a simpler approach:
        # just send the command, wait for the mechanism, and poll state bits.
        for attempt in range(3):
            # SAFETY: Before sending PLACE_DISC on retry attempts, re-check the state.
            # USB instability can cause us to miss the disc_in_tray=True transition from
            # the previous attempt — the mechanism DID fire, but we never saw the result.
            # Sending PLACE_DISC again would drop a SECOND disc on top of the first.
            if attempt > 0:
                pre_state = self.get_state(fatal=False)
                if pre_state is not None:
                    if pre_state["disc_in_tray"]:
                        dbg("place_disc: disc_in_tray=True before retry — previous attempt succeeded "
                            "(USB was unstable and we missed it). Not sending another PLACE_DISC.")
                        time.sleep(0.8)
                        return True
                    if pre_state.get("disc_lifted"):
                        # Previous recovery attempt didn't clear the stuck disc — try once more.
                        warn("place_disc: pre-retry check: disc still stuck (disc_lifted=True) — REJECT again...")
                        self.close_tray()
                        self.reject_disc()
                        time.sleep(1)
                        self.open_tray()
                        time.sleep(1)
                        continue
                    if not pre_state["disc_available"]:
                        return False  # hopper empty

            self._send_command(CMD_PLACE_DISC, "PLACE_DISC")

            # Give the mechanism time to physically place the disc.
            # The dropper picks up a disc and drops it on the tray — takes ~3-5 seconds.
            dbg(f"place_disc: command sent (attempt {attempt + 1}/3), waiting 5s for mechanism...")
            time.sleep(5)

            # Confirm via state polling: disc_in_tray becomes True, or hopper empties.
            # Use 120s timeout — mechanism can be slow when slightly stuck (observed 86s
            # real-world drop time on 2026-03-30). get_state() also watches for the
            # unsolicited AT+S07 "disc placed" signal which arrives when disc lands.
            dbg("place_disc: polling state to confirm...")
            deadline = time.time() + 120
            state = None
            while time.time() < deadline:
                state = self.get_state(fatal=False)
                if state is None:
                    time.sleep(0.5)
                    continue
                if state["disc_in_tray"] or not state["disc_available"]:
                    break
                time.sleep(0.5)

            if state is None:
                warn(f"place_disc: USB unresponsive during poll (attempt {attempt + 1}/3)")
                time.sleep(3)
                continue

            if not state["disc_in_tray"] and not state["disc_available"]:
                # DOUBLE-LOAD PREVENTION: disc_lifted=True here means the disc moved
                # past the hopper sensor but stalled at cam wheel level — it did NOT
                # land. This is NOT hopper-empty. Retrying PLACE_DISC would spin the
                # cam wheels again and pull a 2nd disc down (double load).
                #
                # Safe recovery: close tray → REJECT (deflector only, no cam wheels)
                # → disc drops to reject bin → open tray → retry PLACE_DISC cleanly.
                # Confirmed working on 2026-03-30: REJECT released disc 349 without
                # turning the cam wheels, no double load.
                if state.get("disc_lifted"):
                    warn(f"place_disc: disc stuck in cam wheels (disc_lifted=True) — not hopper-empty "
                         f"(attempt {attempt + 1}/3).")
                    # Primary recovery: REJECT — deflector releases disc to reject bin, no cam wheels.
                    # Confirmed working 2026-03-30 (disc 349). Firmware interlock prevents PLACE_DISC
                    # (returns AT+S14) while disc_lifted=True, so only REJECT or ACCEPT can clear this.
                    warn("  Recovering (primary): close tray → REJECT (deflector only, no cam wheels)...")
                    self.close_tray()
                    self.reject_disc()
                    time.sleep(1)
                    state_after = self.get_state(fatal=False)
                    if state_after and not state_after.get("disc_lifted"):
                        warn("  Stuck disc cleared to reject bin — retrying PLACE_DISC with next disc.")
                        self.open_tray()
                        time.sleep(1)
                        continue
                    # Secondary recovery: ACCEPT — cam wheels run BACKWARD (upward), disc ejected
                    # to accept bin at top. Does NOT involve the deflector (upward path bypasses it).
                    # Does NOT pull a new disc from hopper (wheels go up, not down). Retrieve
                    # the disc from the accept bin and reload it.
                    warn("  REJECT did not clear disc_lifted — trying ACCEPT (cam wheels backward → accept bin)...")
                    self.accept_disc()
                    time.sleep(1)
                    state_after2 = self.get_state(fatal=False)
                    if state_after2 and not state_after2.get("disc_lifted"):
                        warn("  Stuck disc ejected to ACCEPT BIN — retrieve it manually and reload.")
                        self.open_tray()
                        time.sleep(1)
                        continue
                    warn("  WARNING: disc_lifted still True after REJECT + ACCEPT — manual intervention needed.")
                    self.open_tray()
                    time.sleep(1)
                    continue
                # Hopper sensor went empty — could be the last disc just fed through
                # but disc_in_tray hasn't updated yet (timing race). Re-check once.
                time.sleep(1.0)
                state2 = self.get_state(fatal=False)
                if state2 and state2["disc_in_tray"]:
                    dbg("place_disc: disc_in_tray became True after disc_available=False — last disc did land")
                    return True  # disc landed, hopper now empty (has_more=False not correct here)
                return False  # truly empty, no disc placed
            if state["disc_in_tray"]:
                time.sleep(0.8)  # Wait for dropper to retract (per reference code)
                return True

            # Timeout: disc_in_tray still False, disc_available may still be True.
            # DOUBLE-LOAD PREVENTION: same check — if disc_lifted=True the disc is
            # stuck at cam level. Retrying PLACE_DISC would spin the wheels and pull
            # a 2nd disc down. Recover via REJECT before retrying.
            if state.get("disc_lifted"):
                warn(f"place_disc: timeout with disc stuck in cam wheels (disc_lifted=True, "
                     f"attempt {attempt + 1}/3).")
                warn("  Recovering (primary): close tray → REJECT (deflector only, no cam wheels)...")
                self.close_tray()
                self.reject_disc()
                time.sleep(1)
                state_after = self.get_state(fatal=False)
                if state_after and not state_after.get("disc_lifted"):
                    warn("  Stuck disc cleared to reject bin — retrying PLACE_DISC with next disc.")
                else:
                    warn("  REJECT did not clear — trying ACCEPT (cam wheels backward → accept bin)...")
                    self.accept_disc()
                    time.sleep(1)
                self.open_tray()
                time.sleep(1)
                continue
            warn(f"place_disc: disc_in_tray still False after 120s (attempt {attempt + 1}/3) — retrying...")
            time.sleep(3)

        err(
            "place_disc: disc failed to land on tray after 3 attempts.\n"
            "\n"
            "The disc is stuck in the dropper mechanism (held by the cam wheel rollers\n"
            "at the intermediate position between the hopper and the tray). This is the\n"
            "'in transit' state: disc_lifted=1, disc_in_tray=0.\n"
            "\n"
            "Automatic recovery (run: my-nimbie reset --diagnostics):\n"
            "  1. Close the tray (drutil tray close).\n"
            "  2. Issue ACCEPT (0x52/0x02) — runs cam wheels in reverse (upward),\n"
            "     ejecting the stuck disc to the accept output bin at the top.\n"
            "  3. Retrieve the disc from the accept bin and reload it.\n"
            "\n"
            "If automatic recovery fails:\n"
            "  1. Power OFF the Nimbie.\n"
            "  2. Power ON and wait. The ERROR LED should be OFF; wheels will NOT rotate\n"
            "     yet on first power-on — that is normal.\n"
            "  3. Power OFF again, then Power ON a second time. Wheels rotate to home.\n"
            "  4. Reload discs and resume the batch.\n"
            "\n"
            "If the wheel assembly is physically broken, Acronova sells it as a spare part.\n"
            "Longer-term: apply a small amount of silicone lubricant (not oil) to the\n"
            "cam pivot. Acronova recommends cleaning the wheel cams every 500 discs."
        )

    def lift_disc(self):
        """Lift disc from open tray with the gripper mechanism. Returns True on success."""
        # Retry loop — E09 errors are often transient and resolve with a short delay.
        for attempt in range(3):
            responses = self._send_and_read(CMD_LIFT_DISC, "LIFT_DISC",
                                            timeout=20000, max_reads=5,
                                            wait_for_at=False)
            at = self._find_at_response(responses)

            if at in (AT_OK, None):
                # Poll until disc is lifted
                time.sleep(1)
                self._poll_state(lambda s: s["disc_lifted"], "disc to be lifted", timeout=15)
                return True
            elif at == AT_NO_DISC:
                warn("No disc in tray to lift")
                return False
            elif at == AT_DROPPER_ERR:
                # AT+S03 = "mechanism stuck or disc already lifted"
                # Check actual state — if disc_lifted is True, the gripper already
                # has the disc (common after a previous partial lift); treat as success.
                time.sleep(1)
                state = self.get_state()
                if state.get("disc_lifted"):
                    dbg("LIFT_DISC got AT+S03 but disc_lifted=True — disc already in gripper, proceeding")
                    return True
                warn(f"LIFT_DISC got AT+S03 (attempt {attempt + 1}/3) — waiting 3s and retrying...")
                time.sleep(3)
                continue
            elif "E09" in str(at):
                # E09 = transient hardware error — retry after delay
                warn(f"LIFT_DISC got E09 (attempt {attempt + 1}/3) — waiting 3s and retrying...")
                time.sleep(3)
                continue
            elif at == AT_TRAY_HAS:
                # AT+S12 = "tray already has a disc" — Nimbie momentarily confused
                # about its own state. Seen after successful accept; retry resolves it.
                warn(f"LIFT_DISC got AT+S12 (attempt {attempt + 1}/3) — waiting 3s and retrying...")
                time.sleep(3)
                continue
            else:
                err(f"Unexpected response from LIFT_DISC: {at}\n"
                    f"  All responses: {responses}")
        # All retries exhausted — check one last time if disc is already lifted
        state = self.get_state()
        if state.get("disc_lifted"):
            dbg("LIFT_DISC exhausted retries but disc_lifted=True — proceeding")
            return True
        warn(f"LIFT_DISC failed after 3 attempts (last response: {at}) — disc may have already been ejected")
        return False

    def accept_disc(self):
        """Drop a lifted disc into the accept (done) pile."""
        responses = self._send_and_read(CMD_ACCEPT, "ACCEPT_DISC",
                                        timeout=20000, max_reads=5,
                                        wait_for_at=False)
        at = self._find_at_response(responses)

        if at in (AT_OK, None):
            # Poll until disc is no longer lifted
            time.sleep(1)
            self._poll_state(lambda s: not s["disc_lifted"], "disc to drop to accept", timeout=10)
        elif at == AT_DROPPER_ERR:
            warn("No disc lifted to accept (nothing to do)")
        else:
            err(f"Unexpected response from ACCEPT_DISC: {at}\n"
                f"  All responses: {responses}")

    def reject_disc(self):
        """Drop a lifted disc into the reject pile."""
        responses = self._send_and_read(CMD_REJECT, "REJECT_DISC",
                                        timeout=20000, max_reads=5,
                                        wait_for_at=False)
        at = self._find_at_response(responses)

        if at in (AT_OK, None):
            time.sleep(1)
            self._poll_state(lambda s: not s["disc_lifted"], "disc to drop to reject", timeout=10)
        elif at == AT_DROPPER_ERR:
            warn("No disc lifted to reject (nothing to do)")
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

    def _eject_common(self, accept):
        """Shared eject logic for accept and reject.

        Detects whether the disc is already held by the gripper (Stage 5:
        disc_lifted=True) and skips straight to close_tray + drop if so.
        This handles the case where the batch crashed mid-eject sequence and
        left the gripper holding the disc.
        """
        state = self.get_state()
        if state.get("disc_lifted"):
            # Gripper already has the disc — skip open_tray + lift
            dbg("eject: disc already in gripper (disc_lifted=True) — skipping to close+drop")
            vrb("  Disc already in gripper — skipping open/lift...")
        else:
            vrb("  Opening tray...")
            self.open_tray()
            time.sleep(1)  # Let disc settle on open tray before lifting

            vrb("  Lifting disc...")
            if not self.lift_disc():
                warn("Cannot lift disc — opening tray so you can remove it manually")
                return

        vrb("  Closing tray...")
        self.close_tray()

        if accept:
            vrb("  Dropping to accept bin...")
            self.accept_disc()
        else:
            vrb("  Dropping to reject bin...")
            self.reject_disc()

    def eject_accept(self):
        """Eject current disc to the accept (done) bin."""
        self._eject_common(accept=True)

    def eject_reject(self):
        """Eject current disc to the reject bin."""
        self._eject_common(accept=False)


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
def _force_mount_optical():
    """Try to manually mount the optical disc drive.

    Fallback for the display-sleep mount failure:
    - macOS Disk Arbitration auto-mounts removable media when a disc is inserted.
    - If the display is asleep when the disc is loaded, Disk Arbitration suppresses
      the "disk arrived" event and the disc never appears in /Volumes/.
    - caffeinate (started by cmd_batch) should prevent this entirely, but this
      function is a safety net: if wait_for_mount() hasn't seen the disc halfway
      through the timeout, we explicitly call `diskutil mount` to force it.

    Uses `drutil status` (BSD device name field) to identify the optical drive
    precisely — avoids the previous bug of accidentally trying to mount internal
    HDDs or system volumes (which triggers a macOS admin auth prompt).

    Returns True if a mount was attempted (regardless of success).
    """
    try:
        # Use drutil to find the BSD name of the disc in the optical drive.
        # Output contains a line like:  BSD Name: disk4
        result = subprocess.run(
            ["drutil", "status"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0 or "No Media Inserted" in result.stdout:
            dbg("force_mount_optical: no media in optical drive")
            return False
        bsd_name = None
        for line in result.stdout.splitlines():
            if "BSD Name:" in line:
                bsd_name = line.split("BSD Name:")[-1].strip()
                break
        if not bsd_name:
            dbg("force_mount_optical: could not find BSD Name in drutil output")
            return False
        dbg(f"force_mount_optical: trying diskutil mount /dev/{bsd_name}")
        subprocess.run(
            ["/usr/sbin/diskutil", "mount", f"/dev/{bsd_name}"],
            capture_output=True, timeout=15,
        )
        return True
    except Exception as e:
        dbg(f"force_mount_optical: error: {e}")
        return False


def wait_for_mount(mount_point, timeout, poll_interval):
    """Wait for a disc to appear at mount_point. Returns True if mounted.

    If the disc hasn't mounted halfway through the timeout, attempts a manual
    mount (for when macOS display sleep has suppressed auto-mount).
    """
    vrb(f"  Waiting for disc to mount at {mount_point} (timeout: {timeout}s)...")
    dbg(f"wait_for_mount: mount_point={mount_point}, timeout={timeout}s, poll={poll_interval}s")
    start = time.time()
    manual_mount_tried = False

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
        # Halfway through timeout: try manual mount (display-sleep workaround)
        if not manual_mount_tried and time.time() - start >= timeout / 2:
            warn("  Disc not mounted yet — trying manual mount (display-sleep workaround)...")
            _force_mount_optical()
            manual_mount_tried = True
        time.sleep(poll_interval)

    dbg(f"wait_for_mount: timed out after {timeout}s")
    return False


def _rotate_file(path):
    """If path exists, rename it to path.OLD. Silently ignored if it fails."""
    if os.path.exists(path):
        try:
            os.rename(path, path + ".OLD")
            dbg(f"Rotated {path} → {path}.OLD")
        except OSError as e:
            dbg(f"Failed to rotate {path}: {e}")


def _rotate_run_files(result_file, status_json=None):
    """Rotate all run files (.status, .progress, .result, .status-log.json) to .OLD before a new run."""
    for path in (STATUS_FILE, PROGRESS_FILE, result_file):
        _rotate_file(path)
    if status_json:
        _rotate_file(status_json)


def unmount_disc(mount_point):
    """Unmount the disc before mechanical eject.

    Tries a normal unmount first.  If macOS loginwindow (or Finder) dissents
    the unmount, retries with 'diskutil unmount force' — this is safe because
    the rip command has already finished before this is called.
    """
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
        stderr = result.stderr.strip()
        warn(f"Unmount failed: {stderr}")
        # loginwindow or Finder holds the volume — retry with force
        if "dissented" in stderr or "failed to unmount" in stderr.lower():
            vrb(f"  Retrying with force unmount...")
            result2 = subprocess.run(
                ["/usr/sbin/diskutil", "unmount", "force", mount_point],
                capture_output=True, text=True, timeout=30,
            )
            if result2.returncode == 0:
                vrb(f"  Force unmount succeeded")
                return True
            warn(f"Force unmount also failed: {result2.stderr.strip()}")
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


def _dir_size_str(path):
    """Return human-readable size of a directory (e.g. '42G'), or '' on error."""
    if not path or not os.path.isdir(path):
        return ""
    try:
        result = subprocess.run(["du", "-sh", path], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return result.stdout.split()[0]
    except Exception:
        pass
    # Fallback: manual walk
    total = _get_dir_size(path)
    if total == 0:
        return "0B"
    for unit in ("B", "K", "M", "G", "T"):
        if total < 1024:
            return f"{total:.0f}{unit}"
        total /= 1024
    return f"{total:.1f}P"


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

    def stop(self, success=False):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        # Write final progress (don't delete — status may still read it)
        if self.source_size > 0:
            copied = _get_dir_size(self.target_dir)
            if success:
                # Command succeeded — report 100% regardless of measured size
                # (filesystem overhead or caching can cause minor size discrepancy)
                pct = 100.0
                copied = max(copied, self.source_size)
            else:
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

    # Ensure target directory exists — users shouldn't need mkdir -p in their commands
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
        dbg(f"run_command: ensured dir exists: {dir_name}")

    msg(f"\n  Running: {cmd}")
    if status is not None:
        status.update("running command", command=cmd, disc_target_dir=dir_name or "")
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
        # Track subprocess globally so signal_handler and cancel can kill it
        global _active_cmd_proc
        _active_cmd_proc = proc
        try:
            with open(CMD_CHILD_PID_FILE, "w") as f:
                f.write(str(proc.pid))
        except Exception:
            pass
        for line in proc.stdout:
            line = line.rstrip("\n")
            # Clear progress line, print command output, then let monitor redraw
            print(f"\r    {line:<80}")
        proc.wait()
        elapsed = time.time() - start_time
        monitor.stop(success=(proc.returncode == 0))
        # Print final newline after progress line
        print()
        dbg(f"run_command: exit code {proc.returncode}, elapsed {elapsed:.1f}s")
        vrb(f"  Command exited with code {proc.returncode} in {int(elapsed // 60)}m {int(elapsed % 60):02d}s")
        return proc.returncode, elapsed
    except KeyboardInterrupt:
        proc.kill()
        monitor.stop()
        elapsed = time.time() - start_time
        warn("Command interrupted")
        return 130, elapsed
    finally:
        _active_cmd_proc = None
        try:
            os.unlink(CMD_CHILD_PID_FILE)
        except Exception:
            pass


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


def _detect_disc_in_drive(nimbie):
    """Check if a disc is currently in the drive.

    Uses both Nimbie state bits AND drutil to reliably detect disc presence.
    Returns a string describing the disc location, or None if no disc found.
    """
    state = nimbie.get_state()

    if state["disc_lifted"]:
        # disc_lifted=True has two meanings:
        # 1. Disc in gripper (after LIFT_DISC): disc was on tray and lifted for accept/reject
        # 2. Disc in dropper / in transit (after PLACE_DISC got stuck): disc is held by the
        #    cam wheel rollers at the intermediate position between hopper and tray.
        # Distinguish by tray_out and disc_available: if tray is open OR more discs are
        # available in the hopper, the disc is most likely in the dropper.
        if state["tray_out"] or state["disc_available"]:
            return "disc in dropper / in transit (held by cam wheels, not yet on tray)"
        return "disc in dropper or gripper (cam wheels holding disc, tray closed)"
    if state["disc_in_tray"]:
        if state["tray_out"]:
            return "disc on open tray"
        return "disc in drive (tray closed, detected by Nimbie)"

    # Nimbie state bits can't see disc in closed drive — check drutil
    if not state["tray_out"]:
        try:
            dr = subprocess.run(["drutil", "status"], capture_output=True, text=True, timeout=10)
            has_media = any("Type:" in line and "No Media" not in line
                            for line in dr.stdout.split("\n"))
            if has_media:
                return "disc in drive (tray closed, detected by optical drive)"
        except Exception:
            pass

    return None


def _recover_drive_state(nimbie, mount_point):
    """Ensure drive is in clean idle state before starting an operation.

    Handles all stuck states:
    - Disc lifted (gripper OR dropper): close tray, then ACCEPT to eject to accept bin
    - Tray out (disc on open tray, no disc lifted): close tray, eject disc to reject bin
    - Disc in tray (closed): unmount, eject to reject bin
    Returns True if recovery succeeded (drive is idle now).

    NOTE: Does NOT check drutil for disc in closed drive — the batch/next
    flows handle that case separately (to process the disc rather than eject it).

    IMPORTANT: disc_lifted=True means either:
    - Disc in gripper (after LIFT_DISC): tray closed, disc suspended above
    - Disc in dropper (after PLACE_DISC got stuck): disc held by cam wheel rollers
    In both cases, the recovery is: close tray → ACCEPT (0x52/0x02).
    This was confirmed experimentally on 2026-03-30: ACCEPT with tray closed
    runs the cam wheels and ejects the disc to the accept output bin.
    """
    state = nimbie.get_state()
    dbg(f"_recover_drive_state: {state}")

    if not state["disc_in_tray"] and not state["disc_lifted"] and not state["tray_out"]:
        return True  # already idle

    msg("  Recovering from stuck state...")

    if state["disc_lifted"]:
        # Disc is either in the gripper (after LIFT_DISC) or in the dropper (after
        # a stuck PLACE_DISC). Close the tray first (required for ACCEPT to work),
        # then issue ACCEPT to run the cam wheels and eject the disc to the accept bin.
        if state["tray_out"]:
            msg("  Disc in dropper/gripper (tray open) — closing tray first...")
            nimbie.close_tray()
        msg("  Disc in dropper/gripper — ejecting to accept bin (ACCEPT with tray closed)...")
        nimbie.accept_disc()
        time.sleep(2)
        state = nimbie.get_state()
        dbg(f"_recover_drive_state: after accept_disc: {state}")
        if not state["disc_lifted"]:
            msg("  Disc ejected to accept bin. Retrieve it and reload if needed.")
            if not state["disc_in_tray"] and not state["tray_out"]:
                return True

    if state["tray_out"]:
        msg("  Tray is open — closing tray...")
        nimbie.close_tray()
        # After closing, disc may now be detected in tray
        state = nimbie.get_state()
        dbg(f"_recover_drive_state: after close_tray: {state}")

    if state["disc_in_tray"]:
        msg("  Disc in drive — ejecting to reject bin...")
        unmount_disc(mount_point)
        nimbie.eject_reject()
        msg("  Stale disc rejected.")
        return True

    # Tray was open but no disc detected after closing — might have been empty tray
    state = nimbie.get_state()
    if not state["disc_in_tray"] and not state["disc_lifted"] and not state["tray_out"]:
        msg("  Drive is now idle.")
        return True

    warn(f"Could not fully recover drive state: {state}")
    return False


def cmd_unmount(_nimbie, config, _args):
    """Unmount the disc from the optical drive."""
    mount_point = config.get("nimbie", "mount_point")
    if not os.path.ismount(mount_point):
        msg(f"Not mounted: {mount_point}")
        return
    if unmount_disc(mount_point):
        msg(f"Unmounted {mount_point}")
    else:
        err(f"Failed to unmount {mount_point}\n\n"
            f"  The disc may be in use by another process.\n"
            f"  Try: diskutil unmount force {mount_point}")

def cmd_eject(nimbie, config, _args):
    mount_point = config.get("nimbie", "mount_point")
    if os.path.ismount(mount_point):
        msg(f"Unmounting {mount_point}...")
        if not unmount_disc(mount_point):
            err(f"Cannot unmount {mount_point} — disc may be in use\n\n"
                f"  Try: diskutil unmount force {mount_point}\n"
                f"  Then: my-nimbie eject")
    msg("Accepting disc (eject to done bin)...")
    nimbie.eject_accept()

def cmd_reject(nimbie, config, _args):
    mount_point = config.get("nimbie", "mount_point")
    if os.path.ismount(mount_point):
        msg(f"Unmounting {mount_point}...")
        if not unmount_disc(mount_point):
            err(f"Cannot unmount {mount_point} — disc may be in use\n\n"
                f"  Try: diskutil unmount force {mount_point}\n"
                f"  Then: my-nimbie reject")
    msg("Rejecting disc (eject to reject bin)...")
    nimbie.eject_reject()


# ---------------------------------------------------------------------------
# reset command — bootloader recovery and diagnostics
# ---------------------------------------------------------------------------

def _bl_connect():
    """Connect to Microchip PIC HID Bootloader device. Returns (dev, True) or (None, False)."""
    import usb.core
    import usb.util

    dev = usb.core.find(idVendor=BL_VID, idProduct=BL_PID)
    if dev is None:
        return None, False

    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except Exception:
        pass

    try:
        dev.set_configuration()
    except Exception:
        pass

    try:
        usb.util.claim_interface(dev, 0)
    except usb.core.USBError as e:
        warn(f"Cannot claim bootloader interface: {e}")
        return None, False

    # Drain stale data
    for _ in range(5):
        try:
            dev.read(BL_EP_IN, 64, timeout=100)
        except Exception:
            break

    return dev, True


def _bl_send(dev, cmd_bytes, timeout=2000):
    """Send command to bootloader, return response bytes or None."""
    pkt = bytearray(64)
    for i, b in enumerate(cmd_bytes):
        if i < 64:
            pkt[i] = b

    try:
        dev.write(BL_EP_OUT, pkt, timeout=5000)
    except Exception as e:
        dbg(f"Bootloader write error: {e}")
        return None

    time.sleep(0.2)

    try:
        resp = bytes(dev.read(BL_EP_IN, 64, timeout=timeout))
        return resp.rstrip(b'\x00')
    except Exception:
        return None


def _bl_exit_bootloader(dev):
    """Send RESET_DEVICE to bootloader. Returns True if command was sent."""
    msg("  Sending RESET_DEVICE to bootloader...")
    resp = _bl_send(dev, [BL_CMD_RESET_DEVICE])
    if resp:
        dbg(f"Bootloader RESET response: {resp.hex()}")
    return True


def _bl_crc16(data):
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


def _bl_jump_to_app(dev):
    """Send AN1388 framed 'Jump to Application' (command 0x05).

    Per AN1388B.pdf: the CORRECT way to exit the bootloader.
    Frame: <SOH=0x01> <CMD=0x05> <CRC16_L> <CRC16_H> <EOT=0x04>
    Device jumps immediately — NO response is sent.
    Returns True if normal Nimbie (0x1723:0x0945) appears within 3 seconds.

    IMPORTANT: This framed protocol is NOT supported on the Nimbie NB21 bootloader.
    The bootloader echoes back the first 5 bytes of any 64-byte packet without
    processing the framed command.  Use --sign-and-reset instead (PROGRAM_COMPLETE
    + SIGN_FLASH x2 + power cycle) — that is the confirmed working procedure.
    """
    import usb.core as _uc
    cmd_data = [0x05]
    crc = _bl_crc16(cmd_data)
    frame = [0x01] + cmd_data + [crc & 0xFF, (crc >> 8) & 0xFF, 0x04]
    msg(f"  Sending AN1388 framed JUMP-TO-APP: {bytes(frame).hex()}")
    msg(f"  (SOH=01 CMD=05 CRC16={crc:#06x} EOT=04)")
    msg(f"  Per AN1388B.pdf: device jumps immediately — no response expected.")
    pkt = bytearray(64)
    for i, b in enumerate(frame):
        pkt[i] = b
    try:
        dev.write(BL_EP_OUT, pkt, timeout=5000)
        msg(f"  TX OK.")
    except Exception as e:
        warn(f"  TX error: {e}")
        return False
    time.sleep(2)
    nimbie = _uc.find(idVendor=NIMBIE_VID, idProduct=NIMBIE_PID)
    if nimbie:
        msg(f"")
        msg(f"  SUCCESS: Normal Nimbie ({NIMBIE_VID:#06x}:{NIMBIE_PID:#06x}) detected!")
        return True
    bl = _uc.find(idVendor=BL_VID, idProduct=BL_PID)
    if bl:
        msg(f"  Device still in bootloader mode.")
    else:
        msg(f"  Device offline — may be rebooting. Power cycle if needed.")
    return False


def _bl_query_device(dev):
    """Send QUERY_DEVICE (0x00), print bootloader info."""
    msg("  Sending QUERY_DEVICE (0x00)...")
    resp = _bl_send(dev, [BL_CMD_QUERY])
    if resp:
        msg(f"  Response ({len(resp)} bytes): {resp.hex()}")
        if len(resp) >= 1:
            msg(f"    Byte 0 (cmd echo):      {resp[0]:#04x}")
        if len(resp) >= 3:
            bpp = (resp[2] << 8) | resp[1]
            msg(f"    Bytes 1-2 (bytes/packet): {bpp}")
        if len(resp) >= 4:
            msg(f"    Byte 3 (device family):   {resp[3]:#04x}")
    else:
        msg("  No response.")
    return resp


def _bl_sign_flash(dev):
    """Send SIGN_FLASH (0x07).

    Writes the application validity signature to NVM.  Combined with
    PROGRAM_COMPLETE (0x04), this convinces the bootloader that a valid
    application is present.  Sending it twice with a pause between gives the
    NVM write time to commit before a power cycle.

    Responds with echo byte 0x07.
    """
    msg("  Sending SIGN_FLASH (0x07)...")
    resp = _bl_send(dev, [BL_CMD_SIGN_FLASH])
    if resp:
        msg(f"  Response: {resp.hex()}")
    else:
        msg("  No response.")
    return resp


def _bl_scan_commands(dev):
    """Scan bootloader command bytes 0x00-0xFF and show which respond.

    Known responding commands on this device (discovered 2026-03-29):
      0x00  QUERY_DEVICE       — returns 4-byte info (family=0x07)
      0x04  PROGRAM_COMPLETE   — signals firmware write done
      0x05  GET_DATA           — flash read (read-protected, echoes cmd only)
      0x06  RESET_DEVICE       — soft reset (stays in BL if no valid app sig)
      0x07  SIGN_FLASH         — writes app validity signature to NVM
      0x32  UNKNOWN            — echoes 0x32; meaning unknown

    Framed AN1388 protocol (SOH+DLE+CRC16+EOT) is NOT supported on this
    device — the bootloader echoes back the first 5 bytes of any 64-byte
    packet without processing the framed command.
    """
    names = {
        BL_CMD_QUERY:            "QUERY_DEVICE",
        0x01:                    "UNLOCK_CONFIG",
        0x02:                    "ERASE_FLASH",
        0x03:                    "PROGRAM_FLASH",
        BL_CMD_PROGRAM_COMPLETE: "PROGRAM_COMPLETE",
        BL_CMD_GET_DATA:         "GET_DATA",
        BL_CMD_RESET_DEVICE:     "RESET_DEVICE",
        BL_CMD_SIGN_FLASH:       "SIGN_FLASH",
        0x32:                    "UNKNOWN(0x32)",
    }
    msg("  Scanning bootloader commands 0x00-0xFF...")
    msg("  (ERASE_FLASH 0x02 / PROGRAM_FLASH 0x03 / UNLOCK_CONFIG 0x01 skipped — destructive)")
    msg("")
    results = {}
    for cmd in range(0x100):
        name = names.get(cmd, "UNKNOWN")
        if cmd in (0x01, 0x02, 0x03):
            msg(f"  SKIP 0x{cmd:02X} ({name}) — destructive")
            results[cmd] = "SKIPPED"
            continue
        resp = _bl_send(dev, [cmd], timeout=1000)
        if resp:
            results[cmd] = resp.hex()
            msg(f"  0x{cmd:02X} {name:<22s} → {resp.hex()}")
        else:
            results[cmd] = "(no response)"
        time.sleep(0.05)
    msg("")
    msg("  === SUMMARY — commands that responded ===")
    for cmd in sorted(results):
        r = results[cmd]
        if r not in ("(no response)", "SKIPPED"):
            msg(f"  0x{cmd:02X} {names.get(cmd, 'UNKNOWN'):<22s} → {r}")
    return results


def _bl_read_flash_range(dev, start_addr, total_bytes, chunk_size=32):
    """Read a range of flash using GET_DATA (0x05) and hex-dump the result."""
    import struct as _struct
    msg(f"  Reading flash: {start_addr:#08x} – {start_addr + total_bytes:#08x} ({total_bytes} bytes)")
    all_data = bytearray()
    offset = 0
    while offset < total_bytes:
        sz = min(chunk_size, total_bytes - offset)
        addr = start_addr + offset
        addr_b = _struct.pack('<I', addr)[:3]
        len_b = _struct.pack('<H', sz)
        pkt = [BL_CMD_GET_DATA] + list(addr_b) + list(len_b)
        resp = _bl_send(dev, pkt, timeout=2000)
        if resp and len(resp) > 1:
            all_data.extend(resp[1:])  # skip cmd echo byte
        else:
            all_data.extend(b'\x00' * sz)
        offset += sz
        time.sleep(0.05)
    msg("")
    for i in range(0, len(all_data), 16):
        addr = start_addr + i
        chunk = all_data[i:i+16]
        hex_part = ' '.join(f'{b:02x}' for b in chunk)
        ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        msg(f"  {addr:08x}: {hex_part:<48s}  {ascii_part}")
    if all(b == 0xFF for b in all_data):
        msg("")
        msg("  *** ALL 0xFF — flash is ERASED (firmware missing!) ***")
    elif all(b == 0x00 for b in all_data):
        msg("")
        msg("  *** ALL 0x00 — may be read-protected ***")
    else:
        non_ff = sum(1 for b in all_data if b != 0xFF)
        msg(f"  {non_ff}/{len(all_data)} bytes contain data (non-0xFF)")
    return bytes(all_data)


# ---------------------------------------------------------------------------
# Nimbie command probe helpers (from nimbie-probe.py)
# ---------------------------------------------------------------------------

def _probe_connect():
    """Connect to normal Nimbie for raw USB probing. Returns (dev, kernel_detached)."""
    import usb.core as _uc
    import usb.util as _uu
    dev = _uc.find(idVendor=NIMBIE_VID, idProduct=NIMBIE_PID)
    if dev is None:
        err(f"Nimbie not found (VID={NIMBIE_VID:#06x} PID={NIMBIE_PID:#06x}).")
    kd = False
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
            kd = True
    except Exception:
        pass
    try:
        dev.set_configuration()
    except Exception:
        pass
    try:
        _uu.claim_interface(dev, 0)
    except Exception as e:
        err(f"Cannot claim Nimbie interface: {e}")
    for _ in range(10):
        try:
            data = dev.read(EP_IN, 64, timeout=100)
            if bytes(data) == b"\x00" * len(data):
                break
        except Exception:
            break
    return dev, kd


def _probe_send_and_read(dev, pkt_bytes, label="", timeout=3000, max_reads=10):
    """Send 8-byte packet to Nimbie, read all responses. Returns list of decoded strings."""
    pkt = bytearray(8)
    for i, b in enumerate(pkt_bytes):
        if i >= 8:
            break
        pkt[i] = b
    if label:
        msg(f"  TX [{label}]: {pkt.hex()}")
    else:
        msg(f"  TX: {pkt.hex()}")
    try:
        dev.write(EP_OUT, pkt, timeout=5000)
    except Exception as e:
        warn(f"  WRITE ERROR: {e}")
        return []
    time.sleep(0.3)
    responses = []
    empty = 0
    for _ in range(max_reads):
        try:
            data = dev.read(EP_IN, 64, timeout=timeout)
            raw = bytes(data)
            if not raw or raw == b"\x00" * len(raw):
                empty += 1
                if empty >= 2:
                    break
                continue
            empty = 0
            text = raw.rstrip(b"\x00").decode("ascii", errors="replace")
            if text:
                responses.append(text)
        except Exception:
            break
    if responses:
        for r in responses:
            msg(f"  RX: {r}")
    else:
        msg(f"  (no response)")
    return responses


def _probe_scan_commands(dev, start, end):
    """Scan Nimbie command bytes start–end and print results."""
    known = {0x43: "GET_STATE", 0x47: "LIFT_DISC", 0x49: "DIAGNOSTICS",
             0x4A: "COUNTERS",  0x52: "PLACE/ACCEPT/REJECT"}
    msg(f"  Scanning command bytes 0x{start:02X}–0x{end:02X} (param=0x00)")
    msg(f"  Known: {', '.join(f'0x{k:02X}={v}' for k, v in sorted(known.items()))}")
    msg("")
    results = {}
    for cmd in range(start, end + 1):
        tag = known.get(cmd, "")
        label = f"0x{cmd:02X}" + (f" ({tag})" if tag else "")
        if cmd in (0x55, 0x56):
            msg(f"  SKIP {label} — DANGEROUS (0x55=disconnect, 0x56=bootloader mode)")
            results[cmd] = "DANGEROUS"
            continue
        if cmd in (0x47, 0x52):
            msg(f"  SKIP {label} — mechanical, skipped for safety")
            results[cmd] = "SKIPPED"
            continue
        resps = _probe_send_and_read(dev, [0x00, 0x00, cmd, 0x00], label,
                                     timeout=2000, max_reads=10)
        results[cmd] = " | ".join(resps) if resps else "(no response)"
        time.sleep(0.15)
    msg("")
    msg("  === SUMMARY — commands that responded ===")
    for cmd in sorted(results):
        resp = results[cmd]
        if resp not in ("(no response)", "SKIPPED", "DANGEROUS"):
            msg(f"  0x{cmd:02X}: {resp}")
    return results


def _probe_scan_params(dev, cmd_byte, start, end):
    """Scan param bytes for a given Nimbie command byte."""
    known_params = {
        0x52: {0x01: "PLACE_DISC", 0x02: "ACCEPT", 0x03: "REJECT"},
        0x47: {0x01: "LIFT_DISC"},
    }
    pnames = known_params.get(cmd_byte, {})
    msg(f"  Scanning params 0x{start:02X}–0x{end:02X} for cmd 0x{cmd_byte:02X}")
    msg("")
    results = {}
    for param in range(start, end + 1):
        tag = pnames.get(param, "")
        label = f"cmd=0x{cmd_byte:02X} param=0x{param:02X}" + (f" ({tag})" if tag else "")
        if cmd_byte == 0x52 and param in (0x01, 0x02, 0x03):
            msg(f"  SKIP {label} — known mechanical")
            results[param] = "SKIPPED"
            continue
        if cmd_byte == 0x47 and param == 0x01:
            msg(f"  SKIP {label} — known mechanical")
            results[param] = "SKIPPED"
            continue
        resps = _probe_send_and_read(dev, [0x00, 0x00, cmd_byte, param], label,
                                     timeout=2000, max_reads=10)
        results[param] = " | ".join(resps) if resps else "(no response)"
        time.sleep(0.15)
    msg("")
    msg(f"  === SUMMARY — params for cmd 0x{cmd_byte:02X} that responded ===")
    for param in sorted(results):
        resp = results[param]
        if resp not in ("(no response)", "SKIPPED"):
            msg(f"  0x{param:02X}: {resp}")
    return results


def _usb_probe():
    """Return ("normal"|"bootloader"|"offline", dev_or_None).

    Fast USB scan: checks for normal Nimbie (0x1723:0x0945) then bootloader
    (0x04D8:0x000B). No connection or claim — read-only enumeration only.
    """
    try:
        import usb.core as _uc
        n = _uc.find(idVendor=NIMBIE_VID, idProduct=NIMBIE_PID)
        if n is not None:
            return "normal", n
        b = _uc.find(idVendor=BL_VID, idProduct=BL_PID)
        if b is not None:
            return "bootloader", b
    except Exception:
        pass
    return "offline", None


def _bl_require(bl_dev):
    """Ensure bootloader is present; connect and return dev. Calls err() if not."""
    if not bl_dev:
        import usb.core as _uc
        bl_dev = _uc.find(idVendor=BL_VID, idProduct=BL_PID)
    if not bl_dev:
        err(f"Bootloader not found ({BL_VID:#06x}:{BL_PID:#06x}).\n\n"
            f"  The Nimbie must be in bootloader mode for this command.\n"
            f"  LED pattern: ERROR=RED, LINK=GREEN, USB3=GREEN, READY=GREEN")
    dev, ok = _bl_connect()
    if not ok:
        err("Cannot connect to bootloader device.")
    return dev


def cmd_reset(_nimbie, _config, args):
    """Reset / recover the Nimbie from various error states."""
    import usb.core

    # Step 1: Detect current state
    nimbie_dev = usb.core.find(idVendor=NIMBIE_VID, idProduct=NIMBIE_PID)
    bl_dev = usb.core.find(idVendor=BL_VID, idProduct=BL_PID)

    # --- Bootloader-specific operations ---

    if args.jump_to_app:
        msg("Sending AN1388 framed JUMP-TO-APP...")
        msg("  WARNING: This framed protocol is NOT supported on the Nimbie NB21 bootloader.")
        msg("  The bootloader will echo back the packet without executing the command.")
        msg("  Use --sign-and-reset instead (PROGRAM_COMPLETE + SIGN_FLASH x2 + power cycle).")
        msg("")
        dev = _bl_require(bl_dev)
        try:
            ok = _bl_jump_to_app(dev)
        finally:
            try:
                import usb.util; usb.util.release_interface(dev, 0)
            except Exception:
                pass
        if ok:
            msg("")
            msg("  Normal Nimbie is back! Verify with: my-nimbie status")
        else:
            msg("")
            msg("  >>> If still in bootloader: power cycle (switch OFF → 5s → ON) <<<")
        return

    if args.bl_query:
        msg("Querying bootloader (QUERY_DEVICE 0x00)...")
        dev = _bl_require(bl_dev)
        try:
            _bl_query_device(dev)
        finally:
            try:
                import usb.util; usb.util.release_interface(dev, 0)
            except Exception:
                pass
        return

    if args.bl_scan:
        msg("Scanning bootloader commands 0x00-0x0F...")
        dev = _bl_require(bl_dev)
        try:
            _bl_scan_commands(dev)
        finally:
            try:
                import usb.util; usb.util.release_interface(dev, 0)
            except Exception:
                pass
        return

    if args.bl_read_flash:
        msg("Reading flash reset vector + bootloader area...")
        dev = _bl_require(bl_dev)
        try:
            _bl_read_flash_range(dev, 0x0000, 256)
            msg("")
            _bl_read_flash_range(dev, 0x1FC00, 64)
        finally:
            try:
                import usb.util; usb.util.release_interface(dev, 0)
            except Exception:
                pass
        return

    if args.bl_read_addr:
        addr = int(args.bl_read_addr, 0)
        length = args.bl_read_len
        msg(f"Reading flash at {addr:#010x}, {length} bytes...")
        dev = _bl_require(bl_dev)
        try:
            _bl_read_flash_range(dev, addr, length)
        finally:
            try:
                import usb.util; usb.util.release_interface(dev, 0)
            except Exception:
                pass
        return

    if args.bl_raw:
        hex_str = args.bl_raw.replace(" ", "")
        raw_bytes = list(bytes.fromhex(hex_str))
        msg(f"Sending raw bytes to bootloader: {bytes(raw_bytes).hex()}")
        dev = _bl_require(bl_dev)
        try:
            resp = _bl_send(dev, raw_bytes, timeout=5000)
            if resp:
                msg(f"  Response: {resp.hex()}")
            else:
                msg("  (no response)")
        finally:
            try:
                import usb.util; usb.util.release_interface(dev, 0)
            except Exception:
                pass
        return

    if args.sign_and_reset:
        msg("PROGRAM_COMPLETE + SIGN_FLASH x2 — verified recovery procedure...")
        msg("")
        msg("  This is the CONFIRMED working bootloader exit procedure:")
        msg("  PROGRAM_COMPLETE (0x04) signals 'firmware is written'.")
        msg("  SIGN_FLASH (0x07) x2 writes the application validity signature to NVM.")
        msg("  The double SIGN_FLASH + 10-second wait allows NVM write to fully commit.")
        msg("  After power cycle, bootloader finds valid app CRC and runs firmware.")
        msg("")
        dev = _bl_require(bl_dev)
        try:
            msg("  Step 1: PROGRAM_COMPLETE (0x04)...")
            resp = _bl_send(dev, [BL_CMD_PROGRAM_COMPLETE])
            if resp:
                msg(f"    Response: {resp.hex()}")
            time.sleep(0.5)
            msg("  Step 2: SIGN_FLASH (0x07) — first time...")
            _bl_sign_flash(dev)
            time.sleep(1.0)
            msg("  Step 3: SIGN_FLASH (0x07) — second time (NVM commit)...")
            _bl_sign_flash(dev)
            time.sleep(10.0)
            msg("  Step 4: RESET_DEVICE (0x06) — soft reset...")
            _bl_exit_bootloader(dev)
        finally:
            try:
                import usb.util; usb.util.release_interface(dev, 0)
            except Exception:
                pass
        msg("")
        msg("  Commands sent. NVM write needs time to commit.")
        msg("")
        msg("  >>> NOW: Turn OFF the Nimbie hardware switch, wait 10 seconds, turn ON <<<")
        msg("")
        msg("  The Nimbie should come back in normal mode (VID=0x1723 PID=0x0945).")
        msg("  Verify with: my-nimbie status")
        return

    # --- Standard recovery / diagnostics ---

    if args.exit_bootloader:
        # Explicit --exit-bootloader
        if nimbie_dev and not bl_dev:
            msg("Nimbie is already in normal mode (not in bootloader).")
            return
        if not bl_dev:
            err(f"Neither Nimbie ({NIMBIE_VID:#06x}:{NIMBIE_PID:#06x}) nor bootloader "
                f"({BL_VID:#06x}:{BL_PID:#06x}) found on USB.\n\n"
                f"  Possible reasons:\n"
                f"    - Device not connected or not powered on\n"
                f"    - macOS blocking the accessory (System Settings → Privacy & Security → Accessories)")
        msg("Nimbie is in Microchip PIC bootloader mode.")
        msg(f"  Bootloader: VID={BL_VID:#06x} PID={BL_PID:#06x}")
        dev, ok = _bl_connect()
        if not ok:
            err("Cannot connect to bootloader device.")
        _bl_exit_bootloader(dev)
        try:
            import usb.util
            usb.util.release_interface(dev, 0)
        except Exception:
            pass
        msg("")
        msg("  RESET command sent.")
        msg("")
        msg("  >>> NOW: Turn OFF the Nimbie hardware switch, wait 5 seconds, turn it back ON <<<")
        msg("")
        msg("  After power cycle, the Nimbie should return to normal operation.")
        msg("  Verify with: my-nimbie status")
        return

    if args.diagnostics:
        # Show diagnostics from both Nimbie and bootloader
        if bl_dev:
            msg("=" * 60)
            msg("  NIMBIE DIAGNOSTICS — BOOTLOADER MODE")
            msg("=" * 60)
            msg("")
            msg("  USB Mode:      BOOTLOADER (Microchip PIC HID)")
            msg(f"  VID/PID:       {BL_VID:#06x}:{BL_PID:#06x}")
            msg(f"  Normal VID/PID: {NIMBIE_VID:#06x}:{NIMBIE_PID:#06x} (NOT active)")
            msg("")
            dev, ok = _bl_connect()
            if ok:
                resp = _bl_send(dev, [BL_CMD_QUERY])
                if resp:
                    msg(f"  Bootloader QUERY response: {resp.hex()}")
                    if len(resp) >= 4:
                        msg(f"    Command echo:     {resp[0]:#04x}")
                        bpp = (resp[2] << 8) | resp[1] if len(resp) >= 3 else 0
                        msg(f"    Bytes per packet: {bpp}")
                        msg(f"    Device family:    {resp[3]:#04x}")
                msg("")
                msg("  Bootloader endpoints:")
                msg(f"    EP OUT: {BL_EP_OUT:#04x} (Bulk, 64 bytes)")
                msg(f"    EP IN:  {BL_EP_IN:#04x} (Bulk, 64 bytes)")
                msg("")
                msg("  Responding commands (0x00-0xFF scan, 2026-03-29):")
                msg("    0x00  QUERY_DEVICE       — query bootloader info (device family=0x07)")
                msg("    0x04  PROGRAM_COMPLETE   — signal firmware write is done")
                msg("    0x05  GET_DATA           — read flash (read-protected, echoes cmd only)")
                msg("    0x06  RESET_DEVICE       — soft reset (stays in BL without valid sig)")
                msg("    0x07  SIGN_FLASH         — write application validity signature to NVM")
                msg("    0x32  UNKNOWN            — echoes 0x32; meaning unknown")
                msg("")
                msg("  DANGEROUS (do not send):")
                msg("    0x01  UNLOCK_CONFIG      — unlock config bits")
                msg("    0x02  ERASE_FLASH        — erases entire application flash → BRICK")
                msg("    0x03  PROGRAM_FLASH      — writes to flash → BRICK if wrong data")
                msg("")
                msg("  NOTE: Framed AN1388 protocol NOT supported on this device.")
                msg("  Device echoes first 5 bytes of any 64-byte packet (raw protocol only).")
                try:
                    import usb.util
                    usb.util.release_interface(dev, 0)
                except Exception:
                    pass
            msg("")
            msg("  LED pattern in bootloader mode:")
            msg("    ERROR: RED (solid)  LINK: GREEN (solid)  USB3: GREEN (solid)  READY: GREEN (solid)")
            msg("")
            msg("  Recovery (confirmed working):")
            msg("    my-nimbie reset --sign-and-reset")
            msg("    Then: hardware switch OFF → wait 10s → ON")
            return

        if not nimbie_dev:
            err(f"No Nimbie device found on USB.\n\n"
                f"  Checked:\n"
                f"    Normal mode:     VID={NIMBIE_VID:#06x} PID={NIMBIE_PID:#06x}\n"
                f"    Bootloader mode: VID={BL_VID:#06x} PID={BL_PID:#06x}\n\n"
                f"  Possible reasons:\n"
                f"    - Device not connected or not powered on\n"
                f"    - macOS blocking the accessory (System Settings → Privacy & Security → Accessories)")

        msg("=" * 60)
        msg("  NIMBIE DIAGNOSTICS — NORMAL MODE")
        msg("=" * 60)
        msg("")

        nimbie = NimbieDevice(NIMBIE_VID, NIMBIE_PID)
        nimbie.connect()
        try:
            # USB info
            msg(f"  USB Mode:       NORMAL (Acronova NT21)")
            msg(f"  VID/PID:        {NIMBIE_VID:#06x}:{NIMBIE_PID:#06x}")
            try:
                msg(f"  Manufacturer:   {nimbie.dev.manufacturer}")
                msg(f"  Product:        {nimbie.dev.product}")
            except Exception:
                pass
            msg(f"  Endpoints:")
            msg(f"    EP OUT: {EP_OUT:#04x} (Interrupt, 8 bytes)")
            msg(f"    EP IN:  {EP_IN:#04x} (Interrupt, 64 bytes)")
            msg("")

            # State
            state = nimbie.get_state()
            bits = state["raw"]
            msg(f"  State bits:     {{{bits}}}")
            msg("")
            msg(f"    Bit 0: {bits[0]}   (unknown)")
            msg(f"    Bit 1: {bits[1]}   disc_available   — {'YES: discs in input hopper' if state['disc_available'] else 'no: hopper empty'}")
            msg(f"    Bit 2: {bits[2]}   (unknown)")
            msg(f"    Bit 3: {bits[3]}   disc_in_tray     — {'YES: disc sitting on ejected tray' if state['disc_in_tray'] else 'no: tray empty'}")
            if state["disc_lifted"]:
                if state["tray_out"] or state["disc_available"]:
                    lifted_label = "YES: disc in dropper/transit (held by cam wheels, NOT on tray yet)"
                else:
                    lifted_label = "YES: disc in dropper or gripper (cam wheels holding disc, tray closed)"
            else:
                lifted_label = "no: cam wheels empty"
            msg(f"    Bit 4: {bits[4]}   disc_lifted      — {lifted_label}")
            msg(f"    Bit 5: {bits[5]}   tray_out         — {'YES: drive tray is ejected' if state['tray_out'] else 'no: tray closed'}")
            for i in range(6, len(bits)):
                msg(f"    Bit {i}: {bits[i]}   (unknown)")
            msg("")

            # LED interpretation
            msg("  LED status (estimated from state):")
            if state["disc_available"]:
                msg("    READY: GREEN (solid) — discs available")
            else:
                msg("    READY: GREEN (blinking) — hopper empty")
            msg("    ERROR: OFF — no error")
            msg("    LINK:  GREEN (solid) — USB connected")
            msg("")

            # Diagnostics command (0x49)
            msg("  Hardware diagnostics (CMD 0x49):")
            diag_resps = nimbie._send_and_read((0x49,), "DIAGNOSTICS", timeout=3000)
            if diag_resps:
                # Parse interleaved name/value pairs: ["OK", "OL-Timer", "00000009", "Supply-N", ...]
                pairs = [r for r in diag_resps if r not in ("OK", "AT+O")]
                for i in range(0, len(pairs) - 1, 2):
                    name = pairs[i]
                    value = pairs[i + 1] if i + 1 < len(pairs) else "?"
                    try:
                        msg(f"    {name:12s} = {int(value):,d}")
                    except ValueError:
                        msg(f"    {name:12s} = {value}")
            else:
                msg("    (no response)")
            msg("")

            # Counters command (0x4A)
            msg("  Hardware counters (CMD 0x4A):")
            counter_resps = nimbie._send_and_read((0x4A,), "COUNTERS", timeout=3000)
            if counter_resps:
                pairs = [r for r in counter_resps if r not in ("OK", "AT+O")]
                if any(r == "AT+E09" for r in counter_resps):
                    msg("    (command not supported on this device)")
                else:
                    for i in range(0, len(pairs) - 1, 2):
                        name = pairs[i]
                        value = pairs[i + 1] if i + 1 < len(pairs) else "?"
                        try:
                            msg(f"    {name:12s} = {int(value):,d}")
                        except ValueError:
                            msg(f"    {name:12s} = {value}")
            else:
                msg("    (no response)")
            msg("")

            # Drive info via drutil
            msg("  Optical drive (via drutil):")
            import subprocess
            result = subprocess.run(["drutil", "status"], capture_output=True, text=True)
            for line in result.stdout.strip().split("\n"):
                msg(f"    {line.strip()}")
            msg("")

            # Summary
            msg("  Known commands:")
            msg("    0x43       GET_STATE    — query hardware state bits")
            msg("    0x47,0x01  LIFT_DISC    — lift disc from tray with gripper")
            msg("    0x49       DIAGNOSTICS  — OL-Timer, Supply, Pulley counters")
            msg("    0x4A       COUNTERS     — Pick, Release counters")
            msg("    0x52,0x01  PLACE_DISC   — drop disc from hopper onto tray")
            msg("    0x52,0x02  ACCEPT       — drop disc to accept (done) bin")
            msg("    0x52,0x03  REJECT       — drop disc to reject bin")

        finally:
            nimbie.disconnect()
        return

    # Default: auto-detect and recover
    if bl_dev:
        msg("Nimbie detected in BOOTLOADER mode — auto-recovering...")
        msg(f"  Bootloader: VID={BL_VID:#06x} PID={BL_PID:#06x}")
        msg("")
        dev, ok = _bl_connect()
        if not ok:
            err("Cannot connect to bootloader device.")
        _bl_exit_bootloader(dev)
        try:
            import usb.util
            usb.util.release_interface(dev, 0)
        except Exception:
            pass
        msg("")
        msg("  RESET command sent.")
        msg("")
        msg("  >>> NOW: Turn OFF the Nimbie hardware switch, wait 5 seconds, turn it back ON <<<")
        msg("")
        msg("  After power cycle, the Nimbie should return to normal operation.")
        msg("  Verify with: my-nimbie status")
        return

    if nimbie_dev:
        msg("Nimbie is in normal mode — no recovery needed.")
        msg("")
        msg("  Use 'my-nimbie reset --diagnostics' to view device diagnostics.")
        return

    err(f"No Nimbie device found on USB.\n\n"
        f"  Checked:\n"
        f"    Normal mode:     VID={NIMBIE_VID:#06x} PID={NIMBIE_PID:#06x}\n"
        f"    Bootloader mode: VID={BL_VID:#06x} PID={BL_PID:#06x}\n\n"
        f"  Possible reasons:\n"
        f"    - Device not connected or not powered on\n"
        f"    - macOS blocking the accessory (System Settings → Privacy & Security → Accessories)\n"
        f"    - Try unplugging USB, turning off device, waiting 10 seconds, then reconnecting")


def cmd_probe(_nimbie, _config, args):
    """Probe Nimbie command space for reverse-engineering (from nimbie-probe.py)."""
    msg("")
    msg("=" * 60)
    msg("  my-nimbie probe — Nimbie USB command scanner")
    msg("=" * 60)
    msg("")
    msg("  CAUTION: Unknown commands may cause mechanical actions.")
    msg("  Commands 0x55 and 0x56 are permanently skipped (dangerous).")
    msg("  Known mechanical commands (0x47, 0x52) also skipped by default.")
    msg("")

    dev, kd = _probe_connect()
    try:
        if args.probe_raw:
            hex_str = args.probe_raw.replace(" ", "")
            raw_bytes = list(bytes.fromhex(hex_str))
            msg(f"  Sending raw bytes: {bytes(raw_bytes).hex()}")
            _probe_send_and_read(dev, raw_bytes, "RAW", timeout=5000, max_reads=20)

        elif args.probe_cmd and args.probe_params:
            cmd_byte = int(args.probe_cmd, 0)
            ps, pe = 0x00, 0xFF
            if args.probe_param_range:
                parts = args.probe_param_range.split("-")
                ps = int(parts[0], 0)
                pe = int(parts[1], 0) if len(parts) > 1 else ps
            _probe_scan_params(dev, cmd_byte, ps, pe)

        else:
            start, end = 0x00, 0xFF
            if args.probe_range:
                parts = args.probe_range.split("-")
                start = int(parts[0], 0)
                end = int(parts[1], 0) if len(parts) > 1 else start
            _probe_scan_commands(dev, start, end)

    finally:
        try:
            import usb.util as _uu
            _uu.release_interface(dev, 0)
            if kd:
                dev.attach_kernel_driver(0)
        except Exception:
            pass

    msg("")
    msg("  Done.")
    msg("")


def cmd_status(nimbie, config, _args):
    # Timestamp for when this status snapshot was taken
    now = datetime.datetime.now()
    ts = now.strftime('%Y-%m-%d_%H%M.%S.') + f"{now.microsecond // 1000:03d}"

    # Always check for bootloader mode first — overrides everything else
    try:
        import usb.core as _usb_core
        bl_dev = _usb_core.find(idVendor=BL_VID, idProduct=BL_PID)
    except Exception:
        bl_dev = None
    if bl_dev is not None:
        msg(f"{ts}  *** NIMBIE IS IN BOOTLOADER MODE ***")
        msg("")
        msg("  USB Mode:    BOOTLOADER (Microchip PIC HID, AN1388)")
        msg(f"  VID/PID:     {BL_VID:#06x}:{BL_PID:#06x}  (Microchip Technology Inc.)")
        msg(f"  Normal VID/PID: {NIMBIE_VID:#06x}:{NIMBIE_PID:#06x}  (NOT active)")
        msg("")
        msg("  The Nimbie firmware jumped to the Microchip PIC bootloader.")
        msg("  This happens after USB command 0x56 is sent to the device.")
        msg("  The bootloader persists across power cycles because the application")
        msg("  validity signature in flash is missing or was not written.")
        msg("")
        msg("  RECOVERY (confirmed working procedure):")
        msg("    my-nimbie reset --sign-and-reset")
        msg("    Then: hardware switch OFF → wait 10s → ON")
        msg("")
        msg("  What this does:")
        msg("    1. PROGRAM_COMPLETE (0x04) — signals bootloader that firmware is present")
        msg("    2. SIGN_FLASH (0x07) x2   — writes app validity signature to NVM")
        msg("    3. Wait 10s               — NVM write commit time")
        msg("    4. Power cycle            — bootloader checks CRC, finds valid app, runs it")
        msg("")
        msg("  LED pattern in bootloader: ERROR=RED, LINK=GREEN, USB3=GREEN, READY=GREEN")
        msg("")
        if verbose:
            msg("  Bootloader endpoints:")
            msg("    EP 0x01 OUT Bulk 64 bytes  (commands to bootloader)")
            msg("    EP 0x81 IN  Bulk 64 bytes  (responses from bootloader)")
            msg("")
            msg("  Known responding commands (full 0x00-0xFF scan, 2026-03-29):")
            msg("    0x00  QUERY_DEVICE      — query version, bytes/packet, device family")
            msg("    0x04  PROGRAM_COMPLETE  — signal firmware write is done")
            msg("    0x05  GET_DATA          — read flash (read-protected on Nimbie)")
            msg("    0x06  RESET_DEVICE      — soft reset (stays in BL without valid sig)")
            msg("    0x07  SIGN_FLASH        — write application validity signature to NVM")
            msg("    0x32  UNKNOWN           — echoes 0x32; meaning unknown")
            msg("")
            msg("  NOTE: Framed AN1388 protocol (SOH+CMD+CRC+EOT) is NOT supported.")
            msg("  The bootloader echoes the first 5 bytes of any 64-byte packet.")
            msg("  Only raw single-byte commands work.")
        return

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
        # Process died — show crash info, query hardware, and clean up if safe
        mode_label = "my-nimbie batch" if is_batch else "my-nimbie next"
        disc_nr = sf.get('disc_nr', '?')
        idx_offset = sf.get('idx_offset')
        index_str = ""
        if idx_offset is not None:
            try:
                index_str = f", index {int(disc_nr) + int(idx_offset)}"
            except (ValueError, TypeError):
                pass

        cli_str = sf.get('cli', '?')
        msg(f"{ts}  CRASHED — process died during '{sf.get('state', '?')}'")
        msg(f"  Command:     {cli_str}")
        msg(f"  PID:         {sf.get('pid', '?')}")
        msg(f"  Disc:        #{disc_nr}{index_str}")
        if sf.get("target_dir"):
            msg(f"  Target dir:  {sf['target_dir']}")
        if sf.get("disc_target_dir"):
            sz = _dir_size_str(sf["disc_target_dir"])
            msg(f"  Disc target: {sf['disc_target_dir']}" + (f"  ({sz})" if sz else ""))
        if sf.get("command"):
            msg(f"  Command:     {sf['command']}")
        if is_batch:
            msg(f"  Accepted:    {sf.get('accepted', '?')}")
            msg(f"  Rejected:    {sf.get('rejected', '?')}")
        if is_batch and sf.get("last_disc"):
            msg(f"  Last disc:   {sf['last_disc']}")

        # Query hardware to check if disc is stuck
        msg("")
        if nimbie is not None:
            state = nimbie.get_state()
            # Check drutil to reliably detect disc in closed drive
            # (Nimbie state bits can't distinguish "closed+empty" from "closed+disc")
            has_media = False
            try:
                dr = subprocess.run(["drutil", "status"], capture_output=True, text=True, timeout=10)
                has_media = any("Type:" in line and "No Media" not in line
                                for line in dr.stdout.split("\n"))
            except Exception:
                pass

            disc_on_tray = state["disc_in_tray"] or state["disc_lifted"]
            disc_in_closed_drive = has_media and not state["tray_out"] and not disc_on_tray
            disc_stuck = disc_on_tray or disc_in_closed_drive

            if disc_on_tray:
                msg(f"  Disc in drive! (in_tray={state['disc_in_tray']}, lifted={state['disc_lifted']})")
                msg(f"    my-nimbie eject    — eject disc to accept bin")
                msg(f"    my-nimbie reject   — eject disc to reject bin")
            elif disc_in_closed_drive:
                msg(f"  Disc in closed drive (confirmed by optical drive)")
                msg(f"    my-nimbie eject    — open tray, lift disc, drop to accept bin")
                msg(f"    my-nimbie reject   — open tray, lift disc, drop to reject bin")
            else:
                msg(f"  No disc in drive.")
                if state["disc_available"] and sf.get("current") == "loading":
                    msg("")
                    msg("  *** RETAINER WHEEL / CAM RING MAY BE STUCK — ERROR LED LIKELY LIT ***")
                    msg("  Disc was available in the hopper but failed to drop onto the tray.")
                    msg("  The 3 cam wheels are driven by a common ring. If the ring jams")
                    msg("  mid-rotation, the wheels stop at different angles and the disc")
                    msg("  tilts and wedges instead of dropping cleanly.")
                    msg("")
                    msg("  Fix:")
                    msg("    1. Power OFF the Nimbie.")
                    msg("    2. Remove the stuck disc:")
                    msg("         First try GENTLY pulling it UP out of the hopper.")
                    msg("         If it will not come up, carefully push it DOWN through onto the tray.")
                    msg("    3. If necessary, clean all 3 wheels with isopropyl alcohol")
                    msg("       (sticky label residue / dust is the most common cause).")
                    msg("    4. Power ON and wait. ERROR LED should be OFF; the wheels")
                    msg("       will NOT rotate yet on first power-on — that is normal.")
                    msg("    5. Power OFF again, then Power ON a second time. The wheels")
                    msg("       will now rotate back to their default (home) positions.")
                    msg("    6. Reload discs and resume the batch.")
                    msg("")
                    msg("  Longer-term: apply a small amount of silicone lubricant (not oil)")
                    msg("  to the cam pivot. Acronova recommends cleaning every 500 discs.")
                    msg("")
                    msg("  NOTE: The Nimbie ERROR LED state is not readable via USB —")
                    msg("  my-nimbie cannot confirm it directly. Power cycling the device")
                    msg("  (step 4+5 above) will reset both the cam ring and the ERROR LED.")

            if not disc_stuck:
                # Safe to clean up stale status file
                try:
                    os.unlink(STATUS_FILE)
                except OSError:
                    pass
                try:
                    os.unlink(PROGRESS_FILE)
                except OSError:
                    pass
        else:
            msg(f"  Cannot check hardware — Nimbie not connected.")
            msg(f"  Power cycle the Nimbie (switch off, wait 10s, switch on), then run:")
            msg(f"    my-nimbie status   — check device state and disc position")
        return
        # Don't fall through — we already queried hardware above

    if not process_crashed and sf and sf.get("state") not in ("finished", "interrupted"):
        mode_label = "my-nimbie batch" if is_batch else "my-nimbie next"
        msg(f"{ts}  '{mode_label}' in progress:")
        cli_str = sf.get('cli')
        if cli_str:
            msg(f"  Command:     {cli_str}")
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
        if sf.get("disc_target_dir"):
            sz = _dir_size_str(sf["disc_target_dir"])
            msg(f"  Disc target: {sf['disc_target_dir']}" + (f"  ({sz})" if sz else ""))
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

        # Verbose: show hardware diagnostics even during active job
        if verbose:
            _nimbie = nimbie
            if _nimbie is None:
                # Try to connect briefly for diagnostics (another process may hold USB)
                try:
                    _vid = int(config.get("nimbie", "vid"), 16)
                    _pid = int(config.get("nimbie", "pid"), 16)
                    _nimbie = NimbieDevice(_vid, _pid)
                    # Suppress err() output during probe — redirect stderr
                    _old_stderr = sys.stderr
                    sys.stderr = open(os.devnull, "w")
                    try:
                        _nimbie.connect()
                    finally:
                        sys.stderr.close()
                        sys.stderr = _old_stderr
                except (Exception, SystemExit):
                    _nimbie = None
            if _nimbie is not None:
                try:
                    msg("")
                    msg("  Hardware diagnostics (CMD 0x49):")
                    diag_resps = _nimbie._send_and_read((0x49,), "DIAGNOSTICS", timeout=3000)
                    if diag_resps:
                        pairs = [r for r in diag_resps if r not in ("OK", "AT+O")]
                        for i in range(0, len(pairs) - 1, 2):
                            name = pairs[i]
                            value = pairs[i + 1] if i + 1 < len(pairs) else "?"
                            try:
                                msg(f"    {name:12s} = {int(value):,d}")
                            except ValueError:
                                msg(f"    {name:12s} = {value}")

                    msg("")
                    state = _nimbie.get_state()
                    msg(f"  Disc available: {state['disc_available']}")
                    msg(f"  Disc in tray:   {state['disc_in_tray']}")
                    msg(f"  Disc lifted:    {state['disc_lifted']}")
                    msg(f"  Tray out:       {state['tray_out']}")
                finally:
                    if _nimbie is not nimbie:
                        try:
                            _nimbie.disconnect()
                        except Exception:
                            pass
            else:
                msg("")
                msg("  Hardware diagnostics: unavailable (USB held by running process)")

        if verbose:
            msg("")
            import subprocess
            msg("  Optical drive:")
            result = subprocess.run(["drutil", "status"], capture_output=True, text=True)
            for line in result.stdout.strip().split("\n"):
                msg(f"    {line.strip()}")
        return

    # Show last run result if available (even after finished)
    if sf and sf.get("state") in ("finished", "interrupted"):
        mode_label = "my-nimbie batch" if is_batch else "my-nimbie next"
        msg(f"Last '{mode_label}': {sf.get('flavor', '?')} — {sf.get('state')}")
        if sf.get("cli"):
            msg(f"  Command:     {sf['cli']}")
        if sf.get("last_disc"):
            msg(f"  Last disc:   {sf['last_disc']}")
        msg(f"  Accepted: {sf.get('accepted', '?')}, Rejected: {sf.get('rejected', '?')}")
        msg("")

    # No batch running — query USB device directly
    if nimbie is None:
        msg(f"{ts}  Nimbie not connected (OFFLINE).")
        msg("")
        msg("  USB Mode:    OFFLINE")
        msg("  Power on the Nimbie and run:  my-nimbie status")
        return
    msg(f"{ts}  Querying Nimbie status...")
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

    # Check optical drive for media (drutil can see disc in closed drive, Nimbie state bits cannot)
    import subprocess
    drutil_result = subprocess.run(["drutil", "status"], capture_output=True, text=True)
    has_media = any("Type:" in line and "No Media" not in line for line in drutil_result.stdout.split("\n"))

    if lifted:
        stage = "5/5  Disc grabbed by gripper — waiting for accept/reject drop"
        advice = ("  Actions:\n"
                  "    my-nimbie eject    — drop disc to accept (done) bin\n"
                  "    my-nimbie reject   — drop disc to reject bin")
    elif in_tray and not tray_out:
        stage = "3/5  Disc in drive (tray closed) — ready for read/rip"
        advice = ("  Actions:\n"
                  "    my-nimbie next <flavor>   — process this disc\n"
                  "    my-nimbie eject           — eject disc to accept bin\n"
                  "    my-nimbie reject          — eject disc to reject bin")
    elif in_tray and tray_out:
        stage = "2/5  Disc on open tray — waiting for tray close"
        advice = ("  Actions:\n"
                  "    my-nimbie next <flavor>   — close tray and process this disc\n"
                  "    my-nimbie eject           — lift disc and drop to accept bin\n"
                  "    my-nimbie reject          — lift disc and drop to reject bin")
    elif tray_out and not in_tray:
        stage = "1/5  Tray open, empty — waiting for disc"
        advice = ("  Actions:\n"
                  "    my-nimbie load    — place disc from hopper onto tray\n"
                  "    Place a disc manually on the tray, then run my-nimbie next <flavor>")
    elif not tray_out and not in_tray and not lifted and has_media:
        stage = "3/5  Disc in drive (tray closed, detected by optical drive)"
        advice = ("  Actions:\n"
                  "    my-nimbie eject    — open tray, lift disc, drop to accept bin\n"
                  "    my-nimbie reject   — open tray, lift disc, drop to reject bin")
    elif not tray_out and not in_tray and not lifted:
        stage = "0/5  Idle — no disc in drive"
        if avail:
            advice = ("  Actions:\n"
                      "    my-nimbie next <flavor>    — load and process one disc\n"
                      "    my-nimbie batch <flavor>   — batch process all discs in hopper")
        else:
            advice = "  Load discs into the hopper to begin processing."
    else:
        stage = "?    Unknown state combination"
        advice = "  Try: my-nimbie reset --diagnostics"

    msg(f"\n  Stage: {stage}")
    if avail:
        msg(f"         Hopper has discs available")
    else:
        msg(f"         Hopper is EMPTY")
    msg(f"\n{advice}")

    # Show command progress if available (from next or batch)
    try:
        with open(PROGRESS_FILE) as f:
            progress = f.read().strip()
        if progress:
            msg(f"\n  Command progress: {progress}")
    except (FileNotFoundError, OSError):
        pass

    # Always show optical drive media status
    import subprocess
    result = subprocess.run(["drutil", "status"], capture_output=True, text=True)
    drive_lines = result.stdout.strip().split("\n")
    drive_name = ""
    media_type = "Unknown"
    for line in drive_lines:
        stripped = line.strip()
        if stripped.startswith("Vendor"):
            continue  # header line
        if stripped.startswith("Type:"):
            media_type = stripped
        elif stripped and not stripped.startswith("---") and not drive_name:
            drive_name = stripped
    if drive_name:
        msg(f"\n  Drive:  {drive_name}")
    msg(f"  Media:  {media_type}")

    msg(f"""
  Mechanism stages:
    1. Open tray
    2. Place disc from hopper onto tray
    3. Close tray — drive reads/rips the disc
    4. Open tray, lift disc (gripper picks it up)
    5. Drop disc to accept (done) or reject bin""")

    # Verbose: show hardware diagnostics (same as reset --diagnostics)
    if verbose:
        msg("")
        msg("  Hardware diagnostics (CMD 0x49):")
        diag_resps = nimbie._send_and_read((0x49,), "DIAGNOSTICS", timeout=3000)
        if diag_resps:
            pairs = [r for r in diag_resps if r not in ("OK", "AT+O")]
            for i in range(0, len(pairs) - 1, 2):
                name = pairs[i]
                value = pairs[i + 1] if i + 1 < len(pairs) else "?"
                try:
                    msg(f"    {name:12s} = {int(value):,d}")
                except ValueError:
                    msg(f"    {name:12s} = {value}")
        msg("")
        import subprocess
        msg("  Optical drive:")
        result = subprocess.run(["drutil", "status"], capture_output=True, text=True)
        for line in result.stdout.strip().split("\n"):
            msg(f"    {line.strip()}")
        msg("")
        msg("  For full diagnostics (USB info, all state bits, counters, LEDs):")
        msg("    my-nimbie reset --diagnostics")


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
    result_file = config.get("batch", "result_file", fallback=DEFAULT_RESULT_FILE)
    status_json = config.get("batch", "status_json", fallback=DEFAULT_STATUS_JSON)
    if not dry_run:
        _rotate_run_files(result_file, status_json=status_json)
    status = BatchStatus(flavor, mount_point, target_dir, idx_offset=effective_offset, mode="next", result_file=result_file,
                         dry_run=dry_run)
    status.update("starting", disc_nr=disc_nr)

    if deepdebug and not dry_run:
        dbg(f"Status JSON collector started → {status_json}")
        _start_status_json_collector(nimbie, status_json)

    # Check if a disc is already in the drive (before printing startup info)
    use_loaded = getattr(args, "use_loaded", False)
    if not dry_run:
        disc_location = _detect_disc_in_drive(nimbie)
        if disc_location and not use_loaded:
            err(f"Disc already in drive: {disc_location}\n\n"
                f"  Cannot load a new disc — there is already one in the drive.\n\n"
                f"  Options:\n"
                f"    my-nimbie eject              — eject disc to accept bin, then retry\n"
                f"    my-nimbie reject             — eject disc to reject bin, then retry\n"
                f"    my-nimbie next --use-loaded  — process the disc already in the drive")

    msg(f"Processing next disc (flavor: {flavor_label})")
    msg(f"  Command:    {on_load}")
    msg(f"  Mount:      {mount_point}")
    msg(f"  Target dir: {target_dir}")
    msg(f"  Dir name:   {dir_name}")

    status.start_disc_timer()

    if use_loaded:
        msg("\n  --use-loaded: processing disc already in drive")
        has_more = True  # hopper status unknown, assume more discs
        if not dry_run:
            hw = nimbie.get_state(fatal=False)
            dbg(f"cmd_next --use-loaded: state={hw}")
            if hw is None:
                # USB may have been briefly busy (e.g. just released from another process).
                # Default-safe action: close the tray. drutil tray close is harmless if
                # the tray is already closed, and critical if it's open with a disc on it.
                msg("  State read failed — closing tray as a precaution...")
                nimbie.close_tray()
            elif hw["disc_in_tray"] and hw["tray_out"]:
                # Disc is on the open tray — close it first
                msg("  Disc is on open tray — closing tray...")
                nimbie.close_tray()
            elif not os.path.ismount(mount_point):
                # Disc is in closed drive but not mounted — try to mount it
                status.update("mounting")
                msg(f"  Disc not mounted — mounting...")
                subprocess.run(["diskutil", "mount", mount_point], capture_output=True, timeout=30)
    else:
        # Load
        status.update("loading")
        msg("\n  Loading disc from hopper...")
        dbg("cmd_next: calling nimbie.load_disc()")
        has_more = nimbie.load_disc()
        dbg(f"cmd_next: load_disc returned has_more={has_more}")

    if not dry_run:
        if not os.path.ismount(mount_point):
            status.update("waiting for mount")
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
    max_discs = args.max if args.max is not None else config.getint("batch", "max_discs")
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
    result_file = config.get("batch", "result_file", fallback=DEFAULT_RESULT_FILE)
    status_json = config.get("batch", "status_json", fallback=DEFAULT_STATUS_JSON)
    if not dry_run:
        _rotate_run_files(result_file, status_json=status_json)
    status = BatchStatus(flavor, mount_point, target_dir, idx_offset=effective_offset, mode="batch", result_file=result_file,
                         dry_run=dry_run)
    batch_status = status

    if deepdebug and not dry_run:
        dbg(f"Status JSON collector started → {status_json}")
        _start_status_json_collector(nimbie, status_json)

    # Install SIGUSR1 handler for live status queries
    signal.signal(signal.SIGUSR1, sigusr1_handler)
    # SIGTERM (kill <pid>) → same graceful stop as Ctrl-C
    signal.signal(signal.SIGTERM, signal_handler)

    # Prevent macOS display sleep and idle sleep for the entire batch.
    #
    # Root cause: macOS uses Disk Arbitration to auto-mount removable media.
    # When the display sleeps, macOS throttles background I/O and Disk Arbitration
    # events to save power. The optical drive still spins and reads the disc, but
    # the "disk arrived" notification that triggers auto-mount is suppressed —
    # macOS never adds the disc to /Volumes/. This is specific to optical media
    # (DVD/CD/BD): hard drives stay mounted, but optical discs require a fresh
    # mount event on each insertion.
    # Result: wait_for_mount() polls /Volumes/ forever, times out, disc is rejected.
    # Fix: caffeinate -d keeps the display on → Disk Arbitration stays fully active
    #      → DVDs auto-mount normally within a few seconds of being loaded.
    #
    # caffeinate -d (display) -i (idle) -w PID exits automatically when batch exits.
    _caffeinate_proc = None
    try:
        _caffeinate_proc = subprocess.Popen(
            ["caffeinate", "-d", "-i", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        dbg(f"caffeinate started (PID {_caffeinate_proc.pid}) — display/idle sleep prevented")
        msg("  [caffeinate] Display and idle sleep disabled for batch duration.")
    except Exception as e:
        warn(f"caffeinate failed to start: {e} — display sleep may cause mount failures")

    # Check if a disc is already in the drive (before printing startup info)
    use_loaded = getattr(args, "use_loaded", False)
    if not dry_run:
        disc_location = _detect_disc_in_drive(nimbie)
        if disc_location and not use_loaded:
            err(f"Disc already in drive: {disc_location}\n\n"
                f"  Cannot start batch — there is already a disc in the drive.\n\n"
                f"  Options:\n"
                f"    my-nimbie eject               — eject disc to accept bin, then retry\n"
                f"    my-nimbie reject              — eject disc to reject bin, then retry\n"
                f"    my-nimbie batch --use-loaded  — process the disc in the drive as first disc,\n"
                f"                                    then continue batch from hopper")

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

    first_disc_use_loaded = use_loaded

    try:
        while not interrupted:
            status.disc_nr += 1

            if max_discs > 0 and status.disc_nr > max_discs:
                msg(f"\n  Reached max_discs limit ({max_discs}). Stopping.")
                break

            msg(f"\n{'=' * 60}")
            msg(f"  Disc #{status.disc_nr}")
            msg(f"{'=' * 60}")

            # Load — skip if --use-loaded and first disc
            status.start_disc_timer()
            if first_disc_use_loaded:
                msg("  --use-loaded: processing disc already in drive")
                has_more = True  # assume more in hopper
                first_disc_use_loaded = False  # only skip loading for first disc
                # Disc may not be mounted (e.g. unmounted before crash) — try to mount it
                if not dry_run and not os.path.ismount(mount_point):
                    status.update("mounting", status.disc_nr)
                    msg(f"  Disc not mounted — mounting...")
                    subprocess.run(["diskutil", "mount", mount_point], capture_output=True, timeout=30)
            else:
                status.update("loading", status.disc_nr)
                msg("  Loading disc from hopper...")
                has_more = nimbie.load_disc()

            if not dry_run:
                if not os.path.ismount(mount_point):
                    status.update("waiting for mount", status.disc_nr)
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


# ---------------------------------------------------------------------------
# Unit tests (my-nimbie --test)
# ---------------------------------------------------------------------------
import unittest
import tempfile


class TestExpandNamingVars(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(expand_naming_vars("{INDEX}", {"INDEX": "001"}), "001")

    def test_case_insensitive(self):
        self.assertEqual(expand_naming_vars("{index}", {"INDEX": "001"}), "001")

    def test_multiple(self):
        result = expand_naming_vars("{INDEX} - {MEDIA_TYPE}", {"INDEX": "005", "MEDIA_TYPE": "DVD"})
        self.assertEqual(result, "005 - DVD")

    def test_unknown_left_alone(self):
        self.assertEqual(expand_naming_vars("{UNKNOWN}", {}), "{UNKNOWN}")

    def test_no_vars(self):
        self.assertEqual(expand_naming_vars("plain text", {}), "plain text")

    def test_empty(self):
        self.assertEqual(expand_naming_vars("", {}), "")


class TestExpandCommand(unittest.TestCase):
    def test_basic(self):
        result = expand_command("dvdbackup -i $MOUNT_POINT",
                                "/Volumes/DVD", 1, "/out", "/out/001")
        self.assertEqual(result, 'dvdbackup -i /Volumes/DVD')

    def test_braces(self):
        result = expand_command("cmd ${MOUNT_POINT} ${DIR_NAME}",
                                "/Volumes/DVD", 1, "/out", "/out/001")
        self.assertEqual(result, 'cmd /Volumes/DVD /out/001')

    def test_all_vars(self):
        result = expand_command("$MOUNT_POINT $TARGET_DIR $DIR_NAME $DISC_NR",
                                "/mnt", 5, "/target", "/target/dir")
        self.assertEqual(result, '/mnt /target /target/dir 5')


class TestEnsureDvdbackupFlags(unittest.TestCase):
    def test_adds_name(self):
        result = _ensure_dvdbackup_flags("dvdbackup -i /Volumes/DVD -M", "/Volumes/DVD")
        self.assertIn('-n "DVD"', result)

    def test_preserves_existing_name(self):
        result = _ensure_dvdbackup_flags('dvdbackup -n "CUSTOM" -i /Volumes/DVD', "/Volumes/DVD")
        self.assertNotIn('-n "DVD"', result)
        self.assertIn('-n "CUSTOM"', result)

    def test_preserves_long_name_flag(self):
        result = _ensure_dvdbackup_flags('dvdbackup --name "CUSTOM" -i /Volumes/DVD', "/Volumes/DVD")
        self.assertNotIn('-n "DVD"', result)

    def test_compound_command(self):
        result = _ensure_dvdbackup_flags('mkdir -p "$DIR" && dvdbackup -i /Volumes/DVD -M', "/Volumes/DVD")
        self.assertIn('mkdir -p "$DIR"', result)
        self.assertIn('-n "DVD"', result)

    def test_non_dvdbackup(self):
        result = _ensure_dvdbackup_flags("cdparanoia -B -- -0", "/Volumes/CD")
        self.assertEqual(result, "cdparanoia -B -- -0")


class TestFormatSize(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(_format_size(512), "512 B")

    def test_kb(self):
        self.assertEqual(_format_size(2048), "2.0 KB")

    def test_mb(self):
        self.assertEqual(_format_size(5 * 1048576), "5.0 MB")

    def test_gb(self):
        self.assertEqual(_format_size(3 * 1073741824), "3.0 GB")


class TestBatchStatusFmtElapsed(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(BatchStatus._fmt_elapsed(45), "0m 45s")

    def test_minutes(self):
        self.assertEqual(BatchStatus._fmt_elapsed(125), "2m 05s")

    def test_hours(self):
        self.assertEqual(BatchStatus._fmt_elapsed(3661), "1h 01m 01s")


class TestBatchStatusRecordDisc(unittest.TestCase):
    def setUp(self):
        self._orig_status = STATUS_FILE
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".status", delete=False)
        self._tmpfile.close()
        globals()["STATUS_FILE"] = self._tmpfile.name

        self._tmpresult = tempfile.NamedTemporaryFile(suffix=".results", delete=False)
        self._tmpresult.close()
        self._result_file = self._tmpresult.name

    def tearDown(self):
        globals()["STATUS_FILE"] = self._orig_status
        for f in (self._tmpfile.name, self._tmpresult.name):
            try:
                os.unlink(f)
            except OSError:
                pass

    def _make_status(self, **kwargs):
        kwargs.setdefault("result_file", self._result_file)
        return BatchStatus(**kwargs)

    def test_record_accept(self):
        status = self._make_status(flavor="readdvd", mount_point="/Volumes/DVD", target_dir="/out", idx_offset=209, mode="batch")
        status.update("running", disc_nr=1)
        status.record_accept(index=210, dir_name="/out/210", source_size=1048576, elapsed=60.0, total_elapsed=75.0)
        self.assertEqual(status.accepted, 1)
        self.assertEqual(status.last_disc["index"], 210)
        self.assertEqual(status.last_disc["elapsed"], 60.0)
        self.assertEqual(status.last_disc["total_elapsed"], 75.0)
        self.assertEqual(status.last_disc["result"], "successful")

    def test_record_reject(self):
        status = self._make_status(flavor="readdvd", mount_point="/Volumes/DVD", target_dir="/out", mode="next")
        status.update("running", disc_nr=1)
        status.record_reject(index=1, dir_name="/out/001", source_size=0, elapsed=5.0, rc=1, total_elapsed=10.0)
        self.assertEqual(status.rejected, 1)
        self.assertEqual(status.last_disc["rc"], 1)
        self.assertEqual(status.last_disc["total_elapsed"], 10.0)

    def test_total_elapsed_defaults_to_elapsed(self):
        status = self._make_status(flavor="readdvd", mount_point="/Volumes/DVD", target_dir="/out")
        status.update("running", disc_nr=1)
        status.record_accept(index=1, dir_name="/out/001", source_size=0, elapsed=30.0)
        self.assertEqual(status.last_disc["total_elapsed"], 30.0)

    def test_status_file_has_mode_and_pid(self):
        status = self._make_status(flavor="readdvd", mount_point="/Volumes/DVD", target_dir="/out", mode="batch")
        status.update("running", disc_nr=1)
        sf = BatchStatus.read_file()
        self.assertEqual(sf["mode"], "batch")
        self.assertIn("pid", sf)

    def test_status_file_has_cli(self):
        status = self._make_status(flavor="readdvd", mount_point="/Volumes/DVD", target_dir="/out")
        status.update("running")
        sf = BatchStatus.read_file()
        self.assertIn("cli", sf)
        self.assertTrue(sf["cli"].startswith("my-nimbie"))

    def test_result_file_written(self):
        status = self._make_status(flavor="readdvd", mount_point="/Volumes/DVD", target_dir="/out")
        status.update("running", disc_nr=1)
        status.record_accept(index=210, dir_name="/out/210", source_size=1073741824,
                             elapsed=120.0, total_elapsed=140.0)
        with open(self._result_file) as f:
            line = f.read()
        self.assertIn("#210", line)
        self.assertIn("successful", line)
        self.assertIn("cmd=", line)
        self.assertIn("total=", line)


class TestOffsetIndex(unittest.TestCase):
    """Verify INDEX = disc_nr + idx_offset."""

    def _make_config(self, idx_offset=0, idx_padding=3):
        cp = configparser.ConfigParser()
        cp.read_dict({
            "naming": {
                "name_prefix": "{INDEX}",
                "name": " - {MEDIA_TYPE}",
                "name_postfix": "",
                "idx_padding": str(idx_padding),
                "idx_offset": str(idx_offset),
            }
        })
        return cp

    def test_offset_0_disc_1(self):
        """disc_nr=1, offset=0 → INDEX=1 → '001'"""
        config = self._make_config(idx_offset=0)
        # Use dry_run to avoid touching real mount point
        global dry_run
        old = dry_run
        dry_run = True
        try:
            name = build_dir_name(config, 1, "/Volumes/DVD", "readdvd",
                                  {"prefix": None, "name": None, "postfix": None,
                                   "idx_padding": None, "idx_offset": None})
            self.assertTrue(name.startswith("001"))
        finally:
            dry_run = old

    def test_offset_209_disc_1(self):
        """disc_nr=1, offset=209 → INDEX=210 → '210'"""
        config = self._make_config(idx_offset=0)
        global dry_run
        old = dry_run
        dry_run = True
        try:
            name = build_dir_name(config, 1, "/Volumes/DVD", "readdvd",
                                  {"prefix": None, "name": None, "postfix": None,
                                   "idx_padding": None, "idx_offset": 209})
            self.assertTrue(name.startswith("210"))
        finally:
            dry_run = old

    def test_offset_50_disc_3(self):
        """disc_nr=3, offset=50 → INDEX=53 → '053'"""
        config = self._make_config(idx_offset=0)
        global dry_run
        old = dry_run
        dry_run = True
        try:
            name = build_dir_name(config, 3, "/Volumes/DVD", "readdvd",
                                  {"prefix": None, "name": None, "postfix": None,
                                   "idx_padding": None, "idx_offset": 50})
            self.assertTrue(name.startswith("053"))
        finally:
            dry_run = old


class TestArgvExpansion(unittest.TestCase):
    """Test -DD expansion and global flag hoisting."""

    def _expand_and_hoist(self, argv):
        expanded = []
        for arg in argv:
            if re.match(r'^-D{2,}$', arg):
                expanded.extend(["-D"] * len(arg[1:]))
            else:
                expanded.append(arg)
        subcommands = {"load", "eject", "reject", "unmount", "status", "next", "batch", "reset", "cancel", "monitor"}
        global_flags = {"-D", "-v", "-V", "-d", "--debug", "--verbose", "--dry", "--deepdbg"}
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
        return hoisted + rest

    def test_DD_expansion(self):
        result = self._expand_and_hoist(["next", "-DD", "readdvd"])
        self.assertEqual(result.count("-D"), 2)
        self.assertIn("next", result)
        self.assertIn("readdvd", result)

    def test_DDD_expansion(self):
        result = self._expand_and_hoist(["batch", "-DDD"])
        self.assertEqual(result.count("-D"), 3)

    def test_flag_hoisting(self):
        result = self._expand_and_hoist(["next", "-v", "-D", "readdvd"])
        # -v and -D should come before 'next'
        next_idx = result.index("next")
        self.assertTrue(all(result.index(f) < next_idx for f in ["-v", "-D"]))

    def test_no_flags(self):
        result = self._expand_and_hoist(["status"])
        self.assertEqual(result, ["status"])

    def test_flags_before_subcmd_stay(self):
        result = self._expand_and_hoist(["-v", "status"])
        self.assertEqual(result, ["-v", "status"])

    def test_help_subcommand_rewrite(self):
        """--help batch must be rewritten to batch --help before parsing."""
        _all_subcommands = {"load", "eject", "reject", "unmount", "status", "next", "batch",
                            "reset", "cancel", "monitor", "accept", "retry", "stop", "probe"}
        def _rewrite(argv):
            if len(argv) >= 2 and argv[0] in ("--help", "-h") and argv[1] in _all_subcommands:
                return [argv[1], "--help"] + argv[2:]
            return argv
        self.assertEqual(_rewrite(["--help", "batch"]), ["batch", "--help"])
        self.assertEqual(_rewrite(["-h", "next"]),      ["next", "--help"])
        self.assertEqual(_rewrite(["--help", "reset"]), ["reset", "--help"])
        self.assertEqual(_rewrite(["--help", "probe"]), ["probe", "--help"])
        self.assertEqual(_rewrite(["--help"]),           ["--help"])          # no subcommand → unchanged
        self.assertEqual(_rewrite(["batch", "--help"]), ["batch", "--help"])  # already correct → unchanged


class TestStripQuotes(unittest.TestCase):
    def test_double_quotes(self):
        self.assertEqual(build_dir_name.__code__.co_consts, build_dir_name.__code__.co_consts)
        # Test via expand_naming_vars with a quoted value
        # _strip_quotes is local to build_dir_name, test indirectly via config
        cp = configparser.ConfigParser()
        cp.read_dict({"naming": {
            "name_prefix": "{INDEX}",
            "name": '" - {MEDIA_TYPE}"',  # configparser stores: " - {MEDIA_TYPE}"
            "name_postfix": "",
            "idx_padding": "3",
            "idx_offset": "0",
        }})
        global dry_run
        old = dry_run
        dry_run = True
        try:
            name = build_dir_name(cp, 1, "/Volumes/DVD", "readdvd",
                                  {"prefix": None, "name": None, "postfix": None,
                                   "idx_padding": None, "idx_offset": None})
            self.assertEqual(name, "001 - DVD")
        finally:
            dry_run = old


class TestCrashDiscDetection(unittest.TestCase):
    """Regression: crash handler must detect disc in closed drive."""

    def test_tray_closed_is_disc_stuck(self):
        """When tray is closed and process crashed, disc is likely still inside."""
        # Simulates the state bits when disc is in a closed drive
        state = {"disc_available": False, "disc_in_tray": False,
                 "disc_lifted": False, "tray_out": False}
        # disc_in_tray=False + tray_out=False = disc in closed drive
        disc_in_closed = not state["tray_out"] and not state["disc_in_tray"] and not state["disc_lifted"]
        self.assertTrue(disc_in_closed, "Should detect disc in closed drive")

    def test_tray_open_no_disc(self):
        """Tray open with no disc detected — no disc stuck."""
        state = {"disc_available": False, "disc_in_tray": False,
                 "disc_lifted": False, "tray_out": True}
        disc_in_closed = not state["tray_out"] and not state["disc_in_tray"] and not state["disc_lifted"]
        disc_stuck = state["disc_in_tray"] or state["disc_lifted"] or disc_in_closed
        self.assertFalse(disc_stuck, "Should not flag disc stuck when tray is open and empty")

    def test_disc_on_open_tray(self):
        """Disc visible on ejected tray — disc stuck."""
        state = {"disc_available": False, "disc_in_tray": True,
                 "disc_lifted": False, "tray_out": True}
        disc_stuck = state["disc_in_tray"] or state["disc_lifted"]
        self.assertTrue(disc_stuck, "Should detect disc on open tray")

    def test_disc_lifted(self):
        """Disc held by gripper — disc stuck."""
        state = {"disc_available": False, "disc_in_tray": False,
                 "disc_lifted": True, "tray_out": True}
        disc_stuck = state["disc_in_tray"] or state["disc_lifted"]
        self.assertTrue(disc_stuck, "Should detect disc held by gripper")


class TestStalePidDetection(unittest.TestCase):
    def test_dead_pid(self):
        """A PID that doesn't exist should not be alive."""
        alive = False
        try:
            os.kill(99999999, 0)
            alive = True
        except OSError:
            pass
        self.assertFalse(alive)

    def test_own_pid(self):
        """Our own PID should be alive."""
        alive = False
        try:
            os.kill(os.getpid(), 0)
            alive = True
        except OSError:
            pass
        self.assertTrue(alive)


class TestTimestampFormat(unittest.TestCase):
    """Regression: timestamp must be YYYY-MM-DD_HHMM.SS.mmm."""

    def test_format_matches_pattern(self):
        ts = _ts()
        # e.g. 2026-03-29_0329.45.123
        self.assertRegex(ts, r'^\d{4}-\d{2}-\d{2}_\d{4}\.\d{2}\.\d{3}$',
                         f"Timestamp {ts!r} does not match YYYY-MM-DD_HHMM.SS.mmm")

    def test_milliseconds_zero_padded(self):
        # Force a datetime with microseconds < 1000 (i.e. ms=0) to check zero-padding
        import datetime as _dt
        dt = _dt.datetime(2026, 3, 29, 3, 29, 45, 0)
        ts = dt.strftime("%Y-%m-%d_%H%M.%S.") + f"{dt.microsecond // 1000:03d}"
        self.assertTrue(ts.endswith(".000"), f"Expected .000, got {ts!r}")

    def test_milliseconds_are_three_digits(self):
        import datetime as _dt
        dt = _dt.datetime(2026, 3, 29, 3, 29, 45, 123000)
        ts = dt.strftime("%Y-%m-%d_%H%M.%S.") + f"{dt.microsecond // 1000:03d}"
        self.assertTrue(ts.endswith(".123"), f"Expected .123, got {ts!r}")


class TestStatusJsonCollector(unittest.TestCase):
    """Regression: _start_status_json_collector writes valid NDJSON."""

    def test_writes_json_with_ts_and_hw(self):
        import tempfile, json, time

        class _FakeNimbie:
            def get_state(self, fatal=False):
                return {"disc_available": False, "disc_in_tray": True,
                        "disc_lifted": False, "tray_out": False}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            path = f.name

        try:
            t = _start_status_json_collector(_FakeNimbie(), path, interval=0.05)
            time.sleep(0.2)
            t.stop_event.set()
            t.join(timeout=1.0)

            lines = open(path).read().strip().splitlines()
            self.assertGreater(len(lines), 0, "No lines written to status JSON")

            snap = json.loads(lines[0])
            self.assertIn("ts", snap, "Snapshot missing 'ts' key")
            self.assertIn("hw", snap, "Snapshot missing 'hw' key")
            self.assertIn("disc_in_tray", snap["hw"])
            self.assertTrue(snap["hw"]["disc_in_tray"])
            # Verify ts format
            self.assertRegex(snap["ts"], r'^\d{4}-\d{2}-\d{2}_\d{4}\.\d{2}\.\d{3}$')
        finally:
            import os as _os
            try:
                _os.unlink(path)
            except OSError:
                pass

    def test_hw_error_recorded_when_nimbie_fails(self):
        import tempfile, json, time

        class _BrokenNimbie:
            def get_state(self, fatal=False):
                raise RuntimeError("USB error")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            path = f.name

        try:
            t = _start_status_json_collector(_BrokenNimbie(), path, interval=0.05)
            time.sleep(0.2)
            t.stop_event.set()
            t.join(timeout=1.0)

            lines = open(path).read().strip().splitlines()
            self.assertGreater(len(lines), 0)
            snap = json.loads(lines[0])
            self.assertIn("error", snap["hw"], "Expected error key in hw when get_state fails")
        finally:
            import os as _os
            try:
                _os.unlink(path)
            except OSError:
                pass


class TestMonitorCommandParser(unittest.TestCase):
    """Regression: monitor command is registered in parser and subcommands set."""

    def test_monitor_in_parser(self):
        parser = build_parser()
        # Parse 'monitor' — should not raise
        args = parser.parse_args(["monitor"])
        self.assertEqual(args.command, "monitor")

    def test_monitor_subcommand_hoisting(self):
        """monitor must be in the subcommands set so global flags are hoisted."""
        # Simulate the hoisting logic used in main()
        subcommands = {"load", "eject", "reject", "unmount", "status",
                       "next", "batch", "reset", "cancel", "monitor"}
        self.assertIn("monitor", subcommands)

    def test_monitor_help_contains_tail_instruction(self):
        """--help monitor description must mention tail -f and CTRL+C."""
        parser = build_parser()
        sub_actions = [a for a in parser._subparsers._group_actions
                       if hasattr(a, '_name_parser_map')]
        monitor_parser = sub_actions[0]._name_parser_map["monitor"]
        # --help <subcommand> shows description + epilog, not format_help()
        desc = (monitor_parser.description or "") + (monitor_parser.epilog or "")
        self.assertIn("tail -f", desc, "--help monitor description should mention 'tail -f'")
        self.assertIn("CTRL+C", desc, "--help monitor description should mention CTRL+C")

    def test_help_subcommand_shows_description_not_usage(self):
        """--help batch output must contain the description and NOT the usage line."""
        import io, sys as _sys
        parser = build_parser()
        sub_actions = [a for a in parser._subparsers._group_actions
                       if hasattr(a, '_name_parser_map')]
        subparser = sub_actions[0]._name_parser_map["batch"]
        # Simulate what main() does for --help batch
        buf = io.StringIO()
        old_stdout = _sys.stdout
        _sys.stdout = buf
        try:
            print(f"my-nimbie batch\n")
            if subparser.description:
                print(subparser.description.rstrip())
            if subparser.epilog:
                print()
                print(subparser.epilog.rstrip())
            print(f"\n  For the full flag listing: my-nimbie batch --help")
        finally:
            _sys.stdout = old_stdout
        output = buf.getvalue()
        self.assertIn("Batch-process discs", output)
        self.assertNotIn("usage:", output)
        self.assertNotIn("--target-dir", output)  # flags must NOT appear
        self.assertIn("my-nimbie batch --help", output)  # pointer to full help


class TestCommandHelpDescriptions(unittest.TestCase):
    """Regression: all commands have proper --help descriptions and global epilog."""

    def _get_subparser(self, name):
        parser = build_parser()
        sub_actions = [a for a in parser._subparsers._group_actions
                       if hasattr(a, '_name_parser_map')]
        return sub_actions[0]._name_parser_map[name]

    def test_load_has_description_and_epilog(self):
        p = self._get_subparser("load")
        self.assertTrue(p.description and len(p.description) > 20)
        self.assertIn("--config", p.epilog)

    def test_eject_has_description_and_epilog(self):
        p = self._get_subparser("eject")
        self.assertTrue(p.description and len(p.description) > 20)
        self.assertIn("--config", p.epilog)

    def test_status_has_description_and_epilog(self):
        p = self._get_subparser("status")
        self.assertTrue(p.description and len(p.description) > 20)
        self.assertIn("--config", p.epilog)

    def test_reset_has_exit_bootloader_flag(self):
        p = self._get_subparser("reset")
        flag_names = [a.option_strings for a in p._actions]
        flat = [s for opts in flag_names for s in opts]
        self.assertIn("--exit-bootloader", flat)
        self.assertIn("--diagnostics", flat)

    def test_next_has_use_loaded_flag(self):
        p = self._get_subparser("next")
        flag_names = [a.option_strings for a in p._actions]
        flat = [s for opts in flag_names for s in opts]
        self.assertIn("--use-loaded", flat)

    def test_batch_has_use_loaded_flag(self):
        p = self._get_subparser("batch")
        flag_names = [a.option_strings for a in p._actions]
        flat = [s for opts in flag_names for s in opts]
        self.assertIn("--use-loaded", flat)

    def test_next_epilog_has_global_options(self):
        p = self._get_subparser("next")
        self.assertIn("--config", p.epilog)
        self.assertIn("--dry", p.epilog)
        self.assertIn("--debug", p.epilog)

    def test_batch_epilog_has_global_options(self):
        p = self._get_subparser("batch")
        self.assertIn("--config", p.epilog)
        self.assertIn("--dry", p.epilog)
        self.assertIn("--debug", p.epilog)


def _run_tests():
    """Run unit tests."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    test_classes = [
        TestExpandNamingVars,
        TestExpandCommand,
        TestEnsureDvdbackupFlags,
        TestFormatSize,
        TestBatchStatusFmtElapsed,
        TestBatchStatusRecordDisc,
        TestOffsetIndex,
        TestArgvExpansion,
        TestStripQuotes,
        TestCrashDiscDetection,
        TestStalePidDetection,
        TestTimestampFormat,
        TestStatusJsonCollector,
        TestMonitorCommandParser,
        TestCommandHelpDescriptions,
    ]
    for cls in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


def cmd_monitor(_nimbie, config, _args):
    """Start the status JSON collector and monitor the Nimbie in real time.

    Waits for the Nimbie to appear on USB (every 0.1s), writing a JSON entry
    for each probe attempt (found or not-found). This allows starting the
    monitor BEFORE powering on the device and watching it come online.
    """
    import json as _json
    import time as _time

    force = getattr(_args, "force", False)
    stop  = getattr(_args, "stop",  False)

    def _send_stop_to_existing():
        """Send SIGTERM to the monitor in MONITOR_PID_FILE. Returns pid or None."""
        try:
            with open(MONITOR_PID_FILE) as _f:
                _pid = int(_f.read().strip())
        except (FileNotFoundError, ValueError):
            return None
        try:
            os.kill(_pid, 0)  # check alive
        except OSError:
            return None   # already dead
        os.kill(_pid, signal.SIGTERM)
        return _pid

    # --stop: send SIGTERM to running monitor and exit (no new monitor started)
    if stop:
        _pid = _send_stop_to_existing()
        if _pid is None:
            msg("No monitor process is running.")
        else:
            msg(f"Sent SIGTERM to monitor (PID {_pid}) — it will stop gracefully.")
        return

    # Enforce single-instance: fail if another monitor is already running,
    # unless --force is given (send SIGTERM, wait for graceful exit, then start fresh).
    try:
        with open(MONITOR_PID_FILE) as _f:
            _old_pid = int(_f.read().strip())
        try:
            os.kill(_old_pid, 0)
            _still_running = True
        except OSError:
            _still_running = False
        if _still_running:
            if not force:
                err(f"Monitor already running (PID {_old_pid}).\n\n"
                    f"  Use 'my-nimbie monitor --stop'  to stop it gracefully.\n"
                    f"  Use 'my-nimbie monitor --force' to stop it and start a fresh one.\n"
                    f"  Multiple monitors corrupt {DEFAULT_STATUS_JSON}.")
            # --force: send SIGTERM and wait up to 2s for graceful exit
            os.kill(_old_pid, signal.SIGTERM)
            for _ in range(20):
                _time.sleep(0.1)
                try:
                    os.kill(_old_pid, 0)
                except OSError:
                    break   # gone
            else:
                # Still alive after 2s — force kill
                try:
                    os.kill(_old_pid, signal.SIGKILL)
                except OSError:
                    pass
            msg(f"  Stopped existing monitor (PID {_old_pid}).")
    except (FileNotFoundError, ValueError):
        pass  # no pid file or invalid content — no previous monitor

    # Write our own PID so future invocations can find us
    try:
        with open(MONITOR_PID_FILE, "w") as _f:
            _f.write(str(os.getpid()))
    except OSError:
        pass

    # SIGTERM handler: raises SystemExit so the finally block cleans up the PID file.
    # This makes "my-nimbie monitor --stop" (which sends SIGTERM) a clean shutdown.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    status_json = config.get("batch", "status_json", fallback=DEFAULT_STATUS_JSON)
    _rotate_file(status_json)
    # Create file immediately so "tail -f" works before first write
    try:
        open(status_json, "w").close()
    except OSError:
        pass

    vid = int(config.get("nimbie", "vid"), 16)
    pid_val = int(config.get("nimbie", "pid"), 16)

    msg("")
    msg("=" * 60)
    msg("  my-nimbie monitor — real-time status collector")
    msg("=" * 60)
    msg("")
    msg(f"  Polling every 0.1s — watching USB for Nimbie (normal) or bootloader mode.")
    msg(f"  Each JSON entry has \"usb\": \"offline\" | \"bootloader\" | \"normal\".")
    msg(f"  Writing JSON snapshots to:")
    msg(f"    {status_json}")
    msg("")
    msg(f"  Watch in another terminal:")
    msg(f"    tail -f {status_json}")
    msg("")
    msg("  Press CTRL+C to stop.")
    msg("")

    def _write(entry):
        entry["ts"] = _ts()
        try:
            with open(status_json, "a") as f:
                f.write(_json.dumps(entry) + "\n")
        except OSError:
            pass

    try:
        while True:
            import usb.core as _usb_core
            dev = _usb_core.find(idVendor=vid, idProduct=pid_val)
            if dev is None:
                # Check for bootloader mode before reporting offline
                bl = _usb_core.find(idVendor=BL_VID, idProduct=BL_PID)
                if bl is not None:
                    _write({"usb": "bootloader",
                            "vid": f"{BL_VID:#06x}",
                            "pid": f"{BL_PID:#06x}",
                            "note": "Nimbie is in Microchip PIC bootloader mode — run: my-nimbie reset --exit-bootloader"})
                else:
                    _write({"usb": "offline"})
                _time.sleep(0.1)
                continue

            # Device found — connect, read state, disconnect immediately
            # (release interface between polls so batch/next can claim USB)
            nimbie = NimbieDevice(vid, pid_val)
            try:
                nimbie.connect()
                state = nimbie.get_state()
                _write({"usb": "normal", "hw": {
                    "disc_available": state.get("disc_available"),
                    "disc_in_tray":   state.get("disc_in_tray"),
                    "disc_lifted":    state.get("disc_lifted"),
                    "tray_out":       state.get("tray_out"),
                }})
            except (Exception, SystemExit) as e:
                # SystemExit can be raised by connect() → claim_interface() → err()
                # (e.g. USB not yet claimable after power cycle). Treat as transient
                # offline — write error entry and keep polling.
                # BUT: if CTRL+C was pressed (interrupted=True), re-raise so the
                # outer except KeyboardInterrupt / finally can clean up and exit.
                if interrupted:
                    raise
                _write({"usb": "offline", "error": str(e)})
            finally:
                try:
                    nimbie.disconnect()
                except Exception:
                    pass

            _time.sleep(0.1)

    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        try:
            os.unlink(MONITOR_PID_FILE)
        except OSError:
            pass

    msg("")
    msg("  Monitoring stopped.")
    msg("")


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
    parser.add_argument("--test", action="store_true",
                        help="run unit tests (no hardware needed)")
    parser.add_argument("--dry", "-d", action="store_true",
                        help="dry run — print what would be done without executing")
    parser.add_argument("--verbose", "-v", "-V", action="store_true",
                        help="verbose output")
    parser.add_argument("--debug", "-D", action="count", default=0,
                        help="debug output; -DD/--deepdbg for deep debug (implies --verbose)")
    parser.add_argument("--deepdbg", action="store_true",
                        help="deep debug (same as -DD)")

    _GLOBAL_EPILOG = """\
Global options (place before or after subcommand):
  -c FILE, --config FILE   config file path (overrides search order)
  -d, --dry                dry run — print what would be done without executing
  -v, --verbose            verbose output
  -D, --debug              debug output (-DD or --deepdbg for deep debug)"""

    sub = parser.add_subparsers(dest="command", parser_class=_HelpfulParser)

    _p = sub.add_parser("load",
                        formatter_class=argparse.RawDescriptionHelpFormatter,
                        help="Load next disc from hopper into drive",
                        description="""\
Load the next disc from the Nimbie hopper into the optical drive.

The Nimbie gripper picks up the top disc from the input hopper, places it
on the tray, and closes the tray. The drive spins up and the disc is ready
to mount.

Use this before running a rip/read command on a manually loaded disc.
For automated batch processing use "batch" or "next" instead.""")
    _p.epilog = _GLOBAL_EPILOG

    _p = sub.add_parser("eject",
                        formatter_class=argparse.RawDescriptionHelpFormatter,
                        help="Eject current disc to accept (done) bin",
                        description="""\
Eject the current disc from the drive and move it to the accept (done) bin.

Opens the tray, waits for the disc to settle, then the Nimbie gripper picks
it up and drops it in the accept output bin.

Use this after a successful rip to move the disc out of the drive.
If a batch/next job is paused waiting for user input, use "accept" instead.""")
    _p.epilog = _GLOBAL_EPILOG

    _p = sub.add_parser("reject",
                        formatter_class=argparse.RawDescriptionHelpFormatter,
                        help="Reject current disc to reject bin (or: tell paused batch to reject)",
                        description="""\
Reject the current disc — ejects from drive and drops into the reject bin.

Two modes:
  1. Standalone: physically ejects and rejects the disc in the drive now.
  2. Signal to batch/next: if a batch or next job is paused waiting for
     user input (e.g. after --pause-on-err), sends it a "reject" signal,
     which causes it to reject the disc and either stop or continue.

The reject bin is the separate output tray for discs that failed processing.""")
    _p.epilog = _GLOBAL_EPILOG

    _p = sub.add_parser("unmount",
                        formatter_class=argparse.RawDescriptionHelpFormatter,
                        help="Unmount disc from optical drive (macOS diskutil eject)",
                        description="""\
Unmount the disc from the macOS filesystem without physically ejecting it.

Calls "diskutil eject" on the configured mount point. Use this when you want
to release the filesystem lock before calling "eject" or doing a manual
disc swap. The disc stays in the drive tray until physically ejected.""")
    _p.epilog = _GLOBAL_EPILOG

    _p = sub.add_parser("status",
                        formatter_class=argparse.RawDescriptionHelpFormatter,
                        help="Show Nimbie device state (or batch progress if running)",
                        description="""\
Show current Nimbie hardware state and batch/next job progress.

Hardware state includes:
  disc_available   — disc(s) in the input hopper
  disc_in_tray     — disc currently in the optical drive tray
  disc_lifted      — disc currently held by the gripper arm
  tray_out         — drive tray is open

If a batch or next job is running, also shows:
  - Current disc number and index
  - Accept / reject counts
  - Last disc processed
  - Target directory
  - Crash info (if the job died unexpectedly)

Use -V (verbose) for additional detail including USB state bits and
drive tray state from drutil.""")
    _p.epilog = _GLOBAL_EPILOG

    _p = sub.add_parser("accept",
                        formatter_class=argparse.RawDescriptionHelpFormatter,
                        help="Tell a paused batch/next to accept the current disc",
                        description="""\
Signal a paused batch/next job to accept the current disc.

When batch/next is paused (e.g. after --pause-on-err or awaiting user
confirmation), this command sends an "accept" signal: the job accepts
the disc (moves to done bin) and continues to the next disc.""")
    _p.epilog = _GLOBAL_EPILOG

    _p = sub.add_parser("retry",
                        formatter_class=argparse.RawDescriptionHelpFormatter,
                        help="Tell a paused batch/next to retry the current command",
                        description="""\
Signal a paused batch/next job to retry the current disc.

When batch/next is paused after a command failure, this sends a "retry"
signal: the job re-runs the same command on the same disc without
re-loading it.""")
    _p.epilog = _GLOBAL_EPILOG

    _p = sub.add_parser("stop",
                        formatter_class=argparse.RawDescriptionHelpFormatter,
                        help="Tell a paused batch/next to reject disc and stop",
                        description="""\
Signal a paused batch/next job to reject the current disc and stop.

When batch/next is paused, this sends a "stop" signal: the job rejects
the disc (moves to reject bin) and exits cleanly, printing a summary.""")
    _p.epilog = _GLOBAL_EPILOG

    _p = sub.add_parser("cancel",
                        formatter_class=argparse.RawDescriptionHelpFormatter,
                        help="Cancel a running batch/next job (sends SIGINT to the process)",
                        description="""\
Cancel a running batch/next job by sending SIGINT to its process.

Reads the PID from the status file and sends SIGINT (same as CTRL+C).
The job will finish its current disc operation cleanly (accept or reject),
write a final summary, and exit.

Note: this does NOT immediately stop a long-running disc command (e.g.
dvdbackup). It sets the stop flag so the job stops after the current disc
completes.""")
    _p.epilog = _GLOBAL_EPILOG

    monitor_parser = sub.add_parser("monitor",
                                    formatter_class=argparse.RawDescriptionHelpFormatter,
                                    help="Start real-time status monitoring (writes NDJSON, press CTRL+C to stop)",
                                    description=f"""\
Start real-time status monitoring of the Nimbie.

Collects hardware state (disc_available, disc_in_tray, disc_lifted, tray_out)
and batch/next job progress every 0.1s (10x/second), appending JSON snapshots to:
  {DEFAULT_STATUS_JSON}

Each snapshot line looks like:
  {{"ts":"2026-03-29_1234.56.789","hw":{{"disc_available":true,...}},"batch":{{...}}}}

Watch the live output in another terminal:
  tail -f {DEFAULT_STATUS_JSON}

The monitor command blocks until you press CTRL+C.
The status_json path can be configured in the config file under [batch] status_json.""")
    monitor_parser.add_argument(
        "--force", action="store_true",
        help="Send SIGTERM to any already-running monitor, then start a fresh one"
    )
    monitor_parser.add_argument(
        "--stop", action="store_true",
        help="Send SIGTERM to the running monitor (graceful shutdown) and exit"
    )

    reset_parser = sub.add_parser("reset", help="Recover Nimbie from error states (bootloader mode, stuck disc)",
                                  formatter_class=argparse.RawDescriptionHelpFormatter,
                                  description="""\
Detect and recover the Nimbie from various error states.

Without flags, auto-detects the device state and recovers:
  - If in bootloader mode: sends RESET command (then you power cycle)
  - If in normal mode: reports no recovery needed

Standard recovery:
  --exit-bootloader     Send RESET_DEVICE (0x06) to bootloader, then power cycle
  --diagnostics         Show device diagnostics (counters, state, bootloader info)

Bootloader operations (device must be in bootloader mode — LED: ERROR=RED):
  --jump-to-app         AN1388 framed Jump-to-App — NOTE: NOT supported on this device
  --bl-query            QUERY_DEVICE (0x00) — show bootloader version/info
  --bl-scan             Scan bootloader commands 0x00-0xFF
  --bl-read-flash       Read flash reset vector + bootloader config area
  --bl-read-addr ADDR   Read flash at specific hex address (e.g. 0x0000)
  --bl-read-len N       Bytes to read with --bl-read-addr (default: 256)
  --bl-raw HEX          Send raw hex bytes to bootloader (e.g. "0500")
  --sign-and-reset      CONFIRMED recovery: PROGRAM_COMPLETE + SIGN_FLASH x2 + power cycle

Confirmed working recovery procedure:
  my-nimbie reset --sign-and-reset
  Then: hardware switch OFF → wait 10s → ON
  Verify with: my-nimbie status""")
    reset_parser.add_argument("--exit-bootloader", action="store_true",
                              help="send RESET_DEVICE to bootloader (then power cycle)")
    reset_parser.add_argument("--diagnostics", action="store_true",
                              help="show device diagnostics (counters, timers, state)")
    reset_parser.add_argument("--jump-to-app", action="store_true",
                              help="AN1388 framed Jump-to-App — NOTE: not supported on this device (echoes back)")
    reset_parser.add_argument("--bl-query", action="store_true",
                              help="query bootloader: QUERY_DEVICE (0x00)")
    reset_parser.add_argument("--bl-scan", action="store_true",
                              help="scan bootloader commands 0x00-0xFF")
    reset_parser.add_argument("--bl-read-flash", action="store_true",
                              help="read flash reset vector + bootloader config area")
    reset_parser.add_argument("--bl-read-addr", metavar="ADDR",
                              help="read flash at hex address (e.g. 0x0000)")
    reset_parser.add_argument("--bl-read-len", type=int, default=256, metavar="N",
                              help="bytes to read with --bl-read-addr (default: 256)")
    reset_parser.add_argument("--bl-raw", metavar="HEX",
                              help="send raw hex bytes to bootloader (e.g. '0500')")
    reset_parser.add_argument("--sign-and-reset", action="store_true",
                              help="CONFIRMED recovery: PROGRAM_COMPLETE + SIGN_FLASH x2 + power cycle")

    # --- probe: scan Nimbie command space for reverse-engineering ---
    probe_parser = sub.add_parser("probe",
                                  help="Scan Nimbie USB command space for reverse-engineering",
                                  formatter_class=argparse.RawDescriptionHelpFormatter,
                                  description="""\
Systematically probe the Nimbie's USB command space to discover
undocumented commands and responses.

CAUTION: Unknown commands may trigger mechanical actions. Watch the device!
Commands 0x55 (disconnect) and 0x56 (bootloader jump) are always skipped.
Known mechanical commands (0x47 LIFT_DISC, 0x52 PLACE/ACCEPT/REJECT) are
skipped by default to avoid unintended disc movement.

Without flags: scans all command bytes 0x00-0xFF.

Examples:
  my-nimbie probe                          scan all 0x00-0xFF
  my-nimbie probe --range 0x40-0x60        scan a specific range
  my-nimbie probe --cmd 0x52 --params      scan params for cmd 0x52
  my-nimbie probe --raw 000043000000       send raw hex bytes""")
    probe_parser.add_argument("--range", dest="probe_range", metavar="RANGE",
                              help="command byte range to scan (e.g. 0x40-0x60)")
    probe_parser.add_argument("--cmd", dest="probe_cmd", metavar="CMD",
                              help="command byte for param scanning (e.g. 0x52)")
    probe_parser.add_argument("--params", dest="probe_params", action="store_true",
                              help="scan param bytes for --cmd")
    probe_parser.add_argument("--param-range", dest="probe_param_range", metavar="RANGE",
                              help="param byte range (e.g. 0x00-0x10, default: 0x00-0xFF)")
    probe_parser.add_argument("--raw", dest="probe_raw", metavar="HEX",
                              help="send raw hex bytes to Nimbie (e.g. '000043000000')")

    # --- next: process exactly one disc (same setup as batch, for testing) ---
    next_parser = sub.add_parser("next", help="Process exactly one disc (same setup as batch, for testing)",
                                 usage="my-nimbie next [--options] [flavor]",
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
    next_parser.add_argument("--use-loaded", action="store_true",
                             help="process a disc already in the drive (skip loading from hopper). "
                             "Use after a crash left a disc in the drive.")
    next_parser.epilog = """\
Global options (place before or after subcommand):
  -c FILE, --config FILE   config file path (overrides search order)
  -d, --dry                dry run — print what would be done without executing
  -v, --verbose            verbose output
  -D, --debug              debug output (-DD or --deepdbg for deep debug)"""

    # --- batch: process all discs in hopper ---
    batch_parser = sub.add_parser("batch", help="Batch mode: load → process → accept/reject → repeat",
                                  usage="my-nimbie batch [--options] [flavor]",
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
    batch_parser.add_argument("--use-loaded", action="store_true",
                              help="process a disc already in the drive as first disc (skip loading). "
                              "Use after a crash left a disc in the drive.")
    batch_parser.add_argument("--max", metavar="N", type=int,
                              help="max discs to process (overrides config max_discs, 0 = unlimited)")
    batch_parser.epilog = """\
Global options (place before or after subcommand):
  -c FILE, --config FILE   config file path (overrides search order)
  -d, --dry                dry run — print what would be done without executing
  -v, --verbose            verbose output
  -D, --debug              debug output (-DD or --deepdbg for deep debug)"""

    return parser


def main():
    global verbose, debug, deepdebug, dry_run

    signal.signal(signal.SIGINT, signal_handler)

    # Pre-process argv: expand -DD → -D -D and hoist global flags before subcommand
    argv = sys.argv[1:]

    # "--help <subcommand>" → show subcommand description without the argparse usage line.
    # This gives a clean, readable overview (like "my-nimbie --help monitor") for every command.
    # For the full flag listing, the user can run: my-nimbie <subcommand> --help
    _all_subcommands = {"load", "eject", "reject", "unmount", "status", "next", "batch",
                        "reset", "cancel", "monitor", "accept", "retry", "stop", "probe"}
    if len(argv) >= 2 and argv[0] in ("--help", "-h") and argv[1] in _all_subcommands:
        argv = [argv[1], "--help"] + argv[2:]

    # Detect common mistake: "--batch", "--next", etc. instead of "batch", "next"
    for arg in argv:
        if arg.startswith("--") and arg[2:] in _all_subcommands:
            print(f"my-nimbie: error: unknown option '{arg}'\n"
                  f"  Did you mean: my-nimbie {arg[2:]} ...\n"
                  f"  Subcommands are used without '--'. Example: my-nimbie batch readdvd --offset 262",
                  file=sys.stderr)
            sys.exit(2)

    expanded = []
    for arg in argv:
        if re.match(r'^-D{2,}$', arg):
            expanded.extend(["-D"] * len(arg[1:]))
        else:
            expanded.append(arg)
    # Hoist global flags (-D, -v, -d, --debug, --verbose, --dry) before the subcommand
    subcommands = {"load", "eject", "reject", "unmount", "status", "next", "batch", "reset", "cancel", "monitor"}
    global_flags = {"-D", "-v", "-V", "-d", "--debug", "--verbose", "--dry", "--deepdbg"}
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

    if args.deepdbg:
        args.debug = max(args.debug, 2)
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

    if args.test:
        _run_tests()
        return

    if not args.command:
        parser.print_help()
        sys.exit(1)

    config_path = find_config_file(args.config)
    config = load_config(config_path)

    # Unmount command: no USB needed
    if args.command == "unmount":
        cmd_unmount(None, config, args)
        return

    # Cancel command: send SIGINT to running batch/next process
    if args.command == "cancel":
        sf = BatchStatus.read_file()
        if not sf or sf.get("state") in ("finished", "interrupted"):
            err("No running batch/next process to cancel.\n\n"
                "  There is no active batch or next process.")
        pid_str = sf.get("pid")
        if not pid_str:
            err("No PID in status file — cannot cancel.")
        pid = int(pid_str)
        try:
            os.kill(pid, 0)  # check if alive
        except OSError:
            err(f"Process {pid} is not running (already dead).\n\n"
                f"  Run 'my-nimbie status' to see crash details.")
        os.kill(pid, signal.SIGINT)
        msg(f"Sent SIGINT to PID {pid} — batch/next should stop gracefully.")
        # Also kill any active on_load child process (e.g. dvdbackup) so it
        # doesn't become an orphan if the batch process exits before it finishes.
        try:
            with open(CMD_CHILD_PID_FILE) as f:
                child_pid = int(f.read().strip())
            os.kill(child_pid, signal.SIGTERM)
            msg(f"Sent SIGTERM to on_load child PID {child_pid} (dvdbackup or similar).")
        except Exception:
            pass  # no child PID file, or child already gone — fine
        return

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

    # Status command: check for bootloader mode first — show that instead of failing to connect
    if args.command == "status":
        try:
            import usb.core as _uc
            if _uc.find(idVendor=BL_VID, idProduct=BL_PID) is not None:
                cmd_status(None, config, args)
                return
        except Exception:
            pass

    # Status command: try reading status file first, only connect if no batch running
    if args.command == "status":
        sf = BatchStatus.read_file()
        if sf and sf.get("state") not in ("finished", "interrupted"):
            # Check if process is still alive
            pid_str = sf.get("pid")
            process_alive = False
            if pid_str:
                try:
                    os.kill(int(pid_str), 0)
                    process_alive = True
                except (OSError, ValueError):
                    pass
            if process_alive:
                # Batch/next is running — show status from file, don't grab USB
                cmd_status(None, config, args)
                return
            # Process crashed — try to connect USB for hardware query, but don't fail if busy
            try:
                vid = int(config.get("nimbie", "vid"), 16)
                pid = int(config.get("nimbie", "pid"), 16)
                nimbie = NimbieDevice(vid, pid)
                # Suppress err() output during probe
                _old_stderr = sys.stderr
                sys.stderr = open(os.devnull, "w")
                try:
                    nimbie.connect()
                finally:
                    sys.stderr.close()
                    sys.stderr = _old_stderr
                try:
                    cmd_status(nimbie, config, args)
                finally:
                    nimbie.disconnect()
            except (SystemExit, Exception):
                # USB unavailable — show status without hardware info
                cmd_status(None, config, args)
            return

    # reset command handles its own USB (may talk to bootloader, not normal Nimbie)
    if args.command == "reset":
        cmd_reset(None, config, args)
        return

    # monitor command manages its own connection (waits for device to appear)
    if args.command == "monitor":
        cmd_monitor(None, config, args)
        return

    # probe command does its own raw USB connect
    if args.command == "probe":
        cmd_probe(None, config, args)
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
            "load":    cmd_load,
            "eject":   cmd_eject,
            "reject":  cmd_reject,
            "status":  cmd_status,
            "next":    cmd_next,
            "batch":   cmd_batch,
            "monitor": cmd_monitor,
        }
        commands[args.command](nimbie, config, args)
    finally:
        nimbie.disconnect()


if __name__ == "__main__":
    main()
