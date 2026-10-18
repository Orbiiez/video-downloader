"""Shared download engine for the app ("Video Downloader.pyw") and the CLI (video_dl.py).

download(url, opts, ...) works out what a link points to and saves it:
  1. a direct file link (video, audio or picture) is streamed straight to disk
  2. yt-dlp: video sites (YouTube, Twitch, X, TikTok and ~1800 more), playlists, HLS/DASH streams
  3. gallery-dl: photo posts and galleries (Instagram, X, Bluesky, Reddit, Tumblr and ~300 more). On sites
     where one post can mix photos and videos it is asked alongside yt-dlp, and it is the fallback
     whenever yt-dlp finds no video
  4. last resort: scan the page HTML for video links
A post or playlist with several items goes through pick(), so the user chooses what to keep.

Each download works in its own hidden folder inside the output folder; only finished files are moved
out, named in the chosen filename style, so the folder never fills up with half-done parts.
"""
import html as htmlmod
import importlib
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

import vdl_cookies

# Packaged .exe: ffmpeg/node ship in bin/, and "Update download engine" drops newer engines in the app data folder.
FROZEN = getattr(sys, "frozen", False)
BUNDLE_BIN = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "bin")
ENGINE_DIR = os.path.join(vdl_cookies.APP_DIR, "engine")
ENGINE_ZIP = os.path.join(ENGINE_DIR, "yt-dlp.zip")
GALLERY_WHL = os.path.join(ENGINE_DIR, "gallery-dl.whl")
_UPDATED = {}  # module -> updated engine archive put on sys.path

