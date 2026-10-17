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
    assert list((local / "Backup").glob("Before get */Song.als"))


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


# ----------------------------------------------------------------------------- projects already on the drive

def existing_drive(tmp_path):
    usb = tmp_path / "USB"
    write(usb / "SampleLib" / "kick.wav", b"K" * 40)
    write(usb / "SampleLib" / "snare.wav", b"S" * 30)
    old = usb / "Music" / "Old Song Project"
    write(old / "Samples" / "Recorded" / "vox.wav", b"V" * 20)
    make_set(old / "Old Song.als", [
        audio_track("Kick", modern_fileref("", "/Volumes/USB/SampleLib/kick.wav", 1, 40)),   # saved on the Mac
        audio_track("Snare", modern_fileref("", "E:/SampleLib/snare.wav", 1, 30)),           # saved on the PC
        audio_track("Vox", modern_fileref("Samples/Recorded/vox.wav", "/x/vox.wav", 3, 20)),
    ])
    for folder in ("Beats", "Remixes"):
        make_set(usb / folder / "Untitled Project" / "Untitled.als", [])
    make_set(usb / "Loose.als", [])  # loose set at the top of the drive: ignored, not swallowed
    return usb, Drive.initialize(usb)


def test_existing_projects_on_drive_are_found(tmp_path):
    mac, pc = stores(tmp_path)
    usb, drive = existing_drive(tmp_path)
    rows = {r.name: r for r in project_rows(pc, drive, rescan=True)}
    assert set(rows) == {"Old Song Project", "Untitled Project", "Untitled Project (Remixes)"}
    assert rows["Old Song Project"].status == "drive_only"
    assert rows["Old Song Project"].manifest["path"] == "Music/Old Song Project"
    assert drive.scan_notes and "loose" in drive.scan_notes[0]
    # rescanning doesn't duplicate or bump anything
    drive.scan_existing(force=True)
    assert drive.manifest("Old Song Project")["revision"] == 1


def test_get_existing_project_and_send_back_to_its_folder(tmp_path):
    mac, pc = stores(tmp_path)
    usb, drive = existing_drive(tmp_path)
    drive.scan_existing(force=True)

    got = pull(pc, drive, "Old Song Project")
    local = got.local_dir
    assert got.main_set == local / "Old Song.als"
    # samples elsewhere on the drive (Mac and PC style paths) were packed into the project
    assert (local / "Samples" / "Imported" / "kick.wav").read_bytes() == b"K" * 40
    assert (local / "Samples" / "Imported" / "snare.wav").is_file()
    assert collect_project(local).missing == []
    assert {r.name: r for r in project_rows(pc, drive)}["Old Song Project"].status == "synced"

    # edit + send: goes back into Music/Old Song Project, not AbletonBridge/Projects
    st = (local / "Old Song.als").stat()
    os.utime(local / "Old Song.als", (st.st_mtime + 100, st.st_mtime + 100))
    res = push(pc, drive, local)
    assert res.revision == 2
    assert (usb / "Music" / "Old Song Project" / "Samples" / "Imported" / "kick.wav").is_file()
    assert not (drive.projects_dir / "Old Song Project").exists()

    # the Mac gets it from the drive and everything resolves there too
    got_mac = pull(mac, drive, "Old Song Project")
    assert collect_project(got_mac.local_dir).missing == []


def test_project_edited_directly_on_drive(tmp_path):
    mac, pc = stores(tmp_path)
    usb, drive = existing_drive(tmp_path)
    drive.scan_existing(force=True)
    pull(pc, drive, "Old Song Project")
    on_drive = usb / "Music" / "Old Song Project" / "Old Song.als"
    st = on_drive.stat()
    # an old, forgotten set in the folder must not count as an edit
    make_set(on_drive.parent / "Old Song old.als", [])
    os.utime(on_drive.parent / "Old Song old.als", (st.st_mtime - 9999, st.st_mtime - 9999))
    assert drive.scan_existing(force=True) == []
    # Live saved the set straight onto the drive
    os.utime(on_drive, (st.st_mtime + 500, st.st_mtime + 500))
    assert drive.scan_existing(force=True) == ["Old Song Project"]
    row = {r.name: r for r in project_rows(pc, drive)}["Old Song Project"]
    assert row.status == "drive_newer" and row.sent_by == "edited on the drive"


