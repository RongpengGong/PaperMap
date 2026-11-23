"""Figure gallery (CLAUDE.md §15.9): figures located by their legends, cropped
from the page render (works for vector figures too), explained by a vision model."""

from __future__ import annotations

import re
from pathlib import Path

import pymupdf

from .config import Config
from .llm import LLMClient
from .registry import Registry, now_iso

LEGEND_RE = re.compile(r"^\s*((?:Extended Data |Supplementary )?Fig(?:ure)?\.?\s*(\d+)[a-z]?)\s*(?:\||:|\.|\s{2,}|\s[A-Z])", re.I)
SCHEMA = """
CREATE TABLE IF NOT EXISTS figures (
    id INTEGER PRIMARY KEY, paper_id INTEGER NOT NULL, label TEXT NOT NULL, page INTEGER NOT NULL,
    x0 REAL, y0 REAL, x1 REAL, y1 REAL, legend TEXT, explanation TEXT, model TEXT, created_at TEXT,
    UNIQUE (paper_id, label)
);
CREATE TABLE IF NOT EXISTS figures_state (paper_id INTEGER PRIMARY KEY, signature TEXT);
"""


def ensure_schema(registry: Registry) -> None:
    with registry._lock:  # noqa: SLF001
        registry.conn.executescript(SCHEMA)
        registry.conn.commit()


def find(pdf: Path) -> list[dict]:
    """Figure regions: the area above each legend (or the previous page when the legend starts a page)."""
    from collections import Counter

    out, seen = [], set()
    with pymupdf.open(pdf) as doc:
        # dominant (body) font size: text inside figures is smaller and must not bound a figure
        sizes: Counter = Counter()
        page_sizes = []
        for page in doc:
            per = {}
            for b in page.get_text("dict").get("blocks", []):
                if b.get("type") != 0:
                    continue
                c: Counter = Counter()
                for ln in b.get("lines", []):
                    for sp in ln.get("spans", []):
                        c[round(sp["size"], 1)] += len(sp["text"].strip())
                if c:
                    sz = c.most_common(1)[0][0]
                    sizes[sz] += sum(c.values())
                    per[tuple(round(v, 1) for v in b["bbox"])] = sz
            page_sizes.append(per)
        body = sizes.most_common(1)[0][0] if sizes else 9.0
        for pno, page in enumerate(doc):
            W, H = page.rect.width, page.rect.height
            blocks = sorted((b for b in page.get_text("blocks") if b[6] == 0), key=lambda b: b[1])

            def body_text(x) -> bool:
                sz = page_sizes[pno].get(tuple(round(v, 1) for v in x[:4]))
                return sz is None or sz >= body - 0.6
            for b in blocks:
                text = b[4].strip()
                m = LEGEND_RE.match(text)
                if not m:
                    continue
                label = re.sub(r"\s+", " ", m.group(1)).replace("Figure", "Fig.").replace("Fig ", "Fig. ")
                label = re.sub(r"Fig\.\.", "Fig.", label)
                if label in seen:
                    continue
                legend_top = b[1]
                # top of the figure: bottom of the last long text block above the legend
                above = [x for x in blocks if x[3] <= legend_top - 2 and len(x[4].strip()) > 250 and body_text(x)]
                top = max([x[3] for x in above] + [36.0])
                legend = " ".join(text.split())
                # legend continues in following blocks until the next long gap
                for nb in blocks:
                    if nb[1] > b[3] and nb[1] - b[3] < 14 and not LEGEND_RE.match(nb[4]):
                        legend += " " + " ".join(nb[4].split())
                if legend_top - top >= 80:
                    rect = (24.0, top + 2, W - 24.0, legend_top - 2)
                    out.append({"label": label, "page": pno, "rect": rect, "legend": legend[:3000]})
                    seen.add(label)
                elif pno > 0:  # legend at the top of a page: the figure is probably the previous page
                    out.append({"label": label, "page": pno - 1, "rect": (24.0, 36.0, W - 24.0, H - 36.0), "legend": legend[:3000]})
                    seen.add(label)
    def order(f: dict) -> tuple:
        ext = "Extended" in f["label"] or "Supplementary" in f["label"]
        num = int(re.search(r"\d+", f["label"]).group(0))
        return (ext, num)
    return sorted(out, key=order)


def sync(registry: Registry, paper_id: int, pdf: Path) -> int:
    ensure_schema(registry)
    sig = f"{pdf.stat().st_size}:{pdf.stat().st_mtime}"
    st = registry.one("SELECT signature FROM figures_state WHERE paper_id=?", (paper_id,))
    if st and st["signature"] == sig:
        return 0
    figs = find(pdf)
    keep = {r["label"]: r for r in registry.query("SELECT * FROM figures WHERE paper_id=?", (paper_id,))}
    with registry._lock:  # noqa: SLF001
        registry.conn.execute("DELETE FROM figures WHERE paper_id=?", (paper_id,))
        for f in figs:
            old = keep.get(f["label"])
            registry.conn.execute(
                "INSERT INTO figures(paper_id, label, page, x0, y0, x1, y1, legend, explanation, model, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (paper_id, f["label"], f["page"], *f["rect"], f["legend"],
                 old["explanation"] if old else None, old["model"] if old else None, now_iso()))
        registry.conn.execute("INSERT OR REPLACE INTO figures_state VALUES (?,?)", (paper_id, sig))
        registry.conn.commit()
    return len(figs)


def image(pdf: Path, fig, cache_dir: Path, scale: float = 2.0) -> Path:
    out = cache_dir / str(fig["paper_id"]) / f"{fig['id']}-{scale}.png"
    if out.exists():
        return out
    with pymupdf.open(pdf) as doc:
        pix = doc[fig["page"]].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False,
                                         clip=pymupdf.Rect(fig["x0"], fig["y0"], fig["x1"], fig["y1"]))
        data = pix.tobytes("png")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out


EXPLAIN = """You are helping a biomedical researcher read one figure from a paper.
You see the figure image and its legend. Explain it in Markdown:
1. **这张图想说明什么**（一句话）
2. **逐个面板**：for each panel (a, b, c…) what is plotted (axes, colours, groups) and what it shows.
3. **关键结论与证据强度**: what the figure supports, and what it does not show (n, statistics, controls missing?).
Use only what is visible in the image and legend; if something is unreadable, say so. Keep gene and software names in English. Write in Simplified Chinese."""


def explain(config: Config, registry: Registry, llm: LLMClient, fig, png: bytes) -> str:
    p = registry.get_paper(fig["paper_id"])
    user = f"Paper: {p['title']}\nFigure: {fig['label']}\nLegend: {fig['legend']}"
    res = llm.chat_text(EXPLAIN, user, purpose="figure", paper_id=fig["paper_id"], max_tokens=2500, images=[png])
    registry.execute("UPDATE figures SET explanation=?, model=? WHERE id=?", (res.raw, res.model, fig["id"]))
    return res.raw
