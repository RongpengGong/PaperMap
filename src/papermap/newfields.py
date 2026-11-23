"""New-category detection: proposals -> dedupe -> pending clusters (_pending/).

A classifier proposal is matched, in order, against:
  1. the pending slug the model explicitly reused (it sees the pending list);
  2. existing taxonomy categories (names + synonyms)  -> merged into that field;
  3. rejected pending clusters                        -> silently dropped;
  4. open pending clusters (names, synonyms, optional embeddings) -> joins it;
  5. otherwise a new pending cluster is opened.
Nothing becomes a real category without the user's confirmation.
"""

from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
from datetime import date
from pathlib import Path

import yaml
from rapidfuzz import fuzz

from .config import Config
from .registry import Registry
from .taxonomy import SLUG_RE, Taxonomy

log = logging.getLogger(__name__)

FUZZY_THRESHOLD = 90
EMBED_THRESHOLD = 0.86


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def aliases(*names: str) -> set[str]:
    """'Glioblastoma (GBM)' -> {'glioblastoma gbm', 'glioblastoma', 'gbm'}."""
    out: set[str] = set()
    for n in names:
        if not n:
            continue
        out.add(norm(n))
        for part in re.split(r"[()（）/、,;]", n):
            if norm(part):
                out.add(norm(part))
    return {a for a in out if a}


def names_match(a: set[str], b: set[str]) -> bool:
    for x in a:
        for y in b:
            if x == y:
                return True
            if min(len(x), len(y)) <= 4:  # acronyms must match exactly
                continue
            if fuzz.token_sort_ratio(x, y) >= FUZZY_THRESHOLD:
                return True
    return False


