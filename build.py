"""Build the standalone Windows release: dist/Video Downloader/ and dist/VideoDownloader-<version>-win64.zip.

Usage: python build.py      (the version comes from version.py)
Needs (on the build machine only): pip install -r requirements.txt pyinstaller, and Node.js installed.
"""
import os
import shutil
import subprocess
import sys

import imageio_ffmpeg

from version import VERSION

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "Video Downloader"


def main():
    version = VERSION
    node = shutil.which("node")
    if not node:
        sys.exit("Node.js not found on PATH; it is bundled so YouTube works without it on users' PCs.")
    bin_dir = os.path.join(HERE, "build", "bin")
    shutil.rmtree(bin_dir, ignore_errors=True)
    os.makedirs(bin_dir)
    shutil.copy(imageio_ffmpeg.get_ffmpeg_exe(), os.path.join(bin_dir, "ffmpeg.exe"))
    shutil.copy(node, os.path.join(bin_dir, "node.exe"))

    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onedir",
        "--name", NAME,
        "--icon", os.path.join(HERE, "assets", "icon.ico"),
        "--add-data", f"{bin_dir}{os.pathsep}bin",
        "--add-data", f"{os.path.join(HERE, 'assets')}{os.pathsep}assets",
        "--collect-all", "yt_dlp", "--collect-all", "yt_dlp_ejs",
        "--exclude-module", "imageio_ffmpeg", "--exclude-module", "numpy",
        os.path.join(HERE, f"{NAME}.pyw"),
    ], cwd=HERE, check=True)

    app_dir = os.path.join(HERE, "dist", NAME)
    shutil.copy(os.path.join(HERE, "README.md"), app_dir)
    archive = os.path.join(HERE, "dist", f"VideoDownloader-{version}-win64")
    shutil.make_archive(archive, "zip", os.path.join(HERE, "dist"), NAME)
    print("Built", archive + ".zip")


if __name__ == "__main__":
    main()
