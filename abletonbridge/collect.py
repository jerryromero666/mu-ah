"""Make an Ableton project folder self-contained.

This does what Live's "Collect All and Save" does, automatically and for every
set in the folder: every file a set uses that lives outside the project folder
(Splice downloads, sample packs, the User Library, Max for Live devices, Core
Library sounds...) is copied into the project, and the set is rewritten so it
finds the file relative to the project folder. It also renames files whose
names Windows can't store (e.g. ``Kick: hard.wav``).

The original set is backed up to ``Backup/`` before it is changed.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .alsfile import PROJECT_RELATIVE, AbletonSet, AlsError, FileRef, find_sets
from .platform_utils import copy_file

WIN_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

IMPORT_DIRS = {"sample": "Samples/Imported", "max": "Presets/Imported"}


def safe_filename(name: str, max_len: int = 120) -> str:
    """A filename that works on Windows, macOS and exFAT drives."""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = WIN_ILLEGAL.sub("_", stem).rstrip(" .") or "file"
    ext = WIN_ILLEGAL.sub("_", ext).strip(" .")
    if stem.upper() in WIN_RESERVED:
        stem = "_" + stem
    room = max_len - (len(ext) + 1 if ext else 0)
    stem = stem[:max(room, 1)].rstrip(" .") or "file"
    return f"{stem}.{ext}" if ext else stem


def name_is_portable(rel_posix: str) -> bool:
    return all(part == safe_filename(part, max_len=255) for part in rel_posix.split("/"))


def file_sha1(path: Path, limit: int | None = None) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        remaining = limit
        while True:
            chunk = fh.read(1024 * 1024 if remaining is None else min(1024 * 1024, remaining))
            if not chunk:
                break
            h.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
                if remaining <= 0:
                    break
    return h.hexdigest()


def same_file_content(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
    except OSError:
        return False
    return file_sha1(a) == file_sha1(b)


def is_inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (ValueError, OSError):
        return False


@dataclass
class MissingFile:
    set_name: str
    filename: str
    last_known_path: str
    kind: str


@dataclass
class CollectResult:
    sets_changed: list[str] = field(default_factory=list)
    files_copied: list[str] = field(default_factory=list)  # project-relative destinations
    refs_fixed: int = 0
    bytes_copied: int = 0
    missing: list[MissingFile] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    external: set[str] = field(default_factory=set)  # files outside the project that get copied in


class FileFinder:
    """Looks for a missing file by name in the project folder and any extra sample folders."""

    def __init__(self, project_dir: Path, search_dirs: list[Path] | None = None):
        self.project_dir = project_dir
        self.search_dirs = [Path(d) for d in (search_dirs or []) if Path(d).is_dir()]
        self._project_index: dict[str, list[Path]] | None = None
        self._extra_index: dict[str, list[Path]] | None = None

    @staticmethod
    def _index(dirs: list[Path], max_files: int = 400_000) -> dict[str, list[Path]]:
        idx: dict[str, list[Path]] = {}
        count = 0
        for d in dirs:
            for dirpath, dirnames, filenames in os.walk(d):
                dirnames[:] = [n for n in dirnames if not n.startswith(".")]
                for fn in filenames:
                    idx.setdefault(fn.lower(), []).append(Path(dirpath) / fn)
                    count += 1
                    if count >= max_files:
                        return idx
        return idx

    def find(self, ref: FileRef) -> Path | None:
        name = ref.filename.lower()
        if not name:
            return None
        if self._project_index is None:
            self._project_index = self._index([self.project_dir])
        hit = self._pick(self._project_index.get(name, []), ref)
        if hit or not self.search_dirs:
            return hit
        if self._extra_index is None:
            self._extra_index = self._index(self.search_dirs)
        return self._pick(self._extra_index.get(name, []), ref)

    @staticmethod
    def _pick(paths: list[Path], ref: FileRef) -> Path | None:
        if not paths:
            return None
        if ref.original_size:
            for p in paths:
                try:
                    if p.stat().st_size == ref.original_size:
                        return p
                except OSError:
                    continue
            return None  # same name, different file: don't guess
        return paths[0]


_VOLUME_PREFIX = re.compile(r"^(?:/Volumes/[^/]+/|[A-Za-z]:/|/media/[^/]+/[^/]+/|/run/media/[^/]+/[^/]+/|/mnt/[^/]+/)")


def remap_to_volumes(abs_path: str, volume_roots: list[Path]) -> list[Path]:
    """'/Volumes/MyDrive/Samples/x.wav' or 'E:/Samples/x.wav' -> '<drive as mounted here>/Samples/x.wav'."""
    p = abs_path.replace("\\", "/")
    m = _VOLUME_PREFIX.match(p)
    if not m:
        return []
    rest = p[m.end():]
    if not rest:
        return []
    return [Path(root, *rest.split("/")) for root in volume_roots]


def resolve(ref: FileRef, project_dir: Path, finder: FileFinder,
            volume_roots: list[Path] | None = None) -> Path | None:
    cands = ref.candidates(project_dir)
    if volume_roots and ref.abs_path:
        cands += remap_to_volumes(ref.abs_path, volume_roots)
    for cand in cands:
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return finder.find(ref)


def is_core_library(path: Path) -> bool:
    s = str(path).replace("\\", "/").lower()
    return "/core library/" in s or "/factory packs/core library" in s


class _Importer:
    def __init__(self, project_dir: Path, result: CollectResult, progress=None):
        self.project_dir = project_dir
        self.result = result
        self.progress = progress
        self.done: dict[str, str] = {}  # resolved source path -> project-relative dest

    def import_file(self, src: Path, kind: str) -> str:
        key = str(src.resolve())
        if key in self.done:
            return self.done[key]
        folder = IMPORT_DIRS.get(kind, IMPORT_DIRS["sample"])
        dest_dir = self.project_dir / Path(*folder.split("/"))
        dest_dir.mkdir(parents=True, exist_ok=True)
        base = safe_filename(src.name)
        stem, dot, ext = base.rpartition(".")
        if not dot:
            stem, ext = base, ""
        existing = {p.name.lower(): p for p in dest_dir.iterdir()}
        n = 1
        while True:
            cand = base if n == 1 else (f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})")
            hit = existing.get(cand.lower())
            if hit is None:
                copy_file(src, dest_dir / cand, progress=self.progress)
                rel = f"{folder}/{cand}"
                self.result.files_copied.append(rel)
                self.result.bytes_copied += src.stat().st_size
                break
            if same_file_content(hit, src):
                rel = f"{folder}/{hit.name}"
                break
            n += 1
        self.done[key] = rel
        return rel


def backup_set(set_path: Path, reason: str = "before Ableton Bridge") -> Path:
    backup_dir = set_path.parent / "Backup"
    backup_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H%M%S")
    dest = backup_dir / f"{set_path.stem} [{stamp}] ({reason}).als"
    copy_file(set_path, dest)
    return dest


def collect_project(project_dir: Path, *, search_dirs: list[Path] | None = None,
                    include_core_library: bool = True, dry_run: bool = False,
                    volume_roots: list[Path] | None = None,
                    progress=None, log=None) -> CollectResult:
    """Copy everything the project's sets use into the project and point the sets at the copies."""
    project_dir = Path(project_dir)
    log = log or (lambda msg: None)
    result = CollectResult()
    finder = FileFinder(project_dir, search_dirs)
    importer = _Importer(project_dir, result, progress)

    for set_path in find_sets(project_dir):
        try:
            als = AbletonSet(set_path)
        except AlsError as exc:
            result.errors.append(str(exc))
            continue
        # A set we can't rewrite safely is still checked (missing files are reported),
        # but nothing is copied for it and it's left exactly as it is.
        edit = not dry_run and als.editable
        if not dry_run and not als.editable:
            result.errors.append(f"{set_path.name}: left unchanged (unusual file layout). If samples are "
                                 "missing in it, open it in Live and use File > Collect All and Save.")
        changed = 0
        failed = False
        for ref in als.filerefs():
            if ref.informational or ref.empty:
                continue
            found = resolve(ref, project_dir, finder, volume_roots)
            if found is None:
                result.missing.append(MissingFile(set_path.name, ref.filename, ref.abs_path or ref.relative_path,
                                                  ref.kind))
                continue
            if is_inside(found, project_dir):
                rel = PurePosixPath(*found.resolve().relative_to(project_dir.resolve()).parts).as_posix()
                if not name_is_portable(rel):
                    if edit:
                        rel = importer.import_file(found, ref.kind)
                        log(f"Renamed for Windows: {found.name} -> {rel}")
                    else:
                        rel = "(renamed for Windows) " + rel
            elif not include_core_library and is_core_library(found):
                continue
            else:
                result.external.add(str(found))
                if edit:
                    rel = importer.import_file(found, ref.kind)
                    log(f"Collected {found.name}")
                else:
                    rel = f"(collect) {found.name}"
            if ref.relative_path_type == PROJECT_RELATIVE and ref.relative_path == rel and (
                    ref.fmt == "modern" or ref.has_relative_path):
                continue
            changed += 1
            if edit:
                try:
                    als.set_project_relative(ref, rel, project_dir / Path(*rel.split("/")))
                except (AlsError, ET.ParseError) as exc:
                    result.errors.append(f"{set_path.name}: left unchanged ({exc}).")
                    failed = True
                    break
        if changed and edit and not failed:
            backup_set(set_path)
            mtime = set_path.stat().st_mtime
            als.save()
            # Keep the original order of save times (so "the set you saved last" stays last),
            # nudged forward so sync still sees the file as changed.
            os.utime(set_path, (mtime + 2, mtime + 2))
            result.sets_changed.append(set_path.name)
        if not failed:
            result.refs_fixed += changed
    return result