if FROZEN:
    for _path, _mod in ((ENGINE_ZIP, "yt_dlp"), (GALLERY_WHL, "gallery_dl")):
        if os.path.exists(_path):
            sys.path.insert(0, _path)
            _UPDATED[_mod] = _path

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".flv", ".wmv", ".ts", ".3gp", ".ogv", ".mpg", ".mpeg"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".oga", ".wav", ".flac", ".weba", ".alac"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".heic", ".bmp", ".jxl", ".tif", ".tiff"}
GENERIC_BINARY = {"", "application/octet-stream", "binary/octet-stream", "application/binary", "application/force-download"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
DEFAULT_OUT = os.path.join(os.path.expanduser("~"), "Downloads")
WORK_PREFIX = ".vdl-work-"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------- options ----------
DEFAULTS = {
    "mode": "auto",              # auto (video with sound) | audio | mute
    "quality": "1080",           # max, or the tallest video to get: 4320 2160 1440 1080 720 480 360 240 144
    "codec": "h264",             # h264 (plays everywhere) | av1 | vp9 | any (best quality, any codec)
    "container": "auto",         # auto | mp4 | mkv | webm
    "audio_format": "mp3",       # best (keep the original, no conversion) | mp3 | m4a | opus | ogg | wav | flac
    "audio_bitrate": "320",      # kbps, for lossy conversions
    "subtitles": "",             # "" = none, else a language code such as "en"
    "auto_subtitles": False,     # use auto-generated captions when there are no real ones
    "subtitle_file": False,      # save a separate .srt instead of putting them inside the video
    "dub_language": "",          # "" = original audio track, else a language code (YouTube dubs)
    "tiktok_full_audio": False,  # audio mode: the full original sound, not just the clip's part
    "twitter_gif": True,         # save X/Twitter GIFs as real .gif files
    "metadata": True,            # title/artist/date tags and chapters
    "cover_art": True,           # thumbnail as cover art on audio files
    "filename": "title",         # see FILENAME_STYLES
    "filename_template": "%(title)s - %(uploader)s",
    "sponsorblock": False,       # cut sponsor segments out of YouTube videos
    "multi": "ask",              # posts/playlists with several items: ask | all | first
    "images": True,              # save the photos in a post, not only its videos
    "clip": None,                # [start_seconds, end_seconds or None]: only that part of the video
    "exact_cuts": True,          # clip exactly (re-encodes the cut points) rather than at the nearest keyframe
    "proxy": "",
    "rate_limit": "",            # e.g. "5M" = 5 MB/s
    "out_dir": DEFAULT_OUT,
}
CHOICES = {
    "mode": ("auto", "audio", "mute"),
    "quality": ("max", "4320", "2160", "1440", "1080", "720", "480", "360", "240", "144"),
    "codec": ("h264", "av1", "vp9", "any"),
    "container": ("auto", "mp4", "mkv", "webm"),
    "audio_format": ("best", "mp3", "m4a", "opus", "ogg", "wav", "flac"),
    "audio_bitrate": ("320", "256", "192", "128", "96", "64"),
    "filename": ("title", "basic", "pretty", "nerdy", "classic", "id", "custom"),
    "multi": ("ask", "all", "first"),
}
FILENAME_STYLES = {
    "title": "Title",
    "basic": "Title (1080p, h264)",
    "pretty": "Title (1080p, h264, youtube)",
    "nerdy": "Title (1080p, h264, youtube, dQw4w9WgXcQ)",
    "classic": "youtube_dQw4w9WgXcQ_1920x1080_h264",
    "id": "Title [dQw4w9WgXcQ]",
    "custom": "Your own pattern",
}
LOSSY_AUDIO = {"mp3", "m4a", "opus", "ogg"}
BROWSERS = ["chrome", "edge", "firefox", "brave", "opera", "vivaldi", "chromium"]

# The old app's quality menu, still accepted by the CLI and when reading old settings.
LEGACY_QUALITY = {
    "Best available": {"quality": "max", "codec": "any"},
    "1080p": {"quality": "1080"},
    "720p": {"quality": "720"},
    "480p": {"quality": "480"},
    "Audio only (mp3)": {"mode": "audio", "audio_format": "mp3"},
}

# gallery-dl categories where one post can hold photos and videos together.
MIXED_SITES = {"twitter", "instagram", "bluesky", "reddit", "tumblr", "tiktok", "pinterest", "facebook",
               "threads", "imgur", "vk", "weibo", "mastodon", "misskey", "lemmy", "xiaohongshu", "flickr"}

PP_STEPS = {
    "Merger": "Merging video and audio", "ExtractAudio": "Converting audio", "FixupM3u8": "Fixing up the stream",
    "FFmpegEmbedSubtitle": "Adding subtitles", "FFmpegSubtitlesConvertor": "Converting subtitles",
    "EmbedThumbnail": "Adding cover art", "FFmpegThumbnailsConvertor": "Preparing cover art",
    "FFmpegMetadata": "Writing tags", "SponsorBlock": "Asking SponsorBlock", "ModifyChapters": "Cutting out sponsors",
    "VideoRemuxer": "Changing container", "MoveFiles": "Finishing",
}

_name_lock = threading.Lock()
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Cancelled(Exception):
    pass


class Login:
    """Which login to use: cookies read live from a browser, a cookies.txt, or the encrypted saved login."""

    def __init__(self, browser=None, cookies_file=None, saved=False):
        self.browser, self.cookies_file, self.saved = browser, cookies_file, saved


def normalize_opts(opts=None):
    """DEFAULTS overlaid with `opts`; unknown values fall back to the default instead of failing."""
    o = dict(DEFAULTS)
    for k, v in (opts or {}).items():
        if k == "quality" and v in LEGACY_QUALITY:
            o.update(LEGACY_QUALITY[v])
        elif k in DEFAULTS and v is not None:
            o[k] = v
    for k, allowed in CHOICES.items():
        o[k] = str(o[k])
        if o[k] not in allowed:
            o[k] = DEFAULTS[k]
    for k in ("subtitles", "dub_language"):
        o[k] = re.sub(r"[^A-Za-z-]", "", str(o[k] or ""))[:12]
    clip = o.get("clip")
    if clip:
        try:
            start = max(0.0, float(clip[0] or 0))
            end = float(clip[1]) if len(clip) > 1 and clip[1] not in (None, "") else None
            o["clip"] = [start, end] if end is None or end > start else None
        except (TypeError, ValueError, IndexError):
            o["clip"] = None
    o["out_dir"] = os.path.expandvars(os.path.expanduser(str(o["out_dir"] or DEFAULT_OUT).strip().strip('"'))) or DEFAULT_OUT
    return o


def parse_time(text):
    """'1:02:03', '2:30', '95' or '95.5' -> seconds; '' -> None. Raises ValueError on junk."""
    text = (text or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3 or not all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts):
        raise ValueError(f"not a time: {text}")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def parse_rate(text):
    """'5M' / '800K' / '1.5MB' -> bytes per second, '' -> None."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmg]?)i?b?\s*", (text or "").lower())
    if not m:
        return None
    return int(float(m.group(1)) * {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2)]) or None


# ---------- small helpers ----------
FRIENDLY_ERRORS = (
    (r"unable to obtain file audio codec", "this video has no sound to save"),
    (r"Unsupported URL", "this site isn't supported (no video or pictures found)"),
    (r"HTTP Error 403", "the site refused the download (403 Forbidden); a login may help"),
    (r"HTTP Error 404|404 Not Found", "nothing found at this link (404) — it may have been deleted"),
    (r"getaddrinfo failed|Name or service not known|nodename nor servname", "couldn't reach the site — check your internet connection"),
)


def clean_error(e):
    msg = _ANSI.sub("", str(e)).strip()
    msg = re.sub(r"^(ERROR:\s*)+", "", msg)
    msg = re.sub(r"^(Postprocessing:\s*)?(WARNING:\s*)?", "", msg)
    msg = re.sub(r"^\[[\w:]+\]\s*[\w-]+:\s*", "", msg)  # "[vimeo] 12345: " prefix
    msg = re.sub(r";? ?please report this issue on.*$", "", msg, flags=re.I | re.S)
    for pattern, friendly in FRIENDLY_ERRORS:
        if re.search(pattern, msg, re.I):
            return friendly
    return msg.split("\n")[0] or e.__class__.__name__


def find_bundled(name):
    exe = os.path.join(BUNDLE_BIN, name + (".exe" if os.name == "nt" else ""))
    return exe if os.path.exists(exe) else None


_ffmpeg_path = None


def find_ffmpeg():
    global _ffmpeg_path
    if _ffmpeg_path and os.path.exists(_ffmpeg_path):
        return _ffmpeg_path
    exe = find_bundled("ffmpeg") or shutil.which("ffmpeg")
    if not exe:
        try:
            import imageio_ffmpeg  # pip-bundled ffmpeg binary
            exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            exe = None
    _ffmpeg_path = exe
    return exe


def safe_filename(name, fallback="video"):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    name = re.sub(r"\s+", " ", name)
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com\d|lpt\d)(\..*)?", name):
        name = "_" + name
    base, ext = os.path.splitext(name)
    if len(ext) > 6 or " " in ext:  # "Mr. Smith goes" is not an extension
        base, ext = name, ""
    return (base[:150].strip(" .") or fallback) + ext


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


def site_of(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def normalize_url(url):
    url = url.strip().strip("\"'<>")
    if not re.match(r"(?i)^https?://", url):
        url = "https://" + url
    return url


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _hide(path):
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(path, 0x2)  # FILE_ATTRIBUTE_HIDDEN
        except Exception:
            pass


def _ext_kind(ext):
    ext = ext.lower() if ext.startswith(".") else "." + ext.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def _fmt_ext(ctype):
    special = {"video/quicktime": ".mov", "video/x-matroska": ".mkv", "video/x-msvideo": ".avi",
               "video/x-flv": ".flv", "video/mp2t": ".ts", "video/x-ms-wmv": ".wmv", "audio/mpeg": ".mp3",
               "audio/mp4": ".m4a", "audio/ogg": ".ogg", "image/jpeg": ".jpg", "image/webp": ".webp"}
    return special.get(ctype) or mimetypes.guess_extension(ctype) or ".bin"


class _Rate:
    """Smoothed speed for downloads that don't report one themselves."""

    def __init__(self):
        self.t, self.b, self.speed = time.monotonic(), 0, None

    def update(self, done):
        now = time.monotonic()
        if now - self.t >= 0.5:
            inst = (done - self.b) / (now - self.t)
            self.speed = inst if self.speed is None else self.speed * 0.7 + inst * 0.3
            self.t, self.b = now, done
        return self.speed


class _Throttle:
    def __init__(self, rate):
        self.rate, self.t0, self.sent = rate, time.monotonic(), 0

    def __call__(self, n):
        if not self.rate:
            return
        self.sent += n
        ahead = self.sent / self.rate - (time.monotonic() - self.t0)
        if ahead > 0:
            time.sleep(min(ahead, 1.0))


# ---------- engines ----------
_engine_lock = threading.Lock()


def _engine(mod):
    """Import yt_dlp / gallery_dl, falling back to the bundled copy if an updated one is broken."""
    if mod in sys.modules:
        return sys.modules[mod]
    with _engine_lock:
        try:
            return importlib.import_module(mod)
        except Exception:
            path = _UPDATED.pop(mod, None)
            if not path or path not in sys.path:
                raise
            sys.path.remove(path)
            for name in [m for m in sys.modules if m == mod or m.startswith(mod + ".")]:
                del sys.modules[name]
            return importlib.import_module(mod)


class _ThreadLog(logging.Handler):
    """Routes gallery-dl's log lines to the log callback of whichever download is running on this thread."""
    local = threading.local()

    def emit(self, record):
        cb = getattr(self.local, "log", None)
        if cb and record.levelno >= logging.INFO:
            try:
                cb(record.getMessage())
            except Exception:
                pass


_gallery_ready = False


_gallery_lock = threading.Lock()


def _gallery():
    global _gallery_ready
    if _gallery_ready:
        return sys.modules["gallery_dl"]
    with _gallery_lock:
        gdl = _engine("gallery_dl")
        if _gallery_ready:
            return gdl
        from gallery_dl import config, job, extractor  # noqa: F401 (loads the submodules)
        # The site list loads lazily and isn't safe to fill from two threads at once: load it all now.
        list(extractor.extractors())
        from gallery_dl.extractor.message import Message
        gdl.Message = Message
        config.set(("output",), "private", True)  # keep _http_headers/_fallback in DataJob results
        config.set(("output",), "mode", "null")
        lg = logging.getLogger("gallery-dl")
        lg.addHandler(_ThreadLog())
        lg.setLevel(logging.INFO)
        lg.propagate = False
        _gallery_ready = True
        return gdl


def warm_up():
    """Import the engines and build their site lists in the background, so the first link is quick."""
    try:
        _specific_extractor("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        _gallery().extractor.find("https://bsky.app/profile/a/post/b")
    except Exception:
        pass


def engine_versions():
    out = {"ffmpeg": bool(find_ffmpeg())}
    try:
        out["yt-dlp"] = _engine("yt_dlp").version.__version__
    except Exception:
        out["yt-dlp"] = None
    try:
        out["gallery-dl"] = _engine("gallery_dl").version.__version__
    except Exception:
        out["gallery-dl"] = None
    return out


_specific_cache = {}


def _specific_extractor(url):
    """Name of the yt-dlp extractor made for this site, or None if only the generic one would try."""
    host = site_of(url)
    if host in _specific_cache:
        return _specific_cache[host] if _specific_cache[host] != "?" else _match_extractor(url)
    name = _match_extractor(url)
    _specific_cache[host] = name or "?"
    return name


_ie_lock = threading.Lock()
_ie_list = None


def _match_extractor(url):
    global _ie_list
    if _ie_list is None:
        with _ie_lock:
            if _ie_list is None:
                _engine("yt_dlp")
                from yt_dlp.extractor import gen_extractor_classes
                _ie_list = list(gen_extractor_classes())
    for ie in _ie_list:
        key = ie.ie_key()
        if key != "Generic" and ie.suitable(url):
            return key
    return None


# ---------- ffmpeg ----------
def run_ffmpeg(args, cancel=None, duration=None, on_progress=None):
    """Run ffmpeg with args; on_progress(fraction) while it works. Raises Cancelled or RuntimeError."""
    ff = find_ffmpeg()
    if not ff:
        raise RuntimeError("ffmpeg is missing")
    cmd = [ff, "-hide_banner", "-nostdin", "-y"]
    if on_progress:
        cmd += ["-progress", "pipe:1", "-nostats"]
    p = subprocess.Popen(cmd + list(args), stdout=subprocess.PIPE if on_progress else subprocess.DEVNULL,
                         stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
                         text=True, encoding="utf-8", errors="replace")
    tail = []

    def read_err():
        for line in p.stderr:
            tail.append(line.rstrip())
            del tail[:-40]

    t = threading.Thread(target=read_err, daemon=True)
    t.start()
    try:
        if on_progress:
            for line in p.stdout:
                if cancel and cancel.is_set():
                    raise Cancelled()
                if line.startswith("out_time_us=") and duration:
                    try:
                        on_progress(min(1.0, int(line.split("=", 1)[1]) / 1e6 / duration))
                    except ValueError:
                        pass
        while p.poll() is None:
            if cancel and cancel.is_set():
                raise Cancelled()
            try:
                p.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                pass
    except BaseException:
        p.kill()
        p.wait()
        raise
    t.join(2)
    if p.returncode != 0:
        msg = next((ln for ln in reversed(tail) if ln and not ln.startswith(("  ", "frame=", "size="))), "")
        raise RuntimeError("ffmpeg failed" + (f": {msg}" if msg else ""))


def probe_media(path):
    """What ffmpeg says about a local file: duration, audio/video presence, size and codec."""
    ff = find_ffmpeg()
    info = {"duration": None, "audio": False, "video": False, "width": None, "height": None, "vcodec": None}
    if not ff:
        return info
    r = subprocess.run([ff, "-hide_banner", "-nostdin", "-i", path], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
    err = r.stderr
    if m := re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err):
        info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    info["audio"] = bool(re.search(r"Stream #\d+:\d+.*?: Audio:", err))
    if m := re.search(r"Stream #\d+:\d+.*?: Video: (\w+).*?, (\d{2,5})x(\d{2,5})", err):
        if "attached pic" not in m.group(0):
            info["video"] = True
            info["vcodec"], info["width"], info["height"] = m.group(1), int(m.group(2)), int(m.group(3))
    return info


def gif_filter(fps=None, width=None):
    """High-quality palette GIF filter chain."""
    pre = []
    if fps:
        pre.append(f"fps={fps}")
    if width:
        pre.append(f"scale='min({width},iw)':-2:flags=lanczos")
    head = (",".join(pre) + ",") if pre else ""
    return (head + "split[a][b];[a]palettegen=stats_mode=diff[p];"
            "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle")


def to_gif(src, dst, cancel=None, on_progress=None, fps=None, width=None, start=None, end=None):
    args = []
    if start:
        args += ["-ss", str(start)]
    if end:
        args += ["-to", str(end)]
    dur = probe_media(src)["duration"]
    if dur and end:
        dur = min(dur, end) - (start or 0)
    elif dur and start:
        dur -= start
    run_ffmpeg(args + ["-i", src, "-vf", gif_filter(fps, width), "-loop", "0", dst], cancel, dur, on_progress)


def strip_audio(src, dst, cancel=None):
    run_ffmpeg(["-i", src, "-map", "0", "-map", "-0:a", "-c", "copy", dst], cancel)


def extract_audio(src, dst, fmt, bitrate="320", cancel=None, on_progress=None):
    codec = {"mp3": ["-c:a", "libmp3lame"], "m4a": ["-c:a", "aac"], "opus": ["-c:a", "libopus"],
             "ogg": ["-c:a", "libvorbis"], "wav": ["-c:a", "pcm_s16le"], "flac": ["-c:a", "flac"]}[fmt]
    if fmt in LOSSY_AUDIO:
        codec += ["-b:a", f"{bitrate}k"]
    run_ffmpeg(["-i", src, "-vn", "-map", "0:a:0?", *codec, dst], cancel, probe_media(src)["duration"], on_progress)


# ---------- naming ----------
CODEC_NAMES = (("avc", "h264"), ("h264", "h264"), ("av01", "av1"), ("av1", "av1"), ("vp09", "vp9"), ("vp9", "vp9"),
               ("hev", "h265"), ("hvc", "h265"), ("h265", "h265"), ("hevc", "h265"), ("vp8", "vp8"))


def codec_name(vcodec):
    v = (vcodec or "").lower()
    if not v or v == "none":
        return None
    return next((name for pre, name in CODEC_NAMES if v.startswith(pre)), v.split(".")[0])


def _site_name(info):
    key = (info.get("extractor_key") or info.get("extractor") or info.get("category") or "").lower()
    key = re.sub(r"(tab|playlist|shorts|clips?|vod)$", "", key.split(":")[0]) or key.split(":")[0]
    return "web" if key in ("", "generic", "html5mediaembed") else key


def styled_name(info, style, template, ext, index=None, count=None):
    """File name (with extension) for a finished download in the chosen style."""
    ext = ext.lstrip(".")
    title = (info.get("title") or info.get("id") or "download").strip()
    vid = str(info.get("id") or "")
    site = _site_name(info)
    kind = _ext_kind(ext)
    height, width = info.get("height"), info.get("width")
    codec = codec_name(info.get("vcodec"))
    video = kind == "video" and height
    tags = [f"{height}p", codec] if video else []
    tags = [t for t in tags if t]
    artist = info.get("artist") or info.get("uploader") or info.get("channel")
    if kind == "audio" and artist and artist.lower() not in title.lower() and style in ("basic", "pretty", "nerdy"):
        title = f"{title} - {artist}"
    if style == "basic":
        base = f"{title} ({', '.join(tags)})" if tags else title
    elif style == "pretty":
        base = f"{title} ({', '.join(tags + [site])})"
    elif style == "nerdy":
        base = f"{title} ({', '.join(tags + [site] + ([vid] if vid else []))})"
    elif style == "classic":
        base = "_".join(p for p in [site, vid or safe_filename(title)[:40],
                                    f"{width}x{height}" if video and width else ("audio" if kind == "audio" else None),
                                    codec if video else None] if p)
    elif style == "id":
        base = f"{title} [{vid}]" if vid else title
    elif style == "custom":
        fields = {**{k: v for k, v in info.items() if isinstance(v, (str, int, float))},
                  "title": title, "site": site, "codec": codec or "", "ext": ext,
                  "resolution": f"{height}p" if height else ""}
        base = re.sub(r"%\((\w+)\)s", lambda m: str(fields.get(m.group(1), "") or ""), template or "%(title)s")
        base = re.sub(r"\s*-\s*$|\(\s*\)|\[\s*\]", "", base).strip() or title
    else:
        base = title
    if index is not None and count and count > 1:
        base += f" {index:0{len(str(count))}d}"
    return safe_filename(base[:180], "download") + "." + ext


# ---------- direct links ----------
MEDIA_URL_RE = re.compile(
    r"""(?:https?:)?(?:\\?/){2}[^\s"'<>()]+?\.(?:mp4|webm|m4v|mov|mkv|m3u8|mpd)(?:\?[^\s"'<>()]*)?"""
    r"""|(?<=src=["'])[^"'<>]+?\.(?:mp4|webm|m4v|mov|mkv|m3u8|mpd)(?:\?[^"'<>]*)?(?=["'])""",
    re.I,
)
OG_VIDEO_RE = re.compile(
    r"""<meta[^>]+(?:property|name)=["'](?:og:video(?::secure_url|:url)?|twitter:player:stream)["'][^>]+content=["']([^"']+)""",
    re.I,
)


def _opener(proxy):
    if proxy and not proxy.lower().startswith("http"):
        return None  # urllib can't do SOCKS: leave it to yt-dlp
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    return urllib.request.build_opener(*handlers)


def direct_download(url, out_dir, progress=None, cancel=None, kinds=("video", "audio", "image"), proxy="", rate=None):
    """Stream a direct file link to disk. Returns [path], or None if the URL isn't a media file of `kinds`."""
    opener = _opener(proxy)
    if opener is None:
        return None
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with opener.open(req, timeout=30) as resp:
        ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        name = os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(resp.url).path))
        cd = resp.headers.get("Content-Disposition", "")
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd, re.I)
        if m:
            name = urllib.parse.unquote(m.group(1))
        ext = os.path.splitext(name)[1].lower()
        kind = ctype.split("/")[0] if ctype.split("/")[0] in ("video", "audio", "image") else None
        if kind is None and ctype in GENERIC_BINARY:
            kind = _ext_kind(ext) if ext else None
        if kind not in kinds or ctype == "image/svg+xml":
            return None
        if _ext_kind(ext) != kind:
            name = (os.path.splitext(name)[0] or kind) + _fmt_ext(ctype)
        name = safe_filename(name, kind)
        total = int(resp.headers.get("Content-Length") or 0) or None
        if progress:
            progress({"status": "info", "title": os.path.splitext(name)[0], "kind": kind, "size": total,
                      "thumbnail": url if kind == "image" else None})
        path = reserve_path(os.path.join(out_dir, name))
        part = path + ".part"
        done, rater, throttle = 0, _Rate(), _Throttle(rate)
        try:
            with open(part, "wb") as f:
                while chunk := resp.read(1 << 18):
                    if cancel and cancel.is_set():
                        raise Cancelled()
                    f.write(chunk)
                    done += len(chunk)
                    throttle(len(chunk))
                    if progress:
                        speed = rater.update(done)
                        progress({"status": "downloading", "downloaded": done, "total": total, "speed": speed,
                                  "eta": (total - done) / speed if speed and total else None})
            if total and done < total:
                raise IOError(f"connection closed early ({done} of {total} bytes)")
            os.replace(part, path)
        except BaseException:
            _remove(part)
            raise
    return [path]


