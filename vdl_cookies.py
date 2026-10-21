"""Safe handling of login cookies (cookies.txt) for the video downloader.

A cookies.txt file works like a key to your accounts, so:
  * imported cookies are stored encrypted with Windows DPAPI — readable only by your
    Windows account on this PC; a copied file is useless anywhere else
  * the plain-text original can be wiped right after import
  * each download gets a short-lived temp file holding ONLY the cookies of the site
    being downloaded from, wiped as soon as the download ends (stale ones are swept on start)
  * the saved login is forgotten automatically after MAX_AGE_DAYS, and expired cookies are dropped
"""
import ctypes
import glob
import json
import math
import os
import sys
import tempfile
import time
import urllib.parse

if os.name == "nt":
    APP_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "VideoDownloader")
elif sys.platform == "darwin":
    APP_DIR = os.path.expanduser("~/Library/Application Support/VideoDownloader")
else:
    APP_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "VideoDownloader")
STORE = os.path.join(APP_DIR, "login.bin")
MAX_AGE_DAYS = 30
TEMP_PREFIX = "vdl_cookies_"
_ENTROPY = b"VideoDownloader cookie store v1"

# Sites that need a sister domain's cookies to count as logged in.
RELATED = {
    "youtube.com": {"google.com"},
    "youtu.be": {"youtube.com", "google.com"},
    "x.com": {"twitter.com"},
    "twitter.com": {"x.com"},
}
VIDEO_SITES = {
    "youtube.com", "youtu.be", "google.com", "vimeo.com", "twitch.tv", "tiktok.com", "instagram.com",
    "facebook.com", "x.com", "twitter.com", "reddit.com", "dailymotion.com", "bilibili.com", "nicovideo.jp",
    "patreon.com", "rumble.com", "kick.com", "soundcloud.com", "bandcamp.com", "crunchyroll.com", "odysee.com",
}


class CookieError(Exception):
    pass


# ---------- Windows DPAPI ----------
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _in_blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(data, protect):
    if os.name != "nt":
        raise CookieError("saved logins are only available on Windows; use 'Read from browser' instead")
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    src, _keep1 = _in_blob(data)
    ent, _keep2 = _in_blob(_ENTROPY)
    out = _Blob()
    UI_FORBIDDEN = 0x1
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(src), "VideoDownloader", ctypes.byref(ent),
                                      None, None, UI_FORBIDDEN, ctypes.byref(out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent),
                                        None, None, UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise CookieError("couldn't " + ("encrypt" if protect else "decrypt") + " the saved login "
                          "(it only opens under the Windows account that saved it)")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


# ---------- helpers ----------
def site_key(host):
    """Rough registrable domain: www.youtube.com -> youtube.com, news.bbc.co.uk -> bbc.co.uk."""
    labels = host.lower().strip(".").split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in {"co", "com", "org", "net", "ac", "gov", "edu", "ne", "or"}:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def wipe(path):
    """Overwrite a file with random bytes, then delete it (best effort; SSDs may keep old blocks)."""
    try:
        size = os.path.getsize(path)
        with open(path, "r+b", buffering=0) as f:
            f.write(os.urandom(size))
            os.fsync(f.fileno())
    except OSError:
        pass
    try:
        os.remove(path)
    except OSError:
        pass


def parse_netscape(text):
    """Parse a cookies.txt into a list of cookie lists; raises CookieError if it isn't one."""
    cookies = []
    for line in text.splitlines():
        line = line.strip("\r\n")
        httponly = line.startswith("#HttpOnly_")
        if httponly:
            line = line[len("#HttpOnly_"):]
        elif not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        domain, flag, path, secure, expires, name, value = parts
        try:
            expires = int(float(expires or 0))
        except ValueError:
            continue
        cookies.append([domain, flag, path, secure, expires, name, value, httponly])
    if not cookies:
        raise CookieError("that file isn't a cookies.txt export (no cookies found in Netscape format)")
    return cookies


def _live(cookies):
    now = time.time()
    return [c for c in cookies if not c[4] or c[4] > now]  # 0 = session cookie


def to_netscape(cookies):
    lines = ["# Netscape HTTP Cookie File", "# Temporary file written by Video Downloader; deleted after use.", ""]
    for domain, flag, path, secure, expires, name, value, httponly in cookies:
        lines.append(("#HttpOnly_" if httponly else "") + "\t".join([domain, flag, path, secure, str(expires), name, value]))
    return "\n".join(lines) + "\n"


def domains_in(cookies):
    return sorted({site_key(c[0]) for c in cookies})


def suggested_sites(sites):
    """Which sites to pre-tick on import: the video sites, or everything if none are recognised."""
    picked = [s for s in sites if s in VIDEO_SITES]
    return picked or list(sites)


# ---------- encrypted store ----------
def read_file(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return _live(parse_netscape(f.read()))


def save_login(cookies, keep_sites):
    keep = set(keep_sites)
    chosen = [c for c in cookies if site_key(c[0]) in keep]
    if not chosen:
        raise CookieError("no sites selected")
    os.makedirs(APP_DIR, exist_ok=True)
    blob = _dpapi(json.dumps({"imported": time.time(), "cookies": chosen}).encode("utf-8"), protect=True)
    tmp = STORE + ".new"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, STORE)
    return domains_in(chosen)


def _load():
    if not os.path.exists(STORE):
        return None
    with open(STORE, "rb") as f:
        data = json.loads(_dpapi(f.read(), protect=False))
    return data


def status():
    """None if nothing saved, else {"sites": [...], "days_left": n}. Expired logins are wiped here."""
    try:
        data = _load()
    except Exception:
        forget()
        return None
    if not data:
        return None
    age_days = (time.time() - data["imported"]) / 86400
    cookies = _live(data["cookies"])
    if age_days >= MAX_AGE_DAYS or not cookies:
        forget()
        return {"expired": True}
    return {"sites": domains_in(cookies), "days_left": max(1, math.ceil(MAX_AGE_DAYS - age_days))}


def forget():
    if os.path.exists(STORE):
        wipe(STORE)


# ---------- per-download temp file ----------
def cookies_for(url, cookies):
    host = urllib.parse.urlparse(url).hostname or ""
    key = site_key(host)
    allowed = {key} | RELATED.get(key, set())
    return [c for c in _live(cookies) if site_key(c[0]) in allowed]


def make_temp(url, source_file=None):
    """Write a temp cookies.txt holding only `url`'s site cookies, from the saved login or `source_file`.

    Returns the path (caller must wipe() it), or None if there are no cookies for that site.
    """
    if source_file:
        cookies = read_file(source_file)
    else:
        st = status()
        if not st or st.get("expired"):
            raise CookieError("the saved login has expired — import a fresh cookies.txt")
        cookies = _load()["cookies"]
    picked = cookies_for(url, cookies)
    if not picked:
        return None
    fd, path = tempfile.mkstemp(suffix=".txt", prefix=TEMP_PREFIX)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(to_netscape(picked))
    return path


def sweep_temp():
    """Wipe temp cookie files left behind by a crash or a killed process."""
    for p in glob.glob(os.path.join(tempfile.gettempdir(), TEMP_PREFIX + "*")):
        wipe(p)
