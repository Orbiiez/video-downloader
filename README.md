# Video Downloader

A small Windows desktop app (tkinter) for downloading videos from URLs, with parallel downloads.

- **Engine** (`vdl_core.py`): tries a direct HTTP download first, then yt-dlp, then falls back to scraping the page HTML.
- **Saved login** (`vdl_cookies.py`): cookies are encrypted with Windows DPAPI and stored in `%APPDATA%\VideoDownloader\login.bin`. Each site gets its own temporary cookie file, and saved logins expire after 30 days. None of this is kept in the repo folder.
- **GUI**: `Video Downloader.pyw`. Settings are saved to `%APPDATA%\VideoDownloader\settings.json`.
- **CLI**: `video_dl.py <url> [-o folder]` (or `video_dl.bat`).

## Setup

```
pip install -r requirements.txt
```

YouTube downloads need Node.js installed, because yt-dlp uses it as the JavaScript runtime. ffmpeg comes bundled through `imageio-ffmpeg`.

Run the app with `pythonw "Video Downloader.pyw"`. Downloads go to `~/Downloads` by default.
