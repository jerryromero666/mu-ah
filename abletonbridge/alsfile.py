"""Read and surgically rewrite Ableton Live Set (.als) files.

An .als file is gzip-compressed XML. Two things matter for moving a project
between computers:

* ``<FileRef>`` elements - every sample, Max for Live device, wavetable etc.
  the set points to. Live 11/12 write::

      <FileRef>
          <RelativePathType Value="3" />
          <RelativePath Value="Samples/Imported/Kick.wav" />
          <Path Value="C:/Users/me/Music/Song Project/Samples/Imported/Kick.wav" />
          <Type Value="1" />
          <LivePackName Value="" />
          <LivePackId Value="" />
          <OriginalFileSize Value="123456" />
          <OriginalCrc Value="5678" />
      </FileRef>

  Live 9/10 write the older form with ``HasRelativePath``, a list of
  ``<RelativePathElement Dir="..."/>`` and ``<Name>``. Both are supported.
  ``RelativePathType`` 3 means "relative to the project folder", which is what
  makes a project portable.

* Plugin descriptions (``VstPluginInfo``, ``Vst3PluginInfo``, ``AuPluginInfo``).

Only the ``<FileRef>`` blocks that need changing are rewritten; every other
byte of the document is left exactly as Live wrote it.
"""

from __future__ import annotations

import gzip
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

PROJECT_RELATIVE = 3

# FileRefs below these elements only describe where something *came from*
# (browser location, last preset used). Live opens the set fine without them.
INFORMATIONAL_ANCESTORS = {"LastPresetRef", "SourceContext", "OriginalFileRef", "BrowserContentPath"}

_FILEREF_RE = re.compile(r"<FileRef>(.*?)</FileRef>", re.S)

TRACK_TAGS = {"AudioTrack", "MidiTrack", "GroupTrack", "ReturnTrack", "MasterTrack", "MainTrack", "PreHearTrack"}


def _val(elem: ET.Element | None, tag: str, default: str = "") -> str:
    if elem is None:
        return default
    child = elem.find(tag)
    if child is None:
        return default
    return child.get("Value", default)


@dataclass
class FileRef:
    index: int
    fmt: str  # "modern" (Live 11+) or "legacy" (Live 9/10)
    context: tuple[str, ...]
    relative_path_type: int | None
    relative_path: str  # posix style, relative to project folder (may be empty)
    abs_path: str  # absolute path as last written by Live (may be from the other OS)
    name: str
    pack_name: str = ""
    original_size: int | None = None
    has_relative_path: bool = True

    @property
    def informational(self) -> bool:
        return bool(INFORMATIONAL_ANCESTORS.intersection(self.context))

    @property
    def empty(self) -> bool:
        return not (self.relative_path or self.abs_path or self.name)

    @property
    def filename(self) -> str:
        if self.name:
            return self.name
        for p in (self.abs_path, self.relative_path):
            if p:
                return re.split(r"[\\/]", p.rstrip("/\\"))[-1]
        return ""

    @property
    def kind(self) -> str:
        if "MxPatchRef" in self.context or self.filename.lower().endswith((".amxd", ".maxpat")):
            return "max"
        return "sample"

    def candidates(self, project_dir: Path) -> list[Path]:
        """Places this file could be, best guess first."""
        out: list[Path] = []
        rel_ok = self.relative_path and not self.relative_path.startswith("/")
        if rel_ok and self.relative_path_type == PROJECT_RELATIVE:
            out.append(project_dir / Path(*PurePosixPath(self.relative_path).parts))
        if self.abs_path and _is_native_abs(self.abs_path):
            out.append(Path(self.abs_path))
        if rel_ok and self.relative_path_type != PROJECT_RELATIVE:
            out.append(project_dir / Path(*PurePosixPath(self.relative_path).parts))
        return out


@dataclass
class PluginRef:
    format: str  # "vst2", "vst3" or "au"
    name: str
    vendor: str = ""
    path: str = ""
    uid: str = ""
    tracks: list[str] = field(default_factory=list)
    frozen_tracks: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return (self.format, self.name.lower())

    @property
    def format_label(self) -> str:
        return {"vst2": "VST2", "vst3": "VST3", "au": "Audio Unit"}.get(self.format, self.format)


class AlsError(Exception):
    pass


