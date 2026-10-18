"""Shared download engine for video_dl.py (CLI) and Video Downloader.pyw (GUI).

download(url, out_dir, ...) tries, in order:
  1. a plain HTTP download, if the URL is a direct video file
  2. yt-dlp (YouTube, Twitter/X, Reddit, TikTok, ~1800 sites, HLS/DASH streams)
  3. scanning the page HTML for video links and feeding those to yt-dlp
"""
import glob
import html as htmlmod
import mimetypes
import os
import re
import shutil
import threading
import urllib.parse
import urllib.request

import vdl_cookies

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".flv", ".wmv", ".ts", ".3gp", ".ogv", ".mpg", ".mpeg"}
GENERIC_BINARY = {"", "application/octet-stream", "binary/octet-stream", "application/binary", "application/force-download"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
DEFAULT_OUT = os.path.join(os.path.expanduser("~"), "Downloads")

QUALITIES = {
    "Best available": "bv*+ba/b",
    "1080p": "bv*[height<=1080]+ba/b[height<=1080]/b",
    "720p": "bv*[height<=720]+ba/b[height<=720]/b",
    "480p": "bv*[height<=480]+ba/b[height<=480]/b",
    "Audio only (mp3)": "ba/b",
}
BROWSERS = ["chrome", "edge", "firefox", "brave", "opera", "vivaldi", "chromium"]

_name_lock = threading.Lock()
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Cancelled(Exception):
    pass


def clean_error(e):
    msg = _ANSI.sub("", str(e)).strip()
    msg = re.sub(r"^(ERROR:\s*)+", "", msg)
    msg = re.sub(r"^\[[\w:]+\]\s*[\w-]+:\s*", "", msg)  # "[vimeo] 12345: " prefix
    return msg.split("\n")[0] or e.__class__.__name__


def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # pip-bundled ffmpeg binary
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def safe_filename(name, fallback="video"):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com\d|lpt\d)(\..*)?", name):
        name = "_" + name
    base, ext = os.path.splitext(name)
    return (base[:150] or fallback) + ext


def reserve_path(path):
    """Atomically claim a free filename, so parallel downloads never write the same file."""
    base, ext = os.path.splitext(path)
    n = 0
    with _name_lock:
        while True:
            cand = path if n == 0 else f"{base} ({n}){ext}"
            if not os.path.exists(cand) and not os.path.exists(cand + ".part"):
                open(cand + ".part", "xb").close()
                return cand
            n += 1


def _fmt_ext(ctype):
    special = {"video/quicktime": ".mov", "video/x-matroska": ".mkv", "video/x-msvideo": ".avi",
               "video/x-flv": ".flv", "video/mp2t": ".ts", "video/x-ms-wmv": ".wmv"}
    return special.get(ctype) or mimetypes.guess_extension(ctype) or ".mp4"


def direct_download(url, out_dir, progress=None, cancel=None):
    """Stream a direct file link to disk. Returns [path], or None if the URL isn't a video file."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        name = os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(resp.url).path))
        cd = resp.headers.get("Content-Disposition", "")
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd, re.I)
        if m:
            name = urllib.parse.unquote(m.group(1))
        ext = os.path.splitext(name)[1].lower()
        if not (ctype.startswith("video/") or (ext in VIDEO_EXTS and ctype in GENERIC_BINARY)):
            return None
        if ext not in VIDEO_EXTS:
            name = (name or "video") + _fmt_ext(ctype)
        path = reserve_path(os.path.join(out_dir, safe_filename(name)))
        part = path + ".part"
        title = os.path.splitext(os.path.basename(path))[0]
        total = int(resp.headers.get("Content-Length") or 0) or None
        done = 0
        try:
            with open(part, "wb") as f:
                while chunk := resp.read(1 << 18):
                    if cancel and cancel.is_set():
                        raise Cancelled()
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress({"status": "downloading", "title": title, "downloaded": done, "total": total})
            if total and done < total:
                raise IOError(f"connection closed early ({done} of {total} bytes)")
            os.replace(part, path)
        except BaseException:
            _remove(part)
            raise
    return [path]


MEDIA_URL_RE = re.compile(
    r"""(?:https?:)?(?:\\?/){2}[^\s"'<>()]+?\.(?:mp4|webm|m4v|mov|mkv|m3u8|mpd)(?:\?[^\s"'<>()]*)?"""
    r"""|(?<=src=["'])[^"'<>]+?\.(?:mp4|webm|m4v|mov|mkv|m3u8|mpd)(?:\?[^"'<>]*)?(?=["'])""",
    re.I,
)
OG_VIDEO_RE = re.compile(
    r"""<meta[^>]+(?:property|name)=["'](?:og:video(?::secure_url|:url)?|twitter:player:stream)["'][^>]+content=["']([^"']+)""",
    re.I,
)


