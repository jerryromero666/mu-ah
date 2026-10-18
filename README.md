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
   
   The easiest place is **Releases**: https://github.com/jerryromero666/mu-ah/releases/latest
   
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

## Already have projects on your drive?

You don't need to move anything. When you set up a drive that already has Ableton projects on it:

- Every project folder anywhere on the drive shows up in the list as **Only on the drive**.
- **Get & Open** copies one to this computer. Samples the set points to elsewhere on the drive
  (for example a sample library folder) are packed into the project, whether the set was last
  saved on the Mac (`/Volumes/...`) or on the PC (`E:\...`).
- **Send** puts the project back **in its original folder on the drive**, not in a new one.
- If you open a project straight from the drive in Live and save it there, the app notices and
  shows it as a newer version.
- Two folders with the same name (e.g. two `Untitled Project`s) get told apart by their parent folder.
- If the same project is already on this computer, Get updates that copy and backs up anything
  that differs first.

Keep each song in its own folder. Loose `.als` files at the very top of the drive are ignored.

## When names are the same

- **Each project carries a hidden ID.** The ID lives in `Ableton Project Info/AbletonBridge.json`
  and travels with the project, so the app knows whether two folders are the same song, whatever
  they're called. Ableton calls every new project "Untitled Project", so this matters.
- **Two different songs with the same name** never replace each other. Send asks first, with
  *Send mine as a separate copy* (it becomes e.g. `Untitled Project (Studio-Mac)`) or *Replace the
  one on the drive*. Get puts a different song into its own folder (`Untitled Project (2)`) instead
  of mixing it into yours. Both show in the list, labelled with their parent folder.
- **The same song changed in two places** gets one question: keep yours and get theirs as a separate
  copy, or get theirs (yours goes to `Backup/Before get <date>/`). Every file of yours that gets
  replaced is backed up first, not just the Live Set.
- **Saved straight onto the drive in Live?** Send and Get check the drive right before they run. A
  set on the drive that would be replaced is first copied to `Backup/Before send <date>/` on the drive.
- **Names Windows can't store** (`What If?.als`, `Mix 10:17.wav`, folders ending in a space) are
  stored on the drive under a safe name (`What If_.als`) instead of failing. Two files whose names
  only differ in capital letters or accents can't both exist on the drive; the app tells you which.
- **Moving or renaming a project folder** on your computer keeps it linked.

## Versions: which computer saved what

- **Changed on both computers?** You get one pop-up showing both versions side by side, with
  *Last saved on Studio-Mac, Sat Oct 17, 20:40* and *Last saved on Studio-PC (this computer), 19:51*.
  The newer one is marked. Pick **Use mine**, **Use the drive's** or **Keep both**. The one you don't
  pick is never deleted.
- **Versions button.** Select a project and click **Versions** to see every saved version: the
  current set, Live's own backups, and the versions kept when you chose one over the other (on this
  computer and on the drive). Each one shows which computer last saved it and when. **Open in Live**
  opens any of them as a copy next to your current set, so nothing is overwritten. If that version
  used different sounds, it opens as a separate copy of the whole project, so it sounds exactly as
  it did.
- Command line: `python AbletonBridge.py versions "<project folder>"`.

## Day to day

- On the computer you worked on: save in Live (or let the app do it) and click **↑ Send to Drive**.
- Plug the drive into the other computer: click **↓ Get from Drive & Open**.
- **Want a specific project?** Click it in the list first (it turns blue). The big buttons then act on
  that project, and the text under them names it. Press **Esc** or click empty space in the list to go
  back to "the latest one".

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

## Updating the app

Once you've installed a version that has this feature, you never download it by hand again:

- Every time a change is pushed to GitHub and all three apps build successfully, they're published as a
  GitHub release (`build-<number>`, newest 10 kept).
- When Ableton Bridge starts, it checks that release. If there's a newer build, a green bar says
  **New version available** and offers **Update now**. That downloads the right file for this computer
  (Windows / Apple Silicon Mac / Intel Mac), checks its SHA-256, swaps the app in place and restarts.
  Settings, sync state and projects stay as they are.
- **Settings → Check for updates** checks on demand. Turn off *Check for a new version when it starts*
  if you'd rather not have it check.
- Update each computer separately, and the copy on the drive too if you start the app from there.
- Releases are at https://github.com/jerryromero666/mu-ah/releases if you ever want to download by hand.

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