def test_fat_timezone_shift_is_not_a_change():
    from abletonbridge.sync import same_meta
    assert same_meta([10, 1000.0], [10, 1000.0 + 3600])
    assert same_meta([10, 1000.0], [10, 1001.5])
    assert not same_meta([10, 1000.0], [10, 1000.0 + 1800])
    assert not same_meta([10, 1000.0], [11, 1000.0])


def test_get_into_existing_local_copy(tmp_path):
    from abletonbridge.core import Bridge
    mac, pc = stores(tmp_path)
    usb, drive = existing_drive(tmp_path)
    # the user already copied this project to the PC by hand, and changed it
    import shutil
    mine = Path(pc.settings.workspace) / "Mixes" / "Old Song Project"
    shutil.copytree(usb / "Music" / "Old Song Project", mine)
    st = (mine / "Old Song.als").stat()
    os.utime(mine / "Old Song.als", (st.st_mtime + 50, st.st_mtime + 50))
    b = Bridge(pc)
    b.drive = drive
    assert {r.name: r for r in b.rows(rescan=True)}["Old Song Project"].status == "unlinked"
    with pytest.raises(Conflict) as exc:
        b.get("Old Song Project")
    assert exc.value.kind == "local_exists"
    res = b.get("Old Song Project", resolution="backup_and_get")
    assert res.local_dir == mine and "Old Song.als" in res.backups
    assert {r.name: r for r in b.rows()}["Old Song Project"].status == "synced"


def test_unusual_fileref_layouts_are_edited_safely(tmp_path):
    """Layouts that real sets contain: attributes on FileRef, empty/self-closing FileRefs, CRLF line ends."""
    import gzip
    proj = tmp_path / "ghost city Project"
    ext = write(tmp_path / "lib" / "808 Hat Open 04.flac", b"H" * 12)
    ref_with_attr = modern_fileref("", str(ext), 1, 12).replace("<FileRef>", '<FileRef Id="7">', 1)
    tracks = [
        audio_track("Hat", ref_with_attr),
        audio_track("Empty", "<FileRef>\n\t\t</FileRef>"),
        audio_track("Closed", "<FileRef />"),
    ]
    make_set(proj / "ghost city.als", tracks)
    xml = read_set_xml(proj / "ghost city.als").replace("\n", "\r\n")
    (proj / "ghost city.als").write_bytes(gzip.compress(xml.encode()))
    before = read_set_xml(proj / "ghost city.als")

    als = AbletonSet(proj / "ghost city.als")
    assert als.editable
    res = collect_project(proj)
    assert res.errors == [] and res.missing == []
    assert res.files_copied == ["Samples/Imported/808 Hat Open 04.flac"]
    after = read_set_xml(proj / "ghost city.als")
    assert '<FileRef Id="7">' in after and "<FileRef>\r\n\t\t</FileRef>" in after and "<FileRef />" in after
    import re
    strip = lambda s: re.sub(r"<FileRef[ >].*?</FileRef>", "", s, flags=re.S)  # noqa: E731
    assert strip(before) == strip(after)
    ref = [r for r in AbletonSet(proj / "ghost city.als").filerefs()
           if not r.informational and not r.empty][0]
    assert ref.relative_path == "Samples/Imported/808 Hat Open 04.flac" and ref.relative_path_type == 3


