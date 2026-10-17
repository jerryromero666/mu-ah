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
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import platform_utils as pu
from .alsfile import find_sets
from .collect import CollectResult, collect_project, file_sha1, name_is_portable, safe_filename
from .config import LocalStore, _atomic_write_json, read_json

MTIME_TOLERANCE = 2.1  # FAT/exFAT timestamps are coarse
EXCLUDED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".Spotlight-V100", ".Trashes", ".fseventsd",
                  ".TemporaryItems", "$RECYCLE.BIN", "System Volume Information", "Icon\r"}
ID_FILE = ("Ableton Project Info", "AbletonBridge.json")


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


def _resolutions(resolution) -> set[str]:
    """Answers the user already gave to this action's questions (one string or several)."""
    if not resolution:
        return set()
    if isinstance(resolution, str):
        return {resolution}
    return set(resolution)


# ---- project identity -------------------------------------------------------------
# A small file inside the project ("Ableton Project Info/AbletonBridge.json") holds a random id.
# It travels with the project, so the app can tell "the same song" from "a different song that
# happens to have the same folder name" (Ableton names every new project "Untitled Project").

def read_project_id(folder: Path) -> str | None:
    data = read_json(Path(folder).joinpath(*ID_FILE))
    pid = (data or {}).get("id")
    return pid if isinstance(pid, str) and pid else None


def write_project_id(folder: Path, pid: str) -> None:
    _atomic_write_json(Path(folder).joinpath(*ID_FILE), {"id": pid, "note": "Used by Ableton Bridge to recognise "
                                                                            "this project. Safe to ignore."})


def ensure_project_id(folder: Path) -> str:
    pid = read_project_id(folder)
    if not pid:
        pid = uuid.uuid4().hex
        write_project_id(folder, pid)
    return pid


def _name_key(rel: str) -> str:
    """Names that the drive / Windows / macOS treat as the same file."""
    return unicodedata.normalize("NFC", rel).casefold()


def drive_names(project_dir: Path, snap: dict[str, list], log=None) -> dict[str, str]:
    """Map each local file (project-relative) to the name it gets on the drive.

    Names the drive or Windows can't store ("What If?.als", "Mix 10:17.wav", a folder ending
    in a space...) get a safe name instead of making the whole Send fail. A file whose only
    problem is its name and that already exists in the project under a safe name (Ableton
    Bridge made that copy for the sets) is left on this computer.
    """
    log = log or (lambda m: None)
    mapping: dict[str, str] = {}
    taken: dict[str, str] = {}
    for rel in sorted(r for r in snap if name_is_portable(r)):
        key = _name_key(rel)
        if key in taken:
            raise BridgeError(
                f"Two files in this project have names that only differ in capital letters or accents: "
                f"\"{taken[key]}\" and \"{rel}\". The drive and the other computer would treat them as one "
                "file - rename or delete one, then send again.")
        taken[key] = rel
        mapping[rel] = rel
    for rel in sorted(r for r in snap if not name_is_portable(r)):
        parts = rel.split("/")
        safe = [safe_filename(p, max_len=255) for p in parts]
        cand = "/".join(safe)
        twin = taken.get(_name_key(cand))
        if twin and snap[twin][0] == snap[rel][0] and _same_bytes(project_dir, twin, rel):
            log(f"Not sending \"{rel}\" - its name doesn't work on Windows and the project already has "
                f"the same file as \"{twin}\".")
            continue
        n = 2
        stem, dot, ext = safe[-1].rpartition(".")
        if not dot:
            stem, ext = safe[-1], ""
        while _name_key(cand) in taken:
            last = f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"
            cand = "/".join(safe[:-1] + [last])
            n += 1
        taken[_name_key(cand)] = rel
        mapping[rel] = cand
        log(f"\"{rel}\" is stored on the drive as \"{cand}\" (its name doesn't work on Windows).")
    return mapping


def _same_bytes(project_dir: Path, a: str, b: str) -> bool:
    try:
        return file_sha1(project_dir / Path(*a.split("/"))) == file_sha1(project_dir / Path(*b.split("/")))
    except OSError:
        return False


