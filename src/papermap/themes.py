"""Color themes: a palette (accent hue) x an appearance (system / light / dark / black).

Blue is the original design and keeps its hand-tuned values in style.css; the
other palettes are generated from a hue so they stay consistent with it.
``python -m papermap.themes`` rewrites web/static/themes.css.
"""

from __future__ import annotations

import colorsys
from pathlib import Path

# id: (label, hue, saturation factor)
PALETTES = {
    "blue": ("晴空蓝", 213, 1.0),
    "pink": ("樱花粉", 336, 1.0),
    "purple": ("薰衣草紫", 266, 0.9),
    "green": ("薄荷绿", 158, 0.85),
    "orange": ("暖阳橙", 26, 1.0),
    "graphite": ("石墨灰", 215, 0.12),
}
APPEARANCES = {"system": "跟随系统", "light": "浅色", "dark": "深色", "black": "纯黑（夜间）"}
SWATCH = {pid: f"hsl({h} {round(72 * s)}% 52%)" for pid, (_, h, s) in PALETTES.items()}


def normalize(raw: dict | None) -> dict:
    raw = raw or {}
    palette = raw.get("palette") if raw.get("palette") in PALETTES else "blue"
    mode = raw.get("mode") if raw.get("mode") in APPEARANCES else "system"
    return {"palette": palette, "mode": mode}


def _hsl(h, s, l, a=None):
    s = max(0, min(100, s))
    return f"hsl({h % 360} {s:.0f}% {l}%" + (f" / {a})" if a is not None else ")")


def _light(h, k):
    S = lambda v: v * k  # noqa: E731
    return {
        "--edge-line": _hsl(h, S(45), 86), "--rim-solid": _hsl(h, S(55), 85),
        "--rim-tint": _hsl(h, S(70), 75, .42), "--rim-glint": _hsl(h, S(100), 98, .78),
        "--rim-sheen": _hsl(h, S(100), 98, .34),
        "--rim-wash": f"linear-gradient(155deg,{_hsl(h, S(100), 98, .35)} 8%,{_hsl(h, S(70), 72, .19)} 46%,"
                      f"{_hsl(h, S(65), 55, .28)} 78%,{_hsl(h + 15, S(70), 85, .34)})",
        "--canvas": _hsl(h, S(30), 97.5), "--canvas-end": _hsl(h, S(30), 97.5), "--bg": _hsl(h, S(20), 97.5),
        "--line": _hsl(h, S(25), 89), "--line-strong": _hsl(h, S(22), 82),
        "--accent": _hsl(h, S(72), 46), "--accent-hover": _hsl(h, S(74), 40), "--accent-soft": _hsl(h, S(90), 95),
        "--side": _hsl(h, S(28), 95), "--side-glass": _hsl(h, S(35), 95, .8), "--native-side": _hsl(h, S(40), 95, .56),
        "--active-surface": f"linear-gradient(155deg,{_hsl(h, S(100), 94, .8)},{_hsl(h, S(65), 79, .49)})",
        "--row-sel": f"linear-gradient(160deg,{_hsl(h, S(100), 95, .85)},{_hsl(h, S(75), 83, .54)})",
        "--side-hover": _hsl(h, S(25), 50, .08), "--toolbar": _hsl(h, S(20), 96), "--reader-bg": _hsl(h, S(22), 91),
        "--selection": _hsl(h, S(90), 87), "--map-method": _hsl(h, S(62), 51), "--map-edge": _hsl(h, S(25), 84),
        "--button": _hsl(h, S(70), 44), "--button-hover": _hsl(h, S(72), 38),
    }