def test_uneditable_set_never_breaks_get(tmp_path, monkeypatch):
    mac, pc = stores(tmp_path)
    usb, drive = existing_drive(tmp_path)
    drive.scan_existing(force=True)
    monkeypatch.setattr(AbletonSet, "editable", property(lambda self: False))
    logs = []
    res = pull(pc, drive, "Old Song Project", log=logs.append)
    assert res.files_received > 0 and res.main_set.is_file()
    assert any("left unchanged" in line for line in logs)
    assert not (res.local_dir / "Samples" / "Imported").exists()  # nothing copied for a set left as-is


# ----------------------------------------------------------------------------- same names (regressions)

def _bump(path, secs=60):
    st = path.stat()
    os.utime(path, (st.st_mtime + secs, st.st_mtime + secs))


def _song(folder, tag, sample=b"S"):
    write(folder / "Samples" / "Recorded" / "Audio 1.wav", sample * 10)
    return make_set(folder / f"{folder.name.replace(' Project', '')}.als",
                    [audio_track(tag, modern_fileref("Samples/Recorded/Audio 1.wav", "/x", 3, len(sample) * 10))])


def _exfat(monkeypatch):
    """Make drive writes behave like exFAT: names with  " * : < > ? \\ |  are refused."""
    import abletonbridge.platform_utils as pu
    real = pu.copy_file

    def copy(src, dst, *a, **k):
        if any(c in Path(dst).name for c in '"*:<>?\\|') or any(c in str(dst) for c in ':?*"<>|'
                                                                 if "USB" in str(dst)):
            raise OSError(22, "Invalid argument")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(pu, "copy_file", copy)


def test_two_different_untitled_projects_never_replace_each_other(tmp_path):
    mac, pc = stores(tmp_path)
    drive = Drive.initialize(tmp_path / "USB")
    a = tmp_path / "Music" / "Untitled Project"
    b = tmp_path / "Desktop" / "Untitled Project"
    _song(a, "SONG-A")
    _song(b, "SONG-B", b"B")
    mac.settings.project_roots = [str(tmp_path / "Music"), str(tmp_path / "Desktop")]
    push(mac, drive, a)
    rows = [r for r in project_rows(mac, drive) if r.local_dir]
    assert len(rows) == 2 and len({r.label for r in rows}) == 2       # both listed, told apart
    with pytest.raises(Conflict) as exc:
        push(mac, drive, b)
    assert exc.value.kind == "name_taken"
    r = push(mac, drive, b, resolution="keep_both")
    assert r.drive_name == "Untitled Project (Studio-Mac)"
    assert "SONG-A" in read_set_xml(drive.projects_dir / "Untitled Project" / "Untitled.als")


def test_replaced_by_a_different_song_never_lands_in_my_folder(tmp_path):
    mac, pc = stores(tmp_path)
    drive = Drive.initialize(tmp_path / "USB")
    mine = Path(mac.settings.workspace) / "Untitled Project"
    _song(mine, "MAC-SONG")
    push(mac, drive, mine)
    theirs = Path(pc.settings.workspace) / "Untitled Project"
    _song(theirs, "PC-SONG", b"P")
    with pytest.raises(Conflict):
        push(pc, drive, theirs)
    push(pc, drive, theirs, resolution="overwrite")
    assert list((drive.projects_dir / "Untitled Project" / "Backup").glob("Before send */Untitled.als"))
    row = {(r.name, r.status) for r in project_rows(mac, drive)}
    assert ("Untitled Project", "local_only") in row and ("Untitled Project", "drive_only") in row
    got = pull(mac, drive, "Untitled Project")
    assert got.local_dir != mine and "PC-SONG" in read_set_xml(got.main_set)
    assert "MAC-SONG" in read_set_xml(mine / "Untitled.als")          # my song untouched


