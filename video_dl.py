"""Command-line video downloader (the app is "Video Downloader.pyw").

Usage:
    python video_dl.py <url> [<url> ...] [-o OUTPUT_DIR] [-q 1080] [--codec h264] [--audio mp3] [--mute]
                       [--subs en] [--clip 1:00-2:30] [--cookies cookies.txt]
    python video_dl.py              (prompts for the URL)
"""
import argparse
import sys

import vdl_core


def progress(d):
    st = d["status"]
    if st == "downloading" and d.get("downloaded"):
        done, total = d["downloaded"], d.get("total")
        item = f"[{d['item']}/{d['items']}] " if d.get("items") else ""
        if total:
            print(f"\r  {item}{done / total:6.1%}  {done / 1e6:.1f} / {total / 1e6:.1f} MB   ", end="", flush=True)
        else:
            print(f"\r  {item}{done / 1e6:.1f} MB   ", end="", flush=True)
    elif st == "info" and d.get("title"):
        count = f" ({d['count']} items)" if d.get("count") and d["count"] > 1 else ""
        print(f"  {d['title']}{count}")
    elif st == "processing":
        print(f"\n  {d.get('step') or 'Processing'}...")


def clip_range(text):
    start, _, end = text.partition("-")
    try:
        return [vdl_core.parse_time(start) or 0, vdl_core.parse_time(end)]
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def main():
    ap = argparse.ArgumentParser(description="Download the video, audio or pictures a URL points to.")
    ap.add_argument("urls", nargs="*", help="video/post page or direct file URLs")
    ap.add_argument("-o", "--out", default=vdl_core.DEFAULT_OUT, help=f"output folder (default: {vdl_core.DEFAULT_OUT})")
    ap.add_argument("-q", "--quality", default="1080", metavar="Q",
                    help="tallest video to get: max, 4320, 2160, 1440, 1080 (default), 720, 480, 360, 240, 144")
    ap.add_argument("--codec", default="h264", choices=vdl_core.CHOICES["codec"],
                    help="h264 plays everywhere (default); av1/vp9 reach higher quality; any = best of all")
    ap.add_argument("--container", default="auto", choices=vdl_core.CHOICES["container"])
    ap.add_argument("--audio", metavar="FORMAT", choices=vdl_core.CHOICES["audio_format"],
                    help="audio only, as best (no conversion), mp3, m4a, opus, ogg, wav or flac")
    ap.add_argument("--bitrate", default="320", choices=vdl_core.CHOICES["audio_bitrate"], help="kbps for --audio")
    ap.add_argument("--mute", action="store_true", help="video without sound")
    ap.add_argument("--subs", metavar="LANG", default="", help="add subtitles in this language, e.g. en")
    ap.add_argument("--clip", type=clip_range, metavar="START-END", help="only this part, e.g. 1:00-2:30 or 90-")
    ap.add_argument("--filename", default="title", choices=vdl_core.CHOICES["filename"], help="file name style")
    ap.add_argument("--sponsorblock", action="store_true", help="cut sponsor segments out of YouTube videos")
    ap.add_argument("--first", action="store_true", help="for posts/playlists with several items, only the first")
    ap.add_argument("--proxy", default="", help="e.g. socks5://127.0.0.1:9050")
    ap.add_argument("-c", "--cookies-from-browser", metavar="BROWSER", choices=vdl_core.BROWSERS,
                    help="use your logged-in session from a browser (for login-only videos)")
    ap.add_argument("--cookies", metavar="FILE",
                    help="cookies.txt exported from your browser (only the downloaded site's cookies are used)")
    ap.add_argument("--saved-login", action="store_true",
                    help="use the encrypted login imported in the Video Downloader app")
    args = ap.parse_args()

    urls = args.urls
    if not urls:
        try:
            urls = [input("URL: ")]
        except EOFError:
            urls = []
    urls = [u.strip() for u in urls if u.strip()]
    if not urls:
        sys.exit("No URL given.")

    opts = {"out_dir": args.out, "quality": args.quality.removesuffix("p") if args.quality not in vdl_core.LEGACY_QUALITY
            else args.quality, "codec": args.codec, "container": args.container, "subtitles": args.subs,
            "clip": args.clip, "filename": args.filename, "sponsorblock": args.sponsorblock,
            "multi": "first" if args.first else "all", "proxy": args.proxy, "audio_bitrate": args.bitrate}
    if args.audio:
        opts.update(mode="audio", audio_format=args.audio)
    elif args.mute:
        opts["mode"] = "mute"
    login = vdl_core.Login(args.cookies_from_browser, args.cookies, args.saved_login)

    failed = 0
    for url in urls:
        print(f"> {url}")
        try:
            paths = vdl_core.download(url, opts, login=login, progress=progress, log=lambda m: None)
            print()
            for p in paths:
                print(f"Saved: {p}")
        except KeyboardInterrupt:
            sys.exit("\nCancelled.")
        except Exception as e:
            failed += 1
            print(f"\nFailed: {vdl_core.clean_error(e)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
