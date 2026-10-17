"""The Ableton Bridge window."""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import APP_NAME, __version__
from . import platform_utils as pu
from .core import Bridge
from .sync import STATUS_TEXT, BridgeError, Conflict

# ----------------------------------------------------------------------------- look

BG = "#16171b"
PANEL = "#202228"
PANEL_HI = "#2a2d35"
TEXT = "#ecedef"
MUTED = "#9ba1ab"
ACCENT = "#ff7a45"
ACCENT_HI = "#ff9466"
BLUE = "#4c9dff"
BLUE_HI = "#6cb0ff"
GREEN = "#3ecf8e"
YELLOW = "#f2c14e"
RED = "#ff5f5f"

if pu.IS_WINDOWS:
    FAMILY = "Segoe UI"
elif pu.IS_MAC:
    FAMILY = "Helvetica Neue"
else:
    FAMILY = "DejaVu Sans"

STATUS_COLORS = {
    "synced": GREEN, "local_newer": ACCENT, "drive_newer": BLUE, "conflict": RED,
    "local_only": MUTED, "drive_only": BLUE, "unlinked": YELLOW,
}
STATUS_ICONS = {
    "synced": "✓", "local_newer": "↑", "drive_newer": "↓", "conflict": "!",
    "local_only": "•", "drive_only": "↓", "unlinked": "?",
}


def ago(ts: float) -> str:
    if not ts:
        return ""
    d = time.time() - ts
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    if d < 7 * 86400:
        return f"{int(d // 86400)} days ago"
    return time.strftime("%b %d", time.localtime(ts))


