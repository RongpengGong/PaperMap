"""Figure gallery (CLAUDE.md §15.9): figures located by their legends, cropped
from the page render (works for vector figures too), explained by a vision model."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import pymupdf

from .config import Config
from .evidence import document_revision
from .i18n import L
from .llm import LLMClient
from .pdf_import import staged_file
from .registry import Registry, now_iso

LEGEND_RE = re.compile(r"^\s*((?:Extended Data |Supplementary )?Fig(?:ure)?\.?\s*(\d+)[a-z]?)\s*(?:\||:|\.|\s{2,}|\s[A-Z])", re.I)
SCHEMA = """
CREATE TABLE IF NOT EXISTS figures (
    id INTEGER PRIMARY KEY, paper_id INTEGER NOT NULL, label TEXT NOT NULL, page INTEGER NOT NULL,
    x0 REAL, y0 REAL, x1 REAL, y1 REAL, legend TEXT, explanation TEXT, model TEXT, created_at TEXT,
    UNIQUE (paper_id, label)
);
CREATE TABLE IF NOT EXISTS figures_state (paper_id INTEGER PRIMARY KEY, signature TEXT);
-- The user's drawing references. Keyed by paper and figure label, so a new PDF version keeps them.
CREATE TABLE IF NOT EXISTS figure_refs (
    paper_id INTEGER NOT NULL, label TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (paper_id, label)
);
-- Plot types the user added (on=1) or removed (on=0) where the legend words got it wrong.
CREATE TABLE IF NOT EXISTS figure_type_edits (
    paper_id INTEGER NOT NULL, label TEXT NOT NULL, kind TEXT NOT NULL, on_ INTEGER NOT NULL,
    PRIMARY KEY (paper_id, label, kind)
);
CREATE TABLE IF NOT EXISTS figure_history (
    id INTEGER PRIMARY KEY, paper_id INTEGER NOT NULL, revision TEXT NOT NULL, label TEXT NOT NULL,
    explanation TEXT, model TEXT, UNIQUE(paper_id, revision, label)
);
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


# Plot types named in figure legends, for filtering the gallery when looking for drawing references.
# Matched on the legend text only (free, no model); a figure can show several types.
FIGURE_TYPES: list[tuple[str, object, re.Pattern]] = [
    (k, L(lbl), re.compile(rx, re.I)) for k, lbl, rx in [
        ("embedding", "UMAP / t-SNE", r"\b(u-?map|t-?sne|tsne|force-directed|phate)\b"),
        ("spatial", "空间分布图", r"\bspatial(ly)?\b|visium|xenium|cosmx|stereo-seq|merfish|slide-seq|\bniche"),
        ("heatmap", "热图", r"heat\s?-?maps?|clustermap|correlation matri"),
        ("dotplot", "气泡 / 点图", r"\bdot\s?-?plots?|bubble\s?(plot|chart)"),
        ("violin", "小提琴 / 箱线图", r"violin|box\s?-?(plots?|and-whisker)|boxplots?|whiskers"),
        ("bar", "柱状图", r"\bbar\s?(plots?|graphs?|charts?)|histograms?"),
        ("volcano", "火山图", r"volcano|ma\s?-?plot"),
        ("enrichment", "通路 / 富集", r"enrichment|\bgsea\b|gene ontology|\bgo terms?|kegg|reactome|hallmark|pathway"),
        ("survival", "生存曲线", r"kaplan|survival|hazard ratio|\bcox\b"),
        ("forest", "森林图", r"forest\s?plot|odds ratio|mendelian randomi"),
        ("roc", "ROC / 校准", r"\broc\b|\bauc\b|receiver operating|calibration curve|decision curve|nomogram"),
        ("trajectory", "轨迹 / 拟时序", r"trajector|pseudotime|pseudo-time|rna velocity|monocle|lineage"),
        ("network", "网络 / 细胞通讯", r"\bnetwork|ligand|receptor pair|cell-cell (communication|interaction)|cellchat|cellphonedb|circos|chord"),
        ("sankey", "桑基 / 冲积图", r"sankey|alluvial|river plot"),
        ("composition", "细胞比例", r"proportion|composition|abundance|fraction of cells|stacked bar"),
        ("staining", "染色 / 显微图像", r"immunohisto|\bihc\b|immunofluoresc|\bif\b staining|h&e|haematoxylin|hematoxylin|microscop|"
                                       r"confocal|staining|stained|scale bars?|\bimc\b|codex|multiplex(ed)? imag"),
        ("blot", "Western / 凝胶", r"western blot|immunoblot|\bgel\b|\bblots?\b"),
        ("flow", "流式", r"flow cytometr|\bfacs\b|gating"),
        ("schematic", "示意图 / 流程图", r"schemati|overview of (the|our)|workflow|study design|graphical|illustrat|diagram|cartoon|model of"),
        ("genome", "基因组轨道 / 曼哈顿", r"manhattan|locuszoom|genome browser|coverage track|\bigv\b|chromatin accessib|atac-seq track"),
    ]
]