def _dark(h, k):
    S = lambda v: v * k  # noqa: E731
    return {
        "--edge-line": _hsl(h, S(28), 33), "--rim-solid": _hsl(h, S(42), 24),
        "--rim-tint": _hsl(h, S(63), 24, .54), "--rim-glint": _hsl(h, S(85), 84, .38),
        "--rim-sheen": _hsl(h, S(80), 82, .17),
        "--rim-wash": f"linear-gradient(155deg,{_hsl(h, S(60), 68, .18)} 8%,{_hsl(h, S(61), 39, .16)} 46%,"
                      f"{_hsl(h, S(51), 43, .27)} 78%,{_hsl(h + 15, S(45), 66, .22)})",
        "--canvas": _hsl(h, S(14), 14), "--canvas-end": _hsl(h, S(14), 14), "--bg": _hsl(h, S(14), 15.5),
        "--card": _hsl(h, S(16), 19), "--side": _hsl(h, S(18), 17.5), "--side-glass": _hsl(h, S(22), 16, .86),
        "--native-side": _hsl(h, S(25), 13, .65), "--chrome": _hsl(h, S(20), 18, .94),
        "--toolbar": _hsl(h, S(20), 19), "--field": _hsl(h, S(22), 14), "--popover": _hsl(h, S(22), 18, .95),
        "--line": _hsl(h, S(20), 26), "--line-strong": _hsl(h, S(18), 38), "--reader-bg": _hsl(h, S(30), 10),
        "--accent": _hsl(h, S(95), 77), "--accent-hover": _hsl(h, S(100), 84), "--accent-soft": _hsl(h, S(40), 28),
        "--active-surface": f"linear-gradient(155deg,{_hsl(h, S(33), 48, .52)},{_hsl(h, S(42), 33, .65)})",
        "--row-sel": f"linear-gradient(160deg,{_hsl(h, S(36), 46, .55)},{_hsl(h, S(42), 32, .68)})",
        "--side-hover": _hsl(h, S(45), 76, .09), "--selection": _hsl(h, S(49), 40),
        "--map-method": _hsl(h, S(100), 78), "--map-edge": _hsl(h, S(22), 34),
        "--button": _hsl(h, S(62), 45), "--button-hover": _hsl(h, S(64), 40),
    }


BLACK = {  # appearance "black": near-true black surfaces, palette keeps its accents
    "--canvas": "#000", "--canvas-end": "#000", "--bg": "#070707", "--card": "#111", "--side": "#0b0b0b",
    "--side-glass": "rgba(11,11,11,.92)", "--native-side": "rgba(0,0,0,.7)", "--chrome": "rgba(8,8,8,.95)",
    "--toolbar": "#131313", "--field": "#0d0d0d", "--popover": "rgba(22,22,22,.97)", "--reader-bg": "#030303",
    "--line": "#242424", "--line-strong": "#3a3a3a", "--edge-line": "#1f1f1f", "--rim-solid": "#050505",
    "--rim-tint": "rgba(0,0,0,.6)", "--rim-glint": "rgba(255,255,255,.08)", "--rim-sheen": "rgba(255,255,255,.04)",
    "--rim-wash": "linear-gradient(155deg,rgba(255,255,255,.03),rgba(0,0,0,0))", "--map-edge": "#2c2c2c",
}


def _block(selector: str, props: dict) -> str:
    return selector + " {\n" + "".join(f"  {k}:{v};\n" for k, v in props.items()) + "}\n"


def css() -> str:
    out = ["/* Generated by `python -m papermap.themes` - do not edit by hand. */\n"]
    dark_sel = ':root[data-appearance="dark"]{p}, :root[data-appearance="black"]{p}'
    for pid, (_, h, k) in PALETTES.items():
        if pid == "blue":
            continue  # hand-tuned in style.css
        p = f'[data-palette="{pid}"]'
        out.append(_block(f":root{p}", _light(h, k)))
        out.append("@media (prefers-color-scheme:dark) {\n" + _block(
            f':root{p}:not([data-appearance="light"])', _dark(h, k)) + "}\n")
        out.append(_block(dark_sel.format(p=p), _dark(h, k)))
    out.append(_block(':root[data-appearance="black"][data-palette]', {"color-scheme": "dark", **BLACK}))
    return "\n".join(out)


def titlebar_colors(palette: str, dark: bool, black: bool = False) -> tuple[tuple, tuple, tuple]:
    """(left, right, solid) RGBA for the native macOS title bar tint."""
    if black:
        return (0.03, 0.03, 0.035, .9), (0.02, 0.02, 0.025, .9), (0.02, 0.02, 0.02, 1)
    if palette == "blue" or palette not in PALETTES:
        if dark:
            return (.165, .306, .439, .68), (.102, .239, .384, .64), (.141, .239, .345, 1)
        return (.675, .839, .965, .56), (.510, .733, .925, .51), (.745, .843, .941, 1)
    _, h, k = PALETTES[palette]

    def rgb(s, l, a):
        r, g, b = colorsys.hls_to_rgb((h % 360) / 360, l / 100, min(1, s * k / 100))
        return (r, g, b, a)
    if dark:
        return rgb(45, 25, .68), rgb(58, 21, .64), rgb(42, 24, 1)
    return rgb(70, 82, .56), rgb(65, 75, .51), rgb(55, 85, 1)


if __name__ == "__main__":
    target = Path(__file__).parent / "web" / "static" / "themes.css"
    target.write_text(css(), "utf-8")
    print("wrote", target)
