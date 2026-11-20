"""Draw Negative's logo: one round photo (sun over hills), half positive, half negative, split on the diagonal.
Original artwork drawn by this script (Pillow only), no third-party image. Writes favicon.png (64),
icon-192.png, icon-512.png and docs/logo/negative-512.png in the repo.
Usage: python3 tools/make_logo.py"""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
S = 2048  # drawn large, scaled down (smooth edges)
SKY, SUN, HILL1, HILL2 = (191, 227, 255), (245, 197, 66), (76, 175, 80), (46, 125, 50)
INK = (20, 20, 20)


def neg(c):
    return tuple(255 - v for v in c)


def scene(positive):
    f = (lambda c: c) if positive else neg
    im = Image.new("RGB", (S, S), f(SKY))
    d = ImageDraw.Draw(im)
    r = int(S * 0.16)
    d.ellipse((int(S * 0.66) - r, int(S * 0.30) - r, int(S * 0.66) + r, int(S * 0.30) + r), fill=f(SUN))
    d.ellipse((int(-S * 0.35), int(S * 0.58), int(S * 0.85), int(S * 1.6)), fill=f(HILL2))
    d.ellipse((int(S * 0.15), int(S * 0.66), int(S * 1.45), int(S * 1.7)), fill=f(HILL1))
    return im


def logo():
    half = Image.new("L", (S, S), 0)
    ImageDraw.Draw(half).polygon([(S, 0), (S, S), (0, S)], fill=255)  # bottom-right half is the negative
    pic = Image.composite(scene(False), scene(True), half)
    ImageDraw.Draw(pic).line([(S, 0), (0, S)], fill=INK, width=36)
    circle = Image.new("L", (S, S), 0)
    ImageDraw.Draw(circle).ellipse((64, 64, S - 64, S - 64), fill=255)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(pic, (0, 0), circle)
    ImageDraw.Draw(out).ellipse((64, 64, S - 64, S - 64), outline=INK + (255,), width=48)
    return out


if __name__ == "__main__":
    im = logo()
    (ROOT / "docs" / "logo").mkdir(parents=True, exist_ok=True)
    for name, px in (("favicon.png", 64), ("icon-192.png", 192), ("icon-512.png", 512), ("docs/logo/negative-512.png", 512)):
        im.resize((px, px), Image.LANCZOS).save(ROOT / name, optimize=True)
        print(name, px)
