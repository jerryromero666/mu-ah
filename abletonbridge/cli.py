"""Command line: python -m abletonbridge <command>."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from . import platform_utils as pu
from .core import Bridge
from .sync import STATUS_TEXT, BridgeError, Conflict


def _progress_bar(frac: float) -> None:
    width = 30
    filled = int(frac * width)
    sys.stdout.write(f"\r  [{'#' * filled}{'.' * (width - filled)}] {int(frac * 100):3d}%")
    sys.stdout.flush()
    if frac >= 1.0:
        sys.stdout.write("\n")


def _ask(conflict: Conflict) -> str | None:
    print(f"\n{conflict.title}\n{conflict.message}\n")
    keys = list(conflict.options)
    for i, k in enumerate(keys, 1):
        print(f"  {i}) {conflict.options[k]}")
    print("  0) Cancel")
    try:
        choice = input("Choose: ").strip()
    except EOFError:
        return None
    if choice.isdigit() and 1 <= int(choice) <= len(keys):
        return keys[int(choice) - 1]
    return None


def _with_conflicts(fn, resolution):
    answers = [resolution] if resolution else []
    while True:
        try:
            return fn(tuple(answers))
        except Conflict as c:
            if any(a in c.options for a in answers):
                raise  # already answered this exact question; don't loop
            choice = _ask(c)
            if not choice:
                print("Cancelled - nothing was changed.")
                return None
            answers.append(choice)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="abletonbridge", description="Move Ableton projects between computers.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("gui", help="open the window (default)")
    sub.add_parser("status", help="list projects and their sync status")
    p = sub.add_parser("send", help="send a project to the drive (default: the one saved most recently)")
    p.add_argument("project", nargs="?", help="project folder")
    p.add_argument("--save", action="store_true", help="press Save in Ableton Live first")
    p.add_argument("--resolve", choices=["keep_both", "overwrite"])
    p = sub.add_parser("get", help="get a project from the drive (default: latest from the other computer)")
    p.add_argument("name", nargs="?")
    p.add_argument("--open", action="store_true", help="open it in Ableton Live afterwards")
    p.add_argument("--resolve", choices=["backup_and_get", "keep_both", "continue"])
    p = sub.add_parser("check", help="health check: missing files, plugins on each computer")
    p.add_argument("project")
    p = sub.add_parser("collect", help="pack every outside file into the project (like Live's Collect All)")
    p.add_argument("project")
    p = sub.add_parser("setup-drive", help="prepare an external drive")
    p.add_argument("path")
    sub.add_parser("scan-plugins", help="rescan this computer's plugins")
    sub.add_parser("clean-drive", help="remove ._ / .DS_Store junk files from the drive")
    args = ap.parse_args(argv)

    if args.cmd in (None, "gui"):
        from .gui import main as gui_main
        return gui_main() or 0

    bridge = Bridge()
    try:
        if args.cmd == "status":
            if bridge.drive:
                print(f"Drive: {bridge.drive.root} ({bridge.drive.filesystem()})")
            else:
                print("Drive: not connected")
            for r in bridge.rows():
                print(f"  {STATUS_TEXT[r.status]:<28} {r.name}")
        elif args.cmd == "send":
            if args.project:
                proj = Path(args.project).expanduser().resolve()
            elif args.save:
                proj = bridge.save_in_live_then_find(log=print)
            else:
                proj = bridge.current_project()
            if not proj:
                raise BridgeError("No project found.")
            res = _with_conflicts(lambda r: bridge.send(proj, resolution=r, progress=_progress_bar, log=print),
                                  args.resolve)
            if res:
                for w in res.warnings:
                    print(f"  ! {w}")
                others = [m for m in res.report.machines if m.id != bridge.store.settings.machine_id]
                for m in others:
                    for line in res.report.problems_for(m.id):
                        print(f"  ! {m.name}: {line}")
        elif args.cmd == "get":
            name = args.name or bridge.latest_from_other_computer()
            if not name:
                raise BridgeError("Nothing on the drive.")
            res = _with_conflicts(lambda r: bridge.get(name, resolution=r, progress=_progress_bar, log=print),
                                  args.resolve)
            if res:
                for line in res.report.problems_for(bridge.store.settings.machine_id):
                    print(f"  ! {line}")
                if args.open and res.main_set:
                    pu.open_path(res.main_set)
        elif args.cmd == "check":
            print(bridge.check(Path(args.project).expanduser().resolve()).as_text())
        elif args.cmd == "collect":
            from .collect import collect_project
            r = collect_project(Path(args.project).expanduser().resolve(), log=print,
                                search_dirs=bridge.store.settings.sample_search_dirs)
            print(f"Collected {len(r.files_copied)} file(s), fixed {r.refs_fixed} reference(s).")
            for m in r.missing:
                print(f"  MISSING: {m.filename} ({m.last_known_path})")
        elif args.cmd == "setup-drive":
            drive, level, advice = bridge.setup_drive(Path(args.path))
            print(f"Drive ready: {drive.root}\n{level.upper()}: {advice}")
        elif args.cmd == "scan-plugins":
            bridge.publish_machine(rescan=True, log=print)
        elif args.cmd == "clean-drive":
            print(f"Removed {bridge.require_drive().clean_junk()} junk file(s).")
    except BridgeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0
