"""Know which plugins are installed on each computer.

Each computer scans its plugin folders (and Live's own plugin database when it
can read it) and leaves the list on the drive. Before a project leaves one
computer, and again before it opens on the other, every plugin the set uses is
checked against the other computer's list.
"""

from __future__ import annotations

import glob
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .alsfile import PluginRef
from .platform_utils import IS_MAC, IS_WINDOWS

FORMAT_EXT = {".vst3": "vst3", ".dll": "vst2", ".vst": "vst2", ".component": "au"}


def default_plugin_dirs() -> list[tuple[str, Path]]:
    home = Path.home()
    if IS_WINDOWS:
        pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        cf = Path(os.environ.get("CommonProgramFiles", pf / "Common Files"))
        return [
            ("vst3", cf / "VST3"),
            ("vst3", Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "Programs" / "Common" / "VST3"),
            ("vst2", pf / "VSTPlugins"),
            ("vst2", pf / "Steinberg" / "VSTPlugins"),
            ("vst2", cf / "VST2"),
            ("vst2", cf / "Steinberg" / "VST2"),
            ("vst2", pf / "Vstplugins"),
        ]
    if IS_MAC:
        dirs = []
        for base in (Path("/Library/Audio/Plug-Ins"), home / "Library" / "Audio" / "Plug-Ins"):
            dirs += [("vst3", base / "VST3"), ("vst2", base / "VST"), ("au", base / "Components")]
        return dirs
    return [("vst3", home / ".vst3"), ("vst3", Path("/usr/lib/vst3")), ("vst2", home / ".vst")]


def _scan_dir(fmt_hint: str, folder: Path, max_depth: int = 5) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not folder.is_dir():
        return out
    base_depth = len(folder.parts)
    for dirpath, dirnames, filenames in os.walk(folder):
        here = Path(dirpath)
        keep = []
        for d in dirnames:
            ext = os.path.splitext(d)[1].lower()
            if ext in FORMAT_EXT:
                out.append((FORMAT_EXT[ext], os.path.splitext(d)[0]))  # bundle: don't descend
            elif len(here.parts) - base_depth < max_depth and not d.startswith("."):
                keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            ext = os.path.splitext(f)[1].lower()
            if ext == ".vst3":
                out.append(("vst3", os.path.splitext(f)[0]))
            elif ext == ".dll" and fmt_hint == "vst2":
                out.append(("vst2", os.path.splitext(f)[0]))
    return out


def live_database_files() -> list[Path]:
    home = Path.home()
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "Ableton" / "Live Database"
    elif IS_MAC:
        base = home / "Library" / "Application Support" / "Ableton" / "Live Database"
    else:
        return []
    return [Path(p) for p in glob.glob(str(base / "Live-plugins-*.db"))]


def read_live_database(db_path: Path) -> list[tuple[str, str]]:
    """Plugin names Live itself found when it scanned plugins (includes shell plugins like Waves).

    The database layout isn't documented, so this only reads it when it looks as
    expected and quietly returns nothing otherwise.
    """
    out: list[tuple[str, str]] = []
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True, timeout=2)
    except (sqlite3.Error, ValueError):
        return out
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        if "plugins" not in tables:
            return out
        cols = [r[1] for r in con.execute("PRAGMA table_info(plugins)")]
        if "name" not in cols:
            return out
        ident = "dev_identifier" if "dev_identifier" in cols else None
        query = f"SELECT name{', ' + ident if ident else ''} FROM plugins"
        for row in con.execute(query):
            name = row[0]
            if not isinstance(name, str) or not name.strip():
                continue
            fmt = "any"
            if ident and isinstance(row[1], str):
                low = row[1].lower()
                if "vst3" in low:
                    fmt = "vst3"
                elif "vst" in low:
                    fmt = "vst2"
                elif low.startswith("device:au") or ":au:" in low:
                    fmt = "au"
            out.append((fmt, name.strip()))
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return out


