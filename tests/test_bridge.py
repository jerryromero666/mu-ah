import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import (au, audio_track, legacy_fileref, m4l, make_set, midi_track, modern_fileref,  # noqa: E402
                     read_set_xml, vst2, vst3)

from abletonbridge.alsfile import AbletonSet  # noqa: E402
from abletonbridge.collect import collect_project, safe_filename  # noqa: E402
from abletonbridge.config import LocalStore  # noqa: E402
from abletonbridge.plugins import check_plugin, names_match  # noqa: E402
from abletonbridge.report import Machine, build_report  # noqa: E402
from abletonbridge.sync import Conflict, Drive, project_rows, pull, push  # noqa: E402


def write(path: Path, data: bytes = b"RIFF....WAVEdata") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def song(tmp_path):
    """A Live 12 project that uses a sample from outside the project, one inside, a M4L device and plugins."""
    proj = tmp_path / "mac" / "Music" / "Song Project"
    external = write(tmp_path / "mac" / "Splice" / "Kick: Hard.wav", b"KICK" * 100)
    inside = write(proj / "Samples" / "Recorded" / "Vox.wav", b"VOX" * 50)
    device = write(tmp_path / "mac" / "UserLib" / "Glue Thing.amxd", b"AMXD")
    tracks = [
        audio_track("Kick", modern_fileref("../Splice/Kick: Hard.wav", str(external), rpt=1, size=400),
                    devices=vst3("Pro-Q 3") + m4l(modern_fileref("", str(device), rpt=1, size=4))),
        audio_track("Vox", modern_fileref("Samples/Recorded/Vox.wav", str(inside), rpt=3, size=150),
                    devices=vst2("Valhalla VintageVerb", "/Library/Audio/Plug-Ins/VST/ValhallaVintageVerb.vst")),
        midi_track("Bass", vst3("Serum") + au("Diva", "u-he"), frozen=True),
        midi_track("Lead", au("Diva", "u-he")),
    ]
    make_set(proj / "Song.als", tracks)
    return proj


def test_parse_filerefs_and_plugins(song):
    als = AbletonSet(song / "Song.als")
    refs = [r for r in als.filerefs() if not r.informational]
    assert {r.filename for r in refs} == {"Kick: Hard.wav", "Vox.wav", "Glue Thing.amxd"}
    informational = [r for r in als.filerefs() if r.informational]
    assert len(informational) == 3  # two SourceContext + one LastPresetRef
    plugins = {(p.format, p.name): p for p in als.plugins()}
    assert set(plugins) == {("vst3", "Pro-Q 3"), ("vst2", "Valhalla VintageVerb"), ("vst3", "Serum"),
                            ("au", "Diva")}
    assert plugins[("au", "Diva")].tracks == ["Bass", "Lead"]
    assert plugins[("au", "Diva")].frozen_tracks == ["Bass"]
    assert plugins[("vst3", "Serum")].frozen_tracks == ["Bass"]


def test_unchanged_set_renders_identically(song):
    als = AbletonSet(song / "Song.als")
    assert als.render() == read_set_xml(song / "Song.als")


def test_collect_makes_project_self_contained(song, tmp_path):
    before = read_set_xml(song / "Song.als")
    res = collect_project(song)
    assert res.missing == []
    assert sorted(res.files_copied) == ["Presets/Imported/Glue Thing.amxd", "Samples/Imported/Kick_ Hard.wav"]
    assert (song / "Samples" / "Imported" / "Kick_ Hard.wav").read_bytes() == b"KICK" * 100
    assert list((song / "Backup").glob("Song *.als")), "original set must be backed up"

    after = read_set_xml(song / "Song.als")
    # Only FileRef blocks changed: everything outside them is byte-identical.
    import re
    strip = lambda s: re.sub(r"<FileRef>.*?</FileRef>", "", s, flags=re.S)  # noqa: E731
    assert strip(before) == strip(after)

    als = AbletonSet(song / "Song.als")
    refs = {r.filename: r for r in als.filerefs() if not r.informational}
    assert refs["Kick_ Hard.wav"].relative_path == "Samples/Imported/Kick_ Hard.wav"
    assert refs["Kick_ Hard.wav"].relative_path_type == 3
    assert refs["Glue Thing.amxd"].relative_path == "Presets/Imported/Glue Thing.amxd"
    # informational refs untouched
    assert "/Users/x/Library/preset.adv" in after

    # Moving the project somewhere else (other computer) - everything still resolves.
    moved = tmp_path / "pc" / "Song Project"
    import shutil
    shutil.copytree(song, moved)
    shutil.rmtree(tmp_path / "mac" / "Splice")
    res2 = collect_project(moved)
    assert res2.missing == [] and res2.files_copied == [] and res2.refs_fixed == 0


def test_collect_dedupes_and_is_idempotent(song):
    collect_project(song)
    again = collect_project(song)
    assert again.files_copied == [] and again.refs_fixed == 0 and again.sets_changed == []