def snapshot(folder: Path) -> dict[str, list]:
    """{relative/posix/path: [size, mtime]} for every file in a project folder."""
    out: dict[str, list] = {}
    folder = pu.long_path(Path(folder))
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
    if not (a and b) or a[0] != b[0]:
        return False
    diff = abs(a[1] - b[1])
    if diff <= MTIME_TOLERANCE:
        return True
    # FAT drives store local time, so the same file can look whole hours older/newer on the other
    # computer (time zone / daylight saving). Same size + whole-hour shift = same file.
    hours = round(diff / 3600)
    return 1 <= hours <= 14 and abs(diff - hours * 3600) <= MTIME_TOLERANCE


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

UNTRACKED_SENDER = {"id": "drive", "name": "already on the drive", "os": ""}
EDITED_ON_DRIVE = {"id": "drive", "name": "edited on the drive", "os": ""}
SCAN_CACHE_SECONDS = 30


def drive_sets_changed(project_dir: Path, manifest: dict) -> bool:
    """Was a Live Set in this drive folder saved by something other than Ableton Bridge
    (e.g. Live opened the project straight from the drive)?

    Old sets left on the drive that are no longer in the manifest don't count; a set
    that's newer than the last send does.
    """
    files = manifest.get("files", {})
    sent_at = manifest.get("pushed_at", 0)
    for s in find_sets(project_dir):
        try:
            st = s.stat()
        except OSError:
            continue
        known = files.get(s.name)
        if known is not None:
            if not same_meta([st.st_size, st.st_mtime], known):
                return True
        elif st.st_mtime > sent_at + 5:
            return True
    return False


class Drive:
    def __init__(self, bridge_dir: Path):
        self.root = Path(bridge_dir)
        self.projects_dir = self.root / "Projects"
        self.meta_dir = self.root / "_bridge"
        self._scanned_at = 0.0
        self.scan_notes: list[str] = []

    @property
    def home(self) -> Path:
        """The folder the AbletonBridge folder sits in (normally the top of the drive)."""
        return self.root.parent

    @property
    def volume(self) -> Path:
        return pu.volume_root(self.root)

    @property
    def search_roots(self) -> list[Path]:
        """Where '/Volumes/<drive>/...' or 'E:/...' paths written by the other computer map to here."""
        roots = [self.home]
        if os.path.normcase(str(self.volume)) != os.path.normcase(str(self.home)):
            roots.append(self.volume)
        return roots

    def project_path(self, name: str, manifest: dict | None = None) -> Path:
        """Where a project lives on the drive: its original folder for projects that were already
        on the drive, AbletonBridge/Projects/<name> for ones sent by the app."""
        manifest = manifest if manifest is not None else self.manifest(name)
        rel = (manifest or {}).get("path")
        if rel:
            return self.home / Path(*rel.split("/"))
        return self.projects_dir / name

    def rel_path(self, folder: Path) -> str:
        return Path(folder).relative_to(self.home).as_posix()

    def scan_existing(self, force: bool = False, log=None) -> list[str]:
        """Find Ableton projects anywhere on the drive that the app doesn't know about yet,
        and notice projects that were edited directly on the drive. Returns names it added/updated."""
        if not force and time.time() - self._scanned_at < SCAN_CACHE_SECONDS:
            return []
        self._scanned_at = time.time()
        self.scan_notes = []
        manifests = self.manifests()
        by_path = {}
        for name, m in manifests.items():
            by_path[os.path.normcase(str(self.project_path(name, m)))] = name
        taken = {n.lower() for n in manifests}
        changed = []
        if find_sets(self.home):
            self.scan_notes.append("There are Live Sets loose at the top of the drive - put each song in "
                                   "its own folder so Ableton Bridge can pick it up.")
        for proj in discover_projects([self.home], max_depth=8, skip=[self.meta_dir]):
            key = os.path.normcase(str(proj))
            name = by_path.get(key)
            if name:
                if self.refresh_project(name, manifests[name]):
                    changed.append(name)
                    if log:
                        log(f"\"{name}\" was changed directly on the drive - it now shows as a newer version.")
                continue
            name = self._unique_name(proj, taken)
            taken.add(name.lower())
            sets = find_sets(proj)
            self.write_manifest(name, {
                "name": name,
                "id": uuid.uuid4().hex,
                "path": self.rel_path(proj),
                "revision": 1,
                "main_set": sets[0].name,
                "sets": [s.name for s in sets],
                "pushed_at": _newest_set_time(proj),
                "pushed_by": UNTRACKED_SENDER,
                "files": snapshot(proj),
            })
            by_path[key] = name
            changed.append(name)
            if log:
                log(f"Found \"{name}\" on the drive ({self.rel_path(proj)}).")
        return changed

    def refresh_project(self, name: str, manifest: dict | None = None) -> bool:
        """If this project's Live Sets were saved straight onto the drive (outside the app), record that
        as a new version. Checked right before every Send and Get, not only when the list refreshes."""
        m = manifest if manifest is not None else self.manifest(name)
        if not m or self.lock_info(name):
            return False
        proj = self.project_path(name, m)
        if not proj.is_dir() or not drive_sets_changed(proj, m):
            return False
        sets = find_sets(proj)
        m.update(files=snapshot(proj), revision=m.get("revision", 0) + 1, pushed_at=_newest_set_time(proj),
                 pushed_by=EDITED_ON_DRIVE, sets=[x.name for x in sets], main_set=sets[0].name)
        self.write_manifest(name, m)
        return True

    def _unique_name(self, proj: Path, taken: set[str]) -> str:
        base = safe_filename(proj.name)
        if base.lower() not in taken:
            return base
        cand = safe_filename(f"{proj.name} ({proj.parent.name})")
        n = 2
        while cand.lower() in taken:
            cand = safe_filename(f"{proj.name} ({proj.parent.name} {n})")
            n += 1
        return cand

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

