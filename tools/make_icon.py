#!/usr/bin/env python3
"""
Generate app.ico for the dashboard / installer.

Draws a life-ring (rescue) with a green up-arrow (restore) on a dark rounded
tile, matching the dashboard's colours. Saves a multi-resolution .ico.

    python tools/make_icon.py   ->  app.ico
"""
from __future__ import annotations
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ensure_pillow():
    try:
        import PIL  # noqa: F401
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pillow"])


def main():
    _ensure_pillow()
    from PIL import Image, ImageDraw

    S = 256
    SS = S * 4                                    # supersample for smooth edges
    img = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    bg = (11, 18, 32, 255)
    cyan = (56, 189, 248, 255)
    white = (241, 245, 249, 255)
    green = (34, 197, 94, 255)

    def sc(v):
        return int(v * SS / S)

    # rounded tile background
    pad = sc(6)
    d.rounded_rectangle([pad, pad, SS - pad, SS - pad], radius=sc(52), fill=bg)

    cx = cy = SS // 2
    R, T = sc(92), sc(36)
    seg = sc(30)

    d.ellipse([cx - R, cy - R, cx + R, cy + R], fill=cyan)        # outer ring disk
    d.rectangle([cx - seg // 2, cy - R, cx + seg // 2, cy + R], fill=white)   # vertical band
    d.rectangle([cx - R, cy - seg // 2, cx + R, cy + seg // 2], fill=white)   # horizontal band
    r = R - T
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=bg)         # punch the hole

    # restore arrow (up) in the hole
    aw, ah, sw = sc(24), sc(30), sc(10)
    d.polygon([(cx, cy - ah), (cx - aw, cy + sc(2)), (cx - sw, cy + sc(2)),
               (cx - sw, cy + ah), (cx + sw, cy + ah), (cx + sw, cy + sc(2)),
               (cx + aw, cy + sc(2))], fill=green)

    img = img.resize((S, S), Image.LANCZOS)
    out = os.path.join(ROOT, "app.ico")
    img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                         (64, 64), (128, 128), (256, 256)])
    print(f"[OK] wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