def test_separate_copy_then_get_the_other_version(tmp_path, song):
    mac, pc = stores(tmp_path)
    drive = Drive.initialize(tmp_path / "USB")
    push(mac, drive, song)
    pc_local = pull(pc, drive, "Song Project").local_dir
    _bump(pc_local / "Song.als", 100)
    push(pc, drive, pc_local)
    _bump(song / "Song.als", 200)
    with pytest.raises(Conflict):
        push(mac, drive, song)
    push(mac, drive, song, resolution="keep_both")
    got = pull(mac, drive, "Song Project")
    assert got.local_dir != song                                       # not merged into my folder
    assert mac.drive_name_for_local(song) == "Song Project (Studio-Mac)"


def test_new_song_saved_where_an_old_one_was_renamed_from(tmp_path):
    mac, pc = stores(tmp_path)
    drive = Drive.initialize(tmp_path / "USB")
    p = tmp_path / "Music" / "Untitled Project"
    _song(p, "OLD")
    push(mac, drive, p)
    p.rename(tmp_path / "Music" / "Night Drive Project")
    _song(p, "NEW", b"N")                                              # Live's default name again
    with pytest.raises(Conflict) as exc:
        push(mac, drive, p)
    assert exc.value.kind == "name_taken"


def test_moved_project_folder_stays_linked(tmp_path, song):
    mac, pc = stores(tmp_path)
    drive = Drive.initialize(tmp_path / "USB")
    push(mac, drive, song)
    moved = tmp_path / "Desktop" / "Song Project"
    moved.parent.mkdir()
    song.rename(moved)
    mac.settings.project_roots = [str(tmp_path / "Desktop")]
    row = {r.name: r for r in project_rows(mac, drive)}["Song Project"]
    assert row.local_dir == moved and row.status == "synced"


def test_saved_on_the_drive_then_send_or_get_right_away(tmp_path, song):
    mac, pc = stores(tmp_path)
    drive = Drive.initialize(tmp_path / "USB")
    push(mac, drive, song)
    pull(pc, drive, "Song Project")
    on_drive = drive.projects_dir / "Song Project" / "Song.als"
    make_set(on_drive, [midi_track("SAVED-ON-DRIVE", "")])
    _bump(on_drive, 600)
    # Get right away (no list refresh in between) gets the drive version
    got = pull(pc, drive, "Song Project")
    assert "SAVED-ON-DRIVE" in read_set_xml(got.main_set)
    # The Mac's older copy can't silently replace it
    _bump(song / "Song.als", 700)
    with pytest.raises(Conflict) as exc:
        push(mac, drive, song)
    assert exc.value.kind == "drive_newer"
    push(mac, drive, song, resolution="overwrite")
    backup = next((drive.projects_dir / "Song Project" / "Backup").glob("Before send */Song.als"))
    assert "SAVED-ON-DRIVE" in read_set_xml(backup)


def test_interrupted_send_answer_doesnt_skip_other_questions(tmp_path, song):
    mac, pc = stores(tmp_path)
    drive = Drive.initialize(tmp_path / "USB")
    push(mac, drive, song)
    local = pull(pc, drive, "Song Project").local_dir
    write(local / "Song.wav", b"MY MIXDOWN")                           # changes on the PC
    write(local / "Samples" / "Recorded" / "Vox.wav", b"EDITED" * 30)
    _bump(song / "Song.als", 300)
    push(mac, drive, song)
    drive.lock("Song Project", "Studio-Mac")
    with pytest.raises(Conflict) as exc:
        pull(pc, drive, "Song Project")
    assert exc.value.kind == "incomplete"
    with pytest.raises(Conflict) as exc:
        pull(pc, drive, "Song Project", resolution=("continue",))
    assert exc.value.kind == "both_changed"
    res = pull(pc, drive, "Song Project", resolution=("continue", "backup_and_get"))
    assert "Samples/Recorded/Vox.wav" in res.backups
    assert next(local.glob("Backup/Before get */Samples/Recorded/Vox.wav")).read_bytes() == b"EDITED" * 30