def scrape_media_urls(page_url, proxy=""):
    """Last resort for sites no engine knows: pull video URLs straight out of the page HTML."""
    opener = _opener(proxy) or urllib.request.build_opener()
    req = urllib.request.Request(page_url, headers={"User-Agent": UA})
    with opener.open(req, timeout=30) as resp:
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


# ---------- yt-dlp ----------
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


CODEC_SORT = {"h264": "vcodec:h264", "av1": "vcodec:av01", "vp9": "vcodec:vp9"}


def ytdlp_args(o, url, ffmpeg=True):
    """yt-dlp command-line flags for these options (format choice, conversions, extras)."""
    mode, q = o["mode"], o["quality"]
    height = None if q == "max" else int(q)
    dub, af = o["dub_language"], o["audio_format"]
    tiktok = vdl_cookies.site_key(site_of(url)) == "tiktok.com"
    args = []
    if not ffmpeg:  # can't merge, convert or cut: take the best single file
        if mode == "audio":
            return ["-f", "ba/b"]
        cap = f"[height<={height}]" if height else ""
        return ["-f", f"b{cap}[ext=mp4]/b{cap}/b"]
    if mode == "audio":
        fmt = "ba/b"
        if dub:
            fmt = f"ba[language^={dub}]/{fmt}"
        if tiktok and o["tiktok_full_audio"]:
            fmt = "audio/" + fmt  # yt-dlp's "audio" format on TikTok is the full original sound
        args += ["-f", fmt, "-x", "--audio-format", af]
        if af in LOSSY_AUDIO:
            args += ["--audio-quality", f"{o['audio_bitrate']}K"]
        if o["cover_art"] and af != "wav":
            args += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]
    else:
        sort = []
        if o["codec"] in CODEC_SORT:
            sort.append(CODEC_SORT[o["codec"]])
        if height:
            sort.append(f"res:{height}")
        container = o["container"]
        if o["codec"] == "h264" or container == "mp4":
            sort.append("acodec:aac")  # AAC keeps MP4 files playable everywhere
        if mode == "mute":
            fmt = "bv/bv*/b"
        elif dub:
            fmt = f"bv*+ba[language^={dub}]/bv*+ba/b"
        else:
            fmt = "bv*+ba/b"
        merge = {"auto": "webm/mkv" if o["codec"] in ("av1", "vp9") else "mp4/mkv"}.get(container, container)
        args += ["-f", fmt, "--merge-output-format", merge]
        if container in ("mp4", "mkv"):
            args += ["--remux-video", container]
        if sort:
            args += ["-S", ",".join(sort)]
        if o["subtitles"] and mode != "audio":
            lang = o["subtitles"]
            args += ["--sub-langs", f"{lang}.*"]
            if o["auto_subtitles"]:
                args.append("--write-auto-subs")
            # --write-subs together with --embed-subs would also leave the .vtt next to the video
            args += ["--write-subs", "--convert-subs", "srt"] if o["subtitle_file"] else ["--embed-subs"]
    if o["metadata"]:
        args.append("--embed-metadata")
    if o["sponsorblock"]:
        args += ["--sponsorblock-remove", "sponsor,selfpromo,interaction"]
    if o["clip"]:
        start, end = o["clip"]
        args += ["--download-sections", f"*{start:g}-{'inf' if end is None else f'{end:g}'}"]
        if o["exact_cuts"] and mode != "audio":
            args.append("--force-keyframes-at-cuts")
    return args


