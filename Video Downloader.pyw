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
import tkinter.font as tkfont
import urllib.parse
import urllib.request
from tkinter import filedialog, messagebox, ttk

# pythonw has no console; give stray prints somewhere harmless to go.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import vdl_cookies
import vdl_core

try:  # thumbnails and the header logo; the app still works without Pillow
    from PIL import Image, ImageDraw, ImageTk
except ImportError:
    Image = None

APP = "Video Downloader"
VERSION = "1.1.0"
RELEASES_API = "https://api.github.com/repos/Orbiiez/video-downloader/releases/latest"
SETTINGS = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "VideoDownloader", "settings.json")
RES = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
URL_TOKEN = re.compile(r"^(https?://\S+|(www\.)?[\w-]+(\.[\w-]+)+(/\S*)?)$", re.I)
ACTIVE = ("starting", "downloading", "processing")
SAVED_LOGIN = "Saved login"

# ---------- look ----------
BG = "#0e0e11"
SURFACE = "#16161a"
RAISED = "#202027"
LINE = "#2a2a32"
TEXT = "#eceae4"
MUTED = "#8b8993"
FAINT = "#5c5a64"
LIME = "#d4ff4f"
ON_LIME = "#101012"
AMBER = "#ffb547"
ERR = "#ff6a55"

# Segoe Fluent Icons / MDL2 glyphs
G_PLAY, G_FOLDER, G_CLOSE, G_RETRY = "", "", "", ""
G_LOCK, G_DOWN, G_MORE, G_CHEV, G_LINK = "", "", "", "", ""
G_SCREEN, G_STACK, G_PASTE = "", "", ""

THUMB_W, THUMB_H = 128, 72  # at 100% scaling


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


