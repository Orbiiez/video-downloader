"""Video Downloader — paste links, they download in parallel.

Double-click this file (or the .exe) to open the app. Links given on the command line are added to the
queue; if the app is already open they are handed to that window instead of starting a second copy.
"""
import os
import subprocess
import sys
import threading
import time

# pythonw has no console; give stray prints somewhere harmless to go.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import vdl_cookies
import vdl_server

APP = "Video Downloader"
MEDIA_TYPES = ("Videos and audio (*.mp4;*.mkv;*.webm;*.mov;*.m4v;*.avi;*.ts;*.flv;*.wmv;*.mp3;*.m4a;*.opus;*.ogg;*.wav;*.flac;*.aac)",
               "All files (*.*)")


def _win_setup():
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VideoDownloader")
    except Exception:
        pass


def _hwnd(window):
    try:
        return int(window.native.Handle.ToInt64())
    except Exception:
        return None


def _title_bar(window, dark):
    """Windows 11: title bar in the app's own colors (Windows 10: just dark or light)."""
    hwnd = _hwnd(window)
    if not hwnd:
        return
    try:
        import ctypes
        set_attr = ctypes.windll.dwmapi.DwmSetWindowAttribute
        set_attr(hwnd, 20, ctypes.byref(ctypes.c_int(1 if dark else 0)), 4)  # immersive dark mode
        rgb = 0x000E0B0B if dark else 0x00EEF3F4  # COLORREF is 0x00BBGGRR: #0b0b0e / #f4f3ee
        set_attr(hwnd, 35, ctypes.byref(ctypes.c_int(rgb)), 4)  # caption color
        set_attr(hwnd, 34, ctypes.byref(ctypes.c_int(rgb)), 4)  # border color
    except Exception:
        pass


def _system_dark():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except Exception:
        return True


def _flash(window):
    """Blink the taskbar button when something needs you and the window isn't in front."""
    hwnd = _hwnd(window)
    if not hwnd or sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        if ctypes.windll.user32.GetForegroundWindow() == hwnd:
            return

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                        ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x3 | 0xC, 0, 0)  # FLASHW_ALL | FLASHW_TIMERNOFG
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


