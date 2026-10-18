"""Command-line video downloader (the app is "Video Downloader.pyw").

Usage:
    python video_dl.py <url> [<url> ...] [-o OUTPUT_DIR] [-q 720p] [--cookies cookies.txt]
    python video_dl.py              (prompts for the URL)
"""
import argparse
import sys

import vdl_core


def progress(d):
    if d["status"] == "downloading" and d.get("downloaded"):
        done, total = d["downloaded"], d.get("total")
        if total:
            print(f"\r  {done / total:6.1%}  {done / 1e6:.1f} / {total / 1e6:.1f} MB   ", end="", flush=True)
        else:
            print(f"\r  {done / 1e6:.1f} MB   ", end="", flush=True)
    elif d["status"] == "finished":
        print()
    elif d["status"] == "processing":
        print(f"  {d.get('step') or 'Processing'}...")


def main():
    ap = argparse.ArgumentParser(description="Download the video a URL points to.")
    ap.add_argument("urls", nargs="*", help="video page or direct file URLs")
    ap.add_argument("-o", "--out", default=vdl_core.DEFAULT_OUT, help=f"output folder (default: {vdl_core.DEFAULT_OUT})")
    ap.add_argument("-q", "--quality", default="Best available", choices=list(vdl_core.QUALITIES))
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
            urls = [input("Video URL: ")]
        except EOFError:
            urls = []
    urls = [u.strip() for u in urls if u.strip()]
    if not urls:
        sys.exit("No URL given.")

    failed = 0
    for url in urls:
        print(f"> {url}")
        try:
            paths = vdl_core.download(url, args.out, args.quality, args.cookies_from_browser,
                                      progress=progress, log=lambda m: print("  " + m), cookies_file=args.cookies,
                                      use_saved_login=args.saved_login)
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
