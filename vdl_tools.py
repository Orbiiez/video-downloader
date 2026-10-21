"""Tools for files already on disk: fix/change container, convert to H.264, extract audio, make a GIF, trim, mute.

run(tool, path, params, progress, cancel) writes a new file next to the original (never overwrites it)
and returns its path. progress(fraction) is called while ffmpeg works.
"""
import os

import vdl_core

TOOLS = {
    "remux": "Fix or change container",
    "h264": "Convert to H.264 MP4",
    "audio": "Extract audio",
    "gif": "Make a GIF",
    "trim": "Trim",
    "mute": "Remove sound",
}
QUALITY_CRF = {"high": 18, "balanced": 22, "small": 28}


def _target(path, suffix, ext, out_dir=None):
    base = os.path.splitext(os.path.basename(path))[0]
    folder = out_dir or os.path.dirname(path)
    return vdl_core.reserve_path(os.path.join(folder, vdl_core.safe_filename(f"{base}{suffix}.{ext}")))


def _time(v):
    if v in (None, ""):
        return None
    return float(v) if isinstance(v, (int, float)) else vdl_core.parse_time(str(v))


def run(tool, path, params=None, progress=None, cancel=None, out_dir=None):
    if tool not in TOOLS:
        raise ValueError(f"unknown tool: {tool}")
    if not os.path.isfile(path):
        raise RuntimeError(f"file not found: {path}")
    p = params or {}
    info = vdl_core.probe_media(path)
    if not (info["video"] or info["audio"]):
        raise RuntimeError("this doesn't look like a video or audio file")
    src_ext = os.path.splitext(path)[1].lstrip(".").lower() or "mp4"
    dur = info["duration"]
    cb = progress or (lambda f: None)

    if tool == "remux":
        ext = p.get("container") or "mp4"
        if ext not in ("mp4", "mkv", "mov", "webm"):
            raise ValueError("container must be mp4, mkv, mov or webm")
        suffix = " (fixed)" if ext == src_ext else ""
        attempts = [["-map", "0", "-c", "copy"], ["-map", "0:v?", "-map", "0:a?", "-c", "copy"]]
        flags = ["-movflags", "+faststart"] if ext in ("mp4", "mov") else []
        out = _target(path, suffix, ext, out_dir)
        return _ffmpeg_out(out, lambda tmp, a: ["-fflags", "+genpts", "-i", path, *a, *flags, tmp],
                           attempts, cancel, dur, cb)

    if tool == "h264":
        crf = QUALITY_CRF.get(p.get("quality"), 22)
        vf = []
        if p.get("max_height"):
            h = int(p["max_height"])
            vf = ["-vf", f"scale=-2:'min({h},ih)'"]
        out = _target(path, " (H.264)", "mp4", out_dir)
        args = ["-i", path, "-map", "0:v:0", "-map", "0:a?", *vf, "-c:v", "libx264", "-preset", p.get("preset", "medium"),
                "-crf", str(crf), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
        return _ffmpeg_out(out, lambda tmp, a: [*args, tmp], [[]], cancel, dur, cb)

    if tool == "audio":
        if not info["audio"]:
            raise RuntimeError("this file has no sound")
        fmt = p.get("format") or "mp3"
        if fmt not in ("mp3", "m4a", "opus", "ogg", "wav", "flac"):
            raise ValueError("unknown audio format")
        out = _target(path, "", fmt, out_dir)
        tmp = _tmp(out)
        try:
            vdl_core.extract_audio(path, tmp, fmt, str(p.get("bitrate") or "320"), cancel, cb)
            return _place(tmp, out)
        except BaseException:
            _cleanup(tmp, out)
            raise

    if tool == "gif":
        if not info["video"]:
            raise RuntimeError("this file has no picture to turn into a GIF")
        start, end = _time(p.get("start")), _time(p.get("end"))
        out = _target(path, "", "gif", out_dir)
        tmp = _tmp(out)
        try:
            vdl_core.to_gif(path, tmp, cancel, cb, fps=int(p.get("fps") or 15), width=int(p.get("width") or 480),
                            start=start, end=end)
            return _place(tmp, out)
        except BaseException:
            _cleanup(tmp, out)
            raise

    if tool == "trim":
        start, end = _time(p.get("start")) or 0, _time(p.get("end"))
        if end is not None and end <= start:
            raise ValueError("the end must be after the start")
        span = ((end if end is not None else (dur or 0)) - start) or None
        cut = ["-ss", f"{start:g}"] + (["-to", f"{end:g}"] if end is not None else [])
        out = _target(path, " (clip)", src_ext, out_dir)
        if p.get("exact", True):
            enc = ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"] \
                if src_ext in ("mp4", "mov", "m4v", "mkv") else []
            args = ["-i", path, *cut, "-map", "0:v?", "-map", "0:a?", *enc]
        else:  # fast: no re-encode, starts at the keyframe before `start`
            length = ["-t", f"{end - start:g}"] if end is not None else []
            args = ["-ss", f"{start:g}", "-i", path, *length, "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero"]
        return _ffmpeg_out(out, lambda tmp, a: [*args, tmp], [[]], cancel, span, cb)

    if tool == "mute":
        out = _target(path, " (muted)", src_ext, out_dir)
        return _ffmpeg_out(out, lambda tmp, a: ["-i", path, "-map", "0", "-map", "-0:a", "-c", "copy", tmp],
                           [[]], cancel, dur, cb)


def _tmp(out):
    base, ext = os.path.splitext(out)
    return f"{base}.vdltmp{ext}"


def _place(tmp, out):
    os.replace(tmp, out)
    vdl_core._remove(out + ".part")
    return out


def _cleanup(tmp, out):
    vdl_core._remove(tmp)
    vdl_core._remove(out + ".part")


def _ffmpeg_out(out, build, attempts, cancel, duration, cb):
    tmp = _tmp(out)
    last = None
    try:
        for a in attempts:
            try:
                vdl_core.run_ffmpeg(build(tmp, a), cancel, duration, cb)
                return _place(tmp, out)
            except vdl_core.Cancelled:
                raise
            except RuntimeError as e:
                last = e
                vdl_core._remove(tmp)
        raise last
    except BaseException:
        _cleanup(tmp, out)
        raise