def figure_types(legend: str | None) -> list[str]:
    return list(_types(legend or ""))


@lru_cache(maxsize=8192)
def _types(text: str) -> tuple[str, ...]:
    return tuple(k for k, _, rx in FIGURE_TYPES if rx.search(text))


TYPE_KEYS = {k for k, _, _ in FIGURE_TYPES}
MAX_NOTE = 2000


def type_edits(registry: Registry) -> dict[tuple[int, str], dict[str, bool]]:
    ensure_schema(registry)
    out: dict[tuple[int, str], dict[str, bool]] = {}
    for r in registry.query("SELECT paper_id, label, kind, on_ FROM figure_type_edits"):
        out.setdefault((r["paper_id"], r["label"]), {})[r["kind"]] = bool(r["on_"])
    return out


def effective_types(legend: str | None, edits: dict[str, bool] | None) -> list[str]:
    """Types from the legend words, corrected by the user's own additions and removals."""
    auto = figure_types(legend)
    edits = edits or {}
    return [k for k, _, _ in FIGURE_TYPES if edits.get(k, k in auto)]


def set_type(registry: Registry, paper_id: int, label: str, kind: str, on: bool, legend: str | None) -> list[str]:
    if kind not in TYPE_KEYS:
        raise ValueError("未知的图型")
    ensure_schema(registry)
    if on == (kind in figure_types(legend)):  # back to what the legend says: no edit needed
        registry.execute("DELETE FROM figure_type_edits WHERE paper_id=? AND label=? AND kind=?", (paper_id, label, kind))
    else:
        registry.execute("INSERT OR REPLACE INTO figure_type_edits VALUES (?,?,?,?)", (paper_id, label, kind, int(on)))
    return effective_types(legend, type_edits(registry).get((paper_id, label)))


def refs(registry: Registry) -> dict[tuple[int, str], str]:
    """(paper_id, label) -> note of every figure the user kept as a reference."""
    ensure_schema(registry)
    return {(r["paper_id"], r["label"]): r["note"] for r in registry.query("SELECT paper_id, label, note FROM figure_refs")}


def set_ref(registry: Registry, paper_id: int, label: str, on: bool, note: str | None = None) -> None:
    ensure_schema(registry)
    if not on:
        registry.execute("DELETE FROM figure_refs WHERE paper_id=? AND label=?", (paper_id, label))
        return
    note = (note or "").strip()[:MAX_NOTE] if note is not None else None
    now = now_iso()
    registry.execute("INSERT INTO figure_refs(paper_id, label, note, created_at, updated_at) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(paper_id, label) DO UPDATE SET note=COALESCE(?, note), updated_at=?",
                     (paper_id, label, note or "", now, now, note, now))


FIGURE_ROWS = "SELECT f.*, s.signature AS revision FROM figures f JOIN figures_state s ON s.paper_id=f.paper_id"
STALE_FIGURE = "图表已更新，请刷新页面后重新打开。"