class BigButton(tk.Frame):
    """A large, colored, two-line button that looks the same on Mac and Windows."""

    def __init__(self, master, title: str, subtitle: str, color: str, hover: str, command):
        super().__init__(master, bg=color, cursor="hand2", padx=22, pady=16)
        self.color, self.hover, self.command, self.enabled = color, hover, command, True
        self.title = tk.Label(self, text=title, bg=color, fg="#ffffff", font=(FAMILY, 17, "bold"), anchor="w")
        self.title.pack(fill="x")
        self.sub = tk.Label(self, text=subtitle, bg=color, fg="#fff4ee", font=(FAMILY, 11), anchor="w",
                            justify="left", wraplength=360)
        self.sub.pack(fill="x", pady=(3, 0))
        for w in (self, self.title, self.sub):
            w.bind("<Enter>", lambda e: self._paint(self.hover))
            w.bind("<Leave>", lambda e: self._paint(self.color))
            w.bind("<ButtonRelease-1>", self._click)

    def _paint(self, c):
        if not self.enabled:
            c = PANEL_HI
        for w in (self, self.title, self.sub):
            w.configure(bg=c)

    def _click(self, _e):
        if self.enabled:
            self.command()

    def set_enabled(self, on: bool):
        self.enabled = on
        self._paint(self.color)
        self.title.configure(fg="#ffffff" if on else MUTED)
        self.sub.configure(fg="#fff4ee" if on else MUTED)

    def set_subtitle(self, text: str):
        self.sub.configure(text=text)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.configure(bg=BG)
        self.geometry("1000x700")
        self.minsize(820, 560)
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.rows = []
        self.bridge = Bridge()
        self._setup_style()
        self._build()
        self.after(80, self._pump)
        self.after(200, self._startup)
        self.after(3000, self._watch_drive)

    # ------------------------------------------------------------------ layout
    def _setup_style(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=TEXT, font=(FAMILY, 11))
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=32,
                     borderwidth=0, font=(FAMILY, 12))
        st.map("Treeview", background=[("selected", PANEL_HI)], foreground=[("selected", "#ffffff")])
        st.configure("Treeview.Heading", background=BG, foreground=MUTED, borderwidth=0, relief="flat",
                     font=(FAMILY, 10, "bold"), padding=(6, 6))
        st.map("Treeview.Heading", background=[("active", BG)])
        st.configure("TButton", background=PANEL_HI, foreground=TEXT, borderwidth=0, focusthickness=0,
                     padding=(14, 8), font=(FAMILY, 11))
        st.map("TButton", background=[("active", "#373b45"), ("disabled", PANEL)],
               foreground=[("disabled", "#5c616b")])
        st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff")
        st.map("Accent.TButton", background=[("active", ACCENT_HI)])
        st.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL, borderwidth=0,
                     lightcolor=ACCENT, darkcolor=ACCENT)
        st.configure("TCheckbutton", background=PANEL, foreground=TEXT)
        st.map("TCheckbutton", background=[("active", PANEL)])
        st.configure("Vertical.TScrollbar", background=PANEL_HI, troughcolor=PANEL, borderwidth=0, arrowsize=0)

    def _build(self):
        # header
        head = tk.Frame(self, bg=BG, padx=24, pady=16)
        head.pack(fill="x")
        self.header = head
        self.banner = tk.Label(self, bg=RED, fg="#ffffff", font=(FAMILY, 11), anchor="w", justify="left",
                               padx=14, pady=8, wraplength=900)
        left = tk.Frame(head, bg=BG)
        left.pack(side="left")
        tk.Label(left, text="ABLETON BRIDGE", bg=BG, fg=TEXT, font=(FAMILY, 18, "bold")).pack(anchor="w")
        self.machine_lbl = tk.Label(left, bg=BG, fg=MUTED, font=(FAMILY, 11))
        self.machine_lbl.pack(anchor="w")
        right = tk.Frame(head, bg=BG)
        right.pack(side="right")
        ttk.Button(right, text="Settings", command=self.open_settings).pack(side="right", padx=(10, 0))
        self.drive_btn = ttk.Button(right, text="Set up drive...", command=self.setup_drive)
        self.drive_box = tk.Frame(right, bg=PANEL, padx=14, pady=8)
        self.drive_box.pack(side="right")
        self.drive_dot = tk.Label(self.drive_box, text="●", bg=PANEL, fg=MUTED, font=(FAMILY, 14))
        self.drive_dot.pack(side="left")
        self.drive_lbl = tk.Label(self.drive_box, text="Looking for drive...", bg=PANEL, fg=TEXT,
                                  font=(FAMILY, 11), justify="left")
        self.drive_lbl.pack(side="left", padx=(6, 0))

        # hero buttons
        hero = tk.Frame(self, bg=BG, padx=24)
        hero.pack(fill="x")
        hero.columnconfigure(0, weight=1, uniform="h")
        hero.columnconfigure(1, weight=1, uniform="h")
        self.send_btn = BigButton(hero, "↑  Send to Drive",
                                  "Saves in Live, packs every sample into the project, copies it to the drive.",
                                  ACCENT, ACCENT_HI, self.action_send_current)
        self.send_btn.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.get_btn = BigButton(hero, "↓  Get from Drive & Open",
                                 "Copies the latest project from the drive to this computer and opens it in Live.",
                                 BLUE, BLUE_HI, self.action_get_latest)
        self.get_btn.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        # project table
        body = tk.Frame(self, bg=BG, padx=24, pady=16)
        body.pack(fill="both", expand=True)
        top = tk.Frame(body, bg=BG)
        top.pack(fill="x", pady=(0, 6))
        tk.Label(top, text="Projects", bg=BG, fg=TEXT, font=(FAMILY, 13, "bold")).pack(side="left")
        ttk.Button(top, text="Refresh", command=lambda: self.refresh(rescan=True)).pack(side="right")

        table = tk.Frame(body, bg=PANEL)
        table.pack(fill="both", expand=True)
        cols = ("status", "sent", "by", "local")
        self.tree = ttk.Treeview(table, columns=cols, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="PROJECT", anchor="w")
        self.tree.heading("status", text="STATUS", anchor="w")
        self.tree.heading("sent", text="ON DRIVE", anchor="w")
        self.tree.heading("by", text="SENT FROM", anchor="w")
        self.tree.heading("local", text="ON THIS COMPUTER", anchor="w")
        self.tree.column("#0", width=280, anchor="w")
        self.tree.column("status", width=210, anchor="w")
        self.tree.column("sent", width=110, anchor="w")
        self.tree.column("by", width=130, anchor="w")
        self.tree.column("local", width=150, anchor="w")
        for key, color in STATUS_COLORS.items():
            self.tree.tag_configure(key, foreground=color)
        sb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_actions())
        self.tree.bind("<Double-1>", lambda e: self.action_primary())

        actions = tk.Frame(body, bg=BG)
        actions.pack(fill="x", pady=(10, 0))
        self.act_send = ttk.Button(actions, text="Send to drive", command=self.action_send_selected)
        self.act_get = ttk.Button(actions, text="Get & open", command=self.action_get_selected)
        self.act_open = ttk.Button(actions, text="Open here", command=self.action_open_selected)
        self.act_check = ttk.Button(actions, text="Check project", command=self.action_check_selected)
        self.act_show = ttk.Button(actions, text="Show in folder", command=self.action_show_selected)
        for b in (self.act_send, self.act_get, self.act_open, self.act_check, self.act_show):
            b.pack(side="left", padx=(0, 8))

        # footer
        foot = tk.Frame(self, bg=PANEL, padx=24, pady=10)
        foot.pack(fill="x", side="bottom", before=body)
        row = tk.Frame(foot, bg=PANEL)
        row.pack(fill="x")
        self.status_lbl = tk.Label(row, text="Ready.", bg=PANEL, fg=TEXT, font=(FAMILY, 11), anchor="w")
        self.status_lbl.pack(side="left", fill="x", expand=True)
        self.log_toggle = tk.Label(row, text="Show log", bg=PANEL, fg=MUTED, cursor="hand2", font=(FAMILY, 10))
        self.log_toggle.pack(side="right")
        self.log_toggle.bind("<Button-1>", lambda e: self._toggle_log())
        self.progress = ttk.Progressbar(foot, mode="determinate", maximum=1.0)
        self.progress.pack(fill="x", pady=(8, 0))
        self.log = tk.Text(foot, height=8, bg=BG, fg=MUTED, relief="flat", font=("Menlo" if pu.IS_MAC else
                                                                                  "Consolas", 10),
                           wrap="word", state="disabled", padx=8, pady=6)
        self._log_visible = False
        self._refresh_header()
        self._update_actions()

    def _toggle_log(self):
        self._log_visible = not self._log_visible
        if self._log_visible:
            self.log.pack(fill="x", pady=(8, 0))
            self.log_toggle.configure(text="Hide log")
        else:
            self.log.pack_forget()
            self.log_toggle.configure(text="Show log")

    # ------------------------------------------------------------------ threading
    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                    self.status_lbl.configure(text=payload, fg=TEXT)
                elif kind == "progress":
                    self.progress["value"] = payload
                elif kind == "call":
                    payload()
        except queue.Empty:
            pass
        self.after(60, self._pump)

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S  ") + text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def log_msg(self, text):
        self.q.put(("log", text))

    def run_task(self, label: str, work, done=None, retry=None):
        """Run work(progress, log) in a thread. Conflicts are put to the user, then retry(resolution) runs."""
        if self.busy:
            return
        self.busy = True
        self._set_busy(True)
        self.progress["value"] = 0
        self.log_msg(label)

        def progress(f):
            self.q.put(("progress", f))

        def runner():
            try:
                result = work(progress, self.log_msg)
            except Conflict as c:
                conflict = c
                self.q.put(("call", lambda: self._finish(None, conflict=conflict, retry=retry)))
                return
            except BridgeError as e:
                msg = str(e)
                self.q.put(("call", lambda: self._finish(None, error=msg)))
                return
            except Exception as e:  # noqa: BLE001
                msg = f"Something went wrong: {e}"
                self.q.put(("log", traceback.format_exc()))
                self.q.put(("call", lambda: self._finish(None, error=msg)))
                return
            self.q.put(("call", lambda: self._finish(result, done=done)))

        threading.Thread(target=runner, daemon=True).start()

    def _finish(self, result, done=None, error=None, conflict=None, retry=None):
        self.busy = False
        self._set_busy(False)
        if error:
            self.progress["value"] = 0
            self.status_lbl.configure(text=error, fg=RED)
            self._append_log("ERROR: " + error)
            messagebox.showerror(APP_NAME, error, parent=self)
            return
        if conflict:
            self.progress["value"] = 0
            self.status_lbl.configure(text=conflict.title, fg=YELLOW)
            choice = ChoiceDialog(self, conflict.title, conflict.message, conflict.options).result
            if choice and retry:
                retry(choice)
            else:
                self.status_lbl.configure(text="Cancelled - nothing was changed.", fg=MUTED)
            return
        self.progress["value"] = 1.0
        if done:
            done(result)

    def _set_busy(self, on: bool):
        self.send_btn.set_enabled(not on)
        self.get_btn.set_enabled(not on and self.bridge.drive is not None)
        self.configure(cursor="watch" if on else "")
        self._update_actions()

    # ------------------------------------------------------------------ startup / drive
    def _startup(self):
        def work(progress, log):
            self.bridge.publish_machine(log=log)
            rows = self.bridge.rows(rescan=True)
            for note in (self.bridge.drive.scan_notes if self.bridge.drive else []):
                log(note)
            return rows

        def done(rows):
            self._fill(rows)
            self.status_lbl.configure(text="Ready." if self.bridge.drive else
                                      "Plug in your transfer drive (or click 'Set up drive...').", fg=TEXT)
        self.run_task("Starting up...", work, done)

    def _watch_drive(self):
        def check():
            before = self.bridge.drive.root if self.bridge.drive else None
            try:
                drive = self.bridge.refresh_drive()
            except Exception:  # noqa: BLE001
                drive = None
            after = drive.root if drive else None
            if before != after:
                self.q.put(("call", self._drive_changed))
        if not self.busy:
            threading.Thread(target=check, daemon=True).start()
        self.after(3000, self._watch_drive)

    def _drive_changed(self):
        self._refresh_header()
        if self.bridge.drive:
            self.log_msg(f"Drive connected: {self.bridge.drive.label}")
            threading.Thread(target=lambda: self.bridge.publish_machine(), daemon=True).start()
        else:
            self.log_msg("Drive disconnected.")
        self.refresh(rescan=True)

    def _refresh_header(self):
        s = self.bridge.store.settings
        self.machine_lbl.configure(text=f"This computer: {s.machine_name} · {pu.os_label()}   v{__version__}")
        d = self.bridge.drive
        if d:
            fs = d.filesystem()
            level, advice = pu.filesystem_advice(fs)
            color = {"ok": GREEN, "warn": YELLOW, "error": RED}[level]
            self.drive_dot.configure(fg=color)
            self.drive_lbl.configure(text=f"Drive: {d.label}  ·  {fs.upper()}")
            self.drive_btn.pack_forget()
            if level != "ok":
                self.banner.configure(text=f"Drive format: {advice}", bg=RED if level == "error" else "#5a4a1c")
                self.banner.pack(fill="x", padx=24, pady=(0, 12), after=self.header)
            else:
                self.banner.pack_forget()
        else:
            self.drive_dot.configure(fg=RED)
            self.drive_lbl.configure(text="No drive plugged in")
            self.drive_btn.pack(side="right", padx=(10, 0))
            self.banner.pack_forget()
        self.get_btn.set_enabled(d is not None and not self.busy)
        self._update_get_subtitle()

    def _update_get_subtitle(self):
        d = self.bridge.drive
        if not d:
            self.get_btn.set_subtitle("Plug in the transfer drive first.")
            return
        mans = d.manifests()
        if not mans:
            self.get_btn.set_subtitle("Nothing on the drive yet - send a project from the other computer.")
            return
        try:
            name = self.bridge.latest_from_other_computer()
        except BridgeError:
            name = None
        if name:
            m = mans[name]
            by = m.get("pushed_by", {})
            when = ago(m.get("pushed_at", 0))
            if by.get("id") == "drive":
                self.get_btn.set_subtitle(f"Latest: \"{name}\" ({by.get('name')}, saved {when}).")
            else:
                self.get_btn.set_subtitle(f"Latest: \"{name}\" from {by.get('name', '?')}, {when}.")

    def setup_drive(self):
        messagebox.showinfo(APP_NAME, "Choose your external drive (the top level of the drive).\n\n"
                                      "An 'AbletonBridge' folder will be created on it. Format the drive as "
                                      "exFAT so both your Mac and your PC can read and write it.", parent=self)
        start = "/Volumes" if pu.IS_MAC else None
        folder = filedialog.askdirectory(parent=self, title="Choose your external drive", initialdir=start)
        if not folder:
            return
        try:
            drive, level, advice = self.bridge.setup_drive(Path(folder))
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Couldn't set up the drive: {e}", parent=self)
            return
        (messagebox.showinfo if level == "ok" else messagebox.showwarning)(
            APP_NAME, f"Drive ready: {drive.root}\n\n{advice}\n\nTip: copy the Ableton Bridge app for Mac and "
                      "for Windows onto the drive too - then it's always with your projects.", parent=self)
        self._refresh_header()
        threading.Thread(target=lambda: self.bridge.publish_machine(), daemon=True).start()
        self.refresh(rescan=True)

    # ------------------------------------------------------------------ table
    def refresh(self, rescan: bool = False):
        if self.busy:
            return

        def work():
            try:
                rows = self.bridge.rows(rescan=rescan)
            except Exception as e:  # noqa: BLE001
                self.q.put(("log", f"Couldn't list projects: {e}"))
                return
            self.q.put(("call", lambda: self._fill(rows)))
        threading.Thread(target=work, daemon=True).start()

    def _fill(self, rows):
        sel = self.selected()
        sel_name = sel.name if sel else None
        self.rows = rows
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(rows):
            local = ""
            if r.local_dir:
                local = f"saved {ago(r.local_mtime)}" if r.local_mtime else "yes"
            by = r.sent_by
            if r.manifest and r.manifest.get("pushed_by", {}).get("id") == "drive":
                folder = r.manifest.get("path", "").rsplit("/", 1)[0] if "/" in r.manifest.get("path", "") else ""
                by = f"drive: {folder or 'top level'}"
            self.tree.insert("", "end", iid=str(i), text="   " + (r.label or r.name),
                             values=(f"{STATUS_ICONS[r.status]}  {STATUS_TEXT[r.status]}",
                                     ago(r.sent_at), by, local), tags=(r.status,))
            if r.name == sel_name:
                self.tree.selection_set(str(i))
        if not rows:
            self.tree.insert("", "end", iid="empty", text="   No Ableton projects found yet",
                             values=("Add your project folders in Settings", "", "", ""), tags=("local_only",))
        self._update_get_subtitle()
        self._update_actions()

    def selected(self):
        sel = self.tree.selection()
        if not sel or sel[0] == "empty":
            return None
        idx = int(sel[0])
        return self.rows[idx] if idx < len(self.rows) else None

    def _update_actions(self):
        r = self.selected()
        has_drive = self.bridge.drive is not None
        busy = self.busy

        def en(btn, on):
            btn.state(["!disabled"] if on and not busy else ["disabled"])
        en(self.act_send, bool(r and r.local_dir and has_drive))
        en(self.act_get, bool(r and r.manifest and has_drive))
        en(self.act_open, bool(r and r.local_dir))
        en(self.act_check, bool(r and r.local_dir))
        en(self.act_show, bool(r and (r.local_dir or (r.manifest and has_drive))))

    # ------------------------------------------------------------------ actions
    def action_primary(self):
        r = self.selected()
        if not r:
            return
        if r.status in ("drive_newer", "drive_only", "conflict") and self.bridge.drive:
            self.action_get_selected()
        elif r.local_dir:
            self.action_open_selected()

    def action_send_current(self):
        if not self.bridge.drive:
            self.bridge.refresh_drive()
            if not self.bridge.drive:
                messagebox.showwarning(APP_NAME, "Plug in your transfer drive first (or click 'Set up drive...').",
                                       parent=self)
                return

        def work(progress, log):
            proj = self.bridge.save_in_live_then_find(log=log)
            if not proj:
                raise BridgeError("Couldn't find any saved Ableton project. Save your set in Live, and make sure "
                                  "its folder is inside one of the project folders listed in Settings.")
            return proj

        self.run_task("Saving in Live...", work, done=lambda proj: self._send(proj))

    def action_send_selected(self):
        r = self.selected()
        if r and r.local_dir:
            self._send(r.local_dir)

    def _send(self, project_dir: Path, resolution: tuple = ()):
        def work(progress, log):
            return self.bridge.send(project_dir, resolution=resolution, progress=progress, log=log)

        self.run_task(f"Sending \"{project_dir.name}\" to the drive...", work, done=self._sent,
                      retry=lambda res: self._send(project_dir, (*resolution, res)))

    def _sent(self, result):
        rep = result.report
        others = [m for m in rep.machines if m.id != self.bridge.store.settings.machine_id]
        problems, warns = [], []
        for m in others:
            problems += [f"{m.name}: {p}" for p in rep.problems_for(m.id)]
            warns += [f"{m.name}: {w}" for w in rep.warnings_for(m.id)]
        local_missing = [f"Missing file: {mf.filename} (not on this computer either)" for mf in rep.missing_files]
        collected = len(result.collect.files_copied)
        if result.files_sent:
            msg = f"✓ Sent \"{result.drive_name}\" ({result.bytes_sent / 1e6:.1f} MB"
            if collected:
                msg += f", packed {collected} outside file{'s' if collected != 1 else ''} into the project"
            msg += ")."
        else:
            msg = f"✓ \"{result.drive_name}\" was already up to date on the drive."
        if not others:
            msg += " Open Ableton Bridge on your other computer once so it can check plugins."
        self.status_lbl.configure(text=msg, fg=GREEN if not problems else YELLOW)
        self._append_log(msg)
        self.refresh()
        if problems or local_missing:
            IssuesDialog(self, "Sent - but heads up",
                         "The project is on the drive. These things won't load on the other computer:",
                         local_missing + problems, warns, rep)

    def action_get_latest(self):
        try:
            name = self.bridge.latest_from_other_computer()
        except BridgeError as e:
            messagebox.showwarning(APP_NAME, str(e), parent=self)
            return
        if not name:
            messagebox.showinfo(APP_NAME, "There are no projects on the drive yet.", parent=self)
            return
        self._get(name)

    def action_get_selected(self):
        r = self.selected()
        if r and r.manifest:
            self._get(r.name)

    def _get(self, name: str, resolution: tuple = ()):
        def work(progress, log):
            return self.bridge.get(name, resolution=resolution, progress=progress, log=log)

        self.run_task(f"Getting \"{name}\" from the drive...", work, done=self._got,
                      retry=lambda res: self._get(name, (*resolution, res)))

    def _got(self, result):
        me = self.bridge.store.settings.machine_id
        rep = result.report
        problems = rep.problems_for(me)
        warns = rep.warnings_for(me)
        if result.already_current:
            msg = f"✓ \"{result.drive_name}\" is already up to date here."
        else:
            msg = f"✓ Got \"{result.drive_name}\" ({result.bytes_received / 1e6:.1f} MB)."
        if result.backups:
            msg += " Your previous version is in Backup/."
        self.status_lbl.configure(text=msg, fg=GREEN if not problems else YELLOW)
        self._append_log(msg)
        self.refresh()
        if result.missing_on_drive:
            problems = [f"Not on the drive: {m}" for m in result.missing_on_drive] + problems
        should_open = self.bridge.store.settings.open_after_get and result.main_set
        if problems:
            d = IssuesDialog(self, "Before you open it",
                             "Some things in this project won't load on this computer:",
                             problems, warns, rep, open_label="Open in Live anyway" if result.main_set else None)
            if d.result == "open" and result.main_set:
                self.bridge.open_set(result.main_set)
        elif should_open:
            self.log_msg(f"Opening {result.main_set.name} in Ableton Live...")
            self.bridge.open_set(result.main_set)

    def action_open_selected(self):
        r = self.selected()
        if not (r and r.local_dir):
            return
        from .alsfile import find_sets
        sets = find_sets(r.local_dir)
        main = (r.manifest or {}).get("main_set")
        target = r.local_dir / main if main and (r.local_dir / main).is_file() else (sets[0] if sets else None)
        if target:
            self.bridge.open_set(target)

    def action_check_selected(self):
        r = self.selected()
        if not (r and r.local_dir):
            return
        def done(rep):
            self.status_lbl.configure(text="Ready.", fg=TEXT)
            ReportWindow(self, rep)
        self.run_task(f"Checking \"{r.name}\"...", lambda p, l: self.bridge.check(r.local_dir), done=done)

    def action_show_selected(self):
        r = self.selected()
        if not r:
            return
        if r.local_dir:
            pu.reveal_path(r.local_dir)
        elif self.bridge.drive:
            pu.reveal_path(self.bridge.drive.project_path(r.name, r.manifest))

    def open_settings(self):
        SettingsDialog(self)