def _ydl(o, url, workdir, *, login_file=None, browser=None, referer=None, hook=None, pp_hook=None, log=None):
    yt_dlp = _engine("yt_dlp")
    ffmpeg = find_ffmpeg()
    tmpl = os.path.join(workdir.replace("%", "%%"), "%(title).120B [%(id)s].%(ext)s")
    args = ["--ignore-config", "--no-mtime", "--windows-filenames", "--no-playlist", "--playlist-end", "1000",
            "-N", "4", "--retries", "5", "--fragment-retries", "10", "-o", tmpl]
    args += ytdlp_args(o, url, bool(ffmpeg))
    if o["proxy"]:
        args += ["--proxy", o["proxy"]]
    if parse_rate(o["rate_limit"]):
        args += ["-r", str(parse_rate(o["rate_limit"]))]
    if browser:
        args += ["--cookies-from-browser", browser]
    if login_file:
        args += ["--cookies", login_file]
    if referer:
        args += ["--referer", referer]
    try:
        opts = yt_dlp.parse_options(args).ydl_opts
    except SystemExit as e:
        raise ValueError(f"invalid download options ({e})")
    if ffmpeg:
        # Clip downloads check for ffmpeg without a YoutubeDL at hand, through this per-thread setting.
        from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor
        FFmpegPostProcessor._ffmpeg_location.set(ffmpeg)
    logger = _Logger(log)
    opts.update({
        "quiet": True, "no_warnings": False, "noprogress": True, "logger": logger,
        "progress_hooks": [hook] if hook else [], "postprocessor_hooks": [pp_hook] if pp_hook else [],
        "ignoreerrors": False, "extract_flat": "in_playlist", "warn_when_outdated": False,
        # YouTube needs a JS runtime; allow node as well as yt-dlp's default deno.
        "js_runtimes": {"deno": {}, "node": {"path": find_bundled("node")}},
    })
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    return yt_dlp.YoutubeDL(opts), logger


