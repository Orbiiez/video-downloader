# Video Downloader

A small Windows desktop app for downloading videos from URLs: paste links and they download in parallel. It works with YouTube, Twitter/X, Reddit, TikTok, and about 1800 other sites, plus direct video links.

## Download

1. Download `VideoDownloader-<version>-win64.zip` from the [latest release](../../releases/latest).
2. Extract it anywhere.
3. Run `Video Downloader.exe`.

Nothing else is needed: Python, ffmpeg and the YouTube JavaScript runtime are bundled. Windows SmartScreen may warn about an unrecognized app the first time; click **More info → Run anyway**.

Downloads go to your `Downloads` folder by default. If a site stops working, click **Update downloader** in the app to fetch the newest yt-dlp engine.

## How it works

- **Engine** (`vdl_core.py`): tries a direct HTTP download first, then yt-dlp, then falls back to scraping the page HTML.
- **Saved login** (`vdl_cookies.py`): cookies are encrypted with Windows DPAPI and stored in `%APPDATA%\VideoDownloader\login.bin`. Each site gets its own temporary cookie file, and saved logins expire after 30 days. None of this is kept in the app folder.
- **GUI**: `Video Downloader.pyw`. Settings are saved to `%APPDATA%\VideoDownloader\settings.json`.
- **CLI**: `video_dl.py <url> [-o folder]` (or `video_dl.bat`), when run from source.

## Running from source

```
pip install -r requirements.txt
pythonw "Video Downloader.pyw"
```

YouTube needs Node.js or Deno installed when running from source.

## Building a release

```
pip install -r requirements.txt pyinstaller
python build.py 1.0.0
```

This needs Node.js installed, because `node.exe` gets bundled. The output is `dist/VideoDownloader-1.0.0-win64.zip`.

## Third-party software in the release

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) and yt-dlp-ejs: Unlicense
- [FFmpeg](https://ffmpeg.org): GPL build from [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg); source at https://ffmpeg.org/download.html
- [Node.js](https://nodejs.org): MIT
- [Sun Valley ttk theme](https://github.com/rdbende/Sun-Valley-ttk-theme): MIT
- Python: PSF License