def _same_folder(a, b) -> bool:
    return os.path.normcase(str(Path(a))) == os.path.normcase(str(Path(b)))


def push(store: LocalStore, drive: Drive, project_dir: Path, *, resolution=None,
         main_set: str | None = None, progress=None, log=None) -> PushResult:
    log = log or (lambda m: None)
    res = _resolutions(resolution)
    project_dir = Path(project_dir)
    me = store.settings
    sets = find_sets(project_dir)
    if not sets:
        raise BridgeError(f"There's no Live Set (.als) in {project_dir}.")
    main = main_set or sets[0].name
    had_id = read_project_id(project_dir)

    # Which drive project is this folder? Only a link made for THIS folder and THIS song counts.
    linked = store.drive_name_for_local(project_dir)
    st = store.project_state(linked) if linked else None
    if st and st.get("project_id") and st["project_id"] != had_id:
        log(f"\"{project_dir.name}\" now holds a different song than the one it was synced with.")
        store.forget_project(linked)
        linked, st = None, None
    drive_name = linked or safe_filename(project_dir.name)
    if st is None and had_id:
        other = store.project_state(drive_name)
        if other and other.get("project_id") == had_id and not Path(other["local_path"]).exists():
            st = other  # same song, folder was moved or renamed on this computer

    drive.refresh_project(drive_name)
    manifest = drive.manifest(drive_name)
    mid = (manifest or {}).get("id")
    # Projects synced by an older version have no id yet: a linked folder takes the drive's id.
    pid = had_id or (mid if st is not None and not st.get("project_id") and mid else None) or uuid.uuid4().hex
    if pid != had_id:
        write_project_id(project_dir, pid)

    if manifest and not res:
        who = manifest.get("pushed_by", {}).get("name", "another computer")
        when = _fmt_time(manifest.get("pushed_at"))
        where = f" (on the drive in {manifest['path']})" if manifest.get("path") else ""
        if st is None or (mid and mid != pid):
            raise Conflict(
                "name_taken", "A different project with this name is on the drive",
                f"The drive already has a project called \"{drive_name}\"{where}, last saved by {who} ({when}). "
                "It isn't this song's copy - it may be a different song with the same name.",
                {"keep_both": "Send mine as a separate copy", "overwrite": "Replace the one on the drive"})
        if manifest.get("revision", 0) > st.get("revision", 0):
            raise Conflict(
                "drive_newer", "The drive has a newer version",
                f"There's a newer version of \"{drive_name}\" on the drive ({who}, {when}) that you haven't "
                "opened on this computer. Sending now would replace it.",
                {"keep_both": "Send mine as a separate copy", "overwrite": "Replace it with mine"})

    if "keep_both" in res:
        base = f"{drive_name} ({me.machine_name})"
        drive_name, n = safe_filename(base), 2
        while drive.manifest(drive_name) is not None:
            drive_name = safe_filename(f"{base} {n}")
            n += 1
        manifest, mid = None, None

    lock = drive.lock_info(drive_name)
    if lock and lock.get("by") != me.machine_name and time.time() - lock.get("at", 0) < 60:
        raise BridgeError(f"{lock.get('by')} is sending this project right now.")

    log(f"Collecting samples and devices into \"{project_dir.name}\"...")
    collect = collect_project(project_dir, search_dirs=[Path(d) for d in me.sample_search_dirs],
                              include_core_library=me.collect_core_library, volume_roots=drive.search_roots,
                              log=log)
    if collect.files_copied:
        log(f"Collected {len(collect.files_copied)} file(s) into the project folder.")
    for err in collect.errors:
        log(f"Warning: {err}")

    snap = snapshot(project_dir)
    names = drive_names(project_dir, snap, log=log)
    dsnap = {names[r]: snap[r] for r in names}
    main_d = names.get(main, safe_filename(main, max_len=255))
    dest = drive.project_path(drive_name, manifest)
    old = (manifest or {}).get("files", {})
    # Replacing a different song (or one we can't vouch for): copy everything, back up its sets.
    replacing_other_song = bool(manifest) and ((mid is not None and mid != pid) or
                                               ("overwrite" in res and st is None))
    backup_drive_sets = replacing_other_song or "overwrite" in res
    to_copy = []
    for rel, drel in names.items():
        target = dest / Path(*drel.split("/"))
        try:
            on_drive_ok = target.is_file() and target.stat().st_size == snap[rel][0]
        except OSError:
            on_drive_ok = False
        if replacing_other_song or not on_drive_ok or not same_meta(snap[rel], old.get(drel)):
            to_copy.append(rel)
    to_copy.sort(key=lambda r: (r.lower().endswith(".als"), r))  # Live Sets last
    total = sum(snap[r][0] for r in to_copy)

    if manifest and not to_copy and set(dsnap) <= set(old) and manifest.get("main_set") == main_d \
            and mid in (None, pid):
        revision = manifest.get("revision", 0)
        if mid is None:
            manifest["id"] = pid
            drive.write_manifest(drive_name, manifest)
        store.set_project_state(drive_name, project_dir, revision, snap, pid)
        log(f"\"{drive_name}\" on the drive is already up to date.")
        return PushResult(drive_name, revision, main_d, 0, 0, collect)

    free = pu.free_space(drive.root)
    if 0 <= free < total + 50 * 1024 * 1024:
        raise BridgeError(f"Not enough space on the drive: need {total / 1e9:.2f} GB, "
                          f"{free / 1e9:.2f} GB free.")

    log(f"Copying {len(to_copy)} changed file(s) to the drive ({total / 1e6:.1f} MB)...")
    prog = _Progress(total, progress)
    stamp = time.strftime("%Y-%m-%d %H%M%S")
    drive.lock(drive_name, me.machine_name)
    try:
        for rel in to_copy:
            drel = names[rel]
            target = dest / Path(*drel.split("/"))
            # Never lose a set that's on the drive: if we're about to replace a Live Set that isn't
            # exactly the version this app last put there, keep a copy in the drive folder's Backup/.
            if drel.lower().endswith(".als") and "/" not in drel and target.is_file():
                st_t = target.stat()
                if backup_drive_sets or not same_meta([st_t.st_size, st_t.st_mtime], old.get(drel)):
                    pu.copy_file(target, dest / "Backup" / f"Before send {stamp}" / drel)
            try:
                pu.copy_file(project_dir / Path(*rel.split("/")), target, progress=prog, mtime=snap[rel][1])
            except OSError as exc:
                raise BridgeError(f"Couldn't copy \"{rel}\" to the drive ({exc.strerror or exc}). "
                                  "Nothing on this computer was changed; fix that file and send again.") from exc
        revision = (manifest or {}).get("revision", 0) + 1
        drive.write_manifest(drive_name, {
            "name": drive_name,
            "id": pid,
            "path": drive.rel_path(dest),
            "revision": revision,
            "main_set": main_d,
            "sets": sorted({names[s.name] for s in sets if s.name in names}),
            "pushed_at": time.time(),
            "pushed_by": {"id": me.machine_id, "name": me.machine_name, "os": pu.os_key()},
            "files": dsnap,
        })
    finally:
        drive.unlock(drive_name)
    if replacing_other_song:
        # Any other folder on this computer that was linked to the song that used to be there isn't anymore.
        for name, pst in list(store.state["projects"].items()):
            if name == drive_name and not _same_folder(pst.get("local_path", ""), project_dir):
                store.forget_project(name)
    store.set_project_state(drive_name, project_dir, revision, snap, pid)
    log(f"Sent \"{drive_name}\" to the drive (version {revision}).")
    return PushResult(drive_name, revision, main_d, len(to_copy), total, collect)


