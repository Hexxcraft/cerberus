"""Regenerate the PWA icons in cerberus/web (needs Pillow). Run: python scripts/make_icons.py"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONT = ROOT / "assets" / "fonts" / "UnifrakturCook-Bold.ttf"


def icon(size: int) -> Image.Image:
    img = Image.new("RGB", (size, size), "#0b0709")
    d = ImageDraw.Draw(img)
    pad = size // 10
    d.rounded_rectangle((pad, pad, size - pad, size - pad), radius=size // 6, outline="#a3151f", width=max(3, size // 40), fill="#16101a")
    font = ImageFont.truetype(str(FONT), int(size * 0.62))
    d.text((size / 2, size * 0.52), "C", font=font, fill="#c9a24a", anchor="mm")
    return img


if __name__ == "__main__":
    for s in (192, 512):
        icon(s).save(ROOT / "cerberus" / "web" / f"icon-{s}.png")
        print("wrote", s)