# ----------------------------------------------------------------------------- dialogs

class _Dialog(tk.Toplevel):
    def __init__(self, master, title: str, width: int = 560):
        super().__init__(master)
        self.title(title)
        self.configure(bg=PANEL, padx=24, pady=20)
        self.transient(master)
        self.resizable(True, True)
        self.result = None
        self.minsize(width, 10)

    def _show(self):
        self.update_idletasks()
        m = self.master
        x = m.winfo_rootx() + (m.winfo_width() - self.winfo_width()) // 2
        y = m.winfo_rooty() + max((m.winfo_height() - self.winfo_height()) // 3, 0)
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()
        self.focus_set()
        self.wait_window()


class ChoiceDialog(_Dialog):
    def __init__(self, master, title, message, options: dict[str, str]):
        super().__init__(master, title)
        tk.Label(self, text=title, bg=PANEL, fg=TEXT, font=(FAMILY, 15, "bold"), anchor="w").pack(fill="x")
        tk.Label(self, text=message, bg=PANEL, fg=MUTED, font=(FAMILY, 11), wraplength=520, justify="left",
                 anchor="w").pack(fill="x", pady=(8, 16))
        row = tk.Frame(self, bg=PANEL)
        row.pack(fill="x")
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="right")
        for i, (key, label) in enumerate(options.items()):
            ttk.Button(row, text=label, style="Accent.TButton" if i == 0 else "TButton",
                       command=lambda k=key: self._pick(k)).pack(side="right", padx=(0, 8))
        self._show()

    def _pick(self, key):
        self.result = key
        self.destroy()


