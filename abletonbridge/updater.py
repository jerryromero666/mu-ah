"""Update the app in place from the GitHub releases of this repository.

Every successful build of the main branch publishes a GitHub release named ``build-<N>`` with the
three apps and a ``latest.json`` holding the build number and each file's SHA-256. The app asks
GitHub for the latest release, and if its build number is newer it can download the right file,
check the SHA-256, swap itself for the new version and restart. Settings, sync state and projects
are stored elsewhere and are untouched.

* Windows: a running .exe can be renamed but not overwritten, so the current exe is renamed to
  ``AbletonBridge.old.exe``, the new one takes its name, and the old one is deleted on next start.
* macOS: the new ``Ableton Bridge.app`` is unpacked next to the current one with ``ditto`` (which
  keeps the bundle's permissions and links), the bundles are swapped, the new one is opened.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import platform_utils as pu

REPO = "jerryromero666/mu-ah"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
WIN_EXE = "AbletonBridge.exe"
MAC_APP = "Ableton Bridge.app"
OLD_SUFFIX = ".old"


class UpdateError(Exception):
    pass


def current_build() -> int:
    try:
        from ._build import BUILD
        return int(BUILD)
    except (ImportError, ValueError, TypeError):
        return 0


def can_self_update() -> bool:
    """Only the packaged app (.exe / .app) can replace itself; running from source can't."""
    return bool(getattr(sys, "frozen", False)) and (pu.IS_WINDOWS or pu.IS_MAC)


