"""Conversions. Every operation writes a NEW file next to the original; the original is never opened for writing,
overwritten, renamed or removed. Output goes to a temp file in DATA_DIR first and is linked into place only when complete."""
import json
import os
import re
import shutil
import subprocess
import secrets
from pathlib import Path

from PIL import Image, ImageOps

IMG_RESIZABLE = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp", ".bmp", ".tif", ".tiff"}
HEIC = {".heic", ".heif"}
OLD_VIDEO = {".mov", ".avi", ".flv", ".wmv"}
MP4 = {".mp4", ".m4v"}
MAX_OUT_PIXELS = 100_000_000  # a resize may not produce more than 100 MP (about 300 MB as RGB): the container has 2 GB
FF_SAFE = ["-protocol_whitelist", "file,pipe"]  # ffmpeg may only open the file itself (no URLs, no playlists)

# id -> (label, source extensions, output extension, parameters it asks for)
OPS = {
    "heic_png": ("HEIC to PNG", HEIC, ".png", ()),
    "heic_jpg": ("HEIC to JPG", HEIC, ".jpg", ("quality",)),
    "png_jpg": ("PNG to JPG", {".png"}, ".jpg", ("quality",)),
    "bmp_png": ("BMP to PNG", {".bmp"}, ".png", ()),
    "img_pct": ("Resize image by %", IMG_RESIZABLE, None, ("percent",)),
    "img_px": ("Resize image to pixels", IMG_RESIZABLE, None, ("width", "height")),
    "vid_mp4": ("Convert to MP4", OLD_VIDEO, ".mp4", ()),
    "mp4_pct": ("Resize MP4 by %", MP4, ".mp4", ("percent",)),
    "mp4_px": ("Resize MP4 to pixels", MP4, ".mp4", ("width", "height")),
}


def ops_for(ext: str):
    return [dict(op=k, label=v[0], params=list(v[3])) for k, v in OPS.items() if ext.lower() in v[1]]


class BadRequest(ValueError):
    pass


def _int(v, lo, hi, name):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise BadRequest(f"{name} must be a whole number")
    if not lo <= n <= hi:
        raise BadRequest(f"{name} must be between {lo} and {hi}")
    return n


def check(op, ext, p, video):
    """Validate and normalise parameters. Raises BadRequest."""
    if op not in OPS or ext.lower() not in OPS[op][1]:
        raise BadRequest("That conversion does not apply to this file")
    out = {}
    need = OPS[op][3]
    if "quality" in need:
        out["quality"] = _int(p.get("quality", 85), 1, 100, "Quality")
    if "percent" in need:
        out["percent"] = _int(p.get("percent"), 10 if video else 1, 100 if video else 400, "Percent")
    if "width" in need:
        lim = (64, 7680) if video else (1, 20000)
        w, h = p.get("width") or None, p.get("height") or None
        if not w and not h:
            raise BadRequest("Give a width, a height, or both")
        out["width"] = _int(w, *lim, "Width") if w else None
        out["height"] = _int(h, *lim, "Height") if h else None
    return out


def _suffix(op, p):
    if "percent" in p:
        return f" ({p['percent']}%)"
    if "width" in p:
        return f" ({p['width'] or ''}x{p['height'] or ''})"
    return ""


def _publish(tmp: Path, src: Path, suffix: str, ext: str) -> Path:
    """Put the finished temp file beside src under a name that does not exist yet (never overwrites)."""
    part = src.parent / f".{secrets.token_hex(4)}.part"
    shutil.copyfile(tmp, part)
    try:
        for n in range(0, 1000):
            final = src.parent / (src.stem + suffix + ("" if n == 0 else f" {n}") + ext)
            try:
                os.link(part, final)  # fails if the name is taken: atomic no-overwrite
            except FileExistsError:
                continue
            st = src.stat()
            os.utime(final, (st.st_atime, st.st_mtime))  # keep the photo's date; only the NEW file is touched
            return final
        raise OSError("could not find a free file name")
    finally:
        part.unlink(missing_ok=True)


