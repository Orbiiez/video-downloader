"""Video Downloader — paste links, they download in parallel.

Double-click this file (or the desktop shortcut) to open the app.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# pythonw has no console; give stray prints somewhere harmless to go.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import vdl_cookies
import vdl_core

APP = "Video Downloader"
SETTINGS = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "VideoDownloader", "settings.json")
URL_TOKEN = re.compile(r"^(https?://\S+|(www\.)?[\w-]+(\.[\w-]+)+(/\S*)?)$", re.I)
ACTIVE = ("starting", "downloading", "processing")
SAVED_LOGIN = "Saved login"


def fmt_bytes(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_eta(s):
    if s is None:
        return ""
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def shorten(text, n=95):
    return text if len(text) <= n else text[: n - 1] + "…"


def windows_dark_mode():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except Exception:
        return False


class Job:
    def __init__(self, url):
        self.url = url
        self.title = url
        self.state = "queued"
        self.detail = "Waiting…"
        self.fraction = None  # None = indeterminate
        self.paths = []
        self.error = None
        self.log = []
        self.cancel = threading.Event()
        self.dirty = True
        self.row = None
        self._files = []
        self._rate = (time.monotonic(), 0, None)  # time, bytes, smoothed speed

    def on_progress(self, d):
        st = d["status"]
        if d.get("title"):
            self.title = d["title"]
        if st == "downloading":
            self.state = "downloading"
            if d.get("file") and d["file"] not in self._files:
                self._files.append(d["file"])
            done, total, speed = d.get("downloaded"), d.get("total"), d.get("speed")
            if speed is None and done is not None:  # direct downloads: measure it ourselves
                t0, b0, sm = self._rate
                now = time.monotonic()
                if now - t0 >= 0.5:
                    inst = (done - b0) / (now - t0)
                    sm = inst if sm is None else sm * 0.7 + inst * 0.3
                    self._rate = (now, done, sm)
                speed = sm
            eta = d.get("eta")
            if eta is None and speed and total and done is not None:
                eta = max(0, (total - done) / speed)
            parts = []
            if total and done is not None:
                self.fraction = min(1.0, done / total)
                parts.append(f"{self.fraction:.0%}")
                parts.append(f"{fmt_bytes(done)} of {fmt_bytes(total)}")
            elif d.get("fragments") and d.get("fragment"):
                self.fraction = min(1.0, d["fragment"] / d["fragments"])
                parts.append(f"{self.fraction:.0%}")
                parts.append(fmt_bytes(done))
            else:
                self.fraction = None
                parts.append(fmt_bytes(done))
            if speed:
                parts.append(f"{fmt_bytes(speed)}/s")
            if eta:
                parts.append(f"{fmt_eta(eta)} left")
            label = "Downloading" if len(self._files) < 2 else f"Downloading part {len(self._files)}"
            self.detail = f"{label} — " + " · ".join(parts)
        elif st == "processing":
            self.state = "processing"
            self.fraction = None
            step = {"Merger": "Merging video and audio", "ExtractAudio": "Converting to mp3",
                    "FixupM3u8": "Fixing up stream", "MoveFiles": "Finishing"}.get(d.get("step"), "Finishing")
            self.detail = step + "…"
        self.dirty = True

    def on_log(self, msg):
        self.log.append(msg)
        if msg.startswith(("Direct download failed", "yt-dlp couldn't", "Trying ")):
            self.detail = shorten(msg, 140)
            self.dirty = True


class Row(ttk.Frame):
    def __init__(self, app, job):
        super().__init__(app.list_frame, padding=(12, 8, 12, 10))
        self.app, self.job = app, job
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, minsize=int(300 * app.root.winfo_fpixels("1i") / 96))
        self.title = ttk.Label(self, font=("Segoe UI Semibold", 10))
        self.title.grid(row=0, column=0, sticky="w")
        self.buttons = ttk.Frame(self)
        self.buttons.grid(row=0, column=1, rowspan=3, sticky="e", padx=(10, 0))
        self.bar = ttk.Progressbar(self, maximum=1.0)
        self.bar.grid(row=1, column=0, sticky="ew", pady=(5, 3))
        self.status = ttk.Label(self, font=("Segoe UI", 9), wraplength=620, justify="left")
        self.status.grid(row=2, column=0, sticky="w")
        ttk.Separator(self).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self._shown_state = None
        self._indeterminate = False
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Copy link", command=lambda: app.copy(job.url))
        menu.add_command(label="Copy details", command=lambda: app.copy("\n".join([job.url, job.detail] + job.log)))
        for w in (self, self.title, self.status, self.bar):
            w.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

    def button(self, text, cmd, accent=False):
        ttk.Button(self.buttons, text=text, command=cmd, style="Accent.TButton" if accent else "TButton",
                   width=8).pack(side="left", padx=(6, 0))

    def render(self):
        j = self.job
        self.title.configure(text=shorten(j.title))
        color = {"failed": self.app.err_color, "done": self.app.ok_color}.get(j.state, self.app.dim_color)
        self.status.configure(text=j.detail, foreground=color)
        busy = j.state in ACTIVE and j.fraction is None
        if busy and not self._indeterminate:
            self.bar.configure(mode="indeterminate")
            self.bar.start(12)
        elif not busy:
            if self._indeterminate:
                self.bar.stop()
            self.bar.configure(mode="determinate")
            self.bar["value"] = 1.0 if j.state == "done" else (j.fraction or 0)
        self._indeterminate = busy
        if j.state != self._shown_state:  # rebuild buttons only when the state changes
            group = lambda s: "active" if s in ACTIVE or s == "queued" else s
            if group(j.state) != group(self._shown_state or ""):
                for b in self.buttons.winfo_children():
                    b.destroy()
                if j.state in ACTIVE or j.state == "queued":
                    self.button("Cancel", lambda: self.app.cancel(j))
                elif j.state == "done":
                    self.button("Play", lambda: self.app.open_file(j.paths[0]), accent=True)
                    self.button("Show", lambda: self.app.show_file(j.paths[0]))
                    self.button("Remove", lambda: self.app.remove(j))
                else:
                    self.button("Retry", lambda: self.app.retry(j), accent=True)
                    self.button("Remove", lambda: self.app.remove(j))
            self._shown_state = j.state


class SitePicker:
    """Modal list of the sites found in a cookies.txt; only the ticked ones are kept."""

    def __init__(self, root, sites, preselected):
        self.result = None
        self.top = top = tk.Toplevel(root)
        top.title("Which logins to keep?")
        top.transient(root)
        top.resizable(False, True)
        frame = ttk.Frame(top, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"This file has logins for {len(sites)} site{'s' * (len(sites) != 1)}.\n"
                              "Tick only the ones you download videos from; the rest are thrown away.",
                  justify="left").pack(anchor="w")
        box = ttk.Frame(frame)
        box.pack(fill="both", expand=True, pady=10)
        self.lb = tk.Listbox(box, selectmode="multiple", height=min(14, max(4, len(sites))), width=40,
                             activestyle="none", exportselection=False, font=("Segoe UI", 10))
        sb = ttk.Scrollbar(box, orient="vertical", command=self.lb.yview)
        self.lb.configure(yscrollcommand=sb.set)
        self.lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.sites = sites
        for i, s in enumerate(sites):
            self.lb.insert("end", s)
            if s in preselected:
                self.lb.selection_set(i)
        btns = ttk.Frame(frame)
        btns.pack(fill="x")
        ttk.Button(btns, text="Keep selected", style="Accent.TButton", command=self.ok).pack(side="right")
        ttk.Button(btns, text="Cancel", command=top.destroy).pack(side="right", padx=(0, 8))
        ttk.Button(btns, text="Select none", command=lambda: self.lb.selection_clear(0, "end")).pack(side="left")
        top.grab_set()
        top.wait_window()

    def ok(self):
        picked = [self.sites[i] for i in self.lb.curselection()]
        if not picked:
            messagebox.showinfo(APP, "Tick at least one site.", parent=self.top)
            return
        self.result = picked
        self.top.destroy()


class App:
    def __init__(self, root):
        self.root = root
        self.jobs = []
        self.settings = self.load_settings()
        root.title(APP)
        root.geometry(self.settings.get("geometry", "1060x700"))
        root.minsize(760, 520)

        dark = windows_dark_mode()
        try:
            import sv_ttk
            sv_ttk.set_theme("dark" if dark else "light")
        except Exception:
            dark = False
        self.err_color = "#ff6b6b" if dark else "#c42b1c"
        self.ok_color = "#4cc66a" if dark else "#0f7b0f"
        self.dim_color = "#b0b0b0" if dark else "#555555"
        text_bg, text_fg = ("#2b2b2b", "#f0f0f0") if dark else ("#ffffff", "#1a1a1a")

        outer = ttk.Frame(root, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)

        # --- link input ---
        ttk.Label(outer, text="Paste video links — one per line, as many as you like",
                  font=("Segoe UI Semibold", 11)).grid(row=0, column=0, sticky="w")
        top = ttk.Frame(outer)
        top.grid(row=1, column=0, sticky="ew", pady=(6, 10))
        top.columnconfigure(0, weight=1)
        self.text = tk.Text(top, height=4, wrap="none", font=("Segoe UI", 10), undo=True, relief="flat",
                            bg=text_bg, fg=text_fg, insertbackground=text_fg, highlightthickness=1,
                            highlightbackground="#555555" if dark else "#c8c8c8", highlightcolor="#3a8ee6",
                            padx=8, pady=6)
        self.text.grid(row=0, column=0, rowspan=2, sticky="nsew")
        self.text.bind("<Control-Return>", lambda e: (self.add_from_text(), "break")[1])
        self.text.bind("<Return>", self.on_enter)
        btns = ttk.Frame(top)
        btns.grid(row=0, column=1, rowspan=2, sticky="ns", padx=(10, 0))
        ttk.Button(btns, text="Download", style="Accent.TButton", width=17,
                   command=self.add_from_text).pack(fill="x")
        ttk.Button(btns, text="Paste & Download", width=17, command=self.paste_and_add).pack(fill="x", pady=(8, 0))

        # --- options ---
        opts = ttk.Frame(outer)
        opts.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        opts.columnconfigure(1, weight=1)
        ttk.Label(opts, text="Save to").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.out_var = tk.StringVar(value=self.settings.get("out_dir", vdl_core.DEFAULT_OUT))
        ttk.Entry(opts, textvariable=self.out_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(opts, text="Change…", command=self.choose_dir).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(opts, text="Open folder", command=lambda: self.open_file(self.out_dir())).grid(row=0, column=3, padx=(8, 0))

        opts2 = ttk.Frame(outer)
        opts2.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(opts2, text="Quality").pack(side="left")
        self.quality_var = tk.StringVar(value=self.settings.get("quality", "Best available"))
        ttk.Combobox(opts2, textvariable=self.quality_var, values=list(vdl_core.QUALITIES), state="readonly",
                     width=17).pack(side="left", padx=(8, 22))
        ttk.Label(opts2, text="Downloads at once").pack(side="left")
        self.parallel_var = tk.IntVar(value=self.settings.get("parallel", 3))
        ttk.Spinbox(opts2, from_=1, to=10, textvariable=self.parallel_var, width=4, state="readonly",
                    command=self.schedule).pack(side="left", padx=(8, 22))
        ttk.Label(opts2, text="Use login from").pack(side="left")
        vdl_cookies.sweep_temp()  # wipe temp cookie files a crash may have left behind
        self.login = vdl_cookies.status()
        choices = ["None", SAVED_LOGIN] + vdl_core.BROWSERS
        choice = self.settings.get("cookies", "None")
        if choice not in choices or (choice == SAVED_LOGIN and not self.has_login()):
            choice = "None"
        self.cookies_var = tk.StringVar(value=choice)
        self._last_cookie_choice = choice
        cb = ttk.Combobox(opts2, textvariable=self.cookies_var, values=choices, state="readonly", width=13)
        cb.pack(side="left", padx=(8, 0))
        cb.bind("<<ComboboxSelected>>", self.on_cookie_choice)
        self.cookie_hint = ttk.Label(opts2, foreground=self.dim_color)
        self.cookie_hint.pack(side="left", padx=(8, 0))
        self.forget_btn = ttk.Button(opts2, text="Forget", command=self.forget_login)
        self.update_cookie_hint()
        if self.login and self.login.get("expired"):
            root.after(600, lambda: messagebox.showinfo(
                APP, f"Your saved login was older than {vdl_cookies.MAX_AGE_DAYS} days, so it was deleted "
                     "for safety.\n\nImport a fresh cookies.txt when you need it again."))

        # --- download list ---
        box = ttk.Frame(outer, style="Card.TFrame", padding=2)
        box.grid(row=4, column=0, sticky="nsew", pady=(6, 0))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        style = ttk.Style()
        list_bg = (style.lookup("TFrame", "background") or style.lookup(".", "background")
                   or ("#1c1c1c" if dark else "#fafafa"))
        self.canvas = tk.Canvas(box, highlightthickness=0, bd=0, bg=list_bg)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(box, orient="vertical", command=self.canvas.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=sb.set)
        self.list_frame = ttk.Frame(self.canvas)
        self.list_frame.columnconfigure(0, weight=1)
        self._win = self.canvas.create_window(0, 0, window=self.list_frame, anchor="nw")
        self.list_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self.on_canvas_resize)
        self.canvas.bind_all("<MouseWheel>", self.on_wheel)
        self.empty = ttk.Label(self.list_frame, text="No downloads yet.\n\nPaste a link above and press Download.",
                               foreground=self.dim_color, justify="center", font=("Segoe UI", 11))
        self.empty.grid(row=0, column=0, pady=80)

        # --- footer ---
        foot = ttk.Frame(outer)
        foot.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        self.summary = ttk.Label(foot, text="", foreground=self.dim_color)
        self.summary.pack(side="left")
        ttk.Button(foot, text="Update downloader", command=self.update_ytdlp).pack(side="right")
        ttk.Button(foot, text="Cancel all", command=self.cancel_all).pack(side="right", padx=(0, 8))
        ttk.Button(foot, text="Clear finished", command=self.clear_finished).pack(side="right", padx=(0, 8))

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Destroy>", self.on_destroy)
        self.text.focus_set()
        self.tick()

    # ---------- settings ----------
    def load_settings(self):
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_settings(self):
        data = {"out_dir": self.out_dir(), "quality": self.quality_var.get(), "parallel": self.parallel(),
                "cookies": self.cookies_var.get(), "geometry": self.root.geometry()}
        try:
            os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass

    def out_dir(self):
        return os.path.expandvars(os.path.expanduser(self.out_var.get().strip().strip('"'))) or vdl_core.DEFAULT_OUT

    def parallel(self):
        try:
            return max(1, min(10, int(self.parallel_var.get())))
        except (tk.TclError, ValueError):
            return 3

    # ---------- adding links ----------
    def on_enter(self, event):
        # Enter on a single-line box downloads; with several lines it just adds a new line.
        if "\n" not in self.text.get("1.0", "end-1c").strip():
            self.add_from_text()
            return "break"

    def add_from_text(self):
        raw = self.text.get("1.0", "end")
        urls = [t for t in raw.split() if URL_TOKEN.match(t)]
        if not urls:
            if raw.strip():
                self.flash("That doesn't look like a link.")
            return
        added = self.add_urls(urls)
        self.text.delete("1.0", "end")
        skipped = len(urls) - added
        self.flash(f"Added {added} link{'s' * (added != 1)}" + (f" ({skipped} already in the list)" if skipped else ""))

    def paste_and_add(self):
        try:
            clip = self.root.clipboard_get()
        except tk.TclError:
            clip = ""
        if not clip.strip():
            self.flash("Clipboard is empty.")
            return
        self.text.insert("end", ("\n" if self.text.get("1.0", "end").strip() else "") + clip)
        self.add_from_text()

    def add_urls(self, urls):
        current = {j.url for j in self.jobs if j.state not in ("failed", "cancelled")}
        added = 0
        for u in urls:
            u = u.strip().rstrip(".,;)")
            if not re.match(r"(?i)^https?://", u):
                u = "https://" + u
            if u in current:
                continue
            current.add(u)
            job = Job(u)
            self.jobs.append(job)
            self.add_row(job)
            added += 1
        self.schedule()
        self.save_settings()
        return added

    def add_row(self, job):
        self.empty.grid_remove()
        job.row = Row(self, job)
        job.row.grid(row=len(self.jobs), column=0, sticky="ew")
        job.row.render()
        self.root.after(50, lambda: self.canvas.yview_moveto(1.0))

    # ---------- running ----------
    def schedule(self):
        running = sum(j.state in ACTIVE for j in self.jobs)
        for job in self.jobs:
            if running >= self.parallel():
                break
            if job.state == "queued":
                job.state, job.detail, job.dirty = "starting", "Looking up the video…", True
                threading.Thread(target=self.worker, args=(job, self.out_dir(), self.quality_var.get(),
                                                           *self.cookies()), daemon=True).start()
                running += 1

    def cookies(self):
        """(browser, use_saved_login) for the current login choice."""
        c = self.cookies_var.get()
        if c == SAVED_LOGIN:
            return None, True
        return (None if c == "None" else c), False

    def has_login(self):
        return bool(self.login) and not self.login.get("expired")

    def on_cookie_choice(self, event=None):
        if self.cookies_var.get() == SAVED_LOGIN and not self.has_login() and not self.import_login():
            self.cookies_var.set(self._last_cookie_choice)
            return
        self._last_cookie_choice = self.cookies_var.get()
        self.update_cookie_hint()
        self.save_settings()

    def import_login(self):
        """cookies.txt -> pick sites -> encrypted store -> offer to wipe the plain-text original."""
        if not messagebox.askokcancel(APP, (
                "Choose a cookies.txt exported from your browser (e.g. with the 'Get cookies.txt LOCALLY' "
                "extension, while logged in to the site).\n\n"
                "Keep it safe: anyone with that file can log in as you. The app will:\n"
                "  \u2022 keep only the sites you tick\n"
                "  \u2022 store them encrypted so only your Windows account can read them\n"
                f"  \u2022 forget them automatically after {vdl_cookies.MAX_AGE_DAYS} days\n"
                "  \u2022 offer to wipe the original file")):
            return False
        path = filedialog.askopenfilename(title="Choose the cookies.txt you exported",
                                          initialdir=os.path.join(os.path.expanduser("~"), "Downloads"),
                                          filetypes=[("Cookie files", "*.txt"), ("All files", "*.*")])
        if not path:
            return False
        try:
            cookies = vdl_cookies.read_file(path)
        except Exception as e:
            messagebox.showerror(APP, f"Couldn't read that file:\n{e}")
            return False
        sites = vdl_cookies.domains_in(cookies)
        keep = SitePicker(self.root, sites, vdl_cookies.suggested_sites(sites)).result
        if not keep:
            return False
        try:
            saved = vdl_cookies.save_login(cookies, keep)
        except Exception as e:
            messagebox.showerror(APP, f"Couldn't save the login:\n{e}")
            return False
        self.login = vdl_cookies.status()
        dropped = len(sites) - len(saved)
        if messagebox.askyesno(APP, (
                f"Saved an encrypted login for {len(saved)} site{'s' * (len(saved) != 1)}"
                + (f" ({dropped} other site{'s' * (dropped != 1)} discarded)" if dropped else "") + ".\n\n"
                f"Delete the original file now? (Recommended)\n{path}\n\n"
                "The app no longer needs it, and it's readable by anything that can open it."), icon="warning"):
            vdl_cookies.wipe(path)
            if os.path.exists(path):
                messagebox.showwarning(APP, f"Couldn't delete the file; please delete it yourself:\n{path}")
        return True

    def forget_login(self):
        if messagebox.askyesno(APP, "Delete the saved login? Login-only videos will need a fresh import."):
            vdl_cookies.forget()
            self.login = None
            self.cookies_var.set("None")
            self.on_cookie_choice()

    def update_cookie_hint(self):
        c = self.cookies_var.get()
        self.forget_btn.pack_forget()
        if c == SAVED_LOGIN and self.has_login():
            sites = self.login["sites"]
            more = f" +{len(sites) - 1}" if len(sites) > 1 else ""
            text = f"\U0001F512 {shorten(sites[0], 18)}{more} \u00b7 {self.login['days_left']}d left"
            self.forget_btn.pack(side="left", padx=(8, 0))
        elif c in ("chrome", "edge", "brave", "opera", "vivaldi", "chromium"):
            text = "(may fail; Saved login is more reliable)"
        elif c == "None":
            text = "(for login-only videos)"
        else:
            text = ""
        self.cookie_hint.configure(text=text)

    def worker(self, job, out_dir, quality, cookies_browser, use_saved_login):
        try:
            job.paths = vdl_core.download(job.url, out_dir, quality, cookies_browser, progress=job.on_progress,
                                          cancel=job.cancel, log=job.on_log, use_saved_login=use_saved_login)
            job.state, job.fraction = "done", 1.0
            names = ", ".join(os.path.basename(p) for p in job.paths)
            job.detail = f"Saved {names}" + (f"  ({fmt_bytes(self.size(job.paths))})" if self.size(job.paths) else "")
            if job.title == job.url:
                job.title = os.path.splitext(os.path.basename(job.paths[0]))[0]
        except vdl_core.Cancelled:
            job.state, job.detail = "cancelled", "Cancelled"
        except Exception as e:
            job.state, job.error = "failed", vdl_core.clean_error(e)
            job.detail = "Failed: " + job.error
            job.log.append(job.detail)
        job.dirty = True

    @staticmethod
    def size(paths):
        try:
            return sum(os.path.getsize(p) for p in paths)
        except OSError:
            return 0

    def tick(self):
        """UI heartbeat: redraw rows that changed and start queued jobs when slots free up."""
        for job in self.jobs:
            if job.dirty and job.row:
                job.dirty = False
                job.row.render()
        self.schedule()
        counts = {}
        for j in self.jobs:
            key = "downloading" if j.state in ACTIVE else j.state
            counts[key] = counts.get(key, 0) + 1
        order = ["downloading", "queued", "done", "failed", "cancelled"]
        self.summary.configure(text=self._flash or "  ·  ".join(f"{counts[k]} {k}" for k in order if counts.get(k)))
        self._tick_id = self.root.after(200, self.tick)

    _flash = None

    def flash(self, msg):
        self._flash = msg
        self.root.after(3000, lambda: setattr(self, "_flash", None) if self._flash == msg else None)

    # ---------- row actions ----------
    def cancel(self, job):
        if job.state == "queued":
            job.state, job.detail = "cancelled", "Cancelled"
        else:
            job.detail = "Cancelling…"
        job.cancel.set()
        job.dirty = True

    def cancel_all(self):
        for j in self.jobs:
            if j.state in ACTIVE or j.state == "queued":
                self.cancel(j)

    def retry(self, job):
        job.cancel = threading.Event()
        job.state, job.detail, job.fraction, job.error, job.log, job._files = "queued", "Waiting…", None, None, [], []
        job.dirty = True
        self.schedule()

    def remove(self, job):
        if job.state in ACTIVE:
            return
        job.row.destroy()
        self.jobs.remove(job)
        for i, j in enumerate(self.jobs):
            j.row.grid(row=i + 1)
        if not self.jobs:
            self.empty.grid()

    def clear_finished(self):
        for j in [j for j in self.jobs if j.state in ("done", "failed", "cancelled")]:
            self.remove(j)

    def copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.flash("Copied.")

    def open_file(self, path):
        try:
            if path == self.out_dir():
                os.makedirs(path, exist_ok=True)
            os.startfile(path)
        except OSError as e:
            messagebox.showerror(APP, f"Couldn't open {path}:\n{e}")

    def show_file(self, path):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])

    def choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.out_dir(), title="Save videos to")
        if d:
            self.out_var.set(os.path.normpath(d))
            self.save_settings()

    def update_ytdlp(self):
        self.flash("Updating the download engine…")
        if vdl_core.FROZEN:  # no pip in the packaged app: fetch yt-dlp's self-contained zip build instead
            threading.Thread(target=self.update_engine_zip, daemon=True).start()
            return
        py = os.path.join(os.path.dirname(sys.executable), "python.exe")
        py = py if os.path.exists(py) else sys.executable

        def run():
            r = subprocess.run([py, "-m", "pip", "install", "-U", "yt-dlp[default]"], capture_output=True, text=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0:
                msg = ("Already up to date." if "already satisfied" in r.stdout and "Successfully" not in r.stdout
                       else "Updated. Restart the app to use the new version.")
                self.root.after(0, lambda: messagebox.showinfo(APP, msg))
            else:
                err = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["unknown error"]
                self.root.after(0, lambda: messagebox.showerror(APP, "Update failed:\n" + err[0]))

        threading.Thread(target=run, daemon=True).start()

    def update_engine_zip(self):
        import urllib.request
        import yt_dlp.version
        api = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
        try:
            req = urllib.request.Request(api, headers={"User-Agent": APP})
            with urllib.request.urlopen(req, timeout=30) as r:
                rel = json.load(r)
            if rel["tag_name"] == yt_dlp.version.__version__:
                msg = "Already up to date."
            else:
                asset = next(a for a in rel["assets"] if a["name"] == "yt-dlp")  # the zipimport build
                os.makedirs(os.path.dirname(vdl_core.ENGINE_ZIP), exist_ok=True)
                tmp = vdl_core.ENGINE_ZIP + ".new"
                urllib.request.urlretrieve(asset["browser_download_url"], tmp)
                os.replace(tmp, vdl_core.ENGINE_ZIP)
                msg = f"Updated to {rel['tag_name']}. Restart the app to use the new version."
            self.root.after(0, lambda: messagebox.showinfo(APP, msg))
        except Exception as e:
            err = vdl_core.clean_error(e)
            self.root.after(0, lambda: messagebox.showerror(APP, "Update failed:\n" + err))

    # ---------- window ----------
    def on_canvas_resize(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)
        for j in self.jobs:
            j.row.status.configure(wraplength=max(300, e.width - 300))

    def on_wheel(self, e):
        if str(e.widget).startswith(str(self.canvas)) and self.list_frame.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-e.delta / 120), "units")

    def on_destroy(self, e):
        if e.widget is self.root:
            for timer in self.root.tk.splitlist(self.root.tk.call("after", "info")):
                self.root.after_cancel(timer)

    def on_close(self):
        busy = [j for j in self.jobs if j.state in ACTIVE or j.state == "queued"]
        if busy and not messagebox.askyesno(APP, f"{len(busy)} download(s) still running. Cancel them and quit?"):
            return
        self.save_settings()
        self.cancel_all()
        deadline = time.monotonic() + 5

        def wait():  # give workers a moment to delete their partial files
            if any(j.state in ACTIVE for j in self.jobs) and time.monotonic() < deadline:
                self.root.after(100, wait)
            else:
                self.root.destroy()

        wait()


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VideoDownloader")
    except Exception:
        pass
    root = tk.Tk()
    app = App(root)
    if len(sys.argv) > 1:  # links passed on the command line
        app.add_urls(sys.argv[1:])
    root.mainloop()


if __name__ == "__main__":
    main()
