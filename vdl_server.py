"""The app's back end: download queue, settings, history, and the local web server the window talks to.

The window (and, when switched on, a phone on the same Wi-Fi) loads the page in ui/ from this server and
drives it through a small JSON API. Live progress goes out as Server-Sent Events.

Security: the desktop server only listens on 127.0.0.1 and every API call needs a random token. The phone
link is a second listener on the local network with its own token and fewer rights: it can add links, watch
the queue and save finished files, but not change settings, read logins or open files on this PC.
"""
import collections
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import vdl_cookies
import vdl_core
import vdl_tools
from version import VERSION

APP = "Video Downloader"
RELEASES_API = "https://api.github.com/repos/Orbiiez/video-downloader/releases/latest"
SETTINGS = os.path.join(vdl_cookies.APP_DIR, "settings.json")
HISTORY = os.path.join(vdl_cookies.APP_DIR, "history.json")
INSTANCE = os.path.join(vdl_cookies.APP_DIR, "instance.json")
RES = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
UI_DIR = os.path.join(RES, "ui")
URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"']+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|tv|be|app|social|io|me|ly|gg|to|co|de|fr|jp|ru|us|uk|ca|au)/[^\s<>\"']*")
ACTIVE = ("starting", "downloading", "processing")
LIVE = ACTIVE + ("queued", "picking")
HISTORY_MAX = 500

APP_DEFAULTS = {
    "parallel": 3,
    "login": "none",            # none | saved | a browser name
    "theme": "system",          # system | dark | light
    "clipboard_watch": False,   # offer links you copy anywhere
    "keep_history": True,
    "onboarded": False,
}
LANGUAGES = [("", "Off"), ("en", "English"), ("es", "Spanish"), ("pt", "Portuguese"), ("fr", "French"),
             ("de", "German"), ("it", "Italian"), ("nl", "Dutch"), ("pl", "Polish"), ("ru", "Russian"),
             ("uk", "Ukrainian"), ("tr", "Turkish"), ("ar", "Arabic"), ("hi", "Hindi"), ("id", "Indonesian"),
             ("vi", "Vietnamese"), ("th", "Thai"), ("ja", "Japanese"), ("ko", "Korean"), ("zh", "Chinese")]


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".new"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def load_settings():
    raw = _read_json(SETTINGS, {})
    s = {**{k: v for k, v in vdl_core.DEFAULTS.items() if k != "clip"}, **APP_DEFAULTS}
    # settings from 1.x: quality menu, "cookies" choice, window geometry
    if raw.get("quality") in vdl_core.LEGACY_QUALITY:
        s.update(vdl_core.LEGACY_QUALITY[raw.pop("quality")])
    cookies = raw.pop("cookies", None)
    if cookies and "login" not in raw:
        raw["login"] = {"None": "none", "Saved login": "saved"}.get(cookies, cookies)
    raw.pop("geometry", None)
    for k, v in raw.items():
        if k in s or k == "window":
            s[k] = v
    s.update({k: v for k, v in vdl_core.normalize_opts(s).items() if k in s and k != "clip"})
    try:
        s["parallel"] = max(1, min(10, int(s["parallel"])))
    except (TypeError, ValueError):
        s["parallel"] = 3
    if s["login"] not in ["none", "saved"] + vdl_core.BROWSERS:
        s["login"] = "none"
    return s


def fmt_bytes(n):
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# ---------- desktop helpers ----------
def open_path(path):
    if sys.platform == "win32":
        os.startfile(path)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


def show_in_folder(path):
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])


def clipboard_text():
    if sys.platform != "win32":
        for cmd in (["pbpaste"], ["xclip", "-o", "-selection", "clipboard"], ["wl-paste"]):
            try:
                return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
            except Exception:
                continue
        return ""
    import ctypes
    from ctypes import wintypes
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    u32.GetClipboardData.restype = wintypes.HANDLE
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    for _ in range(5):
        if u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return ""
    try:
        h = u32.GetClipboardData(13)  # CF_UNICODETEXT
        if not h:
            return ""
        p = k32.GlobalLock(h)
        try:
            return ctypes.wstring_at(p) if p else ""
        finally:
            k32.GlobalUnlock(h)
    finally:
        u32.CloseClipboard()


def clipboard_seq():
    if sys.platform == "win32":
        import ctypes
        return ctypes.windll.user32.GetClipboardSequenceNumber()
    return None


def copy_text(text):
    if sys.platform == "win32":
        subprocess.run(["clip.exe"], input=text.encode("utf-16-le"), creationflags=vdl_core.NO_WINDOW)
    elif sys.platform == "darwin":
        subprocess.run(["pbcopy"], input=text.encode())
    else:
        subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode())