# --------------------------------------------------------------------------- pull

def _free_folder(base: Path) -> Path:
    n = 2
    cand = base
    while cand.exists():
        cand = base.with_name(f"{base.name} ({n})")
        n += 1
    return cand


def pull(store: LocalStore, drive: Drive, drive_name: str, *, resolution=None,
         dest: Path | None = None, progress=None, log=None) -> PullResult:
    log = log or (lambda m: None)
    res = _resolutions(resolution)
    drive.refresh_project(drive_name)
    manifest = drive.manifest(drive_name)
    if not manifest:
        raise BridgeError(f"\"{drive_name}\" isn't on the drive.")
    src = drive.project_path(drive_name, manifest)

    lock = drive.lock_info(drive_name)
    if lock and "continue" not in res:
        raise Conflict(
            "incomplete", "The last send didn't finish",
            f"{lock.get('by', 'The other computer')} started sending \"{drive_name}\" but it didn't finish "
            "(was the drive unplugged?). The newest files may be incomplete - send it again from that computer "
            "if you can.",
            {"continue": "Get the last complete version anyway"})

    mid = manifest.get("id")
    if not mid:
        mid = uuid.uuid4().hex
        if not lock:
            manifest["id"] = mid
            drive.write_manifest(drive_name, manifest)

    st = store.project_state(drive_name)
    if st and st.get("project_id") and st["project_id"] != mid:
        log(f"A different song now uses the name \"{drive_name}\" on the drive - your own copy of the old one "
            f"stays as it is.")
        store.forget_project(drive_name)
        st = None
    if st and not Path(st["local_path"]).parent.exists():
        st = None

    def belongs_elsewhere(folder: Path) -> bool:
        linked = store.drive_name_for_local(folder)
        lid = read_project_id(folder) if folder.exists() else None
        return (linked not in (None, drive_name)) or (lid is not None and lid != mid)

    if st:
        local = Path(st["local_path"])
    else:
        local = Path(dest) if dest else Path(store.settings.workspace) / drive_name
        if local.exists() and belongs_elsewhere(local):
            new_local = _free_folder(local)
            log(f"\"{local.name}\" on this computer is a different song - getting this one as "
                f"\"{new_local.name}\" instead.")
            local = new_local
    revision = manifest.get("revision", 0)
    files: dict = manifest.get("files", {})
    backup_all = "backup_and_get" in res
    changed_here: set[str] = set()

    if local.exists():
        snap = snapshot(local)
        if st:
            local_changes = changed_files(snap, st.get("files", {}))
            changed_here = set(local_changes)
            if local_changes and revision <= st.get("revision", 0):
                log("This computer already has the newest version (plus changes not sent yet).")
                return PullResult(drive_name, local, _main_set_path(local, manifest), 0, 0, already_current=True)
            if local_changes and not res & {"backup_and_get", "keep_both"}:
                raise Conflict(
                    "both_changed", "Changed on both computers",
                    f"\"{drive_name}\" was changed on this computer since it was last synced, and there's a newer "
                    f"version on the drive ({manifest.get('pushed_by', {}).get('name', 'the other computer')}). "
                    f"Changed here: {', '.join(local_changes[:4])}{'...' if len(local_changes) > 4 else ''}",
                    {"keep_both": "Keep mine and get theirs as a separate copy",
                     "backup_and_get": "Get theirs here (my changes go to Backup)"})
        else:
            differs = changed_files({k: v for k, v in snap.items() if k in files}, files)
            changed_here = set(differs)
            if differs and not res & {"backup_and_get", "keep_both"}:
                raise Conflict(
                    "local_exists", "There's already a project with this name here",
                    f"{local} already exists on this computer and isn't the same as the drive version. "
                    "If it's a different song, get the drive one as a separate copy.",
                    {"keep_both": "Get it as a separate copy",
                     "backup_and_get": "It's the same song - update it (my version goes to Backup)"})
        if "keep_both" in res and (changed_here or not st):
            new_local = _free_folder(local)
            log(f"Keeping \"{local.name}\" as it is - getting the drive version as \"{new_local.name}\".")
            if st:
                # my folder keeps its changes but is no longer linked to the drive project
                store.forget_project(drive_name)
                st = None
            local, snap, changed_here, backup_all = new_local, {}, set(), False
    else:
        snap = {}

    to_copy = [rel for rel, meta in files.items() if not same_meta(snap.get(rel), meta)]
    to_copy.sort(key=lambda r: (r.lower().endswith(".als"), r))
    if not to_copy and local.exists():
        if read_project_id(local) != mid:
            write_project_id(local, mid)
        store.set_project_state(drive_name, local, revision, snapshot(local), mid)
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
        if not pu.long_path(s).is_file():
            missing.append(rel)
            continue
        # Back up anything of mine that's about to be replaced: every Live Set, and every file
        # that was changed on this computer since the last sync.
        if pu.long_path(d).exists() and (backup_all or rel in changed_here or rel.lower().endswith(".als")):
            pu.copy_file(d, local / "Backup" / f"Before get {stamp}" / Path(*rel.split("/")))
            backups.append(rel)
        try:
            pu.copy_file(s, d, progress=prog, mtime=files[rel][1])
        except OSError as exc:
            raise BridgeError(f"Couldn't copy \"{rel}\" to this computer ({exc.strerror or exc}). "
                              "Try again; files already copied are kept.") from exc
    if missing:
        log(f"Warning: {len(missing)} file(s) listed for this project are missing from the drive.")
    if read_project_id(local) != mid:
        write_project_id(local, mid)
    # Samples that the set points to elsewhere on the drive (e.g. a sample library folder on it)
    # or that were never collected: pack them in now so the set opens complete.
    s = store.settings
    try:
        collect = collect_project(local, search_dirs=[Path(d) for d in s.sample_search_dirs],
                                  include_core_library=s.collect_core_library, volume_roots=drive.search_roots,
                                  log=log)
        if collect.files_copied:
            log(f"Packed {len(collect.files_copied)} sample(s) from elsewhere on the drive into the project.")
        for err in collect.errors:
            log(f"Warning: {err}")
    except Exception as exc:  # noqa: BLE001 - the project is already copied; never fail the Get here
        log(f"Warning: couldn't pack outside samples into the project ({exc}).")
    store.set_project_state(drive_name, local, revision, snapshot(local), mid)
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
    label: str = ""  # what the list shows (adds the parent folder when two local folders share a name)

    @property
    def status_text(self) -> str:
        return STATUS_TEXT[self.status]

    @property
    def sent_by(self) -> str:
        return (self.manifest or {}).get("pushed_by", {}).get("name", "")

    @property
    def sent_at(self) -> float:
        return (self.manifest or {}).get("pushed_at", 0.0)


SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information", "Backup", "Ableton Project Info", "Samples",
             "Library", "Applications", "Program Files", "Windows", "node_modules"}


def discover_projects(roots: list[Path], max_depth: int = 4, skip: list[Path] | None = None,
                      max_dirs: int = 30000) -> list[Path]:
    found, seen = [], set()
    skip_norm = {os.path.normcase(str(Path(s).resolve())) for s in (skip or [])}
    visited = 0
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            visited += 1
            if visited > max_dirs:
                break
            here = Path(dirpath)
            if os.path.normcase(str(here.resolve())) in skip_norm:
                dirnames[:] = []
                continue
            # A searched folder itself (Desktop, the top of the drive) is never "a project",
            # even with loose sets in it - only folders inside it are.
            if here != root and any(f.lower().endswith(".als") and not f.startswith("._") for f in filenames):
                key = os.path.normcase(str(here.resolve()))
                if key not in seen:
                    seen.add(key)
                    found.append(here)
                dirnames[:] = []
                continue
            if len(here.parts) - base_depth >= max_depth:
                dirnames[:] = []
            else:
                dirnames[:] = [d for d in dirnames if not d.startswith((".", "_")) and d not in SKIP_DIRS
                               and not d.endswith((".app", ".bundle", ".vst3", ".component"))]
    return found


