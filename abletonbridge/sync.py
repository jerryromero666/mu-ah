"""The drive, and moving projects to and from it.

Drive layout::

    <drive>/AbletonBridge/
        bridge.json                     marks the drive
        Projects/<Song Project>/        plain Ableton project folders (open them directly if you like)
        _bridge/manifests/<name>.json   what was sent, by which computer, revision number
        _bridge/machines/<id>.json      each computer's plugins and Live version
        _bridge/locks/<name>.lock       present only while a send is in progress

Safety rules:

* Files are copied to a temporary name and renamed into place, Live Sets last
  and the manifest very last - unplugging the drive mid-copy never leaves a
  half-written set behind.
* A send is refused (until you choose what to do) if the other computer sent a
  newer version you haven't fetched yet. A fetch is refused if it would
  overwrite changes on this computer that were never sent.
* Before a Live Set on this computer is overwritten it is copied to the
  project's Backup folder.
* Nothing is ever deleted.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import platform_utils as pu
from .alsfile import find_sets
from .collect import CollectResult, collect_project, safe_filename
from .config import LocalStore, _atomic_write_json, read_json

MTIME_TOLERANCE = 2.1  # FAT/exFAT timestamps are coarse
EXCLUDED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".Spotlight-V100", ".Trashes", ".fseventsd",
                  ".TemporaryItems", "$RECYCLE.BIN", "System Volume Information"}


class BridgeError(Exception):
    pass


class Conflict(Exception):
    """Needs a decision from the user. ``options`` maps resolution id -> button label."""

    def __init__(self, kind: str, title: str, message: str, options: dict[str, str]):
        super().__init__(message)
        self.kind = kind
        self.title = title
        self.message = message
        self.options = options


def is_excluded(name: str) -> bool:
    return name in EXCLUDED_NAMES or name.startswith("._") or name.endswith((".abtmp", ".tmp"))


def snapshot(folder: Path) -> dict[str, list]:
    """{relative/posix/path: [size, mtime]} for every file in a project folder."""
    out: dict[str, list] = {}
    folder = Path(folder)
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not is_excluded(d)]
        rel_dir = Path(dirpath).relative_to(folder)
        for fn in filenames:
            if is_excluded(fn):
                continue
            p = Path(dirpath) / fn
            try:
                st = p.stat()
            except OSError:
                continue
            rel = (rel_dir / fn).as_posix()
            out[rel] = [st.st_size, round(st.st_mtime, 3)]
    return out


def same_meta(a, b) -> bool:
    return bool(a) and bool(b) and a[0] == b[0] and abs(a[1] - b[1]) <= MTIME_TOLERANCE


def changed_files(current: dict, baseline: dict) -> list[str]:
    return [rel for rel, meta in current.items() if not same_meta(meta, baseline.get(rel))]


def sets_changed_quick(project_dir: Path, baseline: dict) -> bool:
    """Cheap check used for the project list: did any Live Set change since the last sync?"""
    current = {}
    for s in find_sets(project_dir):
        st = s.stat()
        current[s.name] = [st.st_size, st.st_mtime]
    base_sets = {k: v for k, v in baseline.items() if "/" not in k and k.lower().endswith(".als")}
    if set(current) != set(base_sets):
        return True
    return any(not same_meta(v, base_sets.get(k)) for k, v in current.items())


def _fmt_time(ts: float | None) -> str:
    if not ts:
        return ""
    return time.strftime("%b %d, %H:%M", time.localtime(ts))


# --------------------------------------------------------------------------- the drive

class Drive:
    def __init__(self, bridge_dir: Path):
        self.root = Path(bridge_dir)
        self.projects_dir = self.root / "Projects"
        self.meta_dir = self.root / "_bridge"

    @classmethod
    def initialize(cls, location: Path) -> "Drive":
        location = Path(location)
        root = location if location.name == pu.BRIDGE_DIR_NAME else location / pu.BRIDGE_DIR_NAME
        root.mkdir(parents=True, exist_ok=True)
        marker = root / pu.BRIDGE_MARKER
        if not marker.exists():
            _atomic_write_json(marker, {"format": 1, "created": time.time()})
        drive = cls(root)
        for d in (drive.projects_dir, drive.meta_dir / "manifests", drive.meta_dir / "machines",
                  drive.meta_dir / "locks"):
            d.mkdir(parents=True, exist_ok=True)
        return drive

    def is_connected(self) -> bool:
        try:
            return (self.root / pu.BRIDGE_MARKER).is_file()
        except OSError:
            return False

    @property
    def label(self) -> str:
        vol = pu.volume_root(self.root)
        return vol.name or str(vol)

    def filesystem(self) -> str:
        return pu.filesystem_type(self.root)

    # manifests -----------------------------------------------------------------
    def _manifest_path(self, name: str) -> Path:
        return self.meta_dir / "manifests" / f"{name}.json"

    def manifest(self, name: str) -> dict | None:
        return read_json(self._manifest_path(name))

    def write_manifest(self, name: str, data: dict) -> None:
        _atomic_write_json(self._manifest_path(name), data)

    def manifests(self) -> dict[str, dict]:
        out = {}
        folder = self.meta_dir / "manifests"
        if folder.is_dir():
            for p in folder.glob("*.json"):
                if p.name.startswith("._"):
                    continue
                data = read_json(p)
                if data:
                    out[p.stem] = data
        return out

    # locks -----------------------------------------------------------------------
    def _lock_path(self, name: str) -> Path:
        return self.meta_dir / "locks" / f"{name}.lock"

    def lock(self, name: str, machine: str) -> None:
        _atomic_write_json(self._lock_path(name), {"by": machine, "at": time.time()})

    def unlock(self, name: str) -> None:
        try:
            self._lock_path(name).unlink()
        except FileNotFoundError:
            pass

    def lock_info(self, name: str) -> dict | None:
        return read_json(self._lock_path(name))

    # machines --------------------------------------------------------------------
    def write_machine(self, machine: dict) -> None:
        _atomic_write_json(self.meta_dir / "machines" / f"{machine['id']}.json", machine)

    def machines(self) -> list[dict]:
        folder = self.meta_dir / "machines"
        out = []
        if folder.is_dir():
            for p in folder.glob("*.json"):
                if not p.name.startswith("._"):
                    data = read_json(p)
                    if data and data.get("id"):
                        out.append(data)
        return out

    # housekeeping ----------------------------------------------------------------
    def clean_junk(self) -> int:
        """Delete macOS '._' / .DS_Store and Windows Thumbs.db litter inside the AbletonBridge folder."""
        removed = 0
        for dirpath, dirnames, filenames in os.walk(self.root):
            for fn in filenames:
                if fn.startswith("._") or fn in (".DS_Store", "Thumbs.db") or fn.endswith(".abtmp"):
                    try:
                        os.remove(os.path.join(dirpath, fn))
                        removed += 1
                    except OSError:
                        pass
        return removed


def find_drive(store: LocalStore) -> Drive | None:
    candidates = []
    own = pu.running_from_drive_bridge()
    if own:
        candidates.append(own)
    if store.settings.drive_path:
        candidates.append(Path(store.settings.drive_path))
    candidates += pu.find_bridge_drives()
    for c in candidates:
        d = Drive(c)
        if d.is_connected():
            return d
    return None


# --------------------------------------------------------------------------- results

@dataclass
class PushResult:
    drive_name: str
    revision: int
    main_set: str
    files_sent: int
    bytes_sent: int
    collect: CollectResult
    report: object = None


@dataclass
class PullResult:
    drive_name: str
    local_dir: Path
    main_set: Path | None
    files_received: int
    bytes_received: int
    backups: list[str] = field(default_factory=list)
    missing_on_drive: list[str] = field(default_factory=list)
    already_current: bool = False
    report: object = None


class _Progress:
    def __init__(self, total: int, cb):
        self.total = max(total, 1)
        self.done = 0
        self.cb = cb

    def __call__(self, n: int) -> None:
        self.done += n
        if self.cb:
            self.cb(min(self.done / self.total, 1.0))


# --------------------------------------------------------------------------- push

def push(store: LocalStore, drive: Drive, project_dir: Path, *, resolution: str | None = None,
         main_set: str | None = None, progress=None, log=None) -> PushResult:
    log = log or (lambda m: None)
    project_dir = Path(project_dir)
    me = store.settings
    sets = find_sets(project_dir)
    if not sets:
        raise BridgeError(f"There's no Live Set (.als) in {project_dir}.")
    main = main_set or sets[0].name

    drive_name = store.drive_name_for_local(project_dir) or safe_filename(project_dir.name)
    st = store.project_state(drive_name)
    manifest = drive.manifest(drive_name)

    if manifest and resolution is None:
        who = manifest.get("pushed_by", {}).get("name", "another computer")
        when = _fmt_time(manifest.get("pushed_at"))
        if st is None:
            raise Conflict(
                "name_taken", "A project with this name is already on the drive",
                f"The drive already has a project called \"{drive_name}\" (sent from {who}, {when}). "
                "It may be a different song with the same name.",
                {"keep_both": "Send mine as a separate copy", "overwrite": "Replace the one on the drive"})
        if manifest.get("revision", 0) > st.get("revision", 0):
            raise Conflict(
                "drive_newer", "The drive has a newer version",
                f"{who} sent a newer version of \"{drive_name}\" ({when}) that you haven't opened on "
                "this computer. Sending now would replace it.",
                {"keep_both": "Send mine as a separate copy", "overwrite": "Replace it with mine"})

    if resolution == "keep_both":
        base = f"{drive_name} ({me.machine_name})"
        drive_name, n = safe_filename(base), 2
        while drive.manifest(drive_name) is not None:
            drive_name = safe_filename(f"{base} {n}")
            n += 1
        manifest = None
        # this local folder now belongs to the new drive copy
        for name, pst in list(store.state["projects"].items()):
            if os.path.normcase(pst.get("local_path", "")) == os.path.normcase(str(project_dir)):
                del store.state["projects"][name]

    lock = drive.lock_info(drive_name)
    if lock and lock.get("by") != me.machine_name and time.time() - lock.get("at", 0) < 60:
        raise BridgeError(f"{lock.get('by')} is sending this project right now.")

    log(f"Collecting samples and devices into \"{project_dir.name}\"...")
    collect = collect_project(project_dir, search_dirs=[Path(d) for d in me.sample_search_dirs],
                              include_core_library=me.collect_core_library, log=log)
    if collect.files_copied:
        log(f"Collected {len(collect.files_copied)} file(s) into the project folder.")
    for err in collect.errors:
        log(f"Warning: {err}")

    snap = snapshot(project_dir)
    dest = drive.projects_dir / drive_name
    old = (manifest or {}).get("files", {})
    to_copy = []
    for rel, meta in snap.items():
        target = dest / Path(*rel.split("/"))
        try:
            on_drive_ok = target.is_file() and target.stat().st_size == meta[0]
        except OSError:
            on_drive_ok = False
        if not on_drive_ok or not same_meta(meta, old.get(rel)):
            to_copy.append(rel)
    to_copy.sort(key=lambda r: (r.lower().endswith(".als"), r))  # Live Sets last
    total = sum(snap[r][0] for r in to_copy)

    if manifest and not to_copy and set(snap) <= set(old) and manifest.get("main_set") == main:
        revision = manifest.get("revision", 0)
        store.set_project_state(drive_name, project_dir, revision, snap)
        log(f"\"{drive_name}\" on the drive is already up to date.")
        return PushResult(drive_name, revision, main, 0, 0, collect)

    free = pu.free_space(drive.root)
    if 0 <= free < total + 50 * 1024 * 1024:
        raise BridgeError(f"Not enough space on the drive: need {total / 1e9:.2f} GB, "
                          f"{free / 1e9:.2f} GB free.")

    log(f"Copying {len(to_copy)} changed file(s) to the drive ({total / 1e6:.1f} MB)...")
    prog = _Progress(total, progress)
    drive.lock(drive_name, me.machine_name)
    try:
        for rel in to_copy:
            pu.copy_file(project_dir / Path(*rel.split("/")), dest / Path(*rel.split("/")), progress=prog,
                         mtime=snap[rel][1])
        revision = (manifest or {}).get("revision", 0) + 1
        drive.write_manifest(drive_name, {
            "name": drive_name,
            "revision": revision,
            "main_set": main,
            "sets": [s.name for s in sets],
            "pushed_at": time.time(),
            "pushed_by": {"id": me.machine_id, "name": me.machine_name, "os": pu.os_key()},
            "files": snap,
        })
    finally:
        drive.unlock(drive_name)
    store.set_project_state(drive_name, project_dir, revision, snap)
    log(f"Sent \"{drive_name}\" to the drive (version {revision}).")
    return PushResult(drive_name, revision, main, len(to_copy), total, collect)


# --------------------------------------------------------------------------- pull

def pull(store: LocalStore, drive: Drive, drive_name: str, *, resolution: str | None = None,
         progress=None, log=None) -> PullResult:
    log = log or (lambda m: None)
    manifest = drive.manifest(drive_name)
    if not manifest:
        raise BridgeError(f"\"{drive_name}\" isn't on the drive.")
    src = drive.projects_dir / drive_name

    lock = drive.lock_info(drive_name)
    if lock and resolution is None:
        raise Conflict(
            "incomplete", "The last send didn't finish",
            f"{lock.get('by', 'The other computer')} started sending \"{drive_name}\" but it didn't finish "
            "(was the drive unplugged?). The newest files may be incomplete - send it again from that computer "
            "if you can.",
            {"continue": "Get the last complete version anyway"})

    st = store.project_state(drive_name)
    local = Path(st["local_path"]) if st else Path(store.settings.workspace) / drive_name
    revision = manifest.get("revision", 0)
    files: dict = manifest.get("files", {})
    backup_all = False

    if local.exists():
        snap = snapshot(local)
        if st:
            local_changes = changed_files(snap, st.get("files", {}))
            if local_changes and revision <= st.get("revision", 0):
                log("This computer already has the newest version (plus changes not sent yet).")
                return PullResult(drive_name, local, _main_set_path(local, manifest), 0, 0, already_current=True)
            if local_changes and resolution is None:
                raise Conflict(
                    "both_changed", "Changed on both computers",
                    f"\"{drive_name}\" was changed on this computer since it was last synced, and "
                    f"{manifest.get('pushed_by', {}).get('name', 'the other computer')} sent a newer version. "
                    f"Changed here: {', '.join(local_changes[:4])}{'...' if len(local_changes) > 4 else ''}",
                    {"backup_and_get": "Back up my changes, then get the drive version"})
        else:
            differs = changed_files({k: v for k, v in snap.items() if k in files}, files)
            if differs and resolution is None:
                raise Conflict(
                    "local_exists", "There's already a project with this name here",
                    f"{local} already exists on this computer and isn't the same as the drive version.",
                    {"backup_and_get": "Back up the local one, then get the drive version"})
        backup_all = resolution == "backup_and_get"
    else:
        snap = {}

    to_copy = [rel for rel, meta in files.items() if not same_meta(snap.get(rel), meta)]
    to_copy.sort(key=lambda r: (r.lower().endswith(".als"), r))
    if not to_copy and local.exists():
        store.set_project_state(drive_name, local, revision, snapshot(local))
        log("Already up to date.")
        return PullResult(drive_name, local, _main_set_path(local, manifest), 0, 0, already_current=True)

    total = sum(files[r][0] for r in to_copy)
    free = pu.free_space(local.parent if local.parent.exists() else Path.home())
    if 0 <= free < total + 50 * 1024 * 1024:
        raise BridgeError(f"Not enough space on this computer: need {total / 1e9:.2f} GB.")

    log(f"Copying {len(to_copy)} file(s) from the drive ({total / 1e6:.1f} MB)...")
    prog = _Progress(total, progress)
    stamp = time.strftime("%Y-%m-%d %H%M%S")
    backups, missing = [], []
    for rel in to_copy:
        s = src / Path(*rel.split("/"))
        d = local / Path(*rel.split("/"))
        if not s.is_file():
            missing.append(rel)
            continue
        if d.exists() and (backup_all or rel.lower().endswith(".als")):
            b = local / "Backup" / f"Before getting from drive {stamp}" / Path(*rel.split("/"))
            pu.copy_file(d, b)
            backups.append(rel)
        pu.copy_file(s, d, progress=prog, mtime=files[rel][1])
    if missing:
        log(f"Warning: {len(missing)} file(s) listed for this project are missing from the drive.")
    store.set_project_state(drive_name, local, revision, snapshot(local))
    log(f"Got \"{drive_name}\" version {revision} -> {local}")
    return PullResult(drive_name, local, _main_set_path(local, manifest), len(to_copy) - len(missing), total,
                      backups, missing)


def _main_set_path(local: Path, manifest: dict) -> Path | None:
    main = manifest.get("main_set")
    if main and (local / main).is_file():
        return local / main
    sets = find_sets(local)
    return sets[0] if sets else None


# --------------------------------------------------------------------------- project list

STATUS_TEXT = {
    "synced": "In sync",
    "local_newer": "Changes to send",
    "drive_newer": "Newer version on drive",
    "conflict": "Changed on both computers",
    "local_only": "Only on this computer",
    "drive_only": "Only on the drive",
    "unlinked": "Same name on drive",
}


@dataclass
class ProjectRow:
    name: str  # drive name (or folder name if never sent)
    status: str
    local_dir: Path | None = None
    manifest: dict | None = None
    local_mtime: float = 0.0

    @property
    def status_text(self) -> str:
        return STATUS_TEXT[self.status]

    @property
    def sent_by(self) -> str:
        return (self.manifest or {}).get("pushed_by", {}).get("name", "")

    @property
    def sent_at(self) -> float:
        return (self.manifest or {}).get("pushed_at", 0.0)


def discover_projects(roots: list[Path], max_depth: int = 4, skip: list[Path] | None = None) -> list[Path]:
    found, seen = [], set()
    skip_norm = {os.path.normcase(str(Path(s).resolve())) for s in (skip or [])}
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            if os.path.normcase(str(here.resolve())) in skip_norm:
                dirnames[:] = []
                continue
            if any(f.lower().endswith(".als") and not f.startswith("._") for f in filenames):
                key = os.path.normcase(str(here.resolve()))
                if key not in seen:
                    seen.add(key)
                    found.append(here)
                dirnames[:] = []
                continue
            if len(here.parts) - base_depth >= max_depth:
                dirnames[:] = []
            else:
                dirnames[:] = [d for d in dirnames if not d.startswith((".", "_")) and d != "Backup"
                               and not d.endswith((".app", ".bundle"))]
    return found


def newest_set(roots: list[Path], skip: list[Path] | None = None) -> Path | None:
    best, best_m = None, -1.0
    for proj in discover_projects(roots, skip=skip):
        for s in find_sets(proj)[:1]:
            m = s.stat().st_mtime
            if m > best_m:
                best, best_m = s, m
    return best


def project_rows(store: LocalStore, drive: Drive | None) -> list[ProjectRow]:
    manifests = drive.manifests() if drive else {}
    roots = [Path(r) for r in store.settings.project_roots] + [Path(store.settings.workspace)]
    extra = [Path(p["local_path"]) for p in store.state["projects"].values()]
    local_dirs = discover_projects(roots, skip=[drive.root] if drive else None)
    seen = {os.path.normcase(str(d.resolve())) for d in local_dirs}
    for e in extra:
        if e.is_dir() and os.path.normcase(str(e.resolve())) not in seen and find_sets(e):
            local_dirs.append(e)

    rows: dict[str, ProjectRow] = {}
    for d in local_dirs:
        name = store.drive_name_for_local(d) or safe_filename(d.name)
        st = store.project_state(name)
        man = manifests.get(name)
        sets = find_sets(d)
        mtime = sets[0].stat().st_mtime if sets else 0.0
        if man is None:
            status = "local_only"
        elif st is None:
            status = "unlinked"
        else:
            local_changed = sets_changed_quick(d, st.get("files", {}))
            drive_newer = man.get("revision", 0) > st.get("revision", 0)
            status = ("conflict" if local_changed and drive_newer else "drive_newer" if drive_newer
                      else "local_newer" if local_changed else "synced")
        if name in rows:  # two local folders with the same name: keep the newest
            if rows[name].local_mtime >= mtime:
                continue
        rows[name] = ProjectRow(name, status, d, man, mtime)
    for name, man in manifests.items():
        if name not in rows:
            rows[name] = ProjectRow(name, "drive_only", None, man)
    return sorted(rows.values(), key=lambda r: max(r.local_mtime, r.sent_at), reverse=True)


def machine_record(store: LocalStore, inventory: dict, live_versions: list[str]) -> dict:
    return {
        "id": store.settings.machine_id,
        "name": store.settings.machine_name,
        "os": pu.os_key(),
        "updated_at": time.time(),
        "live_versions": live_versions,
        "plugins": inventory,
    }


def export_json(obj) -> str:
    return json.dumps(obj, indent=2, default=str)