def _thumb_of(info):
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbs = [t for t in info.get("thumbnails") or [] if t.get("url")]
    if not thumbs:
        return None
    # Prefer a mid-size picture: big enough for a card, small enough to load fast.
    sized = [t for t in thumbs if (t.get("width") or 0) >= 320]
    return (min(sized, key=lambda t: t["width"]) if sized else thumbs[-1])["url"]


# ---------- the download ----------
def _in_thread(fn, cancel, timeout=None):
    """Run fn() on a helper thread so a cancel can stop waiting for slow extraction."""
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    deadline = time.monotonic() + timeout if timeout else None
    while t.is_alive():
        t.join(0.2)
        if cancel and cancel.is_set():
            raise Cancelled()
        if deadline and time.monotonic() > deadline:
            raise TimeoutError("timed out")
    if "error" in box:
        raise box["error"]
    return box.get("value")


class _Job:
    def __init__(self, url, o, login, progress, cancel, log, pick, spawn, key):
        self.url, self.o, self.login = url, o, login or Login()
        self.progress, self.cancel, self.log = progress, cancel, log
        self.pick, self.spawn = pick, spawn
        self.out_dir = o["out_dir"]
        self.work = os.path.join(self.out_dir, WORK_PREFIX + (key or os.urandom(6).hex()))
        self.cookie_tmp = None
        self.saved = []
        self.items = 1
        self.item = 1
        self.rater = _Rate()

    # --- plumbing ---
    def check(self):
        if self.cancel and self.cancel.is_set():
            raise Cancelled()

    def emit(self, d=None, **kw):
        d = {**(d or {}), **kw}
        if self.items > 1 and d.get("status") == "downloading":
            d.update(item=self.item, items=self.items)
        self.progress(d)

    def workdir(self):
        if not os.path.isdir(self.work):
            os.makedirs(self.work, exist_ok=True)
            _hide(self.work)
        return self.work

    def cookie_file(self, url):
        lg = self.login
        if not (lg.cookies_file or lg.saved):
            return None
        if self.cookie_tmp is None:
            if lg.cookies_file and not os.path.isfile(lg.cookies_file):
                raise RuntimeError(f"cookies file not found: {lg.cookies_file}")
            self.cookie_tmp = vdl_cookies.make_temp(url, lg.cookies_file) or ""
            if not self.cookie_tmp:
                self.log("No saved login for this site; downloading without one.")
        return self.cookie_tmp or None

    def close(self, outcome):
        """outcome: "ok", "cancelled" or "failed". A failed download keeps its partial files for a retry."""
        if self.cookie_tmp:
            vdl_cookies.wipe(self.cookie_tmp)
        if outcome != "failed" or not any(f.endswith((".part", ".ytdl")) for f in self._work_files()):
            shutil.rmtree(self.work, ignore_errors=True)

    def _work_files(self):
        try:
            return os.listdir(self.work)
        except OSError:
            return []

    def finish(self, src, name):
        """Move a finished file out of the work folder under a free name; returns the final path."""
        path = reserve_path(os.path.join(self.out_dir, name))
        try:
            os.replace(src, path)
        finally:
            _remove(path + ".part")
        self.saved.append(path)
        return path

    def choose(self, items):
        """Indices of the items to download (asks the user when there are several)."""
        if len(items) <= 1 or self.o["multi"] == "all" or not self.pick:
            return list(range(len(items)))
        if self.o["multi"] == "first":
            return [0]
        self.check()
        picked = self.pick(items)
        self.check()
        picked = sorted({int(i) for i in picked or [] if 0 <= int(i) < len(items)})
        if not picked:
            raise Cancelled()
        return picked

    # --- main flow ---
    def run(self):
        url, o = self.url, self.o
        os.makedirs(self.out_dir, exist_ok=True)
        path_ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
        plain_media = o["mode"] == "auto" and not o["clip"]
        if _ext_kind(path_ext) or not self._known_site(url):
            kinds = ("image",) + (("video", "audio") if plain_media else ())
            try:
                paths = direct_download(url, self.out_dir, self.emit, self.cancel, kinds, o["proxy"],
                                        parse_rate(o["rate_limit"]))
                if paths:
                    self.saved = paths
                    return paths
            except Cancelled:
                raise
            except Exception as e:
                self.log(f"Direct download failed ({clean_error(e)}); trying the site engines...")

        gallery_cat = self._gallery_category(url)
        gallery_future = None
        if gallery_cat in MIXED_SITES and o["images"] and o["mode"] != "audio":
            gallery_future = self._start(lambda: self.gallery_probe(url))
        first_error = None
        try:
            info, ydl = self.ytdl_probe(url)
        except Cancelled:
            raise
        except Exception as e:
            info, ydl = None, None
            first_error = clean_error(e)
            self.log(f"yt-dlp: {first_error}")

        gallery = None
        if gallery_future:
            gallery = self._join(gallery_future)
        if gallery and any(it["kind"] == "image" for it in gallery["items"]):
            if ydl:
                ydl.close()
            return self.gallery_download(gallery)
        if info is not None:
            return self.ytdl_download(info, ydl)
        if gallery_cat and not gallery_future:
            gallery = self._join(self._start(lambda: self.gallery_probe(url)))
        if gallery and gallery["items"]:
            return self.gallery_download(gallery)

        self.log("Scanning the page for video links...")
        try:
            candidates = scrape_media_urls(url, o["proxy"])
        except Exception as e:
            self.log(f"Couldn't fetch the page: {clean_error(e)}")
            candidates = []
        for media in candidates[:10]:
            self.check()
            self.log(f"Trying {media}")
            try:
                info, ydl = self.ytdl_probe(media, referer=url)
                return self.ytdl_download(info, ydl)
            except Cancelled:
                raise
            except Exception as e:
                self.log(f"  failed: {clean_error(e)}")

        first_error = first_error or "no video or pictures found at this link"
        hint = ""
        if re.search(r"log(ged)?[ -]?in|sign in|cookies|private|members|\bage\b|confirm your age", first_error, re.I):
            hint = " — this needs a login: pick one under Login"
        raise RuntimeError(first_error + hint)

    def _known_site(self, url):
        try:
            return bool(_specific_extractor(url) or self._gallery_category(url))
        except Exception:
            return False

    def _gallery_category(self, url):
        try:
            ex = _gallery().extractor.find(url)
        except Exception:
            return None
        return None if ex is None or ex.category in ("directlink", "generic") else ex.category

    def _start(self, fn):
        box = {"done": threading.Event()}

        def run():
            _ThreadLog.local.log = self.log
            try:
                box["value"] = fn()
            except BaseException as e:
                box["error"] = e
            finally:
                box["done"].set()

        threading.Thread(target=run, daemon=True).start()
        return box

    def _join(self, box, timeout=90):
        deadline = time.monotonic() + timeout
        while not box["done"].wait(0.2):
            self.check()
            if time.monotonic() > deadline:
                self.log("gallery-dl timed out")
                return None
        if "error" in box:
            self.log(f"gallery-dl: {clean_error(box['error'])}")
            return None
        return box.get("value")

    # --- yt-dlp ---
    def ytdl_probe(self, url, referer=None):
        self.emit(status="processing", step="Looking up the link")
        ydl, logger = _ydl(self.o, url, self.workdir(), login_file=self.cookie_file(url),
                           browser=self.login.browser, referer=referer, hook=self._hook, pp_hook=self._pp_hook,
                           log=self.log)
        try:
            info = _in_thread(lambda: ydl.extract_info(url, download=False), self.cancel)
        except Exception as e:
            ydl.close()
            if isinstance(e, Cancelled):
                raise
            raise RuntimeError(clean_error(logger.last_error or e))
        if not info:
            ydl.close()
            raise RuntimeError("no video found on the page")
        return info, ydl

    def ytdl_download(self, info, ydl):
        try:
            if info.get("_type") == "playlist" or info.get("entries") is not None:
                return self._ytdl_playlist(info, ydl)
            self._announce(info)
            self._ytdl_one(info, ydl)
            return self.saved
        finally:
            ydl.close()

    def _announce(self, info, count=None, kind=None):
        size = None
        fmts = info.get("requested_formats") or ([info] if info.get("format_id") else [])
        if fmts and all(f.get("filesize") or f.get("filesize_approx") for f in fmts):
            size = sum(f.get("filesize") or f.get("filesize_approx") for f in fmts)
        if self.o["clip"] and size and info.get("duration"):
            start, end = self.o["clip"]
            size = size * ((end or info["duration"]) - start) / info["duration"]
        self.emit(status="info", title=info.get("title"), thumbnail=_thumb_of(info), duration=info.get("duration"),
                  site=_site_name(info), size=size, count=count,
                  kind=kind or ("audio" if self.o["mode"] == "audio" else "video"))

    def _ytdl_playlist(self, info, ydl):
        entries = [e for e in info.get("entries") or [] if e]
        if not entries:
            raise RuntimeError("the playlist is empty")
        flat = all(e.get("_type") in ("url", "url_transparent") for e in entries)
        items = [{"kind": "audio" if self.o["mode"] == "audio" else "video",
                  "title": e.get("title") or e.get("id") or f"Item {i + 1}", "thumb": _thumb_of(e),
                  "duration": e.get("duration"), "width": e.get("width"), "height": e.get("height")}
                 for i, e in enumerate(entries)]
        self.emit(status="info", title=info.get("title") or "Playlist", thumbnail=_thumb_of(info) or items[0]["thumb"],
                  site=_site_name(info), count=len(entries), kind="playlist")
        picked = self.choose(items)
        if flat:
            urls = [{"url": entries[i].get("url") or entries[i].get("webpage_url"), "title": items[i]["title"],
                     "thumb": items[i]["thumb"], "duration": items[i]["duration"]} for i in picked]
            urls = [u for u in urls if u["url"]]
            if self.spawn:
                self.spawn(urls)
                self.emit(status="spawned", count=len(urls))
                return []
            for n, u in enumerate(urls, 1):  # CLI: one after another
                self.check()
                self.log(f"[{n}/{len(urls)}] {u['title']}")
                sub = _Job(u["url"], {**self.o, "multi": "first"}, self.login, self.progress, self.cancel, self.log,
                           None, None, None)
                try:
                    self.saved += sub.run()
                    sub.close("ok")
                except Cancelled:
                    sub.close("cancelled")
                    raise
                except Exception as e:
                    sub.close("failed")
                    self.log(f"  failed: {clean_error(e)}")
            return self.saved
        self.items = len(picked)
        for n, i in enumerate(picked, 1):
            self.check()
            self.item = n
            self._ytdl_one(entries[i], ydl, index=n if len(picked) > 1 else None, count=len(picked))
        return self.saved

    def _ytdl_one(self, info, ydl, index=None, count=None):
        try:
            result = ydl.process_ie_result(info, download=True)
        except BaseException:
            # yt-dlp sometimes re-wraps a cancel in a DownloadError, so go by the flag.
            self.check()
            raise
        self.check()
        downloads = [d for d in (result or {}).get("requested_downloads") or [] if d.get("filepath")]
        if not downloads:
            raise RuntimeError(clean_error(getattr(ydl.params.get("logger"), "last_error", None) or "nothing was downloaded"))
        for d in downloads:
            path = d["filepath"]
            if not os.path.exists(path):
                continue
            meta = {**result, **{k: v for k, v in d.items() if v is not None}}
            self._finish_video(path, meta, index, count)

    def _finish_video(self, path, meta, index=None, count=None, gif_hint=None):
        o = self.o
        base = os.path.basename(path)
        stem = os.path.splitext(base)[0]
        siblings = [f for f in self._work_files() if f.startswith(stem + ".") and f != base
                    and not f.endswith((".part", ".ytdl", ".webp", ".jpg", ".png", ".json"))]
        kind = _ext_kind(os.path.splitext(path)[1])
        if kind == "video" and o["mode"] in ("mute", "auto"):
            probe = probe_media(path)
            if o["mode"] == "mute" and probe["audio"]:
                self.emit(status="processing", step="Removing the sound")
                out = os.path.join(self.work, stem + ".muted" + os.path.splitext(path)[1])
                strip_audio(path, out, self.cancel)
                os.replace(out, path)
            elif o["mode"] == "auto" and o["twitter_gif"] and not probe["audio"] and probe["video"] \
                    and (gif_hint or (gif_hint is None and _site_name(meta) in ("twitter", "x"))):
                self.emit(status="processing", step="Making a GIF")
                out = os.path.join(self.work, stem + ".gif")
                to_gif(path, out, self.cancel, lambda f: self.emit(status="processing", step="Making a GIF", fraction=f))
                _remove(path)
                path = out
        ext = os.path.splitext(path)[1]
        name = styled_name(meta, o["filename"], o["filename_template"], ext, index, count)
        final = self.finish(path, name)
        new_stem = os.path.splitext(os.path.basename(final))[0]
        for f in siblings:  # subtitles saved next to the video follow its new name
            suffix = f[len(stem):]
            self.finish(os.path.join(self.work, f), new_stem + suffix)
        return final

    def _ytdl_check(self):
        if self.cancel and self.cancel.is_set():
            raise _engine("yt_dlp").utils.DownloadCancelled()

    def _hook(self, d):
        self._ytdl_check()
        if d["status"] == "downloading":
            done, total = d.get("downloaded_bytes"), d.get("total_bytes") or d.get("total_bytes_estimate")
            speed = d.get("speed")
            if speed is None and done is not None:
                speed = self.rater.update(done)
            eta = d.get("eta")
            if eta is None and speed and total and done is not None:
                eta = max(0, (total - done) / speed)
            self.emit(status="downloading", downloaded=done, total=total, speed=speed, eta=eta,
                      fragment=d.get("fragment_index"), fragments=d.get("fragment_count"),
                      file=d.get("filename"))
        elif d["status"] == "finished":
            self.rater = _Rate()

    def _pp_hook(self, d):
        self._ytdl_check()
        if d["status"] == "started":
            self.emit(status="processing", step=PP_STEPS.get(d.get("postprocessor"), "Finishing"))

    # --- gallery-dl ---
    def gallery_probe(self, url):
        gdl = _gallery()
        ex = gdl.extractor.find(url)
        if ex is None:
            return None
        overrides = {"image-range": "1-500"}
        cookie = self.cookie_file(url)
        if cookie:
            overrides["cookies"] = cookie
        elif self.login.browser:
            overrides["cookies"] = [self.login.browser]
        if self.o["proxy"]:
            overrides["proxy"] = self.o["proxy"]
        base = ex.config
        ex.config = lambda key, default=None: overrides[key] if key in overrides else base(key, default)
        j = gdl.job.DataJob(ex, file=None, resolve=1)
        j.run()
        Message = gdl.Message
        items, post = [], {}
        for msg in j.data:
            if msg[0] == Message.Directory and not post:
                post = msg[1]
            elif msg[0] in (Message.Url, Message.Queue):
                link, meta = msg[1], msg[2]
                if msg[0] == Message.Queue:
                    link = "ytdl:" + link
                ext = (meta.get("extension") or "").lower()
                if link.startswith("ytdl:"):
                    kind = "video"
                else:
                    kind = _ext_kind(ext) or _ext_kind(os.path.splitext(urllib.parse.urlparse(link).path)[1]) or "image"
                items.append({"kind": kind, "url": link, "ext": ext, "meta": meta,
                              "title": None, "thumb": link if kind == "image" else None,
                              "headers": meta.get("_http_headers") or {}, "fallback": meta.get("_fallback") or (),
                              "duration": meta.get("duration"), "width": meta.get("width"), "height": meta.get("height"),
                              "gif": meta.get("type") == "animated_gif"})
        if not items and j.exception:
            raise j.exception
        return {"extractor": ex, "post": post, "items": items, "category": ex.category}

    def gallery_download(self, g):
        o, ex, post = self.o, g["extractor"], g["post"]
        items = g["items"]
        if not o["images"] or o["mode"] == "audio":
            items = [it for it in items if it["kind"] != "image"]
            if not items:
                raise RuntimeError("this post only has pictures" + (
                    ", no sound" if o["mode"] == "audio" else " (turn on 'Save pictures from posts' to get them)"))
        title = _post_title(post) or f"{g['category']} {post.get('post_id') or post.get('id') or ''}".strip()
        music = post.get("music") if isinstance(post.get("music"), dict) else {}
        if o["mode"] == "audio" and o["tiktok_full_audio"] and g["category"] == "tiktok" and music.get("playUrl"):
            # The full original sound, rather than the part of it this clip uses.
            items = [{"kind": "audio", "url": music["playUrl"], "ext": "", "meta": {}, "headers": {}, "fallback": (),
                      "thumb": None, "duration": music.get("duration"), "width": None, "height": None, "gif": False}]
        for n, it in enumerate(items, 1):
            it["title"] = f"{it['kind'].capitalize()} {n}"
        first_image = next((it for it in items if it["kind"] == "image"), None)
        self.emit(status="info", title=title, site=g["category"], count=len(items),
                  thumbnail=first_image["url"] if first_image else None,
                  thumb_headers=first_image["headers"] if first_image else None,
                  kind="images" if first_image else ("audio" if o["mode"] == "audio" else "video"))
        picked = self.choose([{k: it[k] for k in ("kind", "title", "thumb", "duration", "width", "height", "headers")}
                              for it in items])
        self.items = len(picked)
        meta_base = {"title": title, "id": str(post.get("post_id") or post.get("id") or post.get("tweet_id") or ""),
                     "category": g["category"], "uploader": _post_author(post)}
        failures = []
        for n, i in enumerate(picked, 1):
            self.check()
            self.item = n
            it = items[i]
            index = n if len(picked) > 1 else None
            try:
                if it["url"].startswith("ytdl:"):
                    sub_url = it["url"][5:]
                    info, ydl = self.ytdl_probe(sub_url)
                    try:
                        self._ytdl_one(info, ydl, index, len(picked))
                    finally:
                        ydl.close()
                    continue
                path = self._fetch(ex, it, n)
                meta = {**meta_base, "width": it["width"], "height": it["height"]}
                if it["kind"] == "image":
                    self.finish(path, styled_name(meta, o["filename"], o["filename_template"],
                                                  os.path.splitext(path)[1], index, len(picked)))
                elif it["kind"] in ("video", "audio") and o["mode"] == "audio":
                    keep = o["audio_format"] == "best" and it["kind"] == "audio"
                    fmt = "m4a" if o["audio_format"] == "best" else o["audio_format"]
                    out = path if keep else os.path.splitext(path)[0] + ".conv." + fmt
                    if not keep:
                        self.emit(status="processing", step="Converting audio")
                        extract_audio(path, out, fmt, o["audio_bitrate"], self.cancel)
                        _remove(path)
                    self.finish(out, styled_name({**meta, "vcodec": "none"}, o["filename"], o["filename_template"],
                                                 os.path.splitext(out)[1], index, len(picked)))
                else:
                    probe = probe_media(path)
                    meta.update(vcodec=probe["vcodec"], width=probe["width"] or meta["width"],
                                height=probe["height"] or meta["height"])
                    self._finish_video(path, meta, index, len(picked), gif_hint=it["gif"])
            except Cancelled:
                raise
            except Exception as e:
                failures.append(clean_error(e))
                self.log(f"Item {n} failed: {clean_error(e)}")
        if not self.saved:
            raise RuntimeError(failures[0] if failures else "nothing was downloaded")
        if failures:
            self.log(f"{len(failures)} of {len(picked)} items failed")
        return self.saved

    def _fetch(self, ex, it, n):
        ext = it["ext"] or os.path.splitext(urllib.parse.urlparse(it["url"]).path)[1].lstrip(".") or "jpg"
        dest = os.path.join(self.workdir(), f"item{n}.{ext}")
        rate = _Throttle(parse_rate(self.o["rate_limit"]))
        last = None
        for link in [it["url"], *it["fallback"]]:
            self.check()
            try:
                with ex.request(link, headers=it["headers"] or None, stream=True) as r:
                    ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
                    if not it["ext"] and ctype:
                        dest = os.path.splitext(dest)[0] + _fmt_ext(ctype)
                    total = int(r.headers.get("Content-Length") or 0) or None
                    done, rater = 0, _Rate()
                    with open(dest + ".part", "wb") as f:
                        for chunk in r.iter_content(1 << 18):
                            self.check()
                            f.write(chunk)
                            done += len(chunk)
                            rate(len(chunk))
                            speed = rater.update(done)
                            self.emit(status="downloading", downloaded=done, total=total, speed=speed,
                                      eta=(total - done) / speed if speed and total else None)
                os.replace(dest + ".part", dest)
                return dest
            except Cancelled:
                _remove(dest + ".part")
                raise
            except Exception as e:
                last = e
                _remove(dest + ".part")
        raise RuntimeError(clean_error(last) if last else "download failed")


