import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from abletonbridge import updater  # noqa: E402


def fake_github(build: int, payloads: dict[str, bytes]):
    """A fake GitHub: the latest release lists latest.json plus the three apps."""
    meta = {"build": build, "commit": "abc1234", "notes": "Big buttons follow the selection",
            "assets": {n: hashlib.sha256(b).hexdigest() for n, b in payloads.items()}}
    files = {f"https://dl/{n}": b for n, b in payloads.items()}
    files["https://dl/latest.json"] = json.dumps(meta).encode()
    release = {"tag_name": f"build-{build}", "html_url": "https://github.com/x/releases/tag/build-%d" % build,
               "assets": [{"name": n, "browser_download_url": f"https://dl/{n}", "size": len(files[f"https://dl/{n}"])}
                          for n in list(payloads) + ["latest.json"]]}

    def fetch(url, timeout=20, progress=None):
        if url == updater.LATEST_URL:
            return json.dumps(release).encode()
        if progress:
            progress(1.0)
        return files[url]
    return fetch


APPS = {"AbletonBridge-Windows.zip": b"win", "AbletonBridge-Mac-AppleSilicon.zip": b"arm",
        "AbletonBridge-Mac-Intel.zip": b"intel"}


def test_asset_for_each_computer():
    assert updater.asset_name("Windows", "AMD64") == "AbletonBridge-Windows.zip"
    assert updater.asset_name("Darwin", "arm64") == "AbletonBridge-Mac-AppleSilicon.zip"
    assert updater.asset_name("Darwin", "x86_64") == "AbletonBridge-Mac-Intel.zip"
    assert updater.asset_name("Linux", "x86_64") is None


def test_check_finds_newer_build_only(monkeypatch):
    monkeypatch.setattr(updater, "asset_name", lambda: "AbletonBridge-Windows.zip")
    fetch = fake_github(14, APPS)
    info = updater.check(current=11, fetch=fetch)
    assert info.build == 14 and info.asset == "AbletonBridge-Windows.zip"
    assert info.sha256 == hashlib.sha256(b"win").hexdigest() and info.notes
    assert updater.check(current=14, fetch=fetch) is None
    assert updater.check(current=20, fetch=fetch) is None


def test_download_checks_the_checksum(monkeypatch):
    monkeypatch.setattr(updater, "asset_name", lambda: "AbletonBridge-Mac-Intel.zip")
    fetch = fake_github(5, APPS)
    info = updater.check(current=1, fetch=fetch)
    path = updater.download(info, fetch=fetch)
    assert path.read_bytes() == b"intel"
    info.sha256 = "0" * 64                                       # tampered / corrupted download
    with pytest.raises(updater.UpdateError, match="checksum"):
        updater.download(info, fetch=fetch)


def test_swap_file_and_bundle(tmp_path):
    exe = tmp_path / "AbletonBridge.exe"
    exe.write_bytes(b"old")
    new = tmp_path / "staging" / "AbletonBridge.exe"
    new.parent.mkdir()
    new.write_bytes(b"new")
    old = updater._swap(exe, new)
    assert exe.read_bytes() == b"new" and old.name == "AbletonBridge.old.exe" and old.read_bytes() == b"old"
    # a leftover from an earlier update doesn't get in the way
    new2 = tmp_path / "staging" / "AbletonBridge.exe"
    new2.write_bytes(b"newer")
    updater._swap(exe, new2)
    assert exe.read_bytes() == b"newer"

    app = tmp_path / "Ableton Bridge.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "v").write_text("1")
    new_app = tmp_path / "st" / "Ableton Bridge.app"
    (new_app / "Contents").mkdir(parents=True)
    (new_app / "Contents" / "v").write_text("2")
    updater._swap(app, new_app)
    assert (app / "Contents" / "v").read_text() == "2"
    assert (tmp_path / "Ableton Bridge.old.app" / "Contents" / "v").read_text() == "1"


def test_swap_puts_the_working_app_back_if_it_fails(tmp_path, monkeypatch):
    exe = tmp_path / "AbletonBridge.exe"
    exe.write_bytes(b"old")
    real = updater.os.replace
    calls = []

    def flaky(a, b):
        calls.append((a, b))
        if len(calls) == 2:
            raise OSError("disk full")
        return real(a, b)
    monkeypatch.setattr(updater.os, "replace", flaky)
    with pytest.raises(OSError):
        updater._swap(exe, tmp_path / "missing.exe")
    assert exe.read_bytes() == b"old"


def _as_windows_app(monkeypatch, tmp_path):
    exe = tmp_path / "AbletonBridge.exe"
    exe.write_bytes(b"old")
    monkeypatch.setattr(updater, "can_self_update", lambda: True)
    monkeypatch.setattr(updater, "running_app_path", lambda: exe)
    monkeypatch.setattr(updater.pu, "IS_WINDOWS", True)
    z = tmp_path / "AbletonBridge-Windows.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("AbletonBridge.exe", b"new build")
    return exe, z


def test_install_on_windows(monkeypatch, tmp_path):
    exe, z = _as_windows_app(monkeypatch, tmp_path)
    cmd = updater.install(z)
    assert cmd == [str(exe)] and exe.read_bytes() == b"new build"
    assert not list(tmp_path.glob(".AbletonBridge-update-*"))      # staging cleaned up
    updater.cleanup_old_versions()
    assert not (tmp_path / "AbletonBridge.old.exe").exists()


def test_install_on_windows_when_the_running_exe_is_locked(monkeypatch, tmp_path):
    exe, z = _as_windows_app(monkeypatch, tmp_path)
    monkeypatch.setattr(updater, "_swap", lambda a, b: (_ for _ in ()).throw(PermissionError("in use")))
    cmd = updater.install(z)
    assert cmd[:2] == ["cmd", "/c"]
    bat = Path(cmd[2]).read_text()
    pending = tmp_path / "AbletonBridge.new.exe"
    assert pending.read_bytes() == b"new build" and exe.read_bytes() == b"old"
    assert f'move /Y "{pending}" "{exe}"' in bat and f'start "" "{exe}"' in bat


def test_running_from_source_never_self_updates(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert not updater.can_self_update()
    with pytest.raises(updater.UpdateError, match="git pull"):
        updater.install(Path("x.zip"))
