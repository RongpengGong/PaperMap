"""Draw the PaperMap icon (a small knowledge graph) with Pillow.

Writes src/papermap/web/static/icon.png (app/tray/favicon) and, at build time,
build/PaperMap.icns (macOS, via iconutil) and build/PaperMap.ico (Windows).
"""

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
NODES = [(0.30, 0.34, 0.085), (0.68, 0.28, 0.07), (0.70, 0.66, 0.095), (0.33, 0.70, 0.065), (0.50, 0.50, 0.11)]
EDGES = [(4, 0), (4, 1), (4, 2), (4, 3), (0, 3), (1, 2)]


def draw(size: int, background: bool = True) -> Image.Image:
    s = size * 4  # supersample for smooth edges
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if background:
        d.rounded_rectangle([s * 0.04, s * 0.04, s * 0.96, s * 0.96], radius=s * 0.22, fill=(47, 111, 222, 255))
        node, edge, accent = (255, 255, 255, 255), (255, 255, 255, 170), (255, 196, 120, 255)
    else:  # monochrome template for the macOS menu bar
        node = edge = accent = (0, 0, 0, 255)
    for a, b in EDGES:
        (x1, y1, _), (x2, y2, _) = NODES[a], NODES[b]
        d.line([x1 * s, y1 * s, x2 * s, y2 * s], fill=edge, width=int(s * (0.03 if background else 0.06)))
    for i, (x, y, r) in enumerate(NODES):
        r = r * (1.0 if background else 1.25)
        d.ellipse([(x - r) * s, (y - r) * s, (x + r) * s, (y + r) * s], fill=accent if i == 2 else node)
    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    static = ROOT / "src" / "papermap" / "web" / "static"
    draw(512).save(static / "icon.png")
    draw(64, background=False).save(static / "tray.png")
    out = ROOT / "build"
    out.mkdir(exist_ok=True)
    draw(256).save(out / "PaperMap.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    if sys.platform == "darwin":
        iconset = out / "PaperMap.iconset"
        iconset.mkdir(exist_ok=True)
        for px in (16, 32, 128, 256, 512):
            draw(px).save(iconset / f"icon_{px}x{px}.png")
            draw(px * 2).save(iconset / f"icon_{px}x{px}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out / "PaperMap.icns")], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