def scrape_media_urls(page_url):
    """Last resort for sites yt-dlp doesn't know: pull video URLs straight out of the page HTML."""
    req = urllib.request.Request(page_url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read(8 << 20).decode(resp.headers.get_content_charset() or "utf-8", "replace")
        base = resp.url
    urls = []
    for u in OG_VIDEO_RE.findall(html) + MEDIA_URL_RE.findall(html):
        u = htmlmod.unescape(u).replace("\\/", "/").replace("\\u0026", "&")
        u = urllib.parse.urljoin(base, u)
        if u.startswith(("http://", "https://")) and u not in urls:
            urls.append(u)
    # Prefer streams/manifests and plain files over thumbnails/previews.
    urls.sort(key=lambda u: (not re.search(r"\.(m3u8|mpd)", u, re.I), bool(re.search(r"preview|thumb|teaser", u, re.I))))
    return urls


class _Logger:
    """Collects yt-dlp output instead of letting it write to the console (there is none under pythonw)."""

    def __init__(self, log=None):
        self.log, self.last_error = log, None

    def debug(self, msg):
        if self.log and not msg.startswith("[debug]"):
            self.log(_ANSI.sub("", msg))

    info = debug

    def warning(self, msg):
        if self.log:
            self.log(_ANSI.sub("", "WARNING: " + msg))

    def error(self, msg):
        self.last_error = _ANSI.sub("", msg)
        if self.log:
            self.log(self.last_error)


def ytdlp_download(url, out_dir, quality="Best available", cookies_browser=None, referer=None,
                   progress=None, cancel=None, log=None, cookies_file=None, use_saved_login=False):
    import yt_dlp

    ffmpeg = find_ffmpeg()
    fmt = QUALITIES.get(quality, QUALITIES["Best available"])
    if not ffmpeg:  # can't merge separate video/audio streams, so take the best single file
        fmt = "ba/b" if quality.startswith("Audio") else "b[ext=mp4]/b"
    tmp_files = set()

    def hook(d):
        for key in ("tmpfilename", "filename"):
            if d.get(key):
                tmp_files.add(d[key] + ("" if key == "tmpfilename" else ".part"))
        if cancel and cancel.is_set():
            raise yt_dlp.utils.DownloadCancelled()
        if progress:
            info = d.get("info_dict") or {}
            progress({"status": d["status"], "title": info.get("title"), "file": d.get("filename"),
                      "downloaded": d.get("downloaded_bytes"),
                      "total": d.get("total_bytes") or d.get("total_bytes_estimate"),
                      "speed": d.get("speed"), "eta": d.get("eta"),
                      "fragment": d.get("fragment_index"), "fragments": d.get("fragment_count")})

    def pp_hook(d):
        if progress and d["status"] == "started":
            progress({"status": "processing", "step": d.get("postprocessor")})

    opts = {
        "outtmpl": os.path.join(out_dir, "%(title).150B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "format": fmt,
        "merge_output_format": "mp4",
        "windowsfilenames": True,
        "quiet": True, "no_warnings": False, "noprogress": True,
        "logger": _Logger(log),
        "progress_hooks": [hook],
        "postprocessor_hooks": [pp_hook],
        "retries": 5, "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        # YouTube needs a JS runtime; allow node as well as yt-dlp's default deno.
        "js_runtimes": {"deno": {}, "node": {}},
    }
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
        if quality.startswith("Audio"):
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}]
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    cookie_tmp = None
    if cookies_file or use_saved_login:
        # Only this site's cookies go into a private temp file, wiped when we're done. yt-dlp also
        # rewrites its cookie file on exit, so the user's original is never touched.
        if cookies_file and not os.path.isfile(cookies_file):
            raise RuntimeError(f"cookies file not found: {cookies_file}")
        cookie_tmp = vdl_cookies.make_temp(referer or url, cookies_file)
        if cookie_tmp:
            opts["cookiefile"] = cookie_tmp
        elif log:
            log("No saved login for this site; downloading without one.")
    if referer:
        opts["http_headers"] = {"Referer": referer}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except BaseException as e:
        # yt-dlp sometimes re-wraps our DownloadCancelled in a DownloadError, so go by the flag.
        if isinstance(e, yt_dlp.utils.DownloadCancelled) or (cancel and cancel.is_set()):
            for t in tmp_files:
                for f in [t, t + ".ytdl"] + glob.glob(glob.escape(t) + "-Frag*"):
                    _remove(f)
            raise Cancelled()
        raise
    finally:
        if cookie_tmp:
            vdl_cookies.wipe(cookie_tmp)
    # A page can hold several videos (a playlist); report every file actually written.
    entries = [e for e in (info.get("entries") or [info]) if e]
    paths = [os.path.normpath(d["filepath"]) for e in entries
             for d in e.get("requested_downloads") or [] if d.get("filepath")]
    if not paths:
        raise RuntimeError("no video found on the page")
    return paths


