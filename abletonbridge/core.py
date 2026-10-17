"""High-level actions used by both the window and the command line."""

from __future__ import annotations

import time
from pathlib import Path

from . import platform_utils as pu
from .alsfile import find_sets
from .config import LocalStore
from .plugins import scan_plugins
from .report import Machine, ProjectReport, build_report, installed_live_versions, machines_from_drive
from .sync import (BridgeError, Drive, PullResult, PushResult, discover_projects, find_drive, machine_record,
                   newest_set, project_rows, pull, push)

INVENTORY_MAX_AGE = 12 * 3600


class Bridge:
    def __init__(self, store: LocalStore | None = None):
        self.store = store or LocalStore()
        self.drive: Drive | None = None
        self.refresh_drive()

    # ------------------------------------------------------------------ drive
    def refresh_drive(self) -> Drive | None:
        self.drive = find_drive(self.store)
        if self.drive and str(self.drive.root) != self.store.settings.drive_path:
            self.store.settings.drive_path = str(self.drive.root)
            self.store.save_settings()
        return self.drive

    def setup_drive(self, location: Path) -> tuple[Drive, str, str]:
        drive = Drive.initialize(Path(location))
        self.drive = drive
        self.store.settings.drive_path = str(drive.root)
        self.store.save_settings()
        level, advice = pu.filesystem_advice(drive.filesystem())
        return drive, level, advice

    def require_drive(self) -> Drive:
        if not self.drive or not self.drive.is_connected():
            self.refresh_drive()
        if not self.drive:
            raise BridgeError("The transfer drive isn't plugged in (or hasn't been set up yet).")
        return self.drive

    # ------------------------------------------------------------------ this computer
    def inventory_path(self) -> Path:
        return self.store.base / "plugins.json"

    def local_machine(self, rescan: bool = False, log=None) -> dict:
        from .config import _atomic_write_json, read_json

        cached = read_json(self.inventory_path())
        if rescan or not cached or time.time() - cached.get("updated_at", 0) > INVENTORY_MAX_AGE:
            if log:
                log("Scanning this computer's plugins...")
            inv = scan_plugins(self.store.settings.extra_plugin_dirs)
            cached = machine_record(self.store, inv, installed_live_versions())
            _atomic_write_json(self.inventory_path(), cached)
            if log:
                total = sum(len(inv[k]) for k in ("vst3", "vst2", "au"))
                log(f"Found {total} plugin files"
                    + (f" and {inv['live_database_entries']} entries in Live's plugin list." if inv.get(
                        "live_database_entries") else "."))
        cached["name"] = self.store.settings.machine_name
        return cached

    def publish_machine(self, rescan: bool = False, log=None) -> None:
        rec = self.local_machine(rescan=rescan, log=log)
        if self.drive and self.drive.is_connected():
            self.drive.write_machine(rec)

    def machines(self) -> list[Machine]:
        """This computer plus every other computer that left its plugin list on the drive."""
        me = self.local_machine()
        records = {me["id"]: me}
        if self.drive and self.drive.is_connected():
            for r in self.drive.machines():
                if r["id"] != me["id"]:
                    records[r["id"]] = r
        return machines_from_drive(list(records.values()))

    def other_machines(self) -> list[Machine]:
        return [m for m in self.machines() if m.id != self.store.settings.machine_id]

    # ------------------------------------------------------------------ projects
    def roots(self) -> list[Path]:
        return [Path(r) for r in self.store.settings.project_roots] + [Path(self.store.settings.workspace)]

    def rows(self, rescan: bool = False):
        return project_rows(self.store, self.drive, rescan=rescan)

    def current_project(self) -> Path | None:
        """The project whose Live Set was saved most recently on this computer."""
        s = newest_set(self.roots(), skip=[self.drive.root] if self.drive else None)
        return s.parent if s else None

    def save_in_live_then_find(self, log=None) -> Path | None:
        """Press Save in Live (if enabled) and return the project that was just saved."""
        log = log or (lambda m: None)
        if self.store.settings.auto_save_in_live and pu.ableton_is_running():
            before = {}
            skip = [self.drive.root] if self.drive else None

            def all_sets():
                out = []
                for proj in discover_projects(self.roots(), skip=skip):
                    out.extend(find_sets(proj))
                return out

            for s in all_sets():
                before[s] = s.stat().st_mtime
            sent, msg = pu.trigger_ableton_save()
            log(msg)
            if sent:
                saved = pu.wait_for_file_change(before, all_sets, timeout=8)
                if saved:
                    log(f"Live saved \"{saved.name}\".")
                    return saved.parent
                log("Live didn't save a set in a known project folder (is it a new, unsaved set? "
                    "Save it once in Live first, or add its folder in Settings). Using the newest saved project.")
        return self.current_project()

    def send(self, project_dir: Path, resolution=None, progress=None, log=None) -> PushResult:
        drive = self.require_drive()
        result = push(self.store, drive, project_dir, resolution=resolution, progress=progress, log=log)
        self.publish_machine(log=log)
        result.report = build_report(project_dir, self.machines(),
                                     search_dirs=self.store.settings.sample_search_dirs)
        return result

    def get(self, drive_name: str, resolution=None, progress=None, log=None) -> PullResult:
        drive = self.require_drive()
        dest = None
        rows = self.rows()  # also re-links project folders that were moved on this computer
        if self.store.project_state(drive_name) is None:
            # Same project already on this computer (e.g. copied by hand before)? Update that copy.
            # Folders known to hold a different song aren't offered (see pull()); with several
            # same-name folders, prefer the one whose parent folder matches the drive location.
            cands = [r.local_dir for r in rows if r.name == drive_name and r.status == "unlinked"]
            parts = (drive.manifest(drive_name) or {}).get("path", "").split("/")
            parent = parts[-2] if len(parts) >= 2 else ""
            cands.sort(key=lambda d: d.parent.name.lower() != parent.lower())
            dest = cands[0] if cands else None
        result = pull(self.store, drive, drive_name, resolution=resolution, dest=dest, progress=progress,
                      log=log)
        me = [m for m in self.machines() if m.id == self.store.settings.machine_id]
        result.report = build_report(result.local_dir, me, search_dirs=self.store.settings.sample_search_dirs)
        return result

    def check(self, project_dir: Path) -> ProjectReport:
        return build_report(project_dir, self.machines(), search_dirs=self.store.settings.sample_search_dirs)

    def latest_from_other_computer(self) -> str | None:
        """Drive project most recently sent by another computer (falls back to most recent overall)."""
        drive = self.require_drive()
        mans = drive.manifests()
        if not mans:
            return None
        mine = self.store.settings.machine_id
        others = {k: v for k, v in mans.items() if v.get("pushed_by", {}).get("id") != mine}
        pool = others or mans
        return max(pool, key=lambda k: pool[k].get("pushed_at", 0))

    def open_set(self, set_path: Path) -> None:
        pu.open_path(set_path)