def test_names_the_drive_cant_store_dont_block_send(tmp_path, monkeypatch):
    _exfat(monkeypatch)
    mac, pc = stores(tmp_path)
    drive = Drive.initialize(tmp_path / "USB")
    proj = tmp_path / "Music" / "What If? Project"
    _song(proj, "WHAT-IF")                                             # -> "What If?.als"
    write(proj / "Bounces" / "Mix 10:17.wav", b"BOUNCE")
    write(proj / "Backup" / "What If? [2026-10-16 221503].als", b"live backup")
    write(proj / "Samples" / "Imported" / "Kick: Hard.wav", b"K" * 9)   # from Live's own Collect All
    make_set(proj / "What If?.als", [
        audio_track("Vox", modern_fileref("Samples/Recorded/Audio 1.wav", "/x", 3, 10)),
        audio_track("Kick", modern_fileref("Samples/Imported/Kick: Hard.wav", "/x", 3, 9))])
    res = push(mac, drive, proj)
    assert res.drive_name == "What If_ Project" and res.main_set == "What If_.als"
    files = drive.manifest("What If_ Project")["files"]
    assert "Bounces/Mix 10_17.wav" in files and "Samples/Imported/Kick_ Hard.wav" in files
    assert not any(":" in f or "?" in f for f in files)
    assert sum(1 for f in files if f.startswith("Samples/Imported/Kick")) == 1   # no duplicate copy
    got = pull(pc, drive, "What If_ Project")
    assert got.main_set.name == "What If_.als" and collect_project(got.local_dir).missing == []
    again = push(mac, drive, proj)                                     # stable: nothing new to send
    assert again.files_sent == 0


def test_case_twins_are_explained(tmp_path):
    from abletonbridge.sync import BridgeError
    mac, pc = stores(tmp_path)
    drive = Drive.initialize(tmp_path / "USB")
    proj = tmp_path / "Music" / "Twin Project"
    _song(proj, "T")
    write(proj / "Samples" / "Recorded" / "audio 1.wav", b"other")
    if (proj / "Samples" / "Recorded" / "Audio 1.wav").read_bytes() == b"other":
        pytest.skip("case-insensitive filesystem")
    with pytest.raises(BridgeError, match="capital letters"):
        push(mac, drive, proj)


def test_projects_synced_by_the_older_version_keep_working(tmp_path, song):
    import json
    mac, pc = stores(tmp_path)
    drive = Drive.initialize(tmp_path / "USB")
    push(mac, drive, song)
    local = pull(pc, drive, "Song Project").local_dir
    # simulate the previous app version: no ids anywhere
    from abletonbridge.sync import ID_FILE
    for folder in (song, local, drive.projects_dir / "Song Project"):
        folder.joinpath(*ID_FILE).unlink()
    m = drive.manifest("Song Project")
    m.pop("id")
    m["files"].pop("/".join(ID_FILE))
    drive.write_manifest("Song Project", m)
    for store in (mac, pc):
        for st in store.state["projects"].values():
            st["project_id"] = None
            st["files"].pop("/".join(ID_FILE), None)
        store.save_state()
    pull(mac, drive, "Song Project")                                   # Mac gets: drive assigns an id
    _bump(local / "Song.als", 50)
    r = push(pc, drive, local)                                         # PC: no "different project" question
    assert r.revision == 2
    assert json.loads(local.joinpath(*ID_FILE).read_text())["id"] == drive.manifest("Song Project")["id"]


def test_names_with_dots_are_not_mangled():
    assert safe_filename("Mr. Brightside Project") == "Mr. Brightside Project"
    assert safe_filename("Vol. 2 Kick.wav") == "Vol. 2 Kick.wav"
    assert safe_filename("Vox feat. J.") == "Vox feat. J"
    assert safe_filename("Kick: Hard.wav") == "Kick_ Hard.wav"
    from abletonbridge.collect import name_is_portable
    assert name_is_portable("Samples/Vol. 2 Pack/Kick.wav")