def download(url, out_dir=DEFAULT_OUT, quality="Best available", cookies_browser=None,
             progress=None, cancel=None, log=None, cookies_file=None, use_saved_login=False):
    """Download whatever video `url` points to. Returns a list of saved file paths.

    For login-only videos pass cookies_browser (read live from that browser), cookies_file
    (a cookies.txt export) or use_saved_login (the encrypted login imported in the app).
    Either way only the cookies of the site being downloaded are handed to yt-dlp.
    """
    log = log or (lambda m: None)
    url = url.strip().strip('"\'')
    if not re.match(r"(?i)^https?://", url):
        url = "https://" + url
    os.makedirs(out_dir, exist_ok=True)

    if not quality.startswith("Audio"):
        try:
            paths = direct_download(url, out_dir, progress, cancel)
            if paths:
                return paths
        except Cancelled:
            raise
        except Exception as e:
            log(f"Direct download failed ({clean_error(e)}); trying yt-dlp...")

    try:
        return ytdlp_download(url, out_dir, quality, cookies_browser, progress=progress, cancel=cancel, log=log,
                              cookies_file=cookies_file, use_saved_login=use_saved_login)
    except Cancelled:
        raise
    except ImportError:
        raise RuntimeError("yt-dlp is not installed. Run: python -m pip install -U yt-dlp")
    except Exception as e:
        first_error = clean_error(e)
        log(f"yt-dlp couldn't handle the page ({first_error}); scanning it for video links...")

    try:
        candidates = scrape_media_urls(url)
    except Exception as e:
        log(f"Couldn't fetch the page: {clean_error(e)}")
        candidates = []
    for media in candidates[:10]:
        if cancel and cancel.is_set():
            raise Cancelled()
        log(f"Trying {media}")
        try:
            return ytdlp_download(media, out_dir, quality, cookies_browser, referer=url,
                                  progress=progress, cancel=cancel, log=log, cookies_file=cookies_file,
                                  use_saved_login=use_saved_login)
        except Cancelled:
            raise
        except Exception as e:
            log(f"  failed: {clean_error(e)}")

    hint = ""
    if re.search(r"log(ged)?[ -]?in|sign in|cookies|private|members|\bage\b", first_error, re.I):
        hint = " — this video needs a login: import one under 'Use login from'"
    raise RuntimeError(first_error + hint)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