def test_missing_file_reported(tmp_path):
    proj = tmp_path / "Lost Project"
    make_set(proj / "Lost.als", [audio_track("A", modern_fileref("", "/gone/away.wav", rpt=1, size=10))])
    res = collect_project(proj)
    assert [m.filename for m in res.missing] == ["away.wav"]


def test_finds_moved_file_by_name_and_size(tmp_path):
    proj = tmp_path / "P Project"
    write(proj / "Samples" / "Somewhere" / "snare.wav", b"S" * 77)
    make_set(proj / "P.als", [audio_track("A", modern_fileref("Samples/Old/snare.wav", "/old/snare.wav", 3, 77))])
    res = collect_project(proj)
    assert res.missing == []
    refs = [r for r in AbletonSet(proj / "P.als").filerefs() if not r.informational]
    assert refs[0].relative_path == "Samples/Somewhere/snare.wav"


def test_legacy_live10_fileref(tmp_path):
    proj = tmp_path / "Old Project"
    ext = write(tmp_path / "lib" / "hat.wav", b"H" * 9)
    hint = [p.rstrip("\\/") for p in ext.parent.parts if p.rstrip("\\/")]
    make_set(proj / "Old.als", [audio_track("A", legacy_fileref([], "hat.wav", has_rel=False, rpt=1, size=9,
                                                                 hint_dirs=hint))], creator="Ableton Live 10.1.30")
    res = collect_project(proj)
    assert res.missing == [] and res.files_copied == ["Samples/Imported/hat.wav"]
    ref = [r for r in AbletonSet(proj / "Old.als").filerefs() if not r.informational][0]
    assert ref.fmt == "legacy" and ref.has_relative_path and ref.relative_path == "Samples/Imported/hat.wav"
    xml = read_set_xml(proj / "Old.als")
    assert 'Dir="Samples"' in xml and 'Dir="Imported"' in xml


def test_windows_absolute_path_ignored_on_mac(tmp_path):
    proj = tmp_path / "X Project"
    write(proj / "Samples" / "Imported" / "a.wav", b"A")
    make_set(proj / "X.als", [audio_track("A", modern_fileref("Samples/Imported/a.wav", "C:/Users/me/a.wav", 3, 1))])
    ref = [r for r in AbletonSet(proj / "X.als").filerefs() if not r.informational][0]
    cands = ref.candidates(proj)
    if os.name != "nt":
        assert cands == [proj / "Samples" / "Imported" / "a.wav"]


def test_safe_filename():
    assert safe_filename('a:b?c*.wav') == "a_b_c_.wav"
    assert safe_filename("CON.wav") == "_CON.wav"
    assert safe_filename("trailing. .wav") == "trailing.wav"


def test_plugin_name_matching():
    assert names_match("Serum", "Serum_x64")
    assert names_match("Pro-Q 3", "FabFilter Pro-Q 3")
    assert names_match("ValhallaVintageVerb", "Valhalla VintageVerb")
    assert not names_match("Serum", "Serum 2")
    assert not names_match("Q", "Pro-Q 3")


def test_plugin_status(song):
    plugins = {(p.format, p.name): p for p in AbletonSet(song / "Song.als").plugins()}
    pc = {"vst3": ["FabFilter Pro-Q 3"], "vst2": ["ValhallaVintageVerb"], "au": [], "any": []}
    assert check_plugin(plugins[("vst3", "Pro-Q 3")], pc, "windows").level == "ok"
    assert check_plugin(plugins[("vst2", "Valhalla VintageVerb")], pc, "windows").level == "ok"
    # Serum missing but only used on a frozen track -> warning, not error
    assert check_plugin(plugins[("vst3", "Serum")], pc, "windows").level == "warn"
    # Diva AU is used on an unfrozen track -> can't load on Windows
    st = check_plugin(plugins[("au", "Diva")], {**pc, "vst3": ["Diva"]}, "windows")
    assert st.level == "missing" and "VST version is installed" in st.text
    assert check_plugin(plugins[("vst3", "Serum")], None, "windows").level in ("unknown", "warn")


def test_report_live_version(song):
    pc = Machine("pc", "Studio-PC", "windows", {"vst3": [], "vst2": [], "au": [], "any": []}, ["11.3"])
    rep = build_report(song, [pc])
    assert any("Older Live versions can't open it" in t for t in rep.problems_for("pc"))
    assert rep.to_collect == 2
    assert "PLUGINS" in rep.as_text()


# ----------------------------------------------------------------------------- sync

def stores(tmp_path):
    mac = LocalStore(tmp_path / "cfg-mac")
    mac.settings.machine_name = "Studio-Mac"
    mac.settings.workspace = str(tmp_path / "mac-ws")
    pc = LocalStore(tmp_path / "cfg-pc")
    pc.settings.machine_name = "Studio-PC"
    pc.settings.workspace = str(tmp_path / "pc-ws")
    for s in (mac, pc):
        s.settings.project_roots = [s.settings.workspace]
        s.save_settings()
    return mac, pc