class AbletonSet:
    def __init__(self, path: Path):
        self.path = Path(path)
        raw = self.path.read_bytes()
        self.gzipped = raw[:2] == b"\x1f\x8b"
        try:
            data = gzip.decompress(raw) if self.gzipped else raw
        except (OSError, EOFError) as exc:
            raise AlsError(f"{self.path.name} is damaged or incomplete ({exc}).") from exc
        try:
            self.text = data.decode("utf-8")
            self.root = ET.fromstring(data)
        except (UnicodeDecodeError, ET.ParseError) as exc:
            raise AlsError(f"{self.path.name} isn't a readable Ableton Live Set ({exc}).") from exc
        if self.root.tag != "Ableton":
            raise AlsError(f"{self.path.name} isn't an Ableton Live Set.")
        self._parents = {child: parent for parent in self.root.iter() for child in parent}
        self._pending: dict[int, ET.Element] = {}
        self._fileref_elems = [e for e in self.root.iter("FileRef") if len(e)]
        self._matches = list(_FILEREF_RE.finditer(self.text))
        if len(self._matches) != len(self._fileref_elems):
            # Should never happen with files Live writes; refuse to edit rather than guess.
            self._editable = False
        else:
            self._editable = True

    # ------------------------------------------------------------------ info
    @property
    def creator(self) -> str:
        return self.root.get("Creator", "")

    @property
    def live_major_version(self) -> int | None:
        m = re.search(r"Live (\d+)", self.creator)
        return int(m.group(1)) if m else None

    def _ancestors(self, elem: ET.Element) -> list[ET.Element]:
        out = []
        cur = self._parents.get(elem)
        while cur is not None:
            out.append(cur)
            cur = self._parents.get(cur)
        return out

    def _track_of(self, elem: ET.Element) -> ET.Element | None:
        for anc in self._ancestors(elem):
            if anc.tag in TRACK_TAGS:
                return anc
        return None

    @staticmethod
    def track_name(track: ET.Element) -> str:
        if track.tag in ("MasterTrack", "MainTrack"):
            return "Main"
        name = track.find("Name")
        if name is not None:
            for tag in ("UserName", "EffectiveName"):
                v = _val(name, tag)
                if v:
                    return v
        return track.tag.replace("Track", " track")

    @staticmethod
    def track_frozen(track: ET.Element) -> bool:
        return _val(track, "Freeze").lower() == "true"

    # ------------------------------------------------------------------ file refs
    def filerefs(self) -> list[FileRef]:
        refs = []
        for i, elem in enumerate(self._fileref_elems):
            context = tuple(a.tag for a in self._ancestors(elem))
            refs.append(self._parse_fileref(i, elem, context))
        return refs

    @staticmethod
    def _parse_fileref(i: int, elem: ET.Element, context: tuple[str, ...]) -> FileRef:
        rpt_text = _val(elem, "RelativePathType")
        rpt = int(rpt_text) if rpt_text.lstrip("-").isdigit() else None
        rel_elem = elem.find("RelativePath")
        orig_size = _val(elem, "OriginalFileSize")
        orig_size_i = int(orig_size) if orig_size.isdigit() else None
        pack = _val(elem, "LivePackName")
        if rel_elem is not None and rel_elem.get("Value") is not None:
            # Live 11 / 12
            rel = rel_elem.get("Value", "").replace("\\", "/")
            abs_path = _val(elem, "Path")
            name = re.split(r"[\\/]", (abs_path or rel).rstrip("/\\"))[-1] if (abs_path or rel) else ""
            return FileRef(i, "modern", context, rpt, rel, abs_path, name, pack, orig_size_i)
        # Live 9 / 10
        name = _val(elem, "Name")
        dirs = [e.get("Dir", "") for e in (rel_elem.findall("RelativePathElement") if rel_elem is not None else [])]
        dirs = [".." if d == "" else d for d in dirs]
        rel = "/".join(dirs + [name]) if name else ""
        has_rel = _val(elem, "HasRelativePath").lower() == "true"
        abs_path = _decode_legacy_data(elem) or _legacy_search_hint(elem, name)
        return FileRef(i, "legacy", context, rpt, rel if has_rel else "", abs_path, name, pack,
                       orig_size_i, has_rel)

    @property
    def editable(self) -> bool:
        return self._editable

    def set_project_relative(self, ref: FileRef, rel_posix: str, abs_path: Path) -> None:
        """Point *ref* at ``project/rel_posix`` (also storing the new absolute path)."""
        if not self._editable:
            raise AlsError(f"{self.path.name}: unexpected file structure, not modifying it.")
        m = self._matches[ref.index]
        block = ET.fromstring(m.group(0))
        abs_str = str(abs_path).replace("\\", "/")
        if ref.fmt == "modern":
            _set_child_value(block, "RelativePathType", str(PROJECT_RELATIVE))
            _set_child_value(block, "RelativePath", rel_posix)
            _set_child_value(block, "Path", abs_str)
        else:
            _set_child_value(block, "HasRelativePath", "true")
            _set_child_value(block, "RelativePathType", str(PROJECT_RELATIVE))
            parts = rel_posix.split("/")
            rel_elem = block.find("RelativePath")
            if rel_elem is None:
                rel_elem = ET.SubElement(block, "RelativePath")
            tail = rel_elem.tail
            for child in list(rel_elem):
                rel_elem.remove(child)
            for n, d in enumerate(parts[:-1]):
                child = ET.SubElement(rel_elem, "RelativePathElement", {"Id": str(n), "Dir": d})
                child.tail = "\n"
            rel_elem.text = "\n" if len(parts) > 1 else None
            rel_elem.tail = tail
            _set_child_value(block, "Name", parts[-1])
        self._pending[ref.index] = block

    def has_changes(self) -> bool:
        return bool(self._pending)

    def render(self) -> str:
        if not self._pending:
            return self.text
        pieces, pos = [], 0
        for i, m in enumerate(self._matches):
            if i in self._pending:
                pieces.append(self.text[pos:m.start()])
                pieces.append(ET.tostring(self._pending[i], encoding="unicode"))
                pos = m.end()
        pieces.append(self.text[pos:])
        return "".join(pieces)

    def save(self, path: Path | None = None) -> None:
        target = Path(path or self.path)
        data = self.render().encode("utf-8")
        if self.gzipped:
            data = gzip.compress(data, compresslevel=6)
        tmp = target.with_name(target.name + ".abtmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)

    # ------------------------------------------------------------------ plugins
    def plugins(self) -> list[PluginRef]:
        found: dict[tuple[str, str], PluginRef] = {}
        for elem in self.root.iter():
            if elem.tag == "VstPluginInfo":
                path = _val(elem, "Path")
                name = _val(elem, "PlugName") or _stem(path) or _val(elem, "FileName")
                p = PluginRef("vst2", name, path=path, uid=_val(elem, "UniqueId"))
            elif elem.tag == "Vst3PluginInfo":
                uid_elem = elem.find("Uid")
                uid = ""
                if uid_elem is not None:
                    uid = "-".join(c.get("Value", "") for c in uid_elem)
                p = PluginRef("vst3", _val(elem, "Name"), uid=uid)
            elif elem.tag == "AuPluginInfo":
                p = PluginRef("au", _val(elem, "Name"), vendor=_val(elem, "Manufacturer"))
            else:
                continue
            if not p.name:
                continue
            existing = found.setdefault(p.key, p)
            track = self._track_of(elem)
            tname = self.track_name(track) if track is not None else "?"
            if tname not in existing.tracks:
                existing.tracks.append(tname)
                if track is not None and self.track_frozen(track):
                    existing.frozen_tracks.append(tname)
        return sorted(found.values(), key=lambda p: (p.name.lower(), p.format))


def _is_native_abs(path: str) -> bool:
    """True if *path* is an absolute path on the OS we're running on.

    A Mac path ("/Users/...") means nothing on Windows and a Windows path
    ("C:/Users/...") means nothing on a Mac, so those are never tried.
    """
    is_win_path = re.match(r"^[A-Za-z]:[\\/]", path) is not None
    if os.name == "nt":
        return is_win_path or path.startswith(("\\\\", "//"))
    return path.startswith("/") and not is_win_path


def _set_child_value(block: ET.Element, tag: str, value: str) -> None:
    child = block.find(tag)
    if child is None:
        child = ET.SubElement(block, tag)
    child.set("Value", value)


def _stem(path: str) -> str:
    if not path:
        return ""
    base = re.split(r"[\\/]", path.rstrip("/\\"))[-1]
    return re.sub(r"\.(dll|vst|vst3|component)$", "", base, flags=re.I)


def _decode_legacy_data(elem: ET.Element) -> str:
    """Live 9/10 on Windows store the absolute path as hex-encoded UTF-16."""
    data = elem.find("Data")
    if data is None or not (data.text or "").strip():
        return ""
    hexstr = re.sub(r"\s+", "", data.text)
    try:
        raw = bytes.fromhex(hexstr)
    except ValueError:
        return ""
    try:
        txt = raw.decode("utf-16-le").rstrip("\x00")
    except UnicodeDecodeError:
        return ""
    if re.match(r"^[A-Za-z]:[\\/]", txt) and all(c.isprintable() for c in txt):
        return txt.replace("\\", "/")
    return ""


def _legacy_search_hint(elem: ET.Element, name: str) -> str:
    hint = elem.find("SearchHint/PathHint")
    if hint is None or not name:
        return ""
    dirs = [e.get("Dir", "").rstrip("\\/") for e in hint.findall("RelativePathElement")]
    dirs = [d for d in dirs if d]
    if not dirs:
        return ""
    if re.match(r"^[A-Za-z]:$", dirs[0]):
        return "/".join(dirs + [name])
    return "/" + "/".join(dirs + [name])


def find_sets(project_dir: Path) -> list[Path]:
    """Live Sets in a project folder (top level only, Backup/ excluded), newest first."""
    sets = [p for p in Path(project_dir).glob("*.als") if p.is_file() and not p.name.startswith("._")]
    return sorted(sets, key=lambda p: p.stat().st_mtime, reverse=True)
