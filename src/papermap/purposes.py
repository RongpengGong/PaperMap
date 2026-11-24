"""Purpose tags: why a paper is in the library for the user ("cite in intro", "method reference"...).

They only organise and export; like stars they never change how evidence is weighed.
"""
from __future__ import annotations

from .i18n import L
from .registry import Registry, now_iso

PRESETS = {
    "intro": L("引言可引用"),
    "methods": L("方法参考"),
    "discussion": L("讨论对比"),
    "data": L("数据来源"),
    "figure": L("作图参考"),
    "toread": L("待精读"),
}
MAX_LEN = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_purposes (
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    tag TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (paper_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_paper_purposes_tag ON paper_purposes(tag);
"""


def ensure_schema(registry: Registry) -> None:
    with registry._lock:  # noqa: SLF001
        registry.conn.executescript(SCHEMA)


def clean(tag: str) -> str:
    tag = " ".join(str(tag or "").replace("#", " ").split())[:MAX_LEN]
    if not tag:
        raise ValueError("用途不能为空")
    return tag


def label(tag: str) -> str:
    return PRESETS.get(tag, tag)  # presets are L labels: templates translate them


def of_paper(registry: Registry, paper_id: int) -> list[str]:
    ensure_schema(registry)
    return [r["tag"] for r in registry.query("SELECT tag FROM paper_purposes WHERE paper_id=? ORDER BY created_at", (paper_id,))]


def set_tag(registry: Registry, paper_ids: list[int], tag: str, on: bool) -> int:
    ensure_schema(registry)
    tag = clean(tag)
    n = 0
    for pid in paper_ids:
        if on:
            n += registry.execute("INSERT OR IGNORE INTO paper_purposes(paper_id, tag, created_at) VALUES (?,?,?)",
                                  (pid, tag, now_iso())).rowcount
        else:
            n += registry.execute("DELETE FROM paper_purposes WHERE paper_id=? AND tag=?", (pid, tag)).rowcount
    return n


def counts(registry: Registry) -> list[dict]:
    """Tags in use with their paper counts: presets first (in their order), then the user's own."""
    ensure_schema(registry)
    rows = {r["tag"]: r["n"] for r in registry.query(
        "SELECT pp.tag, count(*) AS n FROM paper_purposes pp JOIN papers p ON p.id=pp.paper_id "
        "WHERE p.status != 'ignored' GROUP BY pp.tag")}
    order = [t for t in PRESETS if t in rows] + sorted(t for t in rows if t not in PRESETS)
    return [{"tag": t, "label": label(t), "n": rows[t]} for t in order]


def all_tags(registry: Registry) -> list[dict]:
    """Every preset plus custom tags in use, for the toggles on a paper."""
    used = {c["tag"] for c in counts(registry)}
    return [{"tag": t, "label": v, "preset": True} for t, v in PRESETS.items()] + [
        {"tag": t, "label": t, "preset": False} for t in sorted(used - set(PRESETS))]