def copy_files(paths):
    """Put files on the clipboard, ready to paste into Explorer, Discord, a chat..."""
    if sys.platform != "win32":
        raise RuntimeError("copying files is only supported on Windows")
    lit = ",".join("'" + p.replace("'", "''") + "'" for p in paths)
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", f"Set-Clipboard -LiteralPath {lit}"],
                       capture_output=True, text=True, creationflags=vdl_core.NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError("couldn't copy the file")


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; this just picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def extract_urls(text):
    out = []
    for m in URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;:!?)]}'\"")
        u = vdl_core.normalize_url(u)
        if u not in out:
            out.append(u)
    return out


# ---------- jobs ----------
class Job:
    _ids = 0
    _lock = threading.Lock()

    def __init__(self, kind, url=None, opts=None, tool=None, path=None, params=None, parent=None, title=None,
                 thumb=None, duration=None):
        with Job._lock:
            Job._ids += 1
            self.id = f"{int(time.time() * 1000) % 10**8:08d}{Job._ids}"
        self.kind, self.url, self.opts = kind, url, opts or {}
        self.tool, self.path, self.params, self.parent = tool, path, params or {}, parent
        self.title = title or (os.path.basename(path) if path else url)
        self.site = vdl_core.site_of(url) if url else "tool"
        self.thumb, self.thumb_headers, self.duration = thumb, None, duration
        self.media = "file" if kind == "tool" else ("audio" if self.opts.get("mode") == "audio" else "video")
        self.count = None
        self.reset()
        self.created = time.time()

    def reset(self):
        self.state, self.step, self.error = "queued", None, None
        self.fraction = self.downloaded = self.total = self.speed = self.eta = None
        self.item = self.items = None
        self.size_hint = None
        self.paths, self.size, self.spawned = [], None, 0
        self.log = collections.deque(maxlen=300)
        self.picker, self.picked = None, None
        self.pick_event = threading.Event()
        self.cancel = threading.Event()
        self.version = time.monotonic()

    def touch(self):
        self.version = time.monotonic()

    def public(self, remote=False):
        d = {k: getattr(self, k) for k in (
            "id", "kind", "url", "title", "site", "duration", "state", "step", "error", "fraction", "downloaded",
            "total", "speed", "eta", "item", "items", "size", "size_hint", "media", "count", "spawned", "created",
            "tool", "parent")}
        d["thumb"] = bool(self.thumb)
        d["files"] = [{"name": os.path.basename(p), "ext": os.path.splitext(p)[1].lstrip(".").lower()} for p in self.paths]
        d["mode"] = self.opts.get("mode", "auto")
        d["clip"] = self.opts.get("clip")
        if not remote:
            d["paths"] = self.paths
        if self.picker is not None and self.state == "picking":
            d["picker"] = [{k: it.get(k) for k in ("kind", "title", "duration", "width", "height")}
                           | {"thumb": bool(it.get("thumb"))} for it in self.picker]
        return d