def test_renamed_files_resolve_on_the_other_computer(tmp_path, monkeypatch):
    """Every file the app renames is renamed in the set too, so Live finds it on the other computer."""
    _exfat(monkeypatch)
    mac, pc = stores(tmp_path)
    drive = Drive.initialize(tmp_path / "USB")
    proj = tmp_path / "Music" / "Kicks Project"
    k1 = write(tmp_path / "Splice" / "A" / "kick.wav", b"KICK-ONE" * 5)
    k2 = write(tmp_path / "Splice" / "B" / "kick.wav", b"KICK-TWO" * 7)
    k3 = write(proj / "Samples" / "Recorded" / "Take: 1.wav", b"TAKE" * 3)
    make_set(proj / "Kicks.als", [
        audio_track("K1", modern_fileref("", str(k1), 1, 40)),
        audio_track("K2", modern_fileref("", str(k2), 1, 56)),
        audio_track("Take", modern_fileref("Samples/Recorded/Take: 1.wav", str(k3), 3, 12)),
    ])
    res = push(mac, drive, proj)
    assert res.warnings == []
    got = pull(pc, drive, "Kicks Project").local_dir
    import shutil
    shutil.rmtree(tmp_path / "Splice")
    refs = {r.relative_path: r for r in AbletonSet(got / "Kicks.als").filerefs() if not r.informational}
    played = {}
    for rel in refs:
        assert (got / rel).is_file(), f"Live would show {rel} as missing"
        played[rel] = (got / rel).read_bytes()
    assert sorted(played.values()) == sorted([b"KICK-ONE" * 5, b"KICK-TWO" * 7, b"TAKE" * 3])


def test_warns_when_a_used_file_cant_be_renamed_in_the_set(tmp_path, monkeypatch):
    _exfat(monkeypatch)
    mac, pc = stores(tmp_path)
    drive = Drive.initialize(tmp_path / "USB")
    proj = tmp_path / "Music" / "Stuck Project"
    write(proj / "Samples" / "Recorded" / "Take: 1.wav", b"TAKE" * 3)
    make_set(proj / "Stuck.als", [audio_track("T", modern_fileref("Samples/Recorded/Take: 1.wav", "/x", 3, 12))])
    monkeypatch.setattr(AbletonSet, "editable", property(lambda self: False))
    res = push(mac, drive, proj)
    assert len(res.warnings) == 1 and "Collect All and Save" in res.warnings[0]


# ----------------------------------------------------------------------------- versions / who saved it

def _both_edited(tmp_path, song):
    mac, pc = stores(tmp_path)
    drive = Drive.initialize(tmp_path / "USB")
    write(song / "Backup" / "Song [2026-10-16 221503].als", b"live backup made on the mac")
    push(mac, drive, song)
    local = pull(pc, drive, "Song Project").local_dir
    make_set(local / "Song.als", [midi_track("PC-EDIT", "")])            # PC works on it...
    write(local / "Samples" / "Recorded" / "Vox.wav", b"PC VOX" * 40)
    _bump(local / "Song.als", 100)
    make_set(song / "Song.als", [midi_track("MAC-EDIT", "")])            # ...and so does the Mac
    _bump(song / "Song.als", 200)
    push(mac, drive, song)
    return mac, pc, drive, local


def test_question_says_which_computer_saved_each_version(tmp_path, song):
    mac, pc, drive, local = _both_edited(tmp_path, song)
    with pytest.raises(Conflict) as exc:
        pull(pc, drive, "Song Project")
    c = exc.value
    assert set(c.options) == {"use_mine", "backup_and_get", "keep_both"}
    mine, theirs = c.versions
    assert mine["computer"] == "Studio-PC (this computer)" and theirs["computer"] == "Studio-Mac"
    assert mine["when"] and theirs["when"] and c.project["local_dir"] == str(local)
    # an answer meant for the other action is ignored, never treated as "go ahead"
    with pytest.raises(Conflict):
        pull(pc, drive, "Song Project", resolution=("use_mine",))
    with pytest.raises(Conflict):
        pull(pc, drive, "Song Project", resolution="overwrite")
    local_set = (local / "Song.als").read_bytes()
    _bump(local / "Song.als", 5)
    with pytest.raises(Conflict):
        push(pc, drive, local, resolution=("use_theirs",))
    assert (local / "Song.als").read_bytes() == local_set


