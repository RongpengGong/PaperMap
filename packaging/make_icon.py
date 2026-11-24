"""Export the chosen evidence-bookmark artwork for PaperMap's app and web icons."""

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ARTWORK = Path(__file__).with_name("bookmark.png")
WINDOWS_SIZES = [(size, size) for size in (16, 32, 48, 64, 128, 256)]


def menu_bar_template() -> Image.Image:
    """Render the bookmark, page edge and excerpt in a single template colour."""
    image = Image.new("RGBA", (256, 256))
    draw = ImageDraw.Draw(image)
    ink = (0, 0, 0, 255)
    draw.rounded_rectangle((54, 57, 232, 196), radius=22, outline=ink, width=15)
    draw.rounded_rectangle((112, 119, 209, 135), radius=8, fill=ink)
    draw.rounded_rectangle((26, 29, 119, 179), radius=12, fill=ink)
    draw.polygon([(26, 158), (119, 158), (119, 226), (73, 192), (26, 226)], fill=ink)
    return image.resize((64, 64), Image.Resampling.LANCZOS)


def main() -> int:
    static = ROOT / "src" / "papermap" / "web" / "static"
    out = ROOT / "build"
    out.mkdir(exist_ok=True)
    with Image.open(ARTWORK) as artwork:
        artwork.resize((512, 512), Image.Resampling.LANCZOS).save(static / "icon.png")
        artwork.save(out / "PaperMap.ico", sizes=WINDOWS_SIZES)
        if sys.platform == "darwin":
            iconset = out / "PaperMap.iconset"
            iconset.mkdir(exist_ok=True)
            for size in (16, 32, 128, 256, 512):
                for scale, suffix in ((1, ""), (2, "@2x")):
                    pixels = size * scale
                    resized = artwork.resize((pixels, pixels), Image.Resampling.LANCZOS)
                    resized.save(iconset / f"icon_{size}x{size}{suffix}.png")
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out / "PaperMap.icns")],
                           check=True)
    menu_bar_template().save(static / "tray.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
