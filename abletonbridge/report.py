"""Project health check: will this project open cleanly on the other computer?"""

from __future__ import annotations

import glob
import os
import plistlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import platform_utils as pu
from .alsfile import AbletonSet, AlsError, PluginRef, find_sets
from .collect import MissingFile, collect_project
from .plugins import PluginStatus, check_plugin

LEVEL_ORDER = {"missing": 0, "error": 0, "warn": 1, "unknown": 2, "ok": 3}


def installed_live_versions() -> list[str]:
    """Versions of Ableton Live installed on this computer, e.g. ['12.1.5', '11']."""
    versions: list[str] = []
    if pu.IS_MAC:
        apps = glob.glob("/Applications/Ableton Live*.app") + glob.glob(str(Path.home() / "Applications/Ableton Live*.app"))
        for app in apps:
            v = ""
            try:
                with open(os.path.join(app, "Contents", "Info.plist"), "rb") as fh:
                    info = plistlib.load(fh)
                v = str(info.get("CFBundleShortVersionString", "")).split(" ")[0]
            except (OSError, plistlib.InvalidFileException, ValueError):
                pass
            if not v:
                m = re.search(r"Live (\d+)", app)
                v = m.group(1) if m else ""
            if v:
                versions.append(v)
    elif pu.IS_WINDOWS:
        pd = os.environ.get("ProgramData", r"C:\ProgramData")
        for d in glob.glob(os.path.join(pd, "Ableton", "Live *")):
            m = re.search(r"Live (\d+(?:\.\d+)*)", os.path.basename(d))
            if m:
                versions.append(m.group(1))
    return sorted(set(versions), key=_version_tuple, reverse=True)


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


@dataclass
class PluginRow:
    plugin: PluginRef
    status: dict[str, PluginStatus]  # machine id -> status


@dataclass
class Machine:
    id: str
    name: str
    os: str
    inventory: dict | None
    live_versions: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.name} ({pu.os_label({'mac': 'Darwin', 'windows': 'Windows'}.get(self.os, self.os))})"


@dataclass
class Issue:
    level: str  # "error" / "warn" / "info"
    text: str
    machine: str | None = None  # machine id it applies to, None = everywhere


@dataclass
class ProjectReport:
    project_dir: Path
    sets: list[str]
    creator: str
    machines: list[Machine]
    plugins: list[PluginRow]
    missing_files: list[MissingFile]
    to_collect: int
    issues: list[Issue]

    def problems_for(self, machine_id: str) -> list[str]:
        out = [i.text for i in self.issues if i.level == "error" and i.machine in (None, machine_id)]
        for row in self.plugins:
            st = row.status.get(machine_id)
            if st and st.level == "missing":
                out.append(f"{row.plugin.name} ({row.plugin.format_label}) - {st.text}")
        for mf in self.missing_files:
            out.append(f"Missing file: {mf.filename}")
        return out

    def warnings_for(self, machine_id: str) -> list[str]:
        out = [i.text for i in self.issues if i.level == "warn" and i.machine in (None, machine_id)]
        for row in self.plugins:
            st = row.status.get(machine_id)
            if st and st.level in ("warn", "unknown"):
                out.append(f"{row.plugin.name} ({row.plugin.format_label}) - {st.text}")
        return out

    def as_text(self) -> str:
        lines = [f"Project: {self.project_dir.name}", f"Saved with: {self.creator or 'unknown'}",
                 f"Sets: {', '.join(self.sets)}", ""]
        if self.missing_files:
            lines.append("MISSING FILES (not found anywhere on this computer):")
            for m in self.missing_files:
                lines.append(f"  - {m.filename}  [{m.set_name}]  last seen at {m.last_known_path}")
            lines.append("")
        if self.to_collect:
            lines.append(f"{self.to_collect} file(s) live outside the project folder - they'll be copied in "
                         "automatically when you send it.")
            lines.append("")
        for issue in self.issues:
            where = ""
            if issue.machine:
                where = next((f" [{m.label}]" for m in self.machines if m.id == issue.machine), "")
            lines.append(f"{issue.level.upper()}{where}: {issue.text}")
        if self.issues:
            lines.append("")
        lines.append("PLUGINS:")
        if not self.plugins:
            lines.append("  (no third-party plugins)")
        for row in self.plugins:
            lines.append(f"  {row.plugin.name} [{row.plugin.format_label}] on: {', '.join(row.plugin.tracks)}")
            for m in self.machines:
                st = row.status.get(m.id)
                if st:
                    lines.append(f"      {m.label}: {st.level.upper()} - {st.text}")
        return "\n".join(lines)


def machines_from_drive(records: list[dict]) -> list[Machine]:
    return [Machine(r["id"], r.get("name", "?"), r.get("os", "?"), r.get("plugins"), r.get("live_versions", []))
            for r in records]


def build_report(project_dir: Path, machines: list[Machine], *, search_dirs=None) -> ProjectReport:
    project_dir = Path(project_dir)
    sets = find_sets(project_dir)
    issues: list[Issue] = []
    plugins: dict[tuple[str, str], PluginRef] = {}
    creator = ""
    for s in sets:
        try:
            als = AbletonSet(s)
        except AlsError as exc:
            issues.append(Issue("error", str(exc)))
            continue
        creator = creator or als.creator
        for p in als.plugins():
            existing = plugins.get(p.key)
            if existing is None:
                plugins[p.key] = p
            else:
                existing.tracks += [t for t in p.tracks if t not in existing.tracks]
                existing.frozen_tracks += [t for t in p.frozen_tracks if t not in existing.frozen_tracks]

    dry = collect_project(project_dir, search_dirs=search_dirs, dry_run=True)
    to_collect = len(dry.external)

    set_version = re.search(r"Live (\d+(?:\.\d+)*)", creator)
    for m in machines:
        if set_version and m.live_versions:
            newest = max(m.live_versions, key=_version_tuple)
            sv, nv = _version_tuple(set_version.group(1)), _version_tuple(newest)
            if sv[0] > nv[0]:
                issues.append(Issue("error", f"Saved with Live {set_version.group(1)}, but {m.name} has Live "
                                             f"{newest}. Older Live versions can't open it - update Live on "
                                             f"{m.name}.", m.id))
            elif sv[:2] > nv[:2]:
                issues.append(Issue("warn", f"Saved with Live {set_version.group(1)}; {m.name} has Live {newest}. "
                                            "Update Live there to be safe.", m.id))

    long_limit = 200
    for dirpath, _, filenames in os.walk(project_dir):
        for fn in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fn), project_dir)
            if len(rel) > long_limit:
                issues.append(Issue("warn", f"Very long file path ({len(rel)} characters) may not open on "
                                            f"Windows: {rel[:80]}..."))
                break

    rows = []
    for p in sorted(plugins.values(), key=lambda p: (p.name.lower(), p.format)):
        rows.append(PluginRow(p, {m.id: check_plugin(p, m.inventory, m.os) for m in machines}))
    rows.sort(key=lambda r: min((LEVEL_ORDER[s.level] for s in r.status.values()), default=3))
    return ProjectReport(project_dir, [s.name for s in sets], creator, machines, rows, dry.missing, to_collect,
                         issues)