def test_use_the_drives_version_keeps_mine_in_versions(tmp_path, song):
    from abletonbridge.versions import list_versions, open_version
    mac, pc, drive, local = _both_edited(tmp_path, song)
    pull(pc, drive, "Song Project", resolution=("backup_and_get",))
    vs = list_versions(pc, drive, "Song Project", local)
    current = next(v for v in vs if v.kind == "current")
    kept = next(v for v in vs if v.kind == "before_get")
    live = next(v for v in vs if v.kind == "live_backup")
    assert current.computer == "Studio-Mac" and "MAC-EDIT" in read_set_xml(current.set_path)
    assert kept.computer == "Studio-PC (this computer)" and "PC-EDIT" in read_set_xml(kept.set_path)
    assert live.computer == "Studio-Mac"                                  # Live's own backup, made on the Mac
    # opening my old version never overwrites anything; it changed a sound, so it opens as its own copy
    opened = open_version(kept, local)
    assert opened.parent != local and "PC-EDIT" in read_set_xml(opened)
    assert (opened.parent / "Samples" / "Recorded" / "Vox.wav").read_bytes() == b"PC VOX" * 40
    assert "MAC-EDIT" in read_set_xml(local / "Song.als")
    assert (local / "Samples" / "Recorded" / "Vox.wav").read_bytes() != b"PC VOX" * 40


def test_use_mine_keeps_the_drives_version_on_the_drive(tmp_path, song):
    from abletonbridge.versions import list_versions
    mac, pc, drive, local = _both_edited(tmp_path, song)
    with pytest.raises(Conflict) as exc:
        push(pc, drive, local)
    assert set(exc.value.options) == {"overwrite", "use_theirs", "keep_both"}
    assert exc.value.versions[1]["computer"] == "Studio-Mac"
    push(pc, drive, local, resolution=("overwrite",))
    vs = list_versions(pc, drive, "Song Project", local)
    kept = next(v for v in vs if v.kind == "before_send")
    assert kept.on_drive and kept.computer == "Studio-Mac" and "MAC-EDIT" in read_set_xml(kept.set_path)
    assert next(v for v in vs if v.kind == "current").computer == "Studio-PC (this computer)"
    # the Mac sees the PC's version as the current one, saved on Studio-PC
    pull(mac, drive, "Song Project", resolution=("backup_and_get",))
    cur = next(v for v in list_versions(mac, drive, "Song Project", song) if v.kind == "current")
    assert cur.computer == "Studio-PC" and "PC-EDIT" in read_set_xml(cur.set_path)


def test_open_older_set_version_copies_it_next_to_current(tmp_path, song):
    from abletonbridge.versions import list_versions, open_version
    mac, pc = stores(tmp_path)
    drive = Drive.initialize(tmp_path / "USB")
    write(song / "Backup" / "Song [2026-10-16 221503].als", (song / "Song.als").read_bytes())
    push(mac, drive, song)
    vs = list_versions(mac, drive, "Song Project", song)
    live = next(v for v in vs if v.kind == "live_backup")
    assert live.computer == "Studio-Mac (this computer)"
    assert any(v.kind == "before_pack" for v in vs)                  # the app's own backup, labelled as such
    before = (song / "Song.als").read_bytes()
    opened = open_version(live, song)
    assert opened.parent == song and opened.name.startswith("Song (Studio-Mac ") and opened.suffix == ".als"
    assert (song / "Song.als").read_bytes() == before
