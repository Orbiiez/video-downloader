# Video Downloader

A Windows app for saving videos, music and photo posts from links: paste a link (or several) and they download in parallel. It works with YouTube, TikTok, Instagram, X, Reddit, Twitch, Bluesky and about 1800 other sites, plus direct file links.

## Download

1. Download `VideoDownloader-<version>-win64.zip` from the [latest release](../../releases/latest).
2. Extract it anywhere.
3. Run `Video Downloader.exe`.

Nothing else is needed: Python, ffmpeg and the YouTube JavaScript runtime are bundled. Windows SmartScreen may warn about an unrecognized app the first time; click **More info → Run anyway**.

## What it does

- **Paste and go.** Copy a link and press Ctrl+V anywhere in the window, drop links onto it, or turn on *Offer links I copy* to get a one-click Download whenever you copy a video link.
- **Video, Audio or Mute.** One switch picks a video with sound, just the audio (MP3, M4A, Opus, OGG, WAV, FLAC, or the original with no conversion) or a silent video.
- **Formats that play everywhere.** H.264 MP4 by default, or AV1/VP9 for 4K and 8K. Quality from 144p to 8K, MP4/MKV/WebM.
- **Clips.** Download only part of a video, e.g. a few minutes of a long Twitch stream.
- **Photo posts and playlists.** Pictures from Instagram, X, Bluesky, Reddit, Tumblr and more. Posts and playlists with several items open a picker so you choose what to keep.
- **Extras.** Subtitles (inside the video or as .srt), dubbed YouTube audio tracks, TikTok's full original sound, X GIFs as real .gif files, title/artist tags and cover art, SponsorBlock, file name styles, speed limit and proxy.
- **Tools** for files you already have: fix or change the container, convert to H.264 for editors and phones, extract audio, make a GIF, trim, remove sound.
- **History** of finished downloads, with search and one-click re-download.
- **Phone link.** Turn it on, scan the QR code, and use the app from your phone on the same Wi-Fi: send links to download on the PC and save finished files to the phone.
- **Logins** for private or members-only videos, from your browser or an encrypted saved login.

If a site stops working, open **Settings → About → Update download engines**: sites change often and the engines are updated to keep up.

## How it works

- **Engine** (`vdl_core.py`): direct file links are streamed straight to disk; video sites go through yt-dlp; photo posts and galleries through gallery-dl (also the fallback when yt-dlp finds no video); as a last resort the page is scanned for video links. Each download works in a hidden folder and only finished files are moved out.
- **Tools** (`vdl_tools.py`): ffmpeg jobs that always write a new file next to the original.
- **Back end** (`vdl_server.py`): the queue, settings, history and a small local web server. The window talks to it over a JSON API with live progress via Server-Sent Events. It only listens on 127.0.0.1 and every request needs a random token. The phone link is a separate listener on your network with its own token and fewer rights (it can't change settings, read logins or open files on the PC).
- **Window** (`Video Downloader.pyw` + `ui/`): a native window (pywebview on Microsoft Edge WebView2). Without WebView2 the same page opens in an Edge app window instead.
- **Saved login** (`vdl_cookies.py`): cookies are encrypted with Windows DPAPI and stored in `%APPDATA%\VideoDownloader\login.bin`. Each site gets its own temporary cookie file, and saved logins expire after 30 days. Most downloads don't need a login at all.
- Settings and history are in `%APPDATA%\VideoDownloader\`.
- **CLI**: `video_dl.py <url> [-o folder] [-q 1080] [--codec h264] [--audio mp3] [--mute] [--subs en] [--clip 1:00-2:30]` (or `video_dl.bat`), when run from source. `python video_dl.py -h` lists everything.

## Running from source

```
pip install -r requirements.txt
pythonw "Video Downloader.pyw"
```

YouTube needs Node.js or Deno installed when running from source. The app also runs on macOS and Linux from source; saved logins are Windows-only there, but *Read from browser* works.

## Third-party software in the release

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) and yt-dlp-ejs: Unlicense
- [gallery-dl](https://github.com/mikf/gallery-dl): GPL-2.0
- [pywebview](https://github.com/r0x0r/pywebview): BSD-3-Clause; [pythonnet](https://github.com/pythonnet/pythonnet): MIT; Microsoft Edge WebView2 SDK
- [segno](https://github.com/heuer/segno) (QR codes): BSD-3-Clause
- [FFmpeg](https://ffmpeg.org): GPL build from [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg); source at https://ffmpeg.org/download.html
- [Node.js](https://nodejs.org): MIT
- Python: PSF License
