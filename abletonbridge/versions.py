"""Every saved version of a project, and which computer saved it.

Versions come from:
* the project's current Live Sets,
* Live's own automatic backups (Backup/Song [2026-10-17 141502].als),
* the app's backups made before replacing your files (Backup/Before get <date>/ on this
  computer, Backup/Before send <date>/ in the project folder on the drive),
* a newer version waiting on the drive.

Opening a version never overwrites anything: the set is copied next to the current one under a
name that says where it came from (if the backup also holds sounds that changed, the version is
opened as a separate copy of the whole project).
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import platform_utils as pu
from .alsfile import find_sets
from .collect import safe_filename
from .config import LocalStore, read_json
from .sync import (BACKUP_INFO, THIS_COMPUTER, Drive, _free_folder, drive_saved_at, is_excluded, manifest_origins,
                   same_meta)

KIND_LABEL = {
    "current": "Current version",
    "live_backup": "Live's own backup",
    "before_pack": "Before samples were packed in",
    "before_get": "Kept - replaced by the drive's",
    "before_send": "Kept - replaced on the drive",
    "drive_newer": "Newer, on the drive",
}

PACK_SUFFIX = "(before Ableton Bridge)"
_LIVE_STAMP = re.compile(r"\[(\d{4})-(\d{2})-(\d{2}) (\d{2})(\d{2})(\d{2})\]")


@dataclass
class Version:
    set_path: Path
    computer: str
    when: float
    kind: str
    bundle: Path | None = None  # backup folder (may also hold changed sounds)
    on_drive: bool = False

    @property
    def kind_label(self) -> str:
        return KIND_LABEL[self.kind]

    @property
    def name(self) -> str:
        """The set's name without Live's [date] stamp (the date has its own column)."""
        stem = re.sub(r"\s+", " ", _LIVE_STAMP.sub("", self.set_path.stem).replace(PACK_SUFFIX, "")).strip()
        return stem + self.set_path.suffix


def _stamp_time(name: str, fallback: float) -> float:
    m = _LIVE_STAMP.search(name)
    if not m:
        return fallback
    try:
        return time.mktime(tuple(int(x) for x in m.groups()) + (0, 0, -1))
    except (OverflowError, ValueError):
        return fallback


def _bundle_versions(bundle_root: Path, kind: str, on_drive: bool, default_computer: str) -> list[Version]:
    out = []
    if not bundle_root.is_dir():
        return out
    prefix = "Before get " if kind == "before_get" else "Before send "
    for bundle in sorted(bundle_root.glob(prefix + "*")):
        if not bundle.is_dir():
            continue
        info = read_json(bundle / BACKUP_INFO) or {}
        files = info.get("files", {})
        for s in find_sets(bundle):
            out.append(Version(s, files.get(s.name) or info.get("made_on") or default_computer,
                               s.stat().st_mtime, kind, bundle, on_drive))
    return out


def list_versions(store: LocalStore, drive: Drive | None, drive_name: str, local_dir: Path | None) -> list[Version]:
    me = store.settings.machine_name
    st = store.project_state(drive_name) or {}
    origins: dict = st.get("origins", {})
    baseline: dict = st.get("files", {})
    out: list[Version] = []

    def computer_of(rel: str, path: Path) -> str:
        meta = [path.stat().st_size, path.stat().st_mtime]
        if rel in origins and same_meta(meta, baseline.get(rel)):
            o = origins[rel]
            return o + THIS_COMPUTER if o == me else o
        return me + THIS_COMPUTER  # changed (or created) here since the last sync

    if local_dir and local_dir.is_dir():
        for s in find_sets(local_dir):
            out.append(Version(s, computer_of(s.name, s), s.stat().st_mtime, "current"))
        backup = local_dir / "Backup"
        if backup.is_dir():
            for s in sorted(backup.glob("*.als")):
                if not is_excluded(s.name):
                    kind = "before_pack" if PACK_SUFFIX in s.name else "live_backup"
                    out.append(Version(s, computer_of(f"Backup/{s.name}", s),
                                       _stamp_time(s.name, s.stat().st_mtime), kind))
            out += _bundle_versions(backup, "before_get", False, me + THIS_COMPUTER)

    if drive and drive.is_connected():
        manifest = drive.manifest(drive_name)
        if manifest:
            proj = drive.project_path(drive_name, manifest)
            if manifest.get("revision", 0) > st.get("revision", 0):
                main = proj / manifest.get("main_set", "")
                if main.is_file():
                    who = manifest_origins(manifest).get(main.name) or manifest.get("pushed_by", {}).get("name", "?")
                    out.append(Version(main, who, drive_saved_at(manifest), "drive_newer", on_drive=True))
            out += _bundle_versions(proj / "Backup", "before_send", True, "?")

    for v in out:
        if v.computer == me:
            v.computer = me + THIS_COMPUTER
    return sorted(out, key=lambda v: v.when, reverse=True)


def open_version(v: Version, local_dir: Path | None, log=None) -> Path:
    """Make a copy of this version next to the current project (never overwriting) and return its set."""
    log = log or (lambda m: None)
    if v.kind == "current":
        return v.set_path
    who = v.computer.replace(THIS_COMPUTER, "")
    date = time.strftime("%b %d %H%M", time.localtime(v.when))
    stem = v.set_path.stem
    stem = re.sub(r"\s+", " ", _LIVE_STAMP.sub("", stem).replace(PACK_SUFFIX, "")).strip()
    new_name = safe_filename(f"{stem} ({who} {date}).als", max_len=200)

    if local_dir is None or not local_dir.is_dir():
        raise ValueError("Get the project to this computer first.")
    changed_sounds = v.bundle is not None and any(
        p.is_file() and p.suffix.lower() != ".als" and p.name != BACKUP_INFO for p in v.bundle.rglob("*"))
    if changed_sounds:
        # The version used different sounds than the current project: open it as a separate project copy.
        base = local_dir
        target = _free_folder(base.parent / safe_filename(f"{base.name} ({who} {date})", max_len=200))
        shutil.copytree(pu.long_path(base), pu.long_path(target),
                        ignore=shutil.ignore_patterns("Backup", "._*", ".DS_Store"))
        if v.bundle is not None:
            for p in v.bundle.rglob("*"):
                if p.is_file() and p.name != BACKUP_INFO and p.suffix.lower() != ".als":
                    pu.copy_file(p, target / p.relative_to(v.bundle))
        dest = target / new_name
        pu.copy_file(v.set_path, dest, mtime=time.time())
        log(f"Opened that version as a separate copy: {target.name}")
        return dest
    dest = local_dir / new_name
    n = 2
    while dest.exists():
        dest = local_dir / safe_filename(f"{stem} ({who} {date}) {n}.als", max_len=200)
        n += 1
    pu.copy_file(v.set_path, dest, mtime=time.time())
    log(f"Opened that version as \"{dest.name}\" next to your current set (nothing was overwritten).")
    return dest