def scan_plugins(extra_dirs: list[str] | None = None) -> dict:
    """Return this computer's plugin inventory as a JSON-friendly dict."""
    found: dict[str, set[str]] = {"vst3": set(), "vst2": set(), "au": set(), "any": set()}
    dirs = default_plugin_dirs() + [("vst2", Path(d)) for d in (extra_dirs or [])]
    for fmt_hint, folder in dirs:
        for fmt, name in _scan_dir(fmt_hint, folder):
            found[fmt].add(name)
    db_count = 0
    for db in live_database_files():
        for fmt, name in read_live_database(db):
            found[fmt].add(name)
            db_count += 1
    return {
        "scanned_at": time.time(),
        "folders": [str(d) for _, d in dirs if d.is_dir()],
        "live_database_entries": db_count,
        **{fmt: sorted(names, key=str.lower) for fmt, names in found.items()},
    }


# --------------------------------------------------------------------------- matching

_NOISE = re.compile(
    r"\.(vst3|vst|dll|component)$|\(x64\)|\bx64\b|\bx86_64\b|\b64[- ]?bit\b|_x64|_64\b|\bvst3?\b|\(vst3?\)|\bstereo\b|\bmono\b",
    re.I)


def normalize(name: str) -> str:
    n = _NOISE.sub(" ", name.lower())
    return re.sub(r"[^a-z0-9]+", "", n)


def names_match(a: str, b: str) -> bool:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = sorted((na, nb), key=len)
    # "Pro-Q 3" vs "FabFilter Pro-Q 3": vendor prefix on one side only.
    return len(short) >= 4 and long_.endswith(short)


@dataclass
class PluginStatus:
    level: str  # "ok", "warn", "missing", "unknown"
    text: str


def _has(inventory: dict, fmt: str, plugin: PluginRef) -> bool:
    names = list(inventory.get(fmt, [])) + list(inventory.get("any", []))
    candidates = [plugin.name]
    if plugin.path:
        candidates.append(re.split(r"[\\/]", plugin.path.rstrip("/\\"))[-1])
    return any(names_match(c, n) for c in candidates for n in names)


def _has_shell(inventory: dict) -> bool:
    return any("shell" in n.lower() for fmt in ("vst3", "vst2") for n in inventory.get(fmt, []))


def check_plugin(plugin: PluginRef, inventory: dict | None, target_os: str) -> PluginStatus:
    """How will *plugin* fare on a computer with this inventory and OS ('mac'/'windows')?"""
    frozen_note = ""
    if plugin.tracks and set(plugin.frozen_tracks) >= set(plugin.tracks):
        frozen_note = " Its tracks are frozen, so they'll still play."

    if plugin.format == "au" and target_os != "mac":
        alt = ""
        if inventory and (_has(inventory, "vst3", plugin) or _has(inventory, "vst2", plugin)):
            alt = " The VST version is installed there - swap to it in Live on the Mac."
        level = "warn" if frozen_note else "missing"
        return PluginStatus(level, "Audio Units only work on a Mac." + alt + frozen_note)

    if inventory is None:
        return PluginStatus("unknown", "That computer hasn't scanned its plugins yet "
                                       "(open Ableton Bridge on it once with the drive plugged in).")

    if _has(inventory, plugin.format, plugin):
        return PluginStatus("ok", "Installed")

    others = [f for f in ("vst3", "vst2", "au") if f != plugin.format and _has(inventory, f, plugin)]
    if others:
        label = {"vst3": "VST3", "vst2": "VST2", "au": "Audio Unit"}
        return PluginStatus("warn", f"Installed, but only as {', '.join(label[o] for o in others)} - "
                                    f"the set uses the {plugin.format_label}. Live may load it as missing; "
                                    "use the same format on both computers (VST3 is best)." + frozen_note)
    if _has_shell(inventory):
        return PluginStatus("unknown", "Not found by name - it may be inside a plugin shell "
                                       "(e.g. Waves WaveShell)." + frozen_note)
    level = "warn" if frozen_note else "missing"
    return PluginStatus(level, "Not installed." + (frozen_note or " Install it there, or freeze the track on the "
                                                                  "computer that has it, then send again."))