class IssuesDialog(_Dialog):
    def __init__(self, master, title, intro, problems, warnings, report, open_label=None):
        super().__init__(master, title, width=620)
        tk.Label(self, text=title, bg=PANEL, fg=TEXT, font=(FAMILY, 15, "bold"), anchor="w").pack(fill="x")
        tk.Label(self, text=intro, bg=PANEL, fg=MUTED, font=(FAMILY, 11), anchor="w", justify="left",
                 wraplength=580).pack(fill="x", pady=(6, 10))
        box = tk.Text(self, height=min(14, 2 + len(problems) + len(warnings)), bg=BG, fg=TEXT, relief="flat",
                      font=(FAMILY, 11), wrap="word", padx=10, pady=8)
        box.tag_configure("p", foreground=RED)
        box.tag_configure("w", foreground=YELLOW)
        for p in problems:
            box.insert("end", "✕  " + p + "\n", "p")
        for w in warnings:
            box.insert("end", "!  " + w + "\n", "w")
        box.configure(state="disabled")
        box.pack(fill="both", expand=True)
        tk.Label(self, text="Fixes: install the missing plugin, use VST3 instead of Audio Units, or freeze the "
                            "track (frozen tracks play without the plugin).", bg=PANEL, fg=MUTED,
                 font=(FAMILY, 10), wraplength=580, justify="left", anchor="w").pack(fill="x", pady=(10, 12))
        row = tk.Frame(self, bg=PANEL)
        row.pack(fill="x")
        ttk.Button(row, text="Close", command=self.destroy).pack(side="right")
        if open_label:
            ttk.Button(row, text=open_label, style="Accent.TButton",
                       command=lambda: self._done("open")).pack(side="right", padx=(0, 8))
        ttk.Button(row, text="Full report", command=lambda: self._report(report)).pack(side="left")
        self._show()

    def _done(self, r):
        self.result = r
        self.destroy()

    def _report(self, report):
        master = self.master
        self.grab_release()
        self.destroy()
        ReportWindow(master, report)