def _newest_set_time(folder: Path) -> float:
    sets = find_sets(folder)
    return sets[0].stat().st_mtime if sets else time.time()


def newest_set(roots: list[Path], skip: list[Path] | None = None) -> Path | None:
    best, best_m = None, -1.0
    for proj in discover_projects(roots, skip=skip):
        for s in find_sets(proj)[:1]:
            m = s.stat().st_mtime
            if m > best_m:
                best, best_m = s, m
    return best


def relink_moved(store: LocalStore, folder: Path, pid: str) -> str | None:
    """A synced project folder that was moved or renamed on this computer keeps its link (found by its id)."""
    for name, st in store.state["projects"].items():
        if st.get("project_id") == pid and not Path(st.get("local_path", "")).exists():
            st["local_path"] = str(folder)
            store.save_state()
            return name
    return None


def project_rows(store: LocalStore, drive: Drive | None, rescan: bool = False) -> list[ProjectRow]:
    if drive:
        drive.scan_existing(force=rescan)
    manifests = drive.manifests() if drive else {}
    roots = [Path(r) for r in store.settings.project_roots] + [Path(store.settings.workspace)]
    extra = [Path(p["local_path"]) for p in store.state["projects"].values()]
    local_dirs = discover_projects(roots, skip=[drive.root] if drive else None)
    seen = {os.path.normcase(str(d.resolve())) for d in local_dirs}
    for e in extra:
        if e.is_dir() and os.path.normcase(str(e.resolve())) not in seen and find_sets(e):
            local_dirs.append(e)

    rows: list[ProjectRow] = []
    for d in local_dirs:
        linked = store.drive_name_for_local(d)
        lid = read_project_id(d)
        if not linked and lid:
            linked = relink_moved(store, d, lid)
        name = linked or safe_filename(d.name)
        st = store.project_state(linked) if linked else None
        man = manifests.get(name)
        if st and man and st.get("project_id") and man.get("id") and st["project_id"] != man["id"]:
            st = None  # a different song now uses that name on the drive
        sets = find_sets(d)
        mtime = sets[0].stat().st_mtime if sets else 0.0
        if man is None or (st is None and lid and man.get("id") and lid != man["id"]):
            status = "local_only"  # nothing on the drive, or only a different song with the same name
            man = None
        elif st is None:
            status = "unlinked"
        else:
            local_changed = sets_changed_quick(d, st.get("files", {}))
            drive_newer = man.get("revision", 0) > st.get("revision", 0)
            status = ("conflict" if local_changed and drive_newer else "drive_newer" if drive_newer
                      else "local_newer" if local_changed else "synced")
        rows.append(ProjectRow(name, status, d, man, mtime, name))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.name.lower()] = counts.get(r.name.lower(), 0) + 1
    for r in rows:
        if counts[r.name.lower()] > 1:
            r.label = f"{r.name}  ({r.local_dir.parent.name})"
    covered = {r.name for r in rows if r.status != "local_only"}
    for name, man in manifests.items():
        if name not in covered:
            rows.append(ProjectRow(name, "drive_only", None, man, 0.0, name))
    return sorted(rows, key=lambda r: max(r.local_mtime, r.sent_at), reverse=True)


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