def test_round_trip_and_conflicts(song, tmp_path):
    mac, pc = stores(tmp_path)
    drive = Drive.initialize(tmp_path / "USB")

    r1 = push(mac, drive, song)
    assert r1.revision == 1 and r1.main_set == "Song.als"
    assert (drive.projects_dir / "Song Project" / "Samples" / "Imported" / "Kick_ Hard.wav").is_file()
    assert drive.lock_info("Song Project") is None

    # Nothing changed -> second push copies nothing.
    again = push(mac, drive, song)
    assert again.files_sent == 0 and again.revision == 1

    got = pull(pc, drive, "Song Project")
    local = Path(pc.settings.workspace) / "Song Project"
    assert got.local_dir == local and got.main_set == local / "Song.als"
    assert (local / "Samples" / "Imported" / "Kick_ Hard.wav").read_bytes() == b"KICK" * 100
    assert collect_project(local).missing == []
    assert pull(pc, drive, "Song Project").already_current

    rows = {r.name: r for r in project_rows(pc, drive)}
    assert rows["Song Project"].status == "synced"

    # PC edits and sends back.
    time.sleep(0.01)
    write(local / "Samples" / "Recorded" / "Guitar.wav", b"GTR")
    st = (local / "Song.als").stat()
    os.utime(local / "Song.als", (st.st_mtime + 60, st.st_mtime + 60))
    assert {r.name: r for r in project_rows(pc, drive)}["Song Project"].status == "local_newer"
    r2 = push(pc, drive, local)
    assert r2.revision == 2 and r2.files_sent == 2

    assert {r.name: r for r in project_rows(mac, drive)}["Song Project"].status == "drive_newer"

    # Mac edits its old copy without getting the new one first -> push refused.
    st = (song / "Song.als").stat()
    os.utime(song / "Song.als", (st.st_mtime + 120, st.st_mtime + 120))
    assert {r.name: r for r in project_rows(mac, drive)}["Song Project"].status == "conflict"
    with pytest.raises(Conflict) as exc:
        push(mac, drive, song)
    assert exc.value.kind == "drive_newer"
    with pytest.raises(Conflict) as exc:
        pull(mac, drive, "Song Project")
    assert exc.value.kind == "both_changed"

    # Keep both: Mac's version goes to the drive under a new name.
    r3 = push(mac, drive, song, resolution="keep_both")
    assert r3.drive_name == "Song Project (Studio-Mac)"
    assert set(drive.manifests()) == {"Song Project", "Song Project (Studio-Mac)"}

    # PC can get the Mac copy too.
    got2 = pull(pc, drive, "Song Project (Studio-Mac)")
    assert got2.local_dir == Path(pc.settings.workspace) / "Song Project (Studio-Mac)"


def test_pull_backs_up_local_changes(song, tmp_path):
    mac, pc = stores(tmp_path)
    drive = Drive.initialize(tmp_path / "USB")
    push(mac, drive, song)
    pull(pc, drive, "Song Project")
    local = Path(pc.settings.workspace) / "Song Project"
    # PC changes, mac changes & pushes
    (local / "Song.als").write_bytes((local / "Song.als").read_bytes() + b"")
    st = (local / "Song.als").stat()
    os.utime(local / "Song.als", (st.st_mtime + 300, st.st_mtime + 300))
    st = (song / "Song.als").stat()
    os.utime(song / "Song.als", (st.st_mtime + 600, st.st_mtime + 600))
    push(mac, drive, song)
    with pytest.raises(Conflict):
        pull(pc, drive, "Song Project")
    res = pull(pc, drive, "Song Project", resolution="backup_and_get")
    assert "Song.als" in res.backups
    assert list((local / "Backup").glob("Before getting from drive */Song.als"))


def test_name_taken_on_drive(tmp_path):
    mac, pc = stores(tmp_path)
    drive = Drive.initialize(tmp_path / "USB")
    a = tmp_path / "a" / "Demo Project"
    b = tmp_path / "b" / "Demo Project"
    make_set(a / "Demo.als", [])
    make_set(b / "Demo.als", [])
    push(mac, drive, a)
    with pytest.raises(Conflict) as exc:
        push(pc, drive, b)
    assert exc.value.kind == "name_taken"


def test_interrupted_push_detected(song, tmp_path):
    mac, pc = stores(tmp_path)
    drive = Drive.initialize(tmp_path / "USB")
    push(mac, drive, song)
    drive.lock("Song Project", "Studio-Mac")
    with pytest.raises(Conflict) as exc:
        pull(pc, drive, "Song Project")
    assert exc.value.kind == "incomplete"
    assert pull(pc, drive, "Song Project", resolution="continue").files_received > 0


def test_junk_cleanup(tmp_path):
    drive = Drive.initialize(tmp_path / "USB")
    write(drive.projects_dir / "P" / "._Song.als")
    write(drive.projects_dir / "P" / ".DS_Store")
    write(drive.projects_dir / "P" / "Song.als")
    assert drive.clean_junk() == 2
    assert (drive.projects_dir / "P" / "Song.als").exists()