def asset_name(system: str | None = None, machine: str | None = None) -> str | None:
    system = system or pu.SYSTEM
    machine = (machine or platform.machine()).lower()
    if system == "Windows":
        return "AbletonBridge-Windows.zip"
    if system == "Darwin":
        return "AbletonBridge-Mac-AppleSilicon.zip" if machine in ("arm64", "aarch64") else \
            "AbletonBridge-Mac-Intel.zip"
    return None


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # bundled with the app so HTTPS works on every Mac
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _get(url: str, timeout: float = 20, progress=None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "AbletonBridge-updater",
                                               "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            chunks, done = [], 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                done += len(chunk)
                if progress and total:
                    progress(min(done / total, 1.0))
            return b"".join(chunks)
    except OSError as exc:  # URLError, timeouts, SSL errors
        raise UpdateError(f"Couldn't reach GitHub ({getattr(exc, 'reason', exc)}). Check your internet "
                          "connection and try again.") from exc


@dataclass
class UpdateInfo:
    build: int
    tag: str
    notes: str
    asset: str
    url: str
    sha256: str
    size: int
    page: str


def check(current: int | None = None, fetch=_get) -> UpdateInfo | None:
    """Return the newer release for this computer, or None if this app is up to date."""
    current = current_build() if current is None else current
    name = asset_name()
    if not name:
        return None
    try:
        release = json.loads(fetch(LATEST_URL))
    except ValueError as exc:
        raise UpdateError("GitHub sent an unexpected answer - try again later.") from exc
    assets = {a.get("name"): a for a in release.get("assets", [])}
    meta_asset = assets.get("latest.json")
    if not meta_asset:
        return None
    try:
        meta = json.loads(fetch(meta_asset["browser_download_url"]))
    except ValueError as exc:
        raise UpdateError("The release's latest.json is unreadable.") from exc
    build = int(meta.get("build", 0))
    if build <= current or name not in assets or name not in meta.get("assets", {}):
        return None
    a = assets[name]
    return UpdateInfo(build, release.get("tag_name", ""), meta.get("notes", ""), name,
                      a["browser_download_url"], meta["assets"][name].lower(), int(a.get("size", 0)),
                      release.get("html_url", RELEASES_PAGE))


def download(info: UpdateInfo, progress=None, fetch=_get) -> Path:
    """Download the update and verify its SHA-256. Returns the path of the zip."""
    data = fetch(info.url, timeout=120, progress=progress)
    digest = hashlib.sha256(data).hexdigest()
    if digest != info.sha256:
        raise UpdateError("The downloaded update didn't match its checksum, so it wasn't installed. "
                          "Try again later.")
    folder = Path(tempfile.mkdtemp(prefix="AbletonBridge-update-"))
    path = folder / info.asset
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------------- installing

def _swap(current: Path, new: Path) -> Path:
    """Put *new* where *current* is. The current file/bundle is renamed to '<name>.old' first, which
    works even while it's running. Returns the path of the old copy (deleted on next start)."""
    old = current.with_name(current.stem + OLD_SUFFIX + current.suffix)
    if old.exists():
        if old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
        else:
            try:
                old.unlink()
            except OSError:
                old = current.with_name(f"{current.stem}{OLD_SUFFIX}{os.getpid()}{current.suffix}")
    os.replace(current, old)
    try:
        os.replace(new, current)
    except OSError:
        os.replace(old, current)  # put the working version back
        raise
    return old


def running_app_path() -> Path:
    exe = Path(sys.executable).resolve()
    if pu.IS_MAC:
        for parent in exe.parents:
            if parent.suffix == ".app":
                return parent
    return exe


def cleanup_old_versions() -> None:
    """Delete what a previous update left behind (call on startup)."""
    if not can_self_update():
        return
    app = running_app_path()
    for leftover in app.parent.glob(f"{app.stem}{OLD_SUFFIX}*{app.suffix}"):
        try:
            if leftover.is_dir():
                shutil.rmtree(leftover)
            else:
                leftover.unlink()
        except OSError:
            pass


def install(zip_path: Path) -> list[str]:
    """Replace the running app with the one in *zip_path*. Returns the command that starts the new
    version; the caller runs it and then quits."""
    if not can_self_update():
        raise UpdateError("This copy runs from source code - update it with 'git pull' instead.")
    app = running_app_path()
    staging = app.parent / f".AbletonBridge-update-{os.getpid()}"
    try:
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()
        if pu.IS_WINDOWS:
            with zipfile.ZipFile(zip_path) as z:
                z.extract(WIN_EXE, staging)
            new = staging / WIN_EXE
        else:
            # ditto keeps the app bundle's permissions, symlinks and code signature intact
            r = subprocess.run(["ditto", "-x", "-k", str(zip_path), str(staging)], capture_output=True, text=True)
            if r.returncode != 0:
                raise UpdateError(f"Couldn't unpack the update ({r.stderr.strip()[:200]}).")
            new = staging / MAC_APP
            subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(new)], capture_output=True)
        if not new.exists():
            raise UpdateError("The update file doesn't contain the app.")
        try:
            _swap(app, new)
        except OSError:
            if not pu.IS_WINDOWS:
                raise
            # Windows wouldn't let the running exe be renamed: let a helper swap it once the app has quit.
            pending = app.with_name(app.stem + ".new" + app.suffix)
            os.replace(new, pending)
            return ["cmd", "/c", str(_windows_helper(app, pending))]
    except PermissionError as exc:
        raise UpdateError(f"No permission to replace the app in {app.parent}. Move the app to a folder you "
                          "own (e.g. Applications or Desktop), or download the new version manually.") from exc
    except OSError as exc:
        raise UpdateError(f"Couldn't install the update ({exc}).") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if pu.IS_WINDOWS:
        return [str(app)]
    return ["open", "-n", str(app)]


def _windows_helper(app: Path, pending: Path) -> Path:
    """A batch file that waits until the old exe can be replaced, swaps it, starts the new one."""
    bat = Path(tempfile.gettempdir()) / f"AbletonBridge-update-{os.getpid()}.bat"
    bat.write_text("\r\n".join([
        "@echo off",
        "set /a tries=0",
        ":loop",
        f'move /Y "{pending}" "{app}" >nul 2>&1 && goto done',
        "set /a tries+=1",
        "if %tries% geq 90 goto end",
        "ping -n 2 127.0.0.1 >nul",
        "goto loop",
        ":done",
        f'start "" "{app}"',
        ":end",
        'del "%~f0"',
    ]) + "\r\n", encoding="utf-8")
    return bat


def restart(command: list[str]) -> None:
    flags = 0
    if pu.IS_WINDOWS:
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    subprocess.Popen(command, close_fds=True, creationflags=flags)