class ReportWindow(tk.Toplevel):
    def __init__(self, master, report):
        super().__init__(master)
        self.title(f"Check: {report.project_dir.name}")
        self.configure(bg=PANEL, padx=20, pady=16)
        self.geometry("900x600")
        tk.Label(self, text=report.project_dir.name, bg=PANEL, fg=TEXT, font=(FAMILY, 15, "bold"),
                 anchor="w").pack(fill="x")
        tk.Label(self, text=f"{report.creator or 'Unknown Live version'}  ·  {', '.join(report.sets)}",
                 bg=PANEL, fg=MUTED, font=(FAMILY, 10), anchor="w").pack(fill="x", pady=(2, 10))

        machines = report.machines
        cols = ["format", "tracks"] + [m.id for m in machines]
        tree = ttk.Treeview(self, columns=cols, show="tree headings", height=10)
        tree.heading("#0", text="PLUGIN", anchor="w")
        tree.column("#0", width=200)
        tree.heading("format", text="FORMAT", anchor="w")
        tree.column("format", width=80)
        tree.heading("tracks", text="USED ON", anchor="w")
        tree.column("tracks", width=220)
        for m in machines:
            tree.heading(m.id, text=m.label.upper(), anchor="w")
            tree.column(m.id, width=150)
        for lvl, color in (("ok", GREEN), ("warn", YELLOW), ("missing", RED), ("unknown", MUTED)):
            tree.tag_configure(lvl, foreground=color)
        short = {"ok": "✓  Installed", "warn": "!  Check", "missing": "✕  Won't load", "unknown": "?  Unknown"}
        for i, row in enumerate(report.plugins):
            worst = min(row.status.values(), key=lambda s: {"missing": 0, "warn": 1, "unknown": 2, "ok": 3}[s.level])
            vals = [row.plugin.format_label, ", ".join(row.plugin.tracks)]
            vals += [short[row.status[m.id].level] for m in machines]
            tree.insert("", "end", iid=str(i), text=row.plugin.name, values=vals, tags=(worst.level,))
        if not report.plugins:
            tree.insert("", "end", iid="none", text="No third-party plugins - only Live's own devices.", tags=("ok",))
        tree.pack(fill="both", expand=True)

        detail = tk.Label(self, text="Select a plugin to see details for each computer.", bg=PANEL, fg=MUTED,
                          font=(FAMILY, 11), anchor="w", justify="left", wraplength=840)
        detail.pack(fill="x", pady=(8, 0))

        def on_select(_e):
            sel = tree.selection()
            if not sel or sel[0] == "none":
                return
            row = report.plugins[int(sel[0])]
            lines = [f"{row.plugin.name} ({row.plugin.format_label}) on {', '.join(row.plugin.tracks)}"
                     + (f" - frozen: {', '.join(row.plugin.frozen_tracks)}" if row.plugin.frozen_tracks else "")]
            lines += [f"{m.label}: {row.status[m.id].text}" for m in machines]
            detail.configure(text="\n".join(lines), fg=TEXT)
        tree.bind("<<TreeviewSelect>>", on_select)

        text = tk.Text(self, height=6, bg=BG, fg=TEXT, relief="flat", font=(FAMILY, 11), wrap="word",
                       padx=10, pady=8)
        text.tag_configure("e", foreground=RED)
        text.tag_configure("w", foreground=YELLOW)
        text.tag_configure("ok", foreground=GREEN)
        if report.missing_files:
            text.insert("end", "Missing files (not found on this computer):\n", "e")
            for mf in report.missing_files:
                text.insert("end", f"   {mf.filename}   (last seen: {mf.last_known_path})\n")
        else:
            text.insert("end", "✓ Every sample and device the project uses was found.\n", "ok")
        if report.to_collect:
            text.insert("end", f"{report.to_collect} file(s) are outside the project folder - they'll be packed "
                               "into it automatically when you send it.\n")
        for issue in report.issues:
            text.insert("end", f"{issue.text}\n", "e" if issue.level == "error" else "w")
        if not any(m.os == "windows" for m in machines) or not any(m.os == "mac" for m in machines):
            text.insert("end", "Tip: open Ableton Bridge on the other computer with the drive plugged in, so it "
                               "can list its plugins here.\n")
        text.configure(state="disabled")
        text.pack(fill="x", pady=(12, 0))
        ttk.Button(self, text="Close", command=self.destroy).pack(anchor="e", pady=(12, 0))


