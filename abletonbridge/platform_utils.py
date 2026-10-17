"""Everything that differs between macOS and Windows lives here."""

from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import string
import subprocess
import sys
import time
from pathlib import Path

SYSTEM = platform.system()  # "Darwin", "Windows" or "Linux"
IS_MAC = SYSTEM == "Darwin"
IS_WINDOWS = SYSTEM == "Windows"

BRIDGE_DIR_NAME = "AbletonBridge"
BRIDGE_MARKER = "bridge.json"


def os_label(system: str | None = None) -> str:
    system = system or SYSTEM
    return {"Darwin": "macOS", "Windows": "Windows"}.get(system, system)


def os_key(system: str | None = None) -> str:
    """Short, stable id stored on the drive: 'mac', 'windows' or 'linux'."""
    system = system or SYSTEM
    return {"Darwin": "mac", "Windows": "windows"}.get(system, system.lower())


def machine_name() -> str:
    name = socket.gethostname() or "Computer"
    return re.sub(r"\.local$", "", name)


def config_dir() -> Path:
    if IS_WINDOWS:
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif IS_MAC:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "AbletonBridge"


def default_workspace() -> Path:
    """Where projects fetched from the drive are put on this computer."""
    music = Path.home() / "Music"
    return music / "Ableton Projects"


def default_project_roots() -> list[Path]:
    """Folders searched for Ableton projects on this computer."""
    home = Path.home()
    candidates = [
        default_workspace(),
        home / "Music" / "Ableton",
        home / "Music" / "Ableton Projects",
        home / "Documents" / "Ableton",
        home / "Documents" / "Ableton Projects",
        home / "Desktop",
    ]
    out, seen = [], set()
    for c in candidates:
        key = str(c).lower()
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


# --------------------------------------------------------------------------- drives

def _candidate_volume_roots() -> list[Path]:
    roots: list[Path] = []
    if IS_WINDOWS:
        try:
            import ctypes

            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for i, letter in enumerate(string.ascii_uppercase):
                if letter in "AB":
                    continue
                if bitmask & (1 << i):
                    roots.append(Path(f"{letter}:\\"))
        except Exception:
            roots = [Path(f"{c}:\\") for c in string.ascii_uppercase[2:]]
    elif IS_MAC:
        vol = Path("/Volumes")
        if vol.is_dir():
            roots = [p for p in vol.iterdir() if p.is_dir()]
    else:
        user = os.environ.get("USER", "")
        for base in (Path("/media") / user, Path("/run/media") / user, Path("/media"), Path("/mnt")):
            if base.is_dir():
                try:
                    roots.extend(p for p in base.iterdir() if p.is_dir())
                except OSError:
                    pass
    return roots


def find_bridge_drives() -> list[Path]:
    """Return every mounted volume that has an AbletonBridge folder on it."""
    found = []
    for root in _candidate_volume_roots():
        try:
            if (root / BRIDGE_DIR_NAME / BRIDGE_MARKER).is_file():
                found.append(root / BRIDGE_DIR_NAME)
        except OSError:
            continue
    return found


def running_from_drive_bridge() -> Path | None:
    """If the app itself is launched from the external drive, use that drive."""
    exe = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
    for parent in exe.parents:
        marker = parent / BRIDGE_DIR_NAME / BRIDGE_MARKER
        try:
            if marker.is_file():
                return parent / BRIDGE_DIR_NAME
        except OSError:
            return None
    return None


def volume_root(path: Path) -> Path:
    path = Path(path).resolve()
    if IS_WINDOWS:
        return Path(path.anchor)
    if IS_MAC:
        parts = path.parts
        if len(parts) >= 3 and parts[1] == "Volumes":
            return Path("/", "Volumes", parts[2])
        return Path("/")
    # Linux: longest mount point that contains path
    best = Path("/")
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                mnt = Path(line.split()[1].replace("\\040", " "))
                if str(path).startswith(str(mnt)) and len(str(mnt)) > len(str(best)):
                    best = mnt
    except OSError:
        pass
    return best


def filesystem_type(path: Path) -> str:
    """Best-effort filesystem name for the volume containing *path* (e.g. 'exfat')."""
    path = Path(path)
    try:
        if IS_WINDOWS:
            import ctypes

            buf = ctypes.create_unicode_buffer(64)
            ok = ctypes.windll.kernel32.GetVolumeInformationW(
                ctypes.c_wchar_p(str(volume_root(path))), None, 0, None, None, None, buf, len(buf)
            )
            return buf.value.lower() if ok else "unknown"
        if IS_MAC:
            out = subprocess.run(["mount"], capture_output=True, text=True, timeout=5).stdout
            root = str(volume_root(path))
            for line in out.splitlines():
                m = re.match(r"^.+? on (.+?) \(([^,)]+)", line)
                if m and m.group(1) == root:
                    return m.group(2).lower()
            return "unknown"
        root = str(volume_root(path))
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if parts[1].replace("\\040", " ") == root:
                    return parts[2].lower()
    except Exception:
        pass
    return "unknown"