def sync(registry: Registry, paper_id: int, pdf: Path) -> int:
    ensure_schema(registry)
    revision = document_revision(pdf)
    state = registry.one("SELECT signature FROM figures_state WHERE paper_id=?", (paper_id,))
    if state and state["signature"] == revision:
        return 0

    # Older libraries stored size and mtime. Upgrade a matching index in place so
    # its figure IDs and saved explanations survive the first visit after updating.
    stat = pdf.stat()
    if state and state["signature"] == f"{stat.st_size}:{stat.st_mtime}":
        sources = registry.query("SELECT sha256 FROM files WHERE paper_id=? AND size=? AND mtime=?",
                                 (paper_id, stat.st_size, stat.st_mtime))
        if not sources or any(row["sha256"] == revision for row in sources):
            updated = registry.execute("UPDATE figures_state SET signature=? WHERE paper_id=? AND signature=?",
                                       (revision, paper_id, state["signature"]))
            if updated.rowcount:
                return 0

    figs = find(pdf)
    with registry.transaction() as db:
        if document_revision(pdf) != revision:
            raise ValueError(STALE_FIGURE)
        if db.execute("SELECT 1 FROM figures_state WHERE paper_id=? AND signature=?", (paper_id, revision)).fetchone():
            return 0
        # A paper can switch between PDFs. Keep its IDs and explanations tied
        # to the version that produced them, including when the reader switches back.
        db.execute("INSERT OR REPLACE INTO figure_history(id,paper_id,revision,label,explanation,model) "
                   "SELECT f.id,f.paper_id,s.signature,f.label,f.explanation,f.model FROM figures f "
                   "JOIN figures_state s ON s.paper_id=f.paper_id WHERE f.paper_id=?", (paper_id,))
        last_id = db.execute("SELECT MAX(COALESCE((SELECT MAX(id) FROM figures),0), "
                             "COALESCE((SELECT MAX(id) FROM figure_history),0))").fetchone()[0]
        previous = {r["label"]: r for r in db.execute("SELECT * FROM figure_history WHERE paper_id=? AND revision=?",
                                                      (paper_id, revision))}
        db.execute("DELETE FROM figures WHERE paper_id=?", (paper_id,))
        for f in figs:
            old = previous.get(f["label"])
            if old is None:
                last_id += 1
            db.execute("INSERT INTO figures(id,paper_id,label,page,x0,y0,x1,y1,legend,explanation,model,created_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (old["id"] if old else last_id, paper_id, f["label"], f["page"], *f["rect"], f["legend"],
                        old["explanation"] if old else None, old["model"] if old else None, now_iso()))
        db.execute("INSERT OR REPLACE INTO figures_state VALUES (?,?)", (paper_id, revision))
    return len(figs)


def image(pdf: Path, fig, cache_dir: Path, scale: float = 2.0) -> Path:
    revision = fig["revision"]
    if document_revision(pdf) != revision:
        raise ValueError(STALE_FIGURE)
    out = cache_dir / str(fig["paper_id"]) / revision / f"{fig['id']}-{scale}.png"
    if out.exists():
        return out
    with pymupdf.open(pdf) as doc:
        pix = doc[fig["page"]].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False,
                                         clip=pymupdf.Rect(fig["x0"], fig["y0"], fig["x1"], fig["y1"]))
        data = pix.tobytes("png")
    if document_revision(pdf) != revision:
        raise ValueError(STALE_FIGURE)
    with staged_file(out.parent) as tmp:
        tmp.write_bytes(data)
        tmp.replace(out)
    return out


def chat_attachment(registry: Registry, paper_id: int, pdf: Path | None, figure_id: int, revision: str,
                    cache_dir: Path) -> tuple[dict, bytes]:
    """Use the displayed revision of this paper for both pixels and interpretation."""
    if pdf is None or not pdf.is_file():
        raise ValueError("找不到 PDF")
    ensure_schema(registry)
    fig = registry.one(FIGURE_ROWS + " WHERE f.id=? AND f.paper_id=? AND s.signature=?",
                       (figure_id, paper_id, revision))
    if fig is None:
        raise ValueError(STALE_FIGURE)
    png = image(pdf, fig, cache_dir).read_bytes()
    return {"id": fig["id"], "label": fig["label"], "page": fig["page"] + 1, "revision": revision,
            "legend": fig["legend"] or "", "explanation": fig["explanation"] or ""}, png


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
    updated = registry.execute("UPDATE figures SET explanation=?, model=? WHERE id=? AND paper_id=? AND "
                               "EXISTS (SELECT 1 FROM figures_state WHERE paper_id=? AND signature=?)",
                               (res.raw, res.model, fig["id"], fig["paper_id"], fig["paper_id"], fig["revision"]))
    if not updated.rowcount:
        raise ValueError(STALE_FIGURE)
    return res.raw