def make_slug(name_en: str, name_zh: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", norm(name_en)).strip("-")[:40].strip("-")
    if not base or not SLUG_RE.match(base):
        base = "new-field"
    slug, i = base, 2
    while slug in taken:
        slug, i = f"{base}-{i}", i + 1
    return slug


def _category_aliases(c) -> set[str]:
    return aliases(c.name_zh, c.name_en, c.slug.replace("-", " "), *c.synonyms)


def _pending_aliases(row) -> set[str]:
    return aliases(row["name_zh"] or "", row["name_en"] or "", (row["slug"] or "").replace("-", " "),
                   *json.loads(row["synonyms_json"] or "[]"))


# ------------------------------------------------------------- embeddings
class Embedder:
    """Optional: OpenAI-compatible /embeddings on the configured endpoint."""

    def __init__(self, config: Config):
        self.model = (config.raw.get("embedding") or {}).get("model", "").strip()
        self.config = config

    @property
    def enabled(self) -> bool:
        eff = self.config.effective_llm()
        return bool(self.model and eff.get("api_key") and eff.get("protocol") in ("openai", "responses"))

    def embed(self, text: str) -> list[float] | None:
        if not self.enabled:
            return None
        import httpx

        eff = self.config.effective_llm()
        try:
            r = httpx.post(eff["base_url"].rstrip("/") + "/embeddings",
                           headers={"Authorization": f"Bearer {eff['api_key']}"},
                           json={"model": self.model, "input": text[:2000]}, timeout=30)
            r.raise_for_status()
            return r.json()["data"][0]["embedding"]
        except Exception as exc:  # noqa: BLE001 - embeddings are only a helper
            log.warning("embedding failed: %s", exc)
            return None


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# --------------------------------------------------------------- matching
def route_proposal(registry: Registry, taxonomy: Taxonomy, prop: dict,
                   embedder: Embedder | None = None) -> tuple[str, object]:
    """Decide where a proposal goes. Returns (kind, target):
    ('existing', Category) | ('rejected', row) | ('pending', row) | ('new', None)."""
    names = aliases(prop.get("name_zh", ""), prop.get("name_en", ""))
    if prop.get("pending_slug"):
        row = registry.one("SELECT * FROM pending_fields WHERE slug=?", (prop["pending_slug"],))
        if row is not None:
            if row["status"] == "open":
                return "pending", row
            if row["status"] == "rejected":
                return "rejected", row
            target = taxonomy.by_slug.get(row["merged_into"] or row["slug"])
            if target is not None:
                return "existing", target
    for c in taxonomy.categories:
        if names_match(names, _category_aliases(c)):
            return "existing", c
    for row in registry.pending_fields(status="rejected"):
        if names_match(names, _pending_aliases(row)):
            return "rejected", row
    open_rows = registry.pending_fields(status="open")
    for row in open_rows:
        if names_match(names, _pending_aliases(row)):
            return "pending", row
    if embedder is not None and embedder.enabled and open_rows:
        vec = embedder.embed(f"{prop.get('name_en', '')}: {prop.get('description', '')}")
        if vec:
            prop["_embedding"] = vec
            best, score = None, 0.0
            for row in open_rows:
                if row["embedding_json"]:
                    sc = cosine(vec, json.loads(row["embedding_json"]))
                    if sc > score:
                        best, score = row, sc
            if best is not None and score >= EMBED_THRESHOLD:
                return "pending", best
    return "new", None


def record_proposals(registry: Registry, config: Config, taxonomy: Taxonomy, paper_id: int,
                     proposals: list[dict], embedder: Embedder | None = None) -> list[str]:
    """Store this paper's proposals (replacing earlier LLM ones). Returns log lines."""
    registry.execute("DELETE FROM field_proposals WHERE paper_id=? AND status='open'", (paper_id,))
    notes = []
    for p in proposals:
        kind, target = route_proposal(registry, taxonomy, p, embedder)
        if kind == "existing":
            field = registry.field_by_slug(target.slug)
            existing = registry.one(
                "SELECT status, source FROM paper_fields WHERE paper_id=? AND field_id=?", (paper_id, field["id"]))
            if existing is None:
                registry.set_paper_field(paper_id, field["id"], confidence=0.5, is_primary=0, status="suggested",
                                         source="llm", note="", reason=f"proposed as '{p['name_en'] or p['name_zh']}'")
            registry.add_proposal(paper_id, p["axis"], p["name_zh"], p["name_en"], p["description"], p["reason"],
                                  outcome=f"existing:{target.slug}", status="closed")
            notes.append(f"'{p['name_en'] or p['name_zh']}' matches existing '{target.slug}'")
        elif kind == "rejected":
            registry.add_proposal(paper_id, p["axis"], p["name_zh"], p["name_en"], p["description"], p["reason"],
                                  pending_id=target["id"], outcome="rejected", status="closed")
        elif kind == "pending":
            syn = json.loads(target["synonyms_json"] or "[]")
            for n in (p["name_en"], p["name_zh"]):
                if n and norm(n) not in {norm(x) for x in syn + [target["name_en"] or "", target["name_zh"] or ""]}:
                    syn.append(n)
            registry.update_pending(target["id"], synonyms_json=syn)
            registry.add_proposal(paper_id, p["axis"], p["name_zh"], p["name_en"], p["description"], p["reason"],
                                  pending_id=target["id"], outcome="pending")
            notes.append(f"'{p['name_en'] or p['name_zh']}' joins pending '{target['slug']}'")
        else:
            taken = set(taxonomy.by_slug) | {r["slug"] for r in registry.query("SELECT slug FROM pending_fields")}
            slug = make_slug(p["name_en"] or p["name_zh"], p["name_zh"], taken)
            pid = registry.create_pending(slug, p["axis"], p["name_zh"], p["name_en"], p["description"], [])
            if p.get("_embedding"):
                registry.update_pending(pid, embedding_json=json.dumps(p["_embedding"]))
            elif embedder is not None and embedder.enabled:
                vec = embedder.embed(f"{p['name_en']}: {p['description']}")
                if vec:
                    registry.update_pending(pid, embedding_json=json.dumps(vec))
            registry.add_proposal(paper_id, p["axis"], p["name_zh"], p["name_en"], p["description"], p["reason"],
                                  pending_id=pid, outcome="pending")
            notes.append(f"new pending field '{slug}'")
    return notes


def ready_fields(registry: Registry, threshold: int) -> list:
    return [r for r in registry.pending_fields("open") if r["n_papers"] >= threshold]


def notify_ready(registry: Registry, threshold: int) -> list[str]:
    """Log one event per pending field that newly reached the threshold."""
    out = []
    for r in ready_fields(registry, threshold):
        if not r["ready_notified"]:
            registry.update_pending(r["id"], ready_notified=1)
            msg = (f"Pending field '{r['name_zh'] or r['name_en']}' now has {r['n_papers']} papers - "
                   "confirm, merge or reject it on the review page")
            registry.log("warning", msg)
            out.append(r["slug"])
    return out


# ------------------------------------------------------ _pending/ markdown
def write_pending_pages(knowledge_dir: Path, registry: Registry, threshold: int) -> None:
    """Mirror open pending clusters into knowledge/_pending/*.md (and remove closed ones)."""
    d = knowledge_dir / "_pending"
    d.mkdir(parents=True, exist_ok=True)
    keep = set()
    for r in registry.pending_fields("open"):
        keep.add(f"{r['slug']}.md")
        papers = registry.pending_papers(r["id"])
        front = {"slug": r["slug"], "axis": r["axis"], "name_zh": r["name_zh"], "name_en": r["name_en"],
                 "papers": r["n_papers"], "threshold": threshold, "status": "pending",
                 "synonyms": json.loads(r["synonyms_json"] or "[]"), "updated": date.today().isoformat()}
        lines = ["---", yaml.safe_dump(front, allow_unicode=True, sort_keys=False, width=10**6).strip(), "---", "",
                 f"# 待定类别：{r['name_zh'] or r['name_en']}（{r['name_en']}）", "",
                 r["description"] or "", "",
                 f"已有 {r['n_papers']} / {threshold} 篇论文。达到阈值后会在网页「待审核」提示你建立、合并或拒绝。", "",
                 "## 论文", ""]
        for p in papers:
            target = (p["kb_path"] or "").removesuffix(".md")
            label = f"{p['first_author'] or '?'} {p['year'] or ''} · {p['journal'] or ''}"
            lines.append(f"- [[{target}|{label}]] — {p['reason'] or ''}" if target else f"- {p['title']}")
        (d / f"{r['slug']}.md").write_text("\n".join(lines) + "\n", "utf-8")
    for f in d.glob("*.md"):
        if f.name not in keep:
            f.unlink()
