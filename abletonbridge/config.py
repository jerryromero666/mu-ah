"""Per-computer settings and sync state (stored on this computer, not on the drive)."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import platform_utils as pu


@dataclass
class Settings:
    machine_id: str = ""
    machine_name: str = ""
    workspace: str = ""  # where projects from the drive are put
    project_roots: list[str] = field(default_factory=list)  # where to look for your projects
    drive_path: str = ""  # last used AbletonBridge folder on the drive
    extra_plugin_dirs: list[str] = field(default_factory=list)
    sample_search_dirs: list[str] = field(default_factory=list)
    auto_save_in_live: bool = True
    open_after_get: bool = True
    collect_core_library: bool = True


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


class LocalStore:
    """settings.json + state.json in the per-user config folder."""

    def __init__(self, base: Path | None = None):
        self.base = Path(base) if base else pu.config_dir()
        self.base.mkdir(parents=True, exist_ok=True)
        self.settings = self._load_settings()
        self.state: dict = read_json(self.base / "state.json", {}) or {}
        self.state.setdefault("projects", {})

    def _load_settings(self) -> Settings:
        raw = read_json(self.base / "settings.json", {}) or {}
        known = {k: v for k, v in raw.items() if k in Settings.__dataclass_fields__}
        s = Settings(**known)
        changed = False
        if not s.machine_id:
            s.machine_id = uuid.uuid4().hex[:12]
            changed = True
        if not s.machine_name:
            s.machine_name = pu.machine_name()
            changed = True
        if not s.workspace:
            s.workspace = str(pu.default_workspace())
            changed = True
        if not s.project_roots:
            s.project_roots = [str(p) for p in pu.default_project_roots()]
            changed = True
        if changed:
            _atomic_write_json(self.base / "settings.json", asdict(s))
        return s

    def save_settings(self) -> None:
        _atomic_write_json(self.base / "settings.json", asdict(self.settings))

    def save_state(self) -> None:
        _atomic_write_json(self.base / "state.json", self.state)

    # ------------------------------------------------------------------ project sync state
    def project_state(self, drive_name: str) -> dict | None:
        return self.state["projects"].get(drive_name)

    def set_project_state(self, drive_name: str, local_path: Path, revision: int, files: dict) -> None:
        self.state["projects"][drive_name] = {
            "local_path": str(local_path),
            "revision": revision,
            "files": files,
        }
        self.save_state()

    def drive_name_for_local(self, local_path: Path) -> str | None:
        target = os.path.normcase(str(Path(local_path)))
        for name, st in self.state["projects"].items():
            if os.path.normcase(st.get("local_path", "")) == target:
                return name
        return None