class Manager:
    def __init__(self):
        os.makedirs(vdl_cookies.APP_DIR, exist_ok=True)
        self.settings = load_settings()
        self.jobs = collections.OrderedDict()
        self.lock = threading.RLock()
        self.listeners = []          # (queue, remote)
        self.removed = []
        self.history = _read_json(HISTORY, []) if self.settings["keep_history"] else []
        self.hooks = {}              # set by the launcher: dialog, focus, attention, quit
        self.update_info = None
        self.phone = None
        self._thumbs = collections.OrderedDict()
        self._sent = {}
        self._history_dirty = False
        vdl_cookies.sweep_temp()
        self.login_status = vdl_cookies.status()
        if self.login_status and self.login_status.get("expired") and self.settings["login"] == "saved":
            self.settings["login"] = "none"
        threading.Thread(target=self._broadcaster, daemon=True).start()
        threading.Thread(target=self._clipboard_watch, daemon=True).start()
        threading.Thread(target=vdl_core.warm_up, daemon=True).start()
        threading.Thread(target=self.check_app_update, daemon=True).start()
        threading.Thread(target=vdl_core.sweep_work, args=(self.settings["out_dir"],), daemon=True).start()

    # --- events ---
    def subscribe(self, remote):
        q = queue.Queue(maxsize=500)
        with self.lock:
            self.listeners.append((q, remote))
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.listeners = [(x, r) for x, r in self.listeners if x is not q]

    def clients(self):
        return len(self.listeners)

    def send(self, event, local_only=False):
        with self.lock:
            targets = list(self.listeners)
        for q, remote in targets:
            if local_only and remote:
                continue
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def _broadcaster(self):
        """Every 150 ms, send the jobs that changed (so a fast download doesn't flood the page)."""
        while True:
            time.sleep(0.15)
            with self.lock:
                changed = [j for j in self.jobs.values() if self._sent.get(j.id) != j.version]
                for j in changed:
                    self._sent[j.id] = j.version
                removed, self.removed = self.removed, []
                targets = list(self.listeners)
            if changed or removed:
                for q, remote in targets:
                    ev = {"type": "jobs", "jobs": [j.public(remote) for j in changed], "removed": removed}
                    try:
                        q.put_nowait(ev)
                    except queue.Full:
                        pass
            if self._history_dirty:
                self._history_dirty = False
                _write_json(HISTORY, self.history[:HISTORY_MAX])

    def state(self, remote=False):
        with self.lock:
            jobs = [j.public(remote) for j in self.jobs.values()]
        st = {"type": "state", "role": "remote" if remote else "local", "version": VERSION, "jobs": jobs,
              "update": self.update_info}
        if not remote:
            st.update(settings=self.settings, login=self.login_info(), engines=vdl_core.engine_versions(),
                      phone=self.phone_info(), choices=self.choices(), platform=sys.platform,
                      history_count=len(self.history))
        else:
            st["settings"] = {k: self.settings[k] for k in ("mode", "theme")}
        return st

    def choices(self):
        return {"languages": LANGUAGES, "browsers": vdl_core.BROWSERS, "filename_styles": vdl_core.FILENAME_STYLES,
                "tools": vdl_tools.TOOLS, **{k: list(v) for k, v in vdl_core.CHOICES.items()}}

    def login_info(self):
        st = self.login_status
        if not st or st.get("expired"):
            return {"saved": False, "expired": bool(st and st.get("expired"))}
        return {"saved": True, "sites": st["sites"], "days_left": st["days_left"]}

    # --- settings ---
    def update_settings(self, patch):
        allowed = set(vdl_core.DEFAULTS) - {"clip"} | set(APP_DEFAULTS) | {"window"}
        with self.lock:
            for k, v in (patch or {}).items():
                if k in allowed:
                    self.settings[k] = v
            norm = vdl_core.normalize_opts(self.settings)
            for k in vdl_core.DEFAULTS:
                if k != "clip":
                    self.settings[k] = norm[k]
            try:
                self.settings["parallel"] = max(1, min(10, int(self.settings["parallel"])))
            except (TypeError, ValueError):
                self.settings["parallel"] = 3
            if self.settings["login"] not in ["none", "saved"] + vdl_core.BROWSERS:
                self.settings["login"] = "none"
            if self.settings["login"] == "saved" and not self.login_info()["saved"]:
                self.settings["login"] = "none"
            if not self.settings["keep_history"] and self.history:
                self.history = []
                self._history_dirty = True
            _write_json(SETTINGS, self.settings)
        self.send({"type": "settings", "settings": self.settings}, local_only=True)
        self.schedule()

    def engine_opts(self, overrides=None):
        o = {k: self.settings[k] for k in vdl_core.DEFAULTS if k in self.settings}
        for k, v in (overrides or {}).items():
            if k in ("mode", "clip", "quality", "audio_format", "multi", "subtitles"):
                o[k] = v
        return vdl_core.normalize_opts(o)

    def login(self):
        c = self.settings["login"]
        if c == "saved":
            return vdl_core.Login(saved=True)
        if c in vdl_core.BROWSERS:
            return vdl_core.Login(browser=c)
        return vdl_core.Login()

    # --- queue ---
    def add_urls(self, urls, overrides=None, parent=None, meta=None):
        added, skipped = [], 0
        with self.lock:
            live = {j.url for j in self.jobs.values() if j.kind == "download" and j.state in LIVE}
            for i, u in enumerate(urls):
                u = vdl_core.normalize_url(u)
                if u in live:
                    skipped += 1
                    continue
                live.add(u)
                m = (meta or [{}] * len(urls))[i] if meta else {}
                job = Job("download", u, self.engine_opts(overrides), parent=parent, title=m.get("title"),
                          thumb=m.get("thumb"), duration=m.get("duration"))
                self.jobs[job.id] = job
                added.append(job.id)
        self.schedule()
        return {"added": len(added), "skipped": skipped, "ids": added}

    def add_tool(self, tool, files, params):
        ids = []
        with self.lock:
            for f in files:
                if not os.path.isfile(f):
                    continue
                job = Job("tool", tool=tool, path=f, params=params)
                job.title = os.path.basename(f)
                self.jobs[job.id] = job
                ids.append(job.id)
        self.schedule()
        return {"added": len(ids), "ids": ids}

    def schedule(self):
        with self.lock:
            running = sum(j.state in ACTIVE for j in self.jobs.values())
            for job in self.jobs.values():
                if running >= self.settings["parallel"]:
                    break
                if job.state == "queued":
                    job.state = "starting"
                    job.touch()
                    target = self._run_download if job.kind == "download" else self._run_tool
                    threading.Thread(target=target, args=(job,), daemon=True).start()
                    running += 1

    def _progress(self, job):
        def on(d):
            st = d.get("status")
            if st == "info":
                for k in ("title", "duration", "site", "count"):
                    if d.get(k):
                        setattr(job, k, d[k])
                if d.get("kind"):
                    job.media = d["kind"]
                if d.get("size"):
                    job.size_hint = d["size"]
                if d.get("thumbnail"):
                    job.thumb, job.thumb_headers = d["thumbnail"], d.get("thumb_headers")
            elif st == "downloading":
                if job.state != "picking":
                    job.state = "downloading"
                job.step = None
                job.downloaded, job.total, job.speed, job.eta = d.get("downloaded"), d.get("total"), d.get("speed"), d.get("eta")
                job.item, job.items = d.get("item"), d.get("items")
                if job.total and job.downloaded is not None:
                    job.fraction = min(1.0, job.downloaded / job.total)
                elif d.get("fragments") and d.get("fragment"):
                    job.fraction = min(1.0, d["fragment"] / d["fragments"])
                else:
                    job.fraction = None
            elif st == "processing":
                job.state = "processing" if job.state != "starting" or d.get("step") != "Looking up the link" else "starting"
                job.step = d.get("step")
                job.fraction = d.get("fraction")
            job.touch()
        return on

    def _log(self, job):
        return lambda m: job.log.append(str(m))

    def _pick(self, job):
        def pick(items):
            job.picker, job.picked = items, None
            job.pick_event.clear()
            prev = job.state
            job.state = "picking"
            job.touch()
            self.schedule()  # a job waiting for a choice doesn't hold up the queue
            self.send({"type": "attention", "reason": "pick", "id": job.id}, local_only=False)
            hook = self.hooks.get("attention")
            if hook:
                hook()
            while not job.pick_event.wait(0.3):
                if job.cancel.is_set():
                    raise vdl_core.Cancelled()
            job.state = prev if prev in ACTIVE else "downloading"
            job.touch()
            return job.picked or []
        return pick

    def _spawn(self, job):
        def spawn(entries):
            ov = {"mode": job.opts.get("mode"), "multi": "first"}
            if job.opts.get("clip"):
                ov["clip"] = job.opts["clip"]
            job.spawned = len(entries)
            self.add_urls([e["url"] for e in entries], ov, parent=job.id, meta=entries)
        return spawn

    def _run_download(self, job):
        try:
            paths = vdl_core.download(job.url, job.opts, login=self.login(), progress=self._progress(job),
                                      cancel=job.cancel, log=self._log(job), pick=self._pick(job),
                                      spawn=self._spawn(job), key=job.id)
            self._finished(job, paths)
        except vdl_core.Cancelled:
            job.state, job.step = "cancelled", None
        except Exception as e:
            job.state, job.error = "failed", vdl_core.clean_error(e)
            job.log.append("Failed: " + job.error)
        job.picker = None
        job.touch()
        self.schedule()
        self._maybe_all_done()

    def _run_tool(self, job):
        job.state = "processing"
        job.step = vdl_tools.TOOLS[job.tool]
        job.touch()

        def prog(f):
            job.fraction = f
            job.touch()
        try:
            out = vdl_tools.run(job.tool, job.path, job.params, prog, job.cancel)
            self._finished(job, [out])
        except vdl_core.Cancelled:
            job.state = "cancelled"
        except Exception as e:
            job.state, job.error = "failed", vdl_core.clean_error(e)
            job.log.append("Failed: " + job.error)
        job.touch()
        self.schedule()
        self._maybe_all_done()

    def _finished(self, job, paths):
        job.paths = [p for p in paths if os.path.exists(p)]
        job.size = sum(os.path.getsize(p) for p in job.paths) or None
        job.state, job.fraction, job.step = "done", 1.0, None
        if job.kind == "download" and job.title == job.url and job.paths:
            job.title = os.path.splitext(os.path.basename(job.paths[0]))[0]
        if job.paths and self.settings["keep_history"]:
            with self.lock:
                self.history.insert(0, {"id": job.id, "url": job.url, "title": job.title, "site": job.site,
                                        "media": job.media, "paths": job.paths, "size": job.size,
                                        "time": time.time(), "kind": job.kind, "tool": job.tool,
                                        "thumb": job.thumb if isinstance(job.thumb, str) and not job.thumb_headers else None})
                del self.history[HISTORY_MAX:]
                self._history_dirty = True

    def _maybe_all_done(self):
        with self.lock:
            busy = any(j.state in LIVE for j in self.jobs.values())
            any_done = any(j.state == "done" for j in self.jobs.values())
        if not busy and any_done:
            self.send({"type": "attention", "reason": "done"})
            hook = self.hooks.get("attention")
            if hook:
                hook()

    def job_action(self, jid, action, data, remote=False):
        with self.lock:
            job = self.jobs.get(jid)
        if not job:
            raise KeyError("no such download")
        if action == "cancel":
            if job.state == "queued":
                job.state = "cancelled"
            job.cancel.set()
            job.pick_event.set()
        elif action == "retry":
            if job.state in ("failed", "cancelled"):
                job.reset()
                self.schedule()
        elif action == "remove":
            if job.state in LIVE:
                job.cancel.set()
                job.pick_event.set()
            with self.lock:
                self.jobs.pop(jid, None)
                self._sent.pop(jid, None)
                self.removed.append(jid)
            if job.kind == "download" and job.state != "done":
                vdl_core.discard_work(job.opts.get("out_dir", vdl_core.DEFAULT_OUT), job.id)
        elif action == "pick":
            if job.state == "picking":
                job.picked = [int(i) for i in data.get("indices") or []]
                if not job.picked:
                    job.cancel.set()
                job.pick_event.set()
        elif action == "log":
            return {"log": list(job.log)[-200:], "url": job.url, "error": job.error}
        elif remote:
            raise PermissionError("not allowed from the phone")
        elif action == "open":
            if job.paths:
                open_path(job.paths[int(data.get("n") or 0)])
        elif action == "show":
            if job.paths:
                show_in_folder(job.paths[int(data.get("n") or 0)])
        elif action == "copy_file":
            copy_files(job.paths)
        elif action == "copy_link":
            copy_text(job.url or "")
        else:
            raise ValueError("unknown action")
        job.touch()
        return {"ok": True}

    def bulk(self, action):
        with self.lock:
            jobs = list(self.jobs.values())
        for j in jobs:
            if action == "cancel_all" and j.state in LIVE:
                self.job_action(j.id, "cancel", {})
            elif action == "clear_finished" and j.state in ("done", "failed", "cancelled"):
                self.job_action(j.id, "remove", {})
        return {"ok": True}

    # --- thumbnails ---
    def thumb(self, jid, index):
        with self.lock:
            job = self.jobs.get(jid)
        if not job:
            return None
        if index is None:
            url, headers = job.thumb, job.thumb_headers
        else:
            items = job.picker or []
            if not 0 <= index < len(items):
                return None
            url, headers = items[index].get("thumb"), items[index].get("headers")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return None
        key = url
        if key in self._thumbs:
            self._thumbs.move_to_end(key)
            return self._thumbs[key]
        req = urllib.request.Request(url, headers={"User-Agent": vdl_core.UA, **(headers or {})})
        try:
            opener = vdl_core._opener(self.settings.get("proxy")) or urllib.request.build_opener()
            with opener.open(req, timeout=15) as r:
                ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
                if not ctype.startswith("image/"):
                    return None
                data = r.read(12_000_000)
        except Exception:
            return None
        with self.lock:
            self._thumbs[key] = (ctype, data)
            while len(self._thumbs) > 80:
                self._thumbs.popitem(last=False)
        return ctype, data

    # --- history ---
    def history_list(self, q=""):
        q = (q or "").lower().strip()
        items = [h for h in self.history if not q or q in (h.get("title") or "").lower() or q in (h.get("url") or "").lower()]
        for h in items:
            h["exists"] = any(os.path.exists(p) for p in h.get("paths") or [])
        return items[:300]

    def history_action(self, action, hid=None):
        with self.lock:
            if action == "clear":
                self.history = []
            elif action == "remove":
                self.history = [h for h in self.history if h["id"] != hid]
            elif action in ("open", "show", "redownload"):
                h = next((h for h in self.history if h["id"] == hid), None)
                if not h:
                    raise KeyError("not in history")
                if action == "redownload":
                    if not h.get("url"):
                        raise ValueError("this was made with a tool, not downloaded")
                    return self.add_urls([h["url"]])
                path = next((p for p in h.get("paths") or [] if os.path.exists(p)), None)
                if not path:
                    raise FileNotFoundError("the file has been moved or deleted")
                (open_path if action == "open" else show_in_folder)(path)
            self._history_dirty = True
        return {"ok": True}

    # --- login ---
    def login_action(self, data):
        action = data.get("action")
        if action == "inspect":
            cookies = vdl_cookies.read_file(data["path"])
            sites = vdl_cookies.domains_in(cookies)
            return {"sites": sites, "preselected": vdl_cookies.suggested_sites(sites)}
        if action == "save":
            cookies = vdl_cookies.read_file(data["path"])
            saved = vdl_cookies.save_login(cookies, data.get("sites") or [])
            wiped = False
            if data.get("wipe"):
                vdl_cookies.wipe(data["path"])
                wiped = not os.path.exists(data["path"])
            self.login_status = vdl_cookies.status()
            self.update_settings({"login": "saved"})
            return {"saved": saved, "wiped": wiped, "login": self.login_info()}
        if action == "forget":
            vdl_cookies.forget()
            self.login_status = None
            if self.settings["login"] == "saved":
                self.update_settings({"login": "none"})
            return {"login": self.login_info()}
        raise ValueError("unknown login action")

    # --- clipboard ---
    def _clipboard_watch(self):
        last_seq, seen = clipboard_seq(), set()
        while True:
            time.sleep(0.8)
            if not self.settings.get("clipboard_watch") or not self.clients():
                last_seq = clipboard_seq()
                continue
            seq = clipboard_seq()
            if seq is None or seq == last_seq:
                continue
            last_seq = seq
            try:
                urls = [u for u in extract_urls(clipboard_text())[:20] if u not in seen]
            except Exception:
                continue
            if urls:
                seen.update(urls)
                self.send({"type": "clipboard", "urls": urls}, local_only=True)

    # --- updates ---
    def check_app_update(self, manual=False):
        def ver(tag):
            return tuple(int(n) for n in re.findall(r"\d+", tag)[:3])
        try:
            req = urllib.request.Request(RELEASES_API, headers={"User-Agent": APP})
            with urllib.request.urlopen(req, timeout=15) as r:
                rel = json.load(r)
            if ver(rel["tag_name"]) > ver(VERSION):
                self.update_info = {"tag": rel["tag_name"], "url": rel["html_url"]}
                self.send({"type": "update", "update": self.update_info})
            return {"latest": rel["tag_name"], "newer": bool(self.update_info), "url": rel["html_url"]}
        except Exception as e:
            if manual:
                raise RuntimeError("couldn't check for updates: " + vdl_core.clean_error(e))
            return None

    def update_engines(self):
        if not vdl_core.FROZEN:
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]", "gallery-dl"],
                               capture_output=True, text=True, creationflags=vdl_core.NO_WINDOW)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1:][0] if (r.stderr or r.stdout) else "pip failed")
            return {"message": "Updated. Restart the app to use the new engines."}
        os.makedirs(vdl_core.ENGINE_DIR, exist_ok=True)
        notes = []
        versions = vdl_core.engine_versions()
        req = urllib.request.Request("https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest", headers={"User-Agent": APP})
        with urllib.request.urlopen(req, timeout=30) as r:
            rel = json.load(r)
        if rel["tag_name"] != versions.get("yt-dlp"):
            asset = next(a for a in rel["assets"] if a["name"] == "yt-dlp")  # the zipimport build
            tmp = vdl_core.ENGINE_ZIP + ".new"
            urllib.request.urlretrieve(asset["browser_download_url"], tmp)
            os.replace(tmp, vdl_core.ENGINE_ZIP)
            notes.append(f"yt-dlp {rel['tag_name']}")
        req = urllib.request.Request("https://pypi.org/pypi/gallery-dl/json", headers={"User-Agent": APP})
        with urllib.request.urlopen(req, timeout=30) as r:
            pypi = json.load(r)
        latest = pypi["info"]["version"]
        if latest != versions.get("gallery-dl"):
            whl = next(u for u in pypi["urls"] if u["packagetype"] == "bdist_wheel")
            tmp = vdl_core.GALLERY_WHL + ".new"
            urllib.request.urlretrieve(whl["url"], tmp)
            os.replace(tmp, vdl_core.GALLERY_WHL)
            notes.append(f"gallery-dl {latest}")
        if not notes:
            return {"message": "Already up to date."}
        return {"message": f"Updated {' and '.join(notes)}. Restart the app to use them."}

    def proxy_test(self, proxy):
        proxy = (proxy or "").strip()
        ydl = vdl_core._engine("yt_dlp")
        params = {"quiet": True, "no_warnings": True}
        if proxy:
            params["proxy"] = proxy
        with ydl.YoutubeDL(params) as y:
            data = json.loads(y.urlopen("https://ipinfo.io/json").read().decode())
        return {"ip": data.get("ip"), "country": data.get("country"), "city": data.get("city"), "org": data.get("org")}

    # --- phone link ---
    def phone_info(self):
        if not self.phone:
            return {"on": False}
        return {"on": True, "url": self.phone["url"], "qr": self.phone["qr"], "clients": self.phone["server"].clients()}

    def set_phone(self, on):
        if on and not self.phone:
            token = secrets.token_urlsafe(18)
            srv = Server(self, ("0.0.0.0", 0), token, remote=True)
            ip = lan_ip()
            url = f"http://{ip}:{srv.port}/#t={token}"
            srv.allowed_hosts = {f"{ip}:{srv.port}"}
            srv.start()
            try:
                import segno
                qr = segno.make(url, error="m").png_data_uri(scale=6, border=2)
            except Exception:
                qr = None
            self.phone = {"server": srv, "url": url, "qr": qr}
        elif not on and self.phone:
            self.phone["server"].stop()
            self.phone = None
        return self.phone_info()