def convert_image(src: Path, op: str, p: dict, work: Path) -> Path:
    """EXIF, the ICC profile and XMP are carried into every output format that can hold them; after a resize the EXIF
    pixel dimensions are set to the new size."""
    ext_out = OPS[op][2] or (".jpg" if src.suffix.lower() in {".jpg", ".jpeg", ".jpe", ".jfif"} else src.suffix.lower())
    if ext_out == ".jpeg":
        ext_out = ".jpg"
    with Image.open(src) as im:
        xmp = im.info.get("xmp") or im.info.get("XML:com.adobe.xmp")
        if isinstance(xmp, str):
            xmp = xmp.encode()
        w0, h0 = (im.height, im.width) if im.getexif().get(0x0112, 1) in (5, 6, 7, 8) else im.size  # as shown
        if "percent" in p:
            size = (max(1, round(w0 * p["percent"] / 100)), max(1, round(h0 * p["percent"] / 100)))
        elif "width" in p:
            r = min((p["width"] or 10**9) / w0, (p["height"] or 10**9) / h0)
            size = (max(1, round(w0 * r)), max(1, round(h0 * r)))
        else:
            size = (w0, h0)
        if size[0] * size[1] > MAX_OUT_PIXELS:
            raise BadRequest(f"The result would be {size[0]} x {size[1]} px; the limit is {MAX_OUT_PIXELS // 1_000_000} MP")
        exif, icc = Image.Exif(), im.info.get("icc_profile")
        exif.load(im.getexif().tobytes())  # read from the source: a TIFF's tags do not follow a transposed copy
        exif.pop(0x0112, None)  # orientation is baked into the pixels below, so it cannot be applied twice
        im = ImageOps.exif_transpose(im)
        if size != im.size:
            im = im.resize(size, Image.LANCZOS)
            sub = exif.get_ifd(0x8769)
            if 0xA002 in sub or 0xA003 in sub:
                sub[0xA002], sub[0xA003] = size  # PixelXDimension / PixelYDimension follow the new size
        tmp = work / f"{secrets.token_hex(6)}{ext_out}"
        kw = {"exif": exif.tobytes()} if len(exif) else {}
        if icc:
            kw["icc_profile"] = icc
        if xmp:
            kw["xmp"] = xmp
        if ext_out == ".jpg":
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.getchannel("A"))
                im = bg
            im.convert("RGB").save(tmp, "JPEG", quality=p.get("quality", 92), optimize=True, **kw)
        elif ext_out == ".png":
            from PIL import PngImagePlugin
            info = PngImagePlugin.PngInfo()
            if xmp:
                info.add_itxt("XML:com.adobe.xmp", xmp.decode("utf-8", "replace"))
            im.save(tmp, "PNG", optimize=True, pnginfo=info, **{k: v for k, v in kw.items() if k != "xmp"})
        elif ext_out == ".webp":
            im.save(tmp, "WEBP", quality=92, **kw)
        elif ext_out in (".tif", ".tiff"):
            im.save(tmp, "TIFF", **kw)
        else:  # bmp holds no metadata at all; it is kept as it was (only pixels)
            im.save(tmp)
    try:
        return _publish(tmp, src, _suffix(op, p) if OPS[op][2] is None else "", ext_out)
    finally:
        tmp.unlink(missing_ok=True)


def _duration(src: Path) -> float:
    try:
        out = subprocess.run(["ffprobe", *FF_SAFE, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(src)],
                             capture_output=True, timeout=15).stdout
        return float(json.loads(out)["format"]["duration"])
    except Exception:
        return 0.0


def convert_video(src: Path, op: str, p: dict, work: Path, progress) -> Path:
    tmp = work / f"{secrets.token_hex(6)}.mp4"
    cmd = ["nice", "-n", "10", "ffmpeg", *FF_SAFE, "-v", "error", "-nostdin", "-y", "-i", str(src), "-map", "0:v:0",
           "-map", "0:a:0?", "-map_metadata", "0"]
    if "percent" in p:
        cmd += ["-vf", f"scale=trunc(iw*{p['percent']}/200)*2:trunc(ih*{p['percent']}/200)*2"]
    elif "width" in p:
        cmd += ["-vf", f"scale={p['width'] or 99999}:{p['height'] or 99999}:force_original_aspect_ratio=decrease:force_divisible_by=2"]
    # use_metadata_tags: the mp4 muxer otherwise drops Apple's QuickTime keys (location, make, model, creation date)
    cmd += ["-c:v", "libx264", "-crf", "22", "-preset", "fast", "-pix_fmt", "yuv420p", "-threads", "4", "-c:a", "aac",
            "-b:a", "160k", "-movflags", "+faststart+use_metadata_tags", "-progress", "pipe:1", str(tmp)]
    total = _duration(src)
    pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for line in pr.stdout:
            m = re.match(r"out_time_us=(\d+)", line)
            if m and total:
                progress(min(99, int(int(m.group(1)) / 1e6 / total * 100)))
        err = pr.stderr.read()
        if pr.wait() != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            raise OSError("ffmpeg failed: " + err.strip()[-300:])
        return _publish(tmp, src, _suffix(op, p) if op != "vid_mp4" else "", ".mp4")
    finally:
        if pr.poll() is None:
            pr.kill()
        tmp.unlink(missing_ok=True)
