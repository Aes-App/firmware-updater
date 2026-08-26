"""Generate the AesApp branding assets for the GUI/app from a source logo.

Run this from the python/ directory (needs Pillow + macOS iconutil):

    ./.venv/bin/python make_assets.py                 # uses the default source
    ./.venv/bin/python make_assets.py /path/to/logo.png [--bg '#ffffff']

Writes:
    bt_ota/assets/aesapp_logo.png   in-app header logo (fits height, keeps aspect)
    bt_ota/assets/AesApp.icns       app icon (contain onto a square, macOS only)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from PIL import Image, ImageChops, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "bt_ota", "assets")
DEFAULT_SRC = "/Users/shiji/Documents/Archive/AesApp/AesApp定稿源文件/AesApp1-1.jpg"

LOGO_HEIGHT = 140         # disclaimer / about logo height (px, rendered LANCZOS)
LOGO_HEIGHT_SM = 56       # main-window header logo height (px)
LOGO_PAD = 0.13           # white padding around the wordmark (fraction of its height)
ICON_MASTER = 1024        # icns master size
# The app icon is the distinctive mountain-"A" mark: the top-left of the trimmed
# wordmark. Fractions tuned for the AesApp Communications logo.
ICON_MARK_WFRAC = 0.17
ICON_MARK_HFRAC = 0.80
ICON_PAD = 0.14


def _parse_bg(s: str):
    s = s.strip().lstrip("#")
    if len(s) == 6:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4)) + (255,)
    if s.lower() in ("none", "transparent"):
        return (0, 0, 0, 0)
    raise ValueError(f"bad --bg colour: {s}")


def autotrim(im: Image.Image, border_rgb=(255, 255, 255), thresh: int = 12, pad_frac: float = 0.0) -> Image.Image:
    """Crop uniform (near-white) borders around the logo, leaving a little padding."""
    rgb = im.convert("RGB")
    bg = Image.new("RGB", rgb.size, border_rgb)
    diff = ImageChops.difference(rgb, bg).convert("L").point(lambda p: 255 if p > thresh else 0)
    bbox = diff.getbbox()
    if not bbox:
        return im
    pad = round(min(im.width, im.height) * pad_frac)
    l, t, r, b = bbox
    return im.crop((max(0, l - pad), max(0, t - pad), min(im.width, r + pad), min(im.height, b + pad)))


def fit_height(im: Image.Image, h: int) -> Image.Image:
    w = max(1, round(im.width * h / im.height))
    return im.resize((w, h), Image.LANCZOS)


def add_margin(im: Image.Image, frac: float, bg=(255, 255, 255, 255)) -> Image.Image:
    """Add a uniform `frac`-of-height white margin around the (opaque) logo."""
    im = im.convert("RGBA")
    m = round(im.height * frac)
    out = Image.new("RGBA", (im.width + 2 * m, im.height + 2 * m), bg)
    out.paste(im, (m, m))
    return out


def to_icon(mark: Image.Image, size: int, bg, pad_frac: float = ICON_PAD) -> Image.Image:
    """Center `mark` on a square canvas with padding around it."""
    inner = max(1, round(size * (1 - 2 * pad_frac)))
    m = mark.copy()
    m.thumbnail((inner, inner), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), bg)
    canvas.paste(m, ((size - m.width) // 2, (size - m.height) // 2),
                 m if m.mode == "RGBA" else None)
    return canvas


def crop_mark(im: Image.Image) -> Image.Image:
    """Top-left mountain-'A' mark of the trimmed wordmark."""
    w, h = im.size
    return im.crop((0, 0, int(w * ICON_MARK_WFRAC), int(h * ICON_MARK_HFRAC)))


def round_corners(im: Image.Image, side: int, radius_frac: float, margin_frac: float = 0.06) -> Image.Image:
    """Square RGBA icon of `side`px: the (square) artwork inset by a transparent
    margin, with rounded (transparent) corners. `radius_frac` is the corner
    radius as a fraction of the artwork size (macOS ≈ 0.2237)."""
    im = im.convert("RGBA")
    s0 = max(im.size)
    art = Image.new("RGBA", (s0, s0), (0, 0, 0, 0))
    art.paste(im, ((s0 - im.width) // 2, (s0 - im.height) // 2))
    m = int(side * margin_frac)
    art_size = side - 2 * m
    art = art.resize((art_size, art_size), Image.LANCZOS)
    mask = Image.new("L", (art_size, art_size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, art_size - 1, art_size - 1), radius=int(art_size * radius_frac), fill=255)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(art, (m, m), mask)
    return canvas


def make(src: str, bg=(255, 255, 255, 255), icon_full: bool = False,
         icon_src: str | None = None, margin: float = 0.06) -> None:
    if not os.path.isfile(src):
        raise SystemExit(f"source logo not found: {src}\n"
                         "Pass the path explicitly: make_assets.py /path/to/logo")
    os.makedirs(ASSETS, exist_ok=True)
    im = Image.open(src).convert("RGBA")
    print(f"source: {src}  {im.size}")
    trimmed = autotrim(im)
    print(f"trimmed: {trimmed.size}")

    # In-app logo: add ~13% white padding, then render at two sizes with LANCZOS
    # (no runtime subsample -> no jaggies). Rendered from the full-res crop.
    padded = add_margin(trimmed, LOGO_PAD)
    fit_height(padded, LOGO_HEIGHT).save(os.path.join(ASSETS, "aesapp_logo.png"))
    fit_height(padded, LOGO_HEIGHT_SM).save(os.path.join(ASSETS, "aesapp_logo_sm.png"))
    print(f"wrote aesapp_logo.png ({LOGO_HEIGHT}px) + aesapp_logo_sm.png ({LOGO_HEIGHT_SM}px)")

    if icon_src:
        if not os.path.isfile(icon_src):
            raise SystemExit(f"--icon-src not found: {icon_src}")
        icon = round_corners(Image.open(icon_src), ICON_MASTER, 0.2237, margin)
        print(f"icon: {icon_src} (rounded corners, margin {margin})")
    else:
        mark = trimmed if icon_full else crop_mark(trimmed)
        icon = to_icon(mark, ICON_MASTER, bg)
    # Windows .ico (multi-resolution) — generated on any OS
    icon.save(os.path.join(ASSETS, "AesApp.ico"),
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"wrote {os.path.join(ASSETS, 'AesApp.ico')}")

    if sys.platform == "darwin":
        iconset = os.path.join(ASSETS, "AesApp.iconset")
        os.makedirs(iconset, exist_ok=True)
        for s in (16, 32, 128, 256, 512):
            icon.resize((s, s), Image.LANCZOS).save(os.path.join(iconset, f"icon_{s}x{s}.png"))
            icon.resize((s * 2, s * 2), Image.LANCZOS).save(os.path.join(iconset, f"icon_{s}x{s}@2x.png"))
        icns = os.path.join(ASSETS, "AesApp.icns")
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns], check=True)
        print(f"wrote {icns}")
    else:
        icon.save(os.path.join(ASSETS, "AesApp.png"))
        print("(non-macOS: wrote AesApp.png instead of .icns)")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("src", nargs="?", default=DEFAULT_SRC, help="source logo image")
    p.add_argument("--bg", default="#ffffff", help="icon background colour (hex or 'none')")
    p.add_argument("--icon-full", action="store_true",
                   help="use the full wordmark as the icon instead of the mountain-'A' mark")
    p.add_argument("--icon-src", help="use this pre-made square image as the app icon "
                                      "(rounded transparent corners are added)")
    p.add_argument("--margin", type=float, default=0.06, help="icon transparent margin fraction")
    args = p.parse_args(argv)
    make(args.src, _parse_bg(args.bg), icon_full=args.icon_full,
         icon_src=args.icon_src, margin=args.margin)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