# ---------- HTTP ----------
class Server:
    def __init__(self, manager, addr, token, remote=False):
        self.manager, self.token, self.remote = manager, token, remote
        self.httpd = ThreadingHTTPServer(addr, _handler(self))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.streams = 0

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()
        return self

    def stop(self):
        threading.Thread(target=self.httpd.shutdown, daemon=True).start()
        self.httpd.server_close()

    def clients(self):
        return self.streams

    def url(self):
        return f"http://127.0.0.1:{self.port}/#t={self.token}"


STATIC = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/app.css": "app.css",
          "/icon.png": "../assets/icon.png", "/favicon.ico": "../assets/icon.ico", "/manifest.json": "manifest.json"}
CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def _handler(server):
    mgr = server.manager

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "VideoDownloader"

        def log_message(self, *a):
            pass

        # --- plumbing ---
        def _send(self, code, body=b"", ctype="application/json", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False, default=str).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _host_ok(self):
            return (self.headers.get("Host") or "") in server.allowed_hosts

        def _authed(self, qs):
            tok = self.headers.get("X-Token") or (qs.get("t") or [""])[0]
            return secrets.compare_digest(tok, server.token)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 2_000_000:
                raise ValueError("request too large")
            data = self.rfile.read(n) if n else b"{}"
            return json.loads(data or b"{}")

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(u.query)
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            if u.path in STATIC:
                return self._static(STATIC[u.path])
            if not u.path.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            if not self._authed(qs):
                return self._send(401, {"error": "unauthorized"})
            try:
                if u.path == "/api/events":
                    return self._events()
                if u.path == "/api/state":
                    return self._send(200, mgr.state(server.remote))
                if u.path == "/api/ping":
                    return self._send(200, {"ok": True, "version": VERSION, "clients": mgr.clients()})
                if u.path == "/api/thumb":
                    i = (qs.get("i") or [None])[0]
                    got = mgr.thumb((qs.get("id") or [""])[0], int(i) if i not in (None, "") else None)
                    if not got:
                        return self._send(404, {"error": "no thumbnail"})
                    return self._send(200, got[1], got[0], {"Cache-Control": "max-age=3600"})
                if u.path == "/api/file":
                    return self._file(qs)
                if server.remote:
                    return self._send(403, {"error": "not allowed from the phone"})
                if u.path == "/api/history":
                    return self._send(200, {"items": mgr.history_list((qs.get("q") or [""])[0])})
                if u.path == "/api/clipboard":
                    return self._send(200, {"text": clipboard_text()})
            except Exception as e:
                return self._send(500, {"error": vdl_core.clean_error(e)})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urllib.parse.urlsplit(self.path)
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            if not self._authed({}) or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._send(401, {"error": "unauthorized"})
            try:
                data = self._body()
                return self._send(200, self._route(u.path, data))
            except PermissionError as e:
                return self._send(403, {"error": str(e)})
            except KeyError as e:
                return self._send(404, {"error": str(e).strip("'\"")})
            except Exception as e:
                return self._send(400, {"error": vdl_core.clean_error(e)})

        def _route(self, path, d):
            remote = server.remote
            if path == "/api/add":
                urls = d.get("urls") or extract_urls(d.get("text") or "")
                urls = [vdl_core.normalize_url(x) for x in urls if isinstance(x, str) and x.strip()][:200]
                if not urls:
                    raise ValueError("that doesn't look like a link")
                ov = d.get("overrides") or {}
                return mgr.add_urls(urls, {k: ov[k] for k in ("mode", "clip") if k in ov} if remote else ov)
            if path == "/api/job":
                return mgr.job_action(d.get("id"), d.get("action"), d, remote)
            if path == "/api/jobs":
                if d.get("action") not in ("cancel_all", "clear_finished"):
                    raise ValueError("unknown action")
                return mgr.bulk(d["action"])
            if remote:
                raise PermissionError("not allowed from the phone")
            if path == "/api/settings":
                mgr.update_settings(d.get("patch") or {})
                return {"settings": mgr.settings}
            if path == "/api/dialog":
                hook = mgr.hooks.get("dialog")
                if not hook:
                    raise RuntimeError("no window to show a dialog in")
                return {"paths": hook(d.get("kind"), d.get("start") or mgr.settings["out_dir"]) or []}
            if path == "/api/login":
                return mgr.login_action(d)
            if path == "/api/tool":
                files = [f for f in d.get("files") or [] if isinstance(f, str)]
                if d.get("tool") not in vdl_tools.TOOLS or not files:
                    raise ValueError("choose a tool and at least one file")
                return mgr.add_tool(d["tool"], files, d.get("params") or {})
            if path == "/api/history":
                return mgr.history_action(d.get("action"), d.get("id"))
            if path == "/api/open":
                target = d.get("path") or mgr.settings["out_dir"]
                if d.get("folder"):
                    os.makedirs(target, exist_ok=True)
                open_path(target)
                return {"ok": True}
            if path == "/api/update":
                if d.get("what") == "app":
                    return mgr.check_app_update(manual=True)
                return mgr.update_engines()
            if path == "/api/proxy_test":
                return mgr.proxy_test(d.get("proxy"))
            if path == "/api/phone":
                return mgr.set_phone(bool(d.get("on")))
            if path == "/api/copy":
                copy_text(str(d.get("text") or ""))
                return {"ok": True}
            if path == "/api/focus":
                if mgr.hooks.get("focus"):
                    mgr.hooks["focus"]()
                return {"ok": True}
            if path == "/api/quit":
                if mgr.hooks.get("quit"):
                    threading.Thread(target=mgr.hooks["quit"], daemon=True).start()
                return {"ok": True}
            raise KeyError("not found")

        def _static(self, name):
            path = os.path.normpath(os.path.join(UI_DIR, name))
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                return self._send(404, {"error": "not found"})
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            extra = {"Content-Security-Policy": CSP, "X-Frame-Options": "DENY"} if name.endswith(".html") else {}
            return self._send(200, data, ctype, extra)

        def _file(self, qs):
            with mgr.lock:
                job = mgr.jobs.get((qs.get("id") or [""])[0])
            if not job or job.state != "done" or not job.paths:
                return self._send(404, {"error": "not ready"})
            try:
                path = job.paths[int((qs.get("n") or ["0"])[0])]
            except (ValueError, IndexError):
                return self._send(404, {"error": "no such file"})
            size = os.path.getsize(path)
            name = os.path.basename(path)
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
            self.end_headers()
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = mgr.subscribe(server.remote)
            server.streams += 1
            try:
                self._event(mgr.state(server.remote))
                while True:
                    try:
                        ev = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    self._event(ev)
            except (BrokenPipeError, ConnectionError, OSError):
                pass
            finally:
                server.streams -= 1
                mgr.unsubscribe(q)
                self.close_connection = True

        def _event(self, ev):
            self.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False, default=str).encode() + b"\n\n")
            self.wfile.flush()

    return H


def write_instance(server):
    _write_json(INSTANCE, {"port": server.port, "token": server.token, "pid": os.getpid()})


def find_running():
    """(port, token) of an already-running app that answers, else None."""
    info = _read_json(INSTANCE, None)
    if not info:
        return None
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/ping", headers={"X-Token": info["token"],
                                     "Host": f"127.0.0.1:{info['port']}"})
        with urllib.request.urlopen(req, timeout=2) as r:
            if json.load(r).get("ok"):
                return info["port"], info["token"]
    except Exception:
        pass
    return None


def call_running(port, token, path, data):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(data).encode(),
                                 headers={"X-Token": token, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def start():
    """Create the manager and the desktop server; returns (manager, server)."""
    mgr = Manager()
    srv = Server(mgr, ("127.0.0.1", 0), secrets.token_urlsafe(24)).start()
    write_instance(srv)
    return mgr, srv