def _wait_for_workers(mgr, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with mgr.lock:
            if not any(j.state in vdl_server.ACTIVE for j in mgr.jobs.values()):
                return
        time.sleep(0.1)


def run_window(mgr, srv):
    import webview
    from webview.dom import DOMEventHandler

    if os.environ.get("VDL_DEVTOOLS_PORT"):  # lets the UI tests drive the real window
        webview.settings["REMOTE_DEBUGGING_PORT"] = int(os.environ["VDL_DEVTOOLS_PORT"])
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True

    geo = mgr.settings.get("window") or {}
    theme = mgr.settings.get("theme")
    dark = theme == "dark" or (theme != "light" and _system_dark())
    window = webview.create_window(
        APP, srv.url() + ("&sys=dark" if dark else "&sys=light"), width=int(geo.get("w") or 1080), height=int(geo.get("h") or 760),
        x=geo.get("x"), y=geo.get("y"), min_size=(400, 540), background_color="#0b0b0e" if dark else "#f4f3ee",
        text_select=True, maximized=bool(geo.get("max")))

    def dialog(kind, start):
        if kind == "folder":
            res = window.create_file_dialog(webview.FileDialog.FOLDER, directory=start or "")
        elif kind == "cookies":
            res = window.create_file_dialog(webview.FileDialog.OPEN, directory=os.path.join(os.path.expanduser("~"), "Downloads"),
                                            file_types=("Cookie files (*.txt)", "All files (*.*)"))
        else:
            res = window.create_file_dialog(webview.FileDialog.OPEN, directory=start or "", allow_multiple=True,
                                            file_types=MEDIA_TYPES)
        return list(res or [])

    def focus():
        try:
            window.restore()
            window.show()
            window.on_top = True
            window.on_top = False
        except Exception:
            pass

    def on_drop(e):
        files = (e.get("dataTransfer") or {}).get("files") or []
        paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
        if paths:
            mgr.send({"type": "files", "paths": paths}, local_only=True)

    def on_loaded():
        try:
            window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)
        except Exception:
            pass

    def watch_theme():
        """Follow the Windows light/dark setting and the app's theme choice (title bar and page)."""
        last_sys, last_bar = _system_dark(), None
        while True:
            sys_dark = _system_dark()
            if sys_dark != last_sys:
                last_sys = sys_dark
                mgr.send({"type": "systheme", "theme": "dark" if sys_dark else "light"}, local_only=True)
            theme = mgr.settings.get("theme")
            bar = theme == "dark" or (theme != "light" and sys_dark)
            if bar != last_bar:
                last_bar = bar
                _title_bar(window, bar)
            time.sleep(2)

    def on_shown():
        threading.Thread(target=watch_theme, daemon=True).start()

    remember = {}

    def on_resized(w, h):
        if not remember.get("max"):
            remember.update(w=w, h=h)

    def on_moved(x, y):
        if not remember.get("max"):
            remember.update(x=x, y=y)

    def on_closing():
        busy = [j for j in mgr.jobs.values() if j.state in vdl_server.LIVE]
        if busy and not window.create_confirmation_dialog(
                APP, f"{len(busy)} download{'s are' if len(busy) != 1 else ' is'} still running. Cancel and quit?"):
            return False
        mgr.update_settings({"window": {**geo, **remember}})
        mgr.bulk("cancel_all")
        _wait_for_workers(mgr)  # give workers a moment to delete their partial files
        return True

    window.events.loaded += on_loaded
    window.events.shown += on_shown
    window.events.resized += on_resized
    window.events.moved += on_moved
    window.events.maximized += lambda: remember.update(max=True)
    window.events.restored += lambda: remember.update(max=False)
    window.events.closing += on_closing
    mgr.hooks.update(dialog=dialog, focus=focus, attention=lambda: _flash(window),
                     quit=lambda: window.destroy())
    webview.start(private_mode=False, storage_path=os.path.join(vdl_cookies.APP_DIR, "webview"),
                  debug=bool(os.environ.get("VDL_DEBUG")))


def run_browser(mgr, srv):
    """No WebView2: show the same page in an Edge app window (or the default browser) and stay alive while it's open."""
    url = srv.url()
    edge = None
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base and os.path.exists(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")):
            edge = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
            break
    if edge:
        subprocess.Popen([edge, f"--app={url}", "--user-data-dir=" + os.path.join(vdl_cookies.APP_DIR, "edge")])
    else:
        import webbrowser
        webbrowser.open(url)
    stop = threading.Event()
    mgr.hooks["quit"] = stop.set
    seen, idle_since = False, None
    while not stop.wait(1):
        if mgr.clients():
            seen, idle_since = True, None
        elif seen:
            idle_since = idle_since or time.monotonic()
            busy = any(j.state in vdl_server.LIVE for j in mgr.jobs.values())
            if not busy and time.monotonic() - idle_since > 20:
                break
    mgr.bulk("cancel_all")
    _wait_for_workers(mgr)


def main():
    urls = [a for a in sys.argv[1:] if not a.startswith("-")]
    running = vdl_server.find_running()
    if running:  # hand the links to the open window instead of starting a second app
        port, token = running
        try:
            if urls:
                vdl_server.call_running(port, token, "/api/add", {"urls": urls})
            vdl_server.call_running(port, token, "/api/focus", {})
            return
        except Exception:
            pass
    _win_setup()
    mgr, srv = vdl_server.start()
    if urls:
        mgr.add_urls(urls)
    try:
        import webview  # noqa: F401
        use_window = "--browser" not in sys.argv
    except Exception:
        use_window = False
    if use_window:
        try:
            run_window(mgr, srv)
            return
        except Exception:
            pass  # WebView2 missing or broken: fall back to a browser window
    run_browser(mgr, srv)


if __name__ == "__main__":
    main()