def site_of(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


class Fonts:
    """Resolved once Tk exists; falls back to plain Segoe UI / Consolas on Windows 10."""

    def __init__(self, root):
        have = set(tkfont.families(root))
        pick = lambda *names: next((n for n in names if n in have), names[-1])
        ui = pick("Segoe UI Variable Text", "Segoe UI")
        display = pick("Segoe UI Variable Display", "Segoe UI")
        mono = pick("Cascadia Mono", "Consolas")
        icon = pick("Segoe Fluent Icons", "Segoe MDL2 Assets")
        self.body = (ui, 10)
        self.small = (ui, 9)
        self.title = (display, 11, "bold")
        self.brand = (display, 14, "bold")
        self.empty = (display, 15, "bold")
        self.mono = (mono, 9)
        self.mono_small = (mono, 8)
        self.icon = (icon, 11)
        self.icon_small = (icon, 8)
        self.icon_big = (icon, 30)
        self.button = (ui, 10, "bold")


def dark_title_bar(win):
    """Windows 11: title bar in the app's own colors (Windows 10: just dark)."""
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        set_attr = ctypes.windll.dwmapi.DwmSetWindowAttribute
        set_attr(hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)  # immersive dark mode
        rgb = int(BG[5:7] + BG[3:5] + BG[1:3], 16)  # COLORREF is 0x00BBGGRR
        set_attr(hwnd, 35, ctypes.byref(ctypes.c_int(rgb)), 4)  # caption color
        set_attr(hwnd, 34, ctypes.byref(ctypes.c_int(rgb)), 4)  # border color
    except Exception:
        pass


def set_icon(win):
    try:
        win.iconbitmap(os.path.join(RES, "assets", "icon.ico"))
    except tk.TclError:
        pass


def style_menu(menu, fonts):
    menu.configure(tearoff=0, bg=RAISED, fg=TEXT, activebackground=LIME, activeforeground=ON_LIME,
                   selectcolor=LIME, font=fonts.body, relief="flat", bd=0, activeborderwidth=0)
    return menu


# ---------- small widgets ----------
class Tooltip:
    def __init__(self, widget, text, fonts):
        self.widget, self.text, self.fonts, self.tip, self._after = widget, text, fonts, None, None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def schedule(self, e=None):
        self._after = self.widget.after(450, self.show)

    def show(self):
        if self.tip or not self.widget.winfo_exists():
            return
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        tk.Label(self.tip, text=self.text, bg=RAISED, fg=TEXT, font=self.fonts.small, padx=8, pady=3,
                 highlightthickness=1, highlightbackground=LINE).pack()
        self.tip.update_idletasks()
        self.tip.geometry(f"+{x - self.tip.winfo_width() // 2}+{y}")

    def hide(self, e=None):
        if self._after:
            self.widget.after_cancel(self._after)
            self._after = None
        if self.tip:
            self.tip.destroy()
            self.tip = None


class Btn(tk.Label):
    """Flat button. kind: primary (lime), ghost (text only), icon (glyph only), icon-primary."""

    def __init__(self, parent, text, command, fonts, kind="ghost", bg=BG, tip=None, **kw):
        self.command, self.kind, self.base_bg = command, kind, bg
        icon = kind.startswith("icon")
        font = fonts.icon if icon else (fonts.button if kind == "primary" else fonts.body)
        pad = (7, 5) if icon else ((18, 8) if kind == "primary" else (10, 5))
        super().__init__(parent, text=text, font=font, cursor="hand2", padx=pad[0], pady=pad[1], **kw)
        self.paint(False)
        self.bind("<Enter>", lambda e: self.paint(True))
        self.bind("<Leave>", lambda e: self.paint(False))
        self.bind("<ButtonRelease-1>", self.click)
        if tip:
            Tooltip(self, tip, fonts)

    def paint(self, hover):
        if self.kind == "primary":
            self.configure(bg="#e2ff85" if hover else LIME, fg=ON_LIME)
        elif self.kind == "icon-primary":
            self.configure(bg=RAISED if hover else self.base_bg, fg=LIME)
        else:
            self.configure(bg=RAISED if hover else self.base_bg, fg=TEXT if hover else MUTED)

    def click(self, e):
        if 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height():
            self.command()


class Chip(tk.Frame):
    """Rounded-looking option pill: icon, current value, chevron; click opens a menu."""

    def __init__(self, parent, icon, fonts, build_menu, tip=None):
        super().__init__(parent, bg=SURFACE, highlightthickness=1, highlightbackground=LINE, cursor="hand2")
        self.fonts, self.build_menu = fonts, build_menu
        self.glyph = tk.Label(self, text=icon, font=fonts.icon_small, bg=SURFACE, fg=LIME, padx=0)
        self.glyph.pack(side="left", padx=(10, 6), pady=6)
        self.label = tk.Label(self, font=fonts.body, bg=SURFACE, fg=TEXT)
        self.label.pack(side="left")
        self.chev = tk.Label(self, text=G_CHEV, font=fonts.icon_small, bg=SURFACE, fg=FAINT)
        self.chev.pack(side="left", padx=(8, 10))
        for w in (self, self.glyph, self.label, self.chev):
            w.bind("<Enter>", lambda e: self.paint(True))
            w.bind("<Leave>", lambda e: self.paint(False))
            w.bind("<Button-1>", self.open)
        if tip:
            Tooltip(self, tip, fonts)

    def set(self, text):
        self.label.configure(text=text)

    def paint(self, hover):
        bg = RAISED if hover else SURFACE
        for w in (self, self.glyph, self.label, self.chev):
            w.configure(bg=bg)
        self.configure(highlightbackground=FAINT if hover else LINE)

    def open(self, e=None):
        menu = style_menu(tk.Menu(self), self.fonts)
        self.build_menu(menu)
        menu.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.winfo_height() + 4)


class Bar(tk.Canvas):
    """3px progress line along the bottom of a card; slides a segment when progress is unknown."""

    def __init__(self, parent, scale):
        super().__init__(parent, height=max(2, round(3 * scale)), bg=LINE, highlightthickness=0, bd=0)
        self.fraction, self.color, self.moving, self.phase = 0.0, LIME, False, 0.0
        self.fill = self.create_rectangle(0, 0, 0, 0, width=0, fill=LIME)
        self.bind("<Configure>", lambda e: self.draw())

    def set(self, fraction, color, moving):
        self.fraction, self.color, self.moving = fraction or 0.0, color, moving
        self.draw()

    def step(self):
        self.phase = (self.phase + 0.012) % 1.3
        self.draw()

    def draw(self):
        w, h = self.winfo_width(), self.winfo_height()
        if self.moving:
            x0 = (self.phase - 0.3) * w
            self.coords(self.fill, x0, 0, x0 + 0.3 * w, h)
        else:
            self.coords(self.fill, 0, 0, w * self.fraction, h)
        self.itemconfigure(self.fill, fill=self.color)