class FolderList(tk.Frame):
    def __init__(self, master, label, items):
        super().__init__(master, bg=PANEL)
        tk.Label(self, text=label, bg=PANEL, fg=TEXT, font=(FAMILY, 11, "bold"), anchor="w").pack(fill="x")
        row = tk.Frame(self, bg=PANEL)
        row.pack(fill="x", pady=(4, 0))
        self.lb = tk.Listbox(row, height=4, bg=BG, fg=TEXT, relief="flat", selectbackground=PANEL_HI,
                             highlightthickness=0, font=(FAMILY, 10))
        self.lb.pack(side="left", fill="x", expand=True)
        for it in items:
            self.lb.insert("end", it)
        btns = tk.Frame(row, bg=PANEL)
        btns.pack(side="left", padx=(8, 0), anchor="n")
        ttk.Button(btns, text="Add...", command=self._add).pack(fill="x")
        ttk.Button(btns, text="Remove", command=self._remove).pack(fill="x", pady=(4, 0))

    def _add(self):
        d = filedialog.askdirectory(parent=self)
        if d:
            self.lb.insert("end", d)

    def _remove(self):
        for i in reversed(self.lb.curselection()):
            self.lb.delete(i)

    def items(self):
        return list(self.lb.get(0, "end"))


class SettingsDialog(_Dialog):
    def __init__(self, master: App):
        super().__init__(master, "Settings", width=640)
        self.app = master
        s = master.bridge.store.settings
        tk.Label(self, text="Settings", bg=PANEL, fg=TEXT, font=(FAMILY, 15, "bold"), anchor="w").pack(fill="x")

        row = tk.Frame(self, bg=PANEL)
        row.pack(fill="x", pady=(12, 0))
        tk.Label(row, text="This computer's name", bg=PANEL, fg=TEXT, font=(FAMILY, 11, "bold")).pack(side="left")
        self.name_var = tk.StringVar(value=s.machine_name)
        tk.Entry(row, textvariable=self.name_var, bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat",
                 width=28).pack(side="left", padx=(10, 0), ipady=4)

        row = tk.Frame(self, bg=PANEL)
        row.pack(fill="x", pady=(12, 0))
        tk.Label(row, text="Put projects from the drive in", bg=PANEL, fg=TEXT,
                 font=(FAMILY, 11, "bold")).pack(side="left")
        self.ws_var = tk.StringVar(value=s.workspace)
        tk.Entry(row, textvariable=self.ws_var, bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat").pack(
            side="left", fill="x", expand=True, padx=(10, 8), ipady=4)
        ttk.Button(row, text="Browse...", command=self._browse_ws).pack(side="left")

        self.roots = FolderList(self, "Folders with your Ableton projects", s.project_roots)
        self.roots.pack(fill="x", pady=(14, 0))
        self.plugdirs = FolderList(self, "Extra plugin folders (if you keep VSTs somewhere custom)",
                                   s.extra_plugin_dirs)
        self.plugdirs.pack(fill="x", pady=(10, 0))
        self.searchdirs = FolderList(self, "Sample folders to search when a file has moved", s.sample_search_dirs)
        self.searchdirs.pack(fill="x", pady=(10, 0))

        self.v_save = tk.BooleanVar(value=s.auto_save_in_live)
        self.v_open = tk.BooleanVar(value=s.open_after_get)
        self.v_core = tk.BooleanVar(value=s.collect_core_library)
        for var, text in ((self.v_save, "Press Save in Ableton Live automatically before sending"),
                          (self.v_open, "Open the project in Live right after getting it from the drive"),
                          (self.v_core, "Also pack Live's Core Library / Pack sounds into the project "
                                        "(safest, uses a little more space)")):
            ttk.Checkbutton(self, text=text, variable=var).pack(anchor="w", pady=(8, 0))

        tools = tk.Frame(self, bg=PANEL)
        tools.pack(fill="x", pady=(16, 0))
        ttk.Button(tools, text="Rescan plugins", command=self._rescan).pack(side="left")
        ttk.Button(tools, text="Set up a drive...", command=self._setup_drive).pack(side="left", padx=(8, 0))
        ttk.Button(tools, text="Clean Mac/Windows junk files on drive", command=self._clean).pack(side="left",
                                                                                                 padx=(8, 0))

        row = tk.Frame(self, bg=PANEL)
        row.pack(fill="x", pady=(18, 0))
        ttk.Button(row, text="Save", style="Accent.TButton", command=self._save).pack(side="right")
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="right", padx=(0, 8))
        self._show()

    def _browse_ws(self):
        d = filedialog.askdirectory(parent=self, initialdir=self.ws_var.get())
        if d:
            self.ws_var.set(d)

    def _rescan(self):
        app = self.app
        self.destroy()
        app.run_task("Scanning plugins...", lambda p, l: app.bridge.publish_machine(rescan=True, log=l),
                     done=lambda _: app.status_lbl.configure(text="✓ Plugin list updated.", fg=GREEN))

    def _setup_drive(self):
        app = self.app
        self.destroy()
        app.setup_drive()

    def _clean(self):
        d = self.app.bridge.drive
        if not d:
            messagebox.showwarning(APP_NAME, "No drive plugged in.", parent=self)
            return
        n = d.clean_junk()
        messagebox.showinfo(APP_NAME, f"Removed {n} junk file(s) from the drive.", parent=self)

    def _save(self):
        s = self.app.bridge.store.settings
        s.machine_name = self.name_var.get().strip() or s.machine_name
        s.workspace = self.ws_var.get().strip() or s.workspace
        s.project_roots = self.roots.items()
        s.extra_plugin_dirs = self.plugdirs.items()
        s.sample_search_dirs = self.searchdirs.items()
        s.auto_save_in_live = self.v_save.get()
        s.open_after_get = self.v_open.get()
        s.collect_core_library = self.v_core.get()
        self.app.bridge.store.save_settings()
        self.destroy()
        self.app._refresh_header()
        threading.Thread(target=lambda: self.app.bridge.publish_machine(), daemon=True).start()
        self.app.refresh()


def main():
    if pu.IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