def _post_title(post):
    for key in ("title", "description", "desc", "content", "text", "caption", "summary"):
        v = post.get(key)
        if isinstance(v, dict):
            v = v.get("text")
        if isinstance(v, str) and v.strip():
            line = re.sub(r"https?://\S+|#\w+", "", v.strip().splitlines()[0]).strip(" -–—:|")
            if line:
                return line[:80].rstrip() + ("…" if len(line) > 80 else "")
    return None


def _post_author(post):
    for key in ("author", "user", "owner", "uploader"):
        v = post.get(key)
        if isinstance(v, dict):
            v = v.get("name") or v.get("nick") or v.get("handle") or v.get("username") or v.get("uniqueId")
        if isinstance(v, str) and v:
            return v
    return post.get("username") or post.get("nickname")


def download(url, opts=None, *, login=None, progress=None, cancel=None, log=None, pick=None, spawn=None, key=None):
    """Download whatever `url` points to with the given options. Returns the saved file paths.

    progress(dict) gets "info" (title, thumbnail, kind, count...), "downloading" and "processing" updates.
    pick(items) is called when a post or playlist holds several items and must return the indices to keep;
    without it everything is downloaded. spawn(entries) receives the chosen videos of a playlist so the caller
    can queue each as its own download; without it they are downloaded one after another here.
    `key` names the work folder, so a retry with the same key can resume partial downloads.
    """
    o = normalize_opts(opts)
    job = _Job(normalize_url(url), o, login, progress or (lambda d: None), cancel, log or (lambda m: None),
               pick, spawn, key)
    outcome = "failed"
    try:
        _ThreadLog.local.log = job.log
        paths = job.run()
        outcome = "ok"
        return paths
    except Cancelled:
        outcome = "cancelled"
        raise
    finally:
        _ThreadLog.local.log = None
        job.close(outcome)


def discard_work(out_dir, key):
    """Delete the work folder a failed download left for resuming."""
    shutil.rmtree(os.path.join(out_dir, WORK_PREFIX + key), ignore_errors=True)


def sweep_work(out_dir, max_age_days=3):
    """Remove work folders left behind by a crash."""
    try:
        names = os.listdir(out_dir)
    except OSError:
        return
    cutoff = time.time() - max_age_days * 86400
    for n in names:
        p = os.path.join(out_dir, n)
        if n.startswith(WORK_PREFIX) and os.path.isdir(p) and os.path.getmtime(p) < cutoff:
            shutil.rmtree(p, ignore_errors=True)