# ---------- thumbnails ----------
def rounded_thumb(img, size, radius, bg):
    """Cover-crop to size, round the corners onto the card color."""
    w, h = size
    src_ratio, dst_ratio = img.width / img.height, w / h
    if src_ratio > dst_ratio:
        cw = int(img.height * dst_ratio)
        img = img.crop(((img.width - cw) // 2, 0, (img.width - cw) // 2 + cw, img.height))
    else:
        ch = int(img.width / dst_ratio)
        img = img.crop((0, (img.height - ch) // 2, img.width, (img.height - ch) // 2 + ch))
    img = img.convert("RGB").resize((w * 2, h * 2), Image.LANCZOS)
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius=radius * 2, fill=255)
    out = Image.new("RGB", img.size, bg)
    out.paste(img, (0, 0), mask)
    return out.resize((w, h), Image.LANCZOS)


def placeholder_thumb(size, radius, dim=False):
    """Dark tile with the app's play-down mark, for links that have no thumbnail (yet)."""
    w, h = size[0] * 2, size[1] * 2
    img = Image.new("RGB", (w, h), SURFACE)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius * 2, fill=RAISED)
    s = h * 0.22
    cx, cy = w / 2, h / 2 - s * 0.35
    color = FAINT if dim else "#3d3d46"
    d.polygon([(cx - s, cy - s * 0.7), (cx + s, cy - s * 0.7), (cx, cy + s * 0.75)], fill=color)
    d.rounded_rectangle((cx - s * 1.1, cy + s * 1.15, cx + s * 1.1, cy + s * 1.4), radius=s * 0.12, fill=color)
    return img.resize(size, Image.LANCZOS)


def fetch_thumb(job, size, radius):
    try:
        req = urllib.request.Request(job.thumb_url, headers={"User-Agent": vdl_core.UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read(8_000_000)
        import io
        job.thumb = rounded_thumb(Image.open(io.BytesIO(data)), size, radius, SURFACE)
        job.dirty = True
    except Exception:
        pass  # keep the placeholder


class Job:
    def __init__(self, url):
        self.url = url
        self.title = url
        self.site = site_of(url)
        self.duration = None
        self.thumb_url = None
        self.thumb = None  # PIL image once fetched
        self.state = "queued"
        self.detail = "Queued"
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
        if d.get("duration"):
            self.duration = d["duration"]
        if d.get("thumbnail") and not self.thumb_url and Image and self.row:
            self.thumb_url = d["thumbnail"]
            threading.Thread(target=fetch_thumb, args=(self, self.row.thumb_size, self.row.radius),
                             daemon=True).start()
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
            if len(self._files) > 1:
                parts.append(f"part {len(self._files)}")
            if total and done is not None:
                self.fraction = min(1.0, done / total)
                parts.append(f"{self.fraction:.0%}")
                parts.append(f"{fmt_bytes(done)} / {fmt_bytes(total)}")
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
            self.detail = "  ·  ".join(parts)
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


class Row(tk.Frame):
    """One download card: thumbnail, title, site/duration, status, actions, progress line."""

    STATUS_COLOR = {"failed": ERR, "processing": AMBER, "downloading": TEXT}

    def __init__(self, app, job):
        super().__init__(app.list_frame, bg=SURFACE)
        self.app, self.job = app, job
        f, s = app.fonts, app.scale
        self.thumb_size, self.radius = (round(THUMB_W * s), round(THUMB_H * s)), round(6 * s)
        self.columnconfigure(1, weight=1)

        self.thumb = tk.Label(self, bg=SURFACE, bd=0)
        self.thumb.grid(row=0, column=0, rowspan=3, padx=(12, 14), pady=12, sticky="w")
        self._thumb_img, self._thumb_kind = None, None

        self.title = tk.Label(self, font=f.title, bg=SURFACE, fg=TEXT, anchor="w")
        self.title.grid(row=0, column=1, sticky="sew", pady=(12, 0))
        meta = tk.Frame(self, bg=SURFACE)
        meta.grid(row=1, column=1, sticky="w", pady=(4, 0))
        self.badge = tk.Label(meta, font=f.mono_small, bg=RAISED, fg=MUTED, padx=6, pady=1)
        self.badge.pack(side="left")
        self.dur = tk.Label(meta, font=f.mono_small, bg=SURFACE, fg=FAINT)
        self.dur.pack(side="left", padx=(8, 0))
        self.status = tk.Label(self, font=f.mono, bg=SURFACE, fg=MUTED, anchor="w", justify="left",
                               wraplength=600)
        self.status.grid(row=2, column=1, sticky="nw", pady=(6, 12))

        self.buttons = tk.Frame(self, bg=SURFACE)
        self.buttons.grid(row=0, column=2, rowspan=3, padx=(8, 10))
        self.bar = Bar(self, s)
        self.bar.grid(row=3, column=0, columnspan=3, sticky="ew")
        self._shown_state = None

        menu = style_menu(tk.Menu(self), f)
        menu.add_command(label="Copy link", command=lambda: app.copy(job.url))
        menu.add_command(label="Copy details", command=lambda: app.copy("\n".join([job.url, job.detail] + job.log)))
        for w in (self, self.thumb, self.title, self.status, meta, self.badge):
            w.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))
            w.bind("<Double-Button-1>", lambda e: self.app.open_file(job.paths[0]) if job.state == "done" else None)

    def button(self, glyph, tip, cmd, primary=False):
        Btn(self.buttons, glyph, cmd, self.app.fonts, kind="icon-primary" if primary else "icon", bg=SURFACE,
            tip=tip).pack(side="left", padx=(2, 0))

    def set_thumb(self):
        j = self.job
        kind = "real" if j.thumb is not None else ("dim" if j.state in ("failed", "cancelled") else "ph")
        if not Image or kind == self._thumb_kind:
            return
        img = j.thumb if kind == "real" else placeholder_thumb(self.thumb_size, self.radius, dim=kind == "dim")
        self._thumb_img = ImageTk.PhotoImage(img)
        self.thumb.configure(image=self._thumb_img)
        self._thumb_kind = kind

    def render(self):
        j = self.job
        self.set_thumb()
        self.title.configure(text=shorten(j.title, 80), fg=FAINT if j.state == "cancelled" else TEXT)
        self.badge.configure(text=(j.site or "link").upper())
        self.dur.configure(text=fmt_eta(j.duration) if j.duration else "")
        self.status.configure(text=j.detail, fg=self.STATUS_COLOR.get(j.state, MUTED))
        busy = j.state in ACTIVE and j.fraction is None
        color = {"failed": ERR, "processing": AMBER, "cancelled": FAINT}.get(j.state, LIME)
        fraction = 1.0 if j.state in ("done", "failed") else (j.fraction or 0)
        self.bar.set(fraction, color, busy)
        if j.state != self._shown_state:  # rebuild buttons only when the state group changes
            group = lambda s: "active" if s in ACTIVE or s == "queued" else s
            if group(j.state) != group(self._shown_state or ""):
                for b in self.buttons.winfo_children():
                    b.destroy()
                if j.state in ACTIVE or j.state == "queued":
                    self.button(G_CLOSE, "Cancel download", lambda: self.app.cancel(j))
                elif j.state == "done":
                    self.button(G_PLAY, "Play", lambda: self.app.open_file(j.paths[0]), primary=True)
                    self.button(G_FOLDER, "Show in folder", lambda: self.app.show_file(j.paths[0]))
                    self.button(G_CLOSE, "Remove from list", lambda: self.app.remove(j))
                else:
                    self.button(G_RETRY, "Try again", lambda: self.app.retry(j), primary=True)
                    self.button(G_CLOSE, "Remove from list", lambda: self.app.remove(j))
            self._shown_state = j.state


class SitePicker:
    """Modal list of the sites found in a cookies.txt; only the ticked ones are kept."""

    def __init__(self, app, sites, preselected):
        f = app.fonts
        self.result = None
        self.top = top = tk.Toplevel(app.root, bg=BG)
        top.title("Which logins to keep?")
        top.transient(app.root)
        top.resizable(False, True)
        set_icon(top)
        dark_title_bar(top)
        frame = tk.Frame(top, bg=BG, padx=20, pady=18)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text=f"This file has logins for {len(sites)} site{'s' * (len(sites) != 1)}",
                 font=f.title, bg=BG, fg=TEXT).pack(anchor="w")
        tk.Label(frame, text="Select only the ones you download videos from. The rest are thrown away.",
                 font=f.body, bg=BG, fg=MUTED).pack(anchor="w", pady=(4, 0))
        box = tk.Frame(frame, bg=SURFACE, highlightthickness=1, highlightbackground=LINE)
        box.pack(fill="both", expand=True, pady=14)
        self.lb = tk.Listbox(box, selectmode="multiple", height=min(14, max(4, len(sites))), width=44,
                             activestyle="none", exportselection=False, font=f.mono, bg=SURFACE, fg=TEXT,
                             selectbackground=LIME, selectforeground=ON_LIME, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.lb.yview, style="Slim.Vertical.TScrollbar")
        self.lb.configure(yscrollcommand=sb.set)
        self.lb.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        sb.pack(side="left", fill="y")
        self.sites = sites
        for i, s in enumerate(sites):
            self.lb.insert("end", s)
            if s in preselected:
                self.lb.selection_set(i)
        btns = tk.Frame(frame, bg=BG)
        btns.pack(fill="x")
        Btn(btns, "Keep selected", self.ok, f, kind="primary").pack(side="right")
        Btn(btns, "Cancel", top.destroy, f).pack(side="right", padx=(0, 8))
        Btn(btns, "Select none", lambda: self.lb.selection_clear(0, "end"), f).pack(side="left")
        top.grab_set()
        top.wait_window()

    def ok(self):
        picked = [self.sites[i] for i in self.lb.curselection()]
        if not picked:
            messagebox.showinfo(APP, "Select at least one site.", parent=self.top)
            return
        self.result = picked
        self.top.destroy()


class App:
    def __init__(self, root):
        self.root = root
        self.jobs = []
        self.settings = self.load_settings()
        self.fonts = f = Fonts(root)
        self.scale = root.winfo_fpixels("1i") / 96
        root.title(APP)
        root.configure(bg=BG)
        root.geometry(self.settings.get("geometry", "1060x720"))
        root.minsize(780, 540)
        set_icon(root)
        dark_title_bar(root)
        self.setup_ttk()

        outer = tk.Frame(root, bg=BG, padx=22, pady=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)

        # --- header: logo, name, live counts, overflow menu ---
        head = tk.Frame(outer, bg=BG)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        self._logo = None
        if Image:
            px = round(26 * self.scale)
            try:
                self._logo = ImageTk.PhotoImage(Image.open(os.path.join(RES, "assets", "icon.png")).resize(
                    (px, px), Image.LANCZOS))
                tk.Label(head, image=self._logo, bg=BG).pack(side="left", padx=(0, 10))
            except OSError:
                pass
        tk.Label(head, text=APP, font=f.brand, bg=BG, fg=TEXT).pack(side="left")
        tk.Label(head, text=f"v{VERSION}", font=f.mono_small, bg=BG, fg=FAINT).pack(side="left", padx=(8, 0), pady=(5, 0))
        more = Btn(head, G_MORE, self.open_more_menu, f, kind="icon", tip="More")
        more.pack(side="right")
        self.more_btn = more
        self.update_btn = None  # "Update to vX" appears here when a newer release exists
        self.summary = tk.Label(head, text="", font=f.mono, bg=BG, fg=MUTED)
        self.summary.pack(side="right", padx=(0, 14))

        # --- link dock ---
        self.dock = dock = tk.Frame(outer, bg=SURFACE, highlightthickness=1, highlightbackground=LINE)
        dock.grid(row=1, column=0, sticky="ew")
        dock.columnconfigure(1, weight=1)
        tk.Label(dock, text=G_LINK, font=f.icon, bg=SURFACE, fg=FAINT).grid(row=0, column=0, sticky="n",
                                                                           padx=(14, 4), pady=(15, 0))
        self.text = tk.Text(dock, height=3, wrap="none", font=f.body, undo=True, relief="flat", bd=0,
                            bg=SURFACE, fg=TEXT, insertbackground=LIME, selectbackground=LIME,
                            selectforeground=ON_LIME, highlightthickness=0, padx=8, pady=12)
        self.text.grid(row=0, column=1, sticky="nsew")
        self.placeholder = tk.Label(dock, text="Paste video links here, one per line", font=f.body,
                                    bg=SURFACE, fg=FAINT, cursor="xterm", padx=0, pady=0, bd=0)
        self.update_placeholder()
        self.placeholder.bind("<Button-1>", lambda e: self.text.focus_set())
        actions = tk.Frame(dock, bg=SURFACE)
        actions.grid(row=0, column=2, sticky="e", padx=12, pady=12)
        Btn(actions, G_PASTE, self.paste_and_add, f, kind="icon", bg=SURFACE,
            tip="Paste from clipboard and download").pack(side="left", padx=(0, 8))
        Btn(actions, "Download", self.add_from_text, f, kind="primary").pack(side="left")
        self.text.bind("<Control-Return>", lambda e: (self.add_from_text(), "break")[1])
        self.text.bind("<Return>", self.on_enter)
        self.text.bind("<FocusIn>", lambda e: dock.configure(highlightbackground="#59642f"))
        self.text.bind("<FocusOut>", lambda e: dock.configure(highlightbackground=LINE))
        for ev in ("<KeyRelease>", "<<Paste>>", "<<Modified>>"):
            self.text.bind(ev, lambda e: self.root.after_idle(self.update_placeholder), add="+")
        for seq in ("<Control-v>", "<Control-V>"):
            self.text.bind(seq, self.on_global_paste)  # runs before the Text class's own paste
            root.bind_all(seq, self.on_global_paste, add="+")

        # --- option chips ---
        opts = tk.Frame(outer, bg=BG)
        opts.grid(row=2, column=0, sticky="ew", pady=(10, 18))
        self.quality_var = tk.StringVar(value=self.settings.get("quality", "Best available"))
        self.parallel_var = tk.IntVar(value=self.settings.get("parallel", 3))
        self.out_var = tk.StringVar(value=self.settings.get("out_dir", vdl_core.DEFAULT_OUT))
        vdl_cookies.sweep_temp()  # wipe temp cookie files a crash may have left behind
        self.login = vdl_cookies.status()
        choice = self.settings.get("cookies", "None")
        if choice not in ["None", SAVED_LOGIN] + vdl_core.BROWSERS or (choice == SAVED_LOGIN and not self.has_login()):
            choice = "None"
        self.cookies_var = tk.StringVar(value=choice)
        self._last_cookie_choice = choice

        self.quality_chip = Chip(opts, G_SCREEN, f, self.quality_menu, tip="Quality")
        self.parallel_chip = Chip(opts, G_STACK, f, self.parallel_menu, tip="How many download at the same time")
        self.login_chip = Chip(opts, G_LOCK, f, self.login_menu, tip="Login for private or members-only videos")
        self.folder_chip = Chip(opts, G_FOLDER, f, self.folder_menu, tip="Where videos are saved")
        for chip in (self.quality_chip, self.parallel_chip, self.login_chip, self.folder_chip):
            chip.pack(side="left", padx=(0, 8))
        tk.Label(opts, text="Ctrl+V anywhere to paste & download", font=f.mono_small, bg=BG,
                 fg=FAINT).pack(side="right")
        self.update_chips()
        if self.login and self.login.get("expired"):
            root.after(600, lambda: messagebox.showinfo(
                APP, f"Your saved login was older than {vdl_cookies.MAX_AGE_DAYS} days, so it was deleted "
                     "for safety.\n\nImport a fresh cookies.txt when you need it again."))

        # --- queue header ---
        qh = tk.Frame(outer, bg=BG)
        qh.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        self.queue_label = tk.Label(qh, text="QUEUE", font=f.mono_small, bg=BG, fg=FAINT)
        self.queue_label.pack(side="left")
        Btn(qh, "Cancel all", self.cancel_all, f).pack(side="right")
        Btn(qh, "Clear finished", self.clear_finished, f).pack(side="right", padx=(0, 4))

        # --- download list ---
        box = tk.Frame(outer, bg=BG)
        box.grid(row=4, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(box, highlightthickness=0, bd=0, bg=BG)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(box, orient="vertical", command=self.canvas.yview, style="Slim.Vertical.TScrollbar")
        sb.grid(row=0, column=1, sticky="ns", padx=(6, 0))

        def on_scroll(first, last):  # only show the scrollbar when there's something to scroll
            sb.set(first, last)
            (sb.grid_remove if float(first) <= 0 and float(last) >= 1 else sb.grid)()

        self.canvas.configure(yscrollcommand=on_scroll)
        self.list_frame = tk.Frame(self.canvas, bg=BG)
        self.list_frame.columnconfigure(0, weight=1)
        self._win = self.canvas.create_window(0, 0, window=self.list_frame, anchor="nw")
        self.list_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self.on_canvas_resize)
        self.canvas.bind_all("<MouseWheel>", self.on_wheel)

        self.empty = tk.Frame(self.list_frame, bg=BG)
        tk.Label(self.empty, text=G_DOWN, font=f.icon_big, bg=BG, fg=LINE).pack()
        tk.Label(self.empty, text="Nothing in the queue", font=f.empty, bg=BG, fg=TEXT).pack(pady=(14, 4))
        tk.Label(self.empty, text="Copy a video link, then press Ctrl+V anywhere in this window.",
                 font=f.body, bg=BG, fg=MUTED).pack()
        tk.Label(self.empty, text="YouTube · X · Reddit · TikTok · Vimeo · Twitch · 1800+ more",
                 font=f.mono_small, bg=BG, fg=FAINT).pack(pady=(12, 0))
        self.empty.grid(row=0, column=0, pady=90)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Destroy>", self.on_destroy)
        self.text.focus_set()
        self._tick_id = self._anim_id = None
        self.tick()
        self.animate()
        threading.Thread(target=self.check_app_update, daemon=True).start()

    def setup_ttk(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.layout("Slim.Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        style.configure("Slim.Vertical.TScrollbar", troughcolor=BG, background=RAISED, bordercolor=BG,
                        lightcolor=RAISED, darkcolor=RAISED, gripcount=0, width=round(8 * self.scale))
        style.map("Slim.Vertical.TScrollbar", background=[("active", LINE)])

    # ---------- option chips ----------
    def update_chips(self):
        self.quality_chip.set(self.quality_var.get())
        self.parallel_chip.set(f"{self.parallel()} at once")
        c = self.cookies_var.get()
        if c == SAVED_LOGIN and self.has_login():
            sites = self.login["sites"]
            more = f" +{len(sites) - 1}" if len(sites) > 1 else ""
            self.login_chip.set(f"{shorten(sites[0], 18)}{more} · {self.login['days_left']}d")
        elif c == "None":
            self.login_chip.set("No login")
        else:
            self.login_chip.set(f"From {c.title()}")
        out = self.out_dir()
        home = os.path.expanduser("~")
        self.folder_chip.set(shorten("~" + out[len(home):] if out.lower().startswith(home.lower()) else out, 34))

    def quality_menu(self, menu):
        for q in vdl_core.QUALITIES:
            menu.add_radiobutton(label=q, variable=self.quality_var, value=q, command=self.on_option)

    def parallel_menu(self, menu):
        for n in range(1, 11):
            menu.add_radiobutton(label=f"{n} at once", variable=self.parallel_var, value=n, command=self.on_option)

    def login_menu(self, menu):
        menu.add_radiobutton(label="No login", variable=self.cookies_var, value="None", command=self.on_cookie_choice)
        label = SAVED_LOGIN if self.has_login() else "Import cookies.txt…"
        menu.add_radiobutton(label=label, variable=self.cookies_var, value=SAVED_LOGIN, command=self.on_cookie_choice)
        menu.add_separator()
        for b in vdl_core.BROWSERS:
            menu.add_radiobutton(label=f"Read from {b.title()}", variable=self.cookies_var, value=b,
                                 command=self.on_cookie_choice)
        if self.has_login():
            menu.add_separator()
            menu.add_command(label="Forget saved login", command=self.forget_login)

    def folder_menu(self, menu):
        menu.add_command(label="Change folder…", command=self.choose_dir)
        menu.add_command(label="Open folder", command=lambda: self.open_file(self.out_dir()))

    def open_more_menu(self):
        menu = style_menu(tk.Menu(self.root), self.fonts)
        menu.add_command(label="Check for app updates", command=lambda: threading.Thread(
            target=self.check_app_update, args=(True,), daemon=True).start())
        menu.add_command(label="Update download engine", command=self.update_ytdlp)
        menu.add_command(label="Open download folder", command=lambda: self.open_file(self.out_dir()))
        menu.add_separator()
        menu.add_command(label="About", command=self.about)
        b = self.more_btn
        menu.tk_popup(b.winfo_rootx() + b.winfo_width() - round(200 * self.scale), b.winfo_rooty() + b.winfo_height() + 4)

    def check_app_update(self, manual=False):
        """Ask GitHub for the latest release; offer it in the header if it's newer than this build."""
        def ver(tag):
            return tuple(int(n) for n in re.findall(r"\d+", tag)[:3])
        try:
            req = urllib.request.Request(RELEASES_API, headers={"User-Agent": APP})
            with urllib.request.urlopen(req, timeout=15) as r:
                rel = json.load(r)
            newer = ver(rel["tag_name"]) > ver(VERSION)
            self.root.after(0, lambda: self.show_app_update(rel["tag_name"], rel["html_url"]) if newer else
                            (manual and messagebox.showinfo(APP, f"You have the latest version ({VERSION}).")))
        except Exception as e:
            if manual:
                err = vdl_core.clean_error(e)
                self.root.after(0, lambda: messagebox.showerror(APP, "Couldn't check for updates:\n" + err))

    def show_app_update(self, tag, url):
        if self.update_btn:
            return
        import webbrowser
        self.update_btn = Btn(self.summary.master, f"Update to {tag}", lambda: webbrowser.open(url), self.fonts,
                              kind="primary", tip="A new version is out: opens the download page")
        self.update_btn.configure(padx=12, pady=2, font=self.fonts.small + ("bold",))
        self.update_btn.pack(side="right", padx=(0, 12), before=self.summary)

    def about(self):
        try:
            import yt_dlp.version
            engine = yt_dlp.version.__version__
        except Exception:
            engine = "not found"
        messagebox.showinfo(APP, f"{APP} {VERSION}\nDownload engine: yt-dlp {engine}\n\n"
                                 "github.com/Orbiiez/video-downloader")

    def on_option(self):
        self.update_chips()
        self.schedule()
        self.save_settings()

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
    def update_placeholder(self):
        if self.text.get("1.0", "end-1c"):
            self.placeholder.place_forget()
        else:
            self.placeholder.place(in_=self.text, x=1, y=0)  # relative to inside the text's padding

    def on_enter(self, event):
        # Enter on a single-line box downloads; with several lines it just adds a new line.
        if "\n" not in self.text.get("1.0", "end-1c").strip():
            self.add_from_text()
            return "break"

    def on_global_paste(self, event):
        if event.widget is self.text and self.text.get("1.0", "end-1c").strip():
            return None  # already typing a list in the box: normal paste
        self.paste_and_add()
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
        self.update_placeholder()
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
        job.row.grid(row=len(self.jobs), column=0, sticky="ew", pady=(0, 8))
        job.row.status.configure(wraplength=max(300, self.canvas.winfo_width() - round(320 * self.scale)))
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
        self._last_cookie_choice = c = self.cookies_var.get()
        if c in ("chrome", "edge", "brave", "opera", "vivaldi", "chromium"):
            self.flash("Chrome-based browsers often block this; a saved login is more reliable.")
        self.update_chips()
        self.save_settings()

    def import_login(self):
        """cookies.txt -> pick sites -> encrypted store -> offer to wipe the plain-text original."""
        if not messagebox.askokcancel(APP, (
                "Choose a cookies.txt exported from your browser (e.g. with the 'Get cookies.txt LOCALLY' "
                "extension, while logged in to the site).\n\n"
                "Keep it safe: anyone with that file can log in as you. The app will:\n"
                "  • keep only the sites you tick\n"
                "  • store them encrypted so only your Windows account can read them\n"
                f"  • forget them automatically after {vdl_cookies.MAX_AGE_DAYS} days\n"
                "  • offer to wipe the original file")):
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
        keep = SitePicker(self, sites, vdl_cookies.suggested_sites(sites)).result
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

    def worker(self, job, out_dir, quality, cookies_browser, use_saved_login):
        try:
            job.paths = vdl_core.download(job.url, out_dir, quality, cookies_browser, progress=job.on_progress,
                                          cancel=job.cancel, log=job.on_log, use_saved_login=use_saved_login)
            job.state, job.fraction = "done", 1.0
            names = ", ".join(os.path.basename(p) for p in job.paths)
            size = self.size(job.paths)
            job.detail = (f"{fmt_bytes(size)}  ·  " if size else "") + f"saved as {names}"
            if job.title == job.url:
                job.title = os.path.splitext(os.path.basename(job.paths[0]))[0]
        except vdl_core.Cancelled:
            job.state, job.detail = "cancelled", "Cancelled"
        except Exception as e:
            job.state, job.error = "failed", vdl_core.clean_error(e)
            job.detail = job.error
            job.log.append("Failed: " + job.error)
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
            key = "active" if j.state in ACTIVE else j.state
            counts[key] = counts.get(key, 0) + 1
        order = [("active", "↓"), ("queued", "…"), ("done", "✓"), ("failed", "✕")]
        self.summary.configure(text=self._flash or "   ".join(f"{sym} {counts[k]} {k}" for k, sym in order
                                                              if counts.get(k)),
                               fg=LIME if self._flash else MUTED)
        self.queue_label.configure(text=f"QUEUE · {len(self.jobs)}" if self.jobs else "QUEUE")
        self._tick_id = self.root.after(200, self.tick)

    def animate(self):
        for j in self.jobs:
            if j.row and j.row.bar.moving:
                j.row.bar.step()
        self._anim_id = self.root.after(30, self.animate)

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
        job.state, job.detail, job.fraction, job.error, job.log, job._files = "queued", "Queued", None, None, [], []
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
            self.update_chips()
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
            j.row.status.configure(wraplength=max(300, e.width - round(320 * self.scale)))

    def on_wheel(self, e):
        if str(e.widget).startswith(str(self.canvas)) and self.list_frame.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-e.delta / 120), "units")

    def on_destroy(self, e):
        if e.widget is self.root:
            for timer in (self._tick_id, self._anim_id):
                if timer:
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
