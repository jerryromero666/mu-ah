# Ableton Bridge

Move Ableton Live projects between your **Mac** and your **PC** on an external drive.
One click to send a project, one click to open it on the other computer. No missing
samples, and you find out about missing plugins before you open the set.

```
 Mac / PC                         external drive                     PC / Mac
┌───────────────┐   ↑ Send   ┌────────────────────┐   ↓ Get & Open   ┌───────────────┐
│ Song Project  │ ─────────▶ │ AbletonBridge/     │ ───────────────▶ │ Song Project  │
│ (in Live)     │            │   Projects/        │                  │ opens in Live │
└───────────────┘            │   _bridge/ (state) │                  └───────────────┘
                             └────────────────────┘
```

## What happens when you click

**↑ Send to Drive**
1. Presses **Save** in Ableton Live for you (Cmd+S / Ctrl+S).
2. Finds the project you just saved.
3. **Packs every outside file into the project.** That covers Splice downloads, sample
   folders, the User Library, Max for Live devices and pack sounds. It works like Live's
   *Collect All and Save*, but you don't have to do it by hand. The set is rewritten so
   every file is found *relative to the project folder*, which is the part that makes a
   project portable. The original set is backed up to `Backup/` first.
4. Renames files that Windows can't store (for example `Kick: Hard.wav` becomes `Kick_ Hard.wav`).
5. Copies only the files that changed to the drive. Samples go first and the Live Set
   goes last, through temporary files, so unplugging mid-copy can't leave a broken set.
6. Checks every plugin in the set against the **other computer's plugin list** and tells
   you what won't load there.

**↓ Get from Drive & Open**
1. Copies the newest project from the other computer to this one (only changed files).
2. Checks plugins and files against **this** computer.
3. Opens the set in Ableton Live. If something won't load, you're told first.

## Things it protects you from

| Problem | What Ableton Bridge does |
|---|---|
| "Media files are missing" | Packs every referenced file into the project and makes all paths project-relative. If a file moved, it searches for it by name and size. |
| Missing plugins | Each computer leaves its plugin list on the drive. Every send and open shows which plugins are missing on which computer. |
| Audio Unit plugins on the PC | AUs only exist on macOS. It warns you, and tells you if the VST3 version is installed so you can switch to it. |
| Plugin installed in a different format | Warns when the set uses VST2 but only VST3 is installed (or the reverse). |
| Set saved in a newer Live | Warns when the other computer has an older Live that can't open the set. |
| Overwriting work | Refuses to send if the other computer sent a newer version you haven't opened, and refuses to fetch over changes you never sent. You choose: keep both, replace, or back up first. Nothing is ever deleted. |
| Drive unplugged mid-copy | Atomic copies plus a lock file. The interrupted send is detected and reported. |
| Drive format | Shows the drive's format and warns if it isn't **exFAT**. NTFS is read-only on a Mac and APFS doesn't work on Windows. |
| `._file` / `.DS_Store` junk | Copies never create them, and *Settings → Clean junk files* removes any that are there. |
| Windows file name and path limits | Renames illegal names and warns about very long paths. |

## Setup (5 minutes, once)

1. **Format the drive as exFAT.** This erases the drive, so back it up first.
   - Mac: Disk Utility → select the drive → Erase → Format **ExFAT**, Scheme **GUID**.
   - Windows: File Explorer → right-click the drive → Format → File system **exFAT**.
2. **Download the app** from this repository's GitHub **Actions** (latest "Build apps" run →
   *Artifacts*) or **Releases**:
   - `AbletonBridge-Windows.zip` → `AbletonBridge.exe`
   - `AbletonBridge-Mac-AppleSilicon.zip` (M1/M2/M3/M4 Macs) or `AbletonBridge-Mac-Intel.zip`
   
   **Tip:** put both apps on the drive itself so they always travel with your projects.
3. **First launch on the Mac:** the app isn't from the App Store, so right-click it → **Open** →
   **Open**. When you first click Send, macOS asks to let Ableton Bridge control your computer
   (System Settings → Privacy & Security → **Accessibility**). Allow it so the app can press Save
   in Live for you.
   On Windows, if SmartScreen appears, click **More info → Run anyway**.
4. Open the app on one computer and click **Set up drive…**, then pick the drive.
5. **Open the app once on each computer with the drive plugged in.** This saves each
   computer's plugin list to the drive, which the plugin checks need.
6. If your projects live somewhere other than `Music` or `Documents`, add that folder under
   **Settings → Folders with your Ableton projects**.

## Day to day

- On the computer you worked on: save in Live (or let the app do it) and click **↑ Send to Drive**.
- Plug the drive into the other computer: click **↓ Get from Drive & Open**.

The project list shows each project's status: *In sync*, *Changes to send*,
*Newer version on drive*, *Changed on both computers*, *Only on this computer* or
*Only on the drive*. Double-click a row to do the obvious thing. **Check project** opens a full
report with a plugin-by-computer table.

## Getting plugins right on both computers

- **Use VST3 everywhere.** VST3 works on both Mac and Windows. Avoid Audio Units in projects you
  move to the PC. In Live → Settings → Plug-Ins you can turn off Audio Units on the Mac so
  you never pick one by accident.
- Install the **same plugins (same major version) on both computers.** Kontakt 7 ≠ Kontakt 8.
- Plugin presets and state are stored in the set, so a plugin that's installed on both computers
  opens with your sound.
- If a plugin only exists on one computer, **freeze** the track before sending. Frozen tracks
  play on the other computer even without the plugin, and Ableton Bridge recognises them.
- Plugins inside shells (e.g. Waves) are checked through Live's own plugin database when it's
  readable. Otherwise they show as "Unknown" rather than "Missing".

## Command line (optional)

```
python AbletonBridge.py                  # the window
python AbletonBridge.py send --save      # press Save in Live, send the current project
python AbletonBridge.py get --open       # get the latest from the other computer and open it
python AbletonBridge.py status
python AbletonBridge.py check "~/Music/Ableton Projects/Song Project"
python AbletonBridge.py collect "<project folder>"   # just make a project self-contained
```

## Running from source

Needs Python 3.10+ from python.org (includes Tk). Double-click `run_mac.command` or
`run_windows.bat`. Tests: `pip install pytest && python -m pytest tests`.

## Where things live

- Drive: `AbletonBridge/Projects/<Project>/` holds normal Ableton project folders that you can
  open directly. `AbletonBridge/_bridge/` holds versions, locks and each computer's plugin list.
- Each computer stores its settings and sync state in
  `~/Library/Application Support/AbletonBridge` (Mac) or `%APPDATA%\AbletonBridge` (Windows).

## Limits

- Live has no public API. The app presses Save with a keystroke and reads and writes Live's
  undocumented `.als` XML format (Live 9 through 12). Only the file references it
  re-points are changed, and everything else in the set stays byte-for-byte identical.
- It can't install plugins or licenses for you. It tells you exactly what's missing where.
- Brand-new sets that were never saved need one manual save in Live so they have a project folder.