def filesystem_advice(fs: str) -> tuple[str, str]:
    """(level, message) where level is 'ok', 'warn' or 'error'."""
    fs = fs.lower()
    if fs in ("exfat",):
        return "ok", "exFAT - readable and writable on both Mac and Windows."
    if fs in ("ntfs", "fuseblk", "ntfs3"):
        return "error", ("NTFS - a Mac can only READ this drive, so it can't save projects to it. "
                         "Back up the drive, then reformat it as exFAT.")
    if fs in ("apfs", "hfs", "hfs+", "hfsplus"):
        return "error", ("Mac-only format (APFS/HFS+) - Windows can't open this drive. "
                         "Back up the drive, then reformat it as exFAT.")
    if fs in ("msdos", "vfat", "fat32", "fat"):
        return "warn", ("FAT32 - works on both computers but can't hold files over 4 GB "
                        "(long recordings). exFAT is recommended.")
    return "warn", f"Filesystem '{fs}' - exFAT is recommended so both Mac and Windows can read and write it."


# --------------------------------------------------------------------------- opening things

def open_path(path: Path) -> None:
    """Open a file with its default application (an .als opens in Ableton Live)."""
    path = str(path)
    if IS_WINDOWS:
        os.startfile(path)  # type: ignore[attr-defined]
    elif IS_MAC:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def reveal_path(path: Path) -> None:
    path = Path(path)
    if IS_WINDOWS:
        subprocess.Popen(["explorer", "/select,", str(path)])
    elif IS_MAC:
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])


def ableton_is_running() -> bool:
    try:
        if IS_WINDOWS:
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                 timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            return re.search(r'"Ableton Live[^"]*\.exe"', out, re.I) is not None
        if IS_MAC:
            r = subprocess.run(["pgrep", "-x", "Live"], capture_output=True, timeout=5)
            return r.returncode == 0
    except Exception:
        pass
    return False


_WIN_SAVE_SCRIPT = r"""
$p = Get-Process | Where-Object { $_.MainWindowTitle -match 'Ableton Live' } | Select-Object -First 1
if (-not $p) { Write-Output 'NOT_RUNNING'; exit 0 }
$w = New-Object -ComObject WScript.Shell
[void]$w.AppActivate($p.Id)
Start-Sleep -Milliseconds 400
$w.SendKeys('^s')
Write-Output 'SENT'
"""

_MAC_SAVE_SCRIPT = """
tell application "System Events"
    if not (exists process "Live") then return "NOT_RUNNING"
    set frontmost of process "Live" to true
end tell
delay 0.4
tell application "System Events" to keystroke "s" using command down
return "SENT"
"""


def trigger_ableton_save() -> tuple[bool, str]:
    """Bring Ableton Live to the front and press Save (Ctrl+S / Cmd+S).

    Returns (sent, message). On macOS this needs Accessibility permission for
    Ableton Bridge (System Settings > Privacy & Security > Accessibility).
    """
    try:
        if IS_WINDOWS:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", _WIN_SAVE_SCRIPT],
                capture_output=True, text=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        elif IS_MAC:
            r = subprocess.run(["osascript", "-e", _MAC_SAVE_SCRIPT], capture_output=True, text=True, timeout=20)
        else:
            return False, "Automatic saving is only available on macOS and Windows."
    except Exception as exc:  # noqa: BLE001
        return False, f"Couldn't talk to Ableton Live ({exc})."
    out = (r.stdout or "").strip()
    if "SENT" in out:
        return True, "Pressed Save in Ableton Live."
    if "NOT_RUNNING" in out:
        return False, "Ableton Live isn't running - using the last saved version."
    err = (r.stderr or "").strip()
    if IS_MAC and ("not allowed" in err or "1002" in err or "assistive" in err.lower()):
        return False, ("macOS blocked the Save keystroke. Allow Ableton Bridge in System Settings > "
                       "Privacy & Security > Accessibility, or just press Cmd+S in Live before sending.")
    return False, f"Couldn't press Save in Ableton Live. {err[:200]}".strip()


def wait_for_file_change(paths_mtimes: dict[Path, float], roots_scan, timeout: float = 10.0) -> Path | None:
    """Poll until one of the given files gets a newer mtime (or a new .als appears)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for als in roots_scan():
            try:
                m = als.stat().st_mtime
            except OSError:
                continue
            if m > paths_mtimes.get(als, 0) + 0.001:
                time.sleep(0.8)  # let Live finish writing
                return als
        time.sleep(0.4)
    return None


def long_path(path: Path) -> Path:
    """On Windows, the '\\\\?\\' form of an absolute path, which isn't limited to 260 characters."""
    if not IS_WINDOWS:
        return Path(path)
    s = os.path.abspath(str(path))
    if s.startswith("\\\\?\\"):
        return Path(s)
    if s.startswith("\\\\"):  # network share
        return Path("\\\\?\\UNC\\" + s[2:])
    return Path("\\\\?\\" + s)


def copy_file(src: Path, dst: Path, progress=None, mtime: float | None = None) -> None:
    """Copy file contents atomically (temp file + rename) without macOS metadata.

    Plain data copy on purpose: copying extended attributes to an exFAT drive is
    what produces the "._filename" junk files.
    """
    src, dst = long_path(src), long_path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".abtmp")
    with open(src, "rb") as fin, open(tmp, "wb") as fout:
        while True:
            chunk = fin.read(4 * 1024 * 1024)
            if not chunk:
                break
            fout.write(chunk)
            if progress:
                progress(len(chunk))
        fout.flush()
        os.fsync(fout.fileno())
    os.replace(tmp, dst)
    st_m = src.stat().st_mtime if mtime is None else mtime
    try:
        os.utime(dst, (st_m, st_m))
    except OSError:
        pass


def free_space(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return -1
