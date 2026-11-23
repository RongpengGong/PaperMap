"""Full-text paragraph-aligned translation (CLAUDE.md §14.4).

Services: the free bilibili Index-Translate API (default, no key), the main
LLM, or custom OpenAI/Anthropic/Gemini-compatible endpoints. Paragraphs are
sent in numbered chunks so the output can be aligned back to the source;
a glossary keeps gene names, tools and taxonomy terms untranslated.
Output: <stem>.translation.md (Obsidian) + <stem>.translation.json (app view).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any, Callable

import httpx
import pymupdf
import yaml

from . import __version__
from .config import Config, effective_llm
from .kb_writer import atomic_write
from .llm import LLMClient, LLMHTTPError, compute_cost, make_client, strip_think
from .pdf_extract import MAX_PAGES_FOR_LLM, _REFERENCE_LINE_RE
from .registry import Registry

log = logging.getLogger(__name__)

PROMPT_VERSION = "translate_v1"
BILIBILI_BASE = "https://index-translate.bilibili.com/v1"
BILIBILI_MODELS = {
    "Index-Translate-35B-A3B": "Index-Translate-35B-A3B（最好，推荐）",
    "Index-Translate-9B": "Index-Translate-9B（均衡）",
    "Index-Translate-2B": "Index-Translate-2B（最快）",
    "Index-Nailong-9B": "Index-Nailong-9B（长文档，术语前后一致）",
}
LANG_NAMES = {"zh": "中文", "en": "英文"}
MODES = {"translate": "翻译", "polish": "润色", "custom": "自定义 prompt"}
CHUNK_CHARS = 3000
WORKERS = 2
MAX_SERVICE_FAILURES = 3  # then use the fallback for the rest of the paper


class TranslationError(Exception):
    pass


# ------------------------------------------------------------- paragraphs
def _clean(text: str) -> str:
    text = re.sub(r"-\n(?=[a-z])", "", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _is_heading(t: str) -> bool:
    words = t.split()
    return (len(t) < 90 and 1 <= len(words) <= 12 and not t.endswith((".", ",", ";", ":"))
            and t[:1].isupper() and not re.search(r"\b(19|20)\d{2}\b", t))


_LEGEND_RE = re.compile(r"^(extended data )?(fig(ure)?\.?|table)\s*\d+", re.I)
_REFS_HEAD = re.compile(r"(references|bibliography|literature cited)", re.I)
_AFTER_REFS = re.compile(r"(online )?methods|materials and methods|data availability|code availability|"
                         r"acknowledg(e)?ments|author contributions|supplementary information|extended data", re.I)


def _blocks(pdf: str | Path) -> list[list[dict]]:
    """Per page: text blocks with their dominant font size and boldness."""
    pages = []
    with pymupdf.open(pdf) as doc:
        for i in range(min(doc.page_count, MAX_PAGES_FOR_LLM)):
            blocks = []
            for b in doc[i].get_text("dict").get("blocks", []):
                if b.get("type") != 0:
                    continue
                lines, sizes, bold = [], Counter(), 0
                for ln in b.get("lines", []):
                    text = "".join(sp["text"] for sp in ln.get("spans", []))
                    if not text.strip():
                        continue
                    lb = 0
                    for sp in ln["spans"]:
                        n = len(sp["text"].strip())
                        sizes[round(sp["size"], 1)] += n
                        if sp.get("flags", 0) & 16 or re.search(r"bold|semibold|black|heavy", sp.get("font", ""), re.I):
                            lb += n
                    lines.append((text, lb >= max(1, len(text.strip())) * 0.6))
                    bold += lb
                total = sum(sizes.values())
                if total:
                    blocks.append({"lines": lines, "size": sizes.most_common(1)[0][0],
                                   "bold": bold >= total * 0.6, "chars": total})
            pages.append(blocks)
    return pages


def paragraphs(pdf: str | Path) -> list[dict]:
    """Reading-order paragraphs of the article text.

    Drops running headers/footers, the reference list and the small text
    inside figures (axis labels etc.); keeps figure legends.
    """
    pages = _blocks(pdf)
    sizes = Counter()
    for page in pages:
        for b in page:
            sizes[b["size"]] += b["chars"]
    if not sizes:
        return []
    body = sizes.most_common(1)[0][0]

    def key(t: str) -> str:
        return re.sub(r"\d+", "#", t.strip().lower())[:120]

    # running headers/footers: short lines repeated on many pages (page numbers normalised)
    counts = Counter(k for page in pages for k in {key(t) for b in page for t, _ in b["lines"] if len(t) < 200})
    limit = max(3, int(len(pages) * 0.3))
    repeated = {k for k, n in counts.items() if n >= limit} if len(pages) >= 4 else set()
    out: list[dict] = []
    in_refs = False
    stop = False
    for page in pages:
        if stop:
            break
        for b in page:
            b = dict(b, lines=[(t, bd) for t, bd in b["lines"] if key(t) not in repeated])
            if not b["lines"]:
                continue
            raw = "\n".join(t for t, _ in b["lines"])
            first = b["lines"][0][0].strip()
            if _END_RE.match(first):
                stop = True  # journal reporting-summary forms are not article text
                break
            legend = bool(_LEGEND_RE.match(first))
            if not legend and b["size"] < body - 1.4:
                continue  # text inside figures, footnotes, small print
            heading_like = (b["bold"] or b["size"] > body + 0.8) and len(raw) < 160 and not raw.rstrip().endswith(".")
            if _REFS_HEAD.fullmatch(first):
                in_refs = True  # Nature puts Methods after the references: skip until the next main section
                rest = [ln for ln in b["lines"][1:] if not _REFERENCE_LINE_RE.search(ln[0].strip())]
                if not rest:
                    continue
                b = dict(b, lines=rest)
                raw = "\n".join(t for t, _ in rest)
                first = rest[0][0].strip()
            if in_refs and _AFTER_REFS.match(first):
                in_refs = False
            if in_refs:
                continue
            if heading_like and not legend:
                t = _clean(raw)
                if 6 <= len(t) <= 160 and re.search(r"[A-Za-z]{3,}", t):  # not panel letters like "d e"
                    out.append({"kind": "heading", "src": t})
                continue
            lines = list(b["lines"])
            head = []  # bold first line(s) of a body block = inline subsection heading
            while lines and lines[0][1] and not legend and len(lines) > 1 and len(lines[0][0]) < 120:
                head.append(lines.pop(0)[0])
            if head and re.search(r"[A-Za-z]{3,}", " ".join(head)):
                out.append({"kind": "heading", "src": _clean("\n".join(head))})
            kept: list[str] = []
            for t, _ in lines:
                st = t.strip()
                if _REFS_HEAD.fullmatch(st):
                    in_refs = True
                    continue
                if in_refs and _AFTER_REFS.fullmatch(st):
                    in_refs = False
                if in_refs or _REFERENCE_LINE_RE.search(st):
                    continue
                kept.append(t)
            for piece in re.split(r"\n\s*\n", "\n".join(kept)):
                t = _clean(piece)
                if len(t) >= 25:
                    out.append({"kind": "para", "src": t})
    return [dict(p, kind="meta") if p["kind"] in ("para", "heading") and is_meta(p["src"]) else p for p in _merge(out)]


_END_RE = re.compile(r"(nature portfolio )?reporting summary\b|(nature research )?reporting summary\b", re.I)


def _merge(out: list[dict]) -> list[dict]:
    merged: list[dict] = []  # re-join paragraphs split across columns/pages
    for p in out:
        prev = merged[-1] if merged else None
        if (prev and prev["kind"] == "para" and p["kind"] == "para" and not _LEGEND_RE.match(p["src"])
                and not re.search(r"[.!?:)\]]$", prev["src"]) and p["src"][:1].islower()):
            prev["src"] = (prev["src"][:-1] if prev["src"].endswith("-") else prev["src"] + " ") + p["src"]
        else:
            merged.append(dict(p))
    return merged


_NAME = r"[A-Z][a-zA-Z\u00C0-\u024F'\-]+(?:\s+[A-Z][a-zA-Z\u00C0-\u024F'.\-]*)*"
_AUTHORS_RE = re.compile(rf"^(?:{_NAME}\s*[\d,*†‡§#]*\s*(?:,|&|\band\b)\s*){{2,}}{_NAME}\s*[\d,*†‡§#]*\.?$")
_META_RE = re.compile(r"^(https?://|doi[:\s]|received:|accepted:|published( online)?:|e-?mail:|correspondence|"
                      r"check for updates|open access|©|\(c\)|copyright)", re.I)


def _is_author_list(t: str) -> bool:
    """'Xing Cai1,5, Yuhang Xiang2,5 & Meisi Yan2' / 'Grant de Jong1,22, Fani Memi1,22,'"""
    parts = [x for x in re.split(r",\s*(?=[A-Z])|\s*&\s*|\s+and\s+", t.strip().rstrip(",;")) if x.strip()]
    named = [x for x in parts if re.fullmatch(r"(?=[^\d]*[a-z])[A-Z][^\d,;:()✉]{1,45}?\s*\d[\d,*†‡§#✉\s]*", x.strip())]
    return len(named) >= 3 and len(named) >= 0.6 * len(parts)


def is_meta(text: str) -> bool:
    """Author lists, DOIs, dates, e-mails: kept verbatim (names must never be 'translated')."""
    t = text.strip()
    return bool(_META_RE.match(t) or _AUTHORS_RE.match(t) or _is_author_list(t)
                or re.fullmatch(r"[\w.+-]+@[\w.-]+", t))


def chunks(paras: list[dict], limit: int = CHUNK_CHARS) -> list[list[int]]:
    groups, cur, size = [], [], 0
    for i, p in enumerate(paras):
        if p.get("kind") == "meta":
            continue
        n = len(p["src"])
        if cur and size + n > limit:
            groups.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += n
    if cur:
        groups.append(cur)
    return groups


# --------------------------------------------------------------- glossary
_GENE_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,7}(?:-[A-Z0-9]+)?\+?\b")


def glossary(card: dict, taxonomy, text: str, limit: int = 60) -> list[str]:
    """Terms to keep verbatim: tools, platforms, gene symbols, taxonomy synonyms."""
    terms: list[str] = []
    from .kb_fields import tool_name

    for t in card.get("tools", []) + (card.get("design") or {}).get("platforms", []):
        t = tool_name(t)
        if t and t in text:
            terms.append(t)
    if taxonomy is not None:
        for c in taxonomy.categories:
            for s in c.synonyms:
                if re.fullmatch(r"[A-Za-z0-9+\- ]{2,30}", s) and re.search(r"[A-Z]", s) and s in text:
                    terms.append(s)
    genes = Counter(m for m in _GENE_RE.findall(text) if not m.isdigit() and m not in {"DNA", "RNA", "THE", "AND", "FOR"})
    terms += [g for g, n in genes.most_common(40) if n >= 2]
    seen, out = set(), []
    for t in terms:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:limit]


# ---------------------------------------------------------------- prompts
def build_prompt(text: str, target: str, mode: str, custom: str, terms: list[str], numbered: bool) -> str:
    """Mirrors the canonical Index-Translate instruction format (works for general LLMs too)."""
    tgt = LANG_NAMES.get(target, target)
    if mode == "custom" and custom.strip():
        return custom.replace("{target}", tgt).replace("{text}", text)
    constraints = []
    if terms:
        constraints.append("【硬性要求】专名/术语对照: " + "、".join(f"{t}→{t}" for t in terms))
    if numbered:
        constraints.append("【硬性要求】保留每段开头的方括号编号（如 [3]），逐段对应输出，段落数量和顺序不变，不要合并或拆分段落")
    constraints.append("【注意】这是生物医学论文，使用规范的学术中文；基因名、蛋白名、软件名和数据库名保持原文" if target == "zh"
                       else "【注意】This is a biomedical paper; keep gene, protein, software and database names unchanged")
    if mode == "polish":
        head = f"请润色以下学术文本，使其更通顺准确，输出语言为{tgt}，并且严格遵循所有约束要求。"
    else:
        head = f"请将以下论文文本翻译成{tgt}，并且严格遵循所有约束要求。"
    req = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(constraints))
    return f"{head}\n\n【源文】\n{text.strip()}\n\n【约束要求】\n{req}\n\n只输出译文，不要有任何额外说明。"


def parse_numbered(answer: str, ids: list[int]) -> dict[int, str] | None:
    parts = re.split(r"(?m)^\s*\[(\d+)\]\s*", "\n" + answer.strip())
    found = {int(parts[i]): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    return found if set(found) == set(ids) and all(found.values()) else None


# --------------------------------------------------------------- services
def service_by_id(config: Config, sid: str | None) -> dict:
    tr = config.raw["translation"]
    sid = sid or tr.get("default") or "bilibili"
    for s in tr.get("services", []):
        if s.get("id") == sid:
            return s
    raise TranslationError(f"unknown translation service '{sid}'")


class Translator:
    def __init__(self, config: Config, registry: Registry, llm: LLMClient, service: dict,
                 transport: httpx.BaseTransport | None = None):
        self.config, self.registry, self.llm, self.service = config, registry, llm, service
        self.http = httpx.Client(timeout=180, transport=transport,
                                 headers={"User-Agent": f"PaperMap/{__version__}"})
        self.notes: list[str] = []
        self.failures = 0

    @property
    def model(self) -> str:
        kind = self.service["kind"]
        if kind == "bilibili":
            return self.service.get("model") or "Index-Translate-35B-A3B"
        if kind == "main":
            return self.config.effective_llm()["model"]
        return self.service.get("model", "")

    def _bilibili(self, prompt: str, paper_id: int | None) -> str:
        """Streaming request: the free gateway cuts non-streamed answers at ~30 s."""
        body = {"model": self.model, "messages": [{"role": "user", "content": prompt}], "temperature": 0,
                "max_tokens": min(16384, max(512, len(prompt) * 2)), "stream": True,
                "stream_options": {"include_usage": True}, "chat_template_kwargs": {"enable_thinking": False}}
        last: Exception | None = None
        for attempt in range(2):
            try:
                parts, usage = [], {}
                with self.http.stream("POST", f"{BILIBILI_BASE}/chat/completions", json=body,
                                      timeout=httpx.Timeout(180, read=60)) as r:
                    if r.status_code >= 400:
                        raise LLMHTTPError(r.status_code, r.read().decode("utf-8", "replace")[:200])
                    for line in r.iter_lines():
                        if not line.startswith("data: ") or line[6:].strip() == "[DONE]":
                            continue
                        d = json.loads(line[6:])
                        usage = d.get("usage") or usage
                        for ch in d.get("choices") or []:
                            parts.append((ch.get("delta") or {}).get("content") or (ch.get("message") or {}).get("content") or "")
                text = strip_think("".join(parts))
                if not text:
                    raise LLMHTTPError(0, "empty answer")
                self.registry.record_llm_call(paper_id, "translate", self.model, int(usage.get("prompt_tokens") or 0),
                                              int(usage.get("completion_tokens") or 0), 0.0, True)
                return text
            except (httpx.HTTPError, LLMHTTPError, ValueError) as exc:
                last = exc
                time.sleep(2)
        raise LLMHTTPError(getattr(last, "status_code", 0), str(last))

    def _llm(self, prompt: str, paper_id: int | None, kind: str) -> str:
        if kind == "main":
            return self.llm.chat_text("", prompt, purpose="translate", paper_id=paper_id, max_tokens=8192).raw
        s = self.service
        eff = effective_llm({"provider": "custom", "custom": {
            "base_url": s.get("base_url", ""), "api_key": s.get("api_key", ""), "protocol": s.get("protocol", "openai"),
            "model": s.get("model", ""), "price_input_per_1m": s.get("price_input_per_1m", 0),
            "price_output_per_1m": s.get("price_output_per_1m", 0)}})
        return self.llm.chat_text("", prompt, purpose="translate", paper_id=paper_id, max_tokens=8192,
                                  eff=eff, client=make_client(eff))

    def call(self, prompt: str, paper_id: int | None) -> tuple[str, str]:
        """Returns (text, via). Falls back to the main LLM if the service fails."""
        kind = self.service["kind"]
        try:
            if self.failures >= MAX_SERVICE_FAILURES:
                raise LLMHTTPError(0, "service skipped after repeated failures")
            text = self._bilibili(prompt, paper_id) if kind == "bilibili" else self._llm(prompt, paper_id, kind)
            self.failures = 0
            return text, self.model
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            fb = self.config.raw["translation"].get("fallback_to_main", True)
            if kind != "main" and fb and self.config.model_ready():
                log.warning("translation service failed (%s); falling back to main model", exc)
                note = f"部分段落 {self.service.get('name')} 没有响应，改用主模型翻译"
                if note not in self.notes:
                    self.notes.append(note)
                return self._llm(prompt, paper_id, "main"), self.config.effective_llm()["model"] + " (fallback)"
            raise

    def translate_chunk(self, paras: list[dict], ids: list[int], target: str, terms: list[str],
                        paper_id: int | None) -> dict[int, tuple[str, str]]:
        mode, custom = self.service.get("mode", "translate"), self.service.get("prompt", "")
        local_terms = [t for t in terms if any(t in paras[i]["src"] for i in ids)]
        if len(ids) > 1:
            text = "\n\n".join(f"[{n + 1}] {paras[i]['src']}" for n, i in enumerate(ids))
            answer, via = self.call(build_prompt(text, target, mode, custom, local_terms, True), paper_id)
            parsed = parse_numbered(answer, list(range(1, len(ids) + 1)))
            if parsed is not None:
                return {i: (parsed[n + 1], via) for n, i in enumerate(ids)}
        out = {}
        for i in ids:  # alignment failed (or single paragraph): one by one
            answer, via = self.call(build_prompt(paras[i]["src"], target, mode, custom, local_terms, False), paper_id)
            out[i] = (answer.strip(), via)
        return out


# ------------------------------------------------------------------ write
def render_md(meta: dict, result: dict, language: str) -> str:
    zh = language != "en"
    front = {"title": meta.get("title"), "doi": meta.get("doi"), "service": result["service"],
             "model": result["model"], "target_language": result["target"], "generated": result["generated"],
             "prompt_version": PROMPT_VERSION}
    out = ["---", yaml.safe_dump(front, allow_unicode=True, sort_keys=False, width=10**6).strip(), "---", "",
           f"# {'译文对照' if zh else 'Parallel translation'} · {meta.get('title') or ''}", "",
           (f"> 由 {result['service']}（{result['model']}）自动翻译，仅供阅读参考，引用请以原文为准。" if zh
            else f"> Machine translation by {result['service']} ({result['model']}); cite the original."), ""]
    for note in result.get("notes", []):
        out += [f"> ⚠️ {note}", ""]
    for p in result["paragraphs"]:
        if p["kind"] == "meta":
            out += [p["src"], ""]
        elif p["kind"] == "heading":
            out += [f"## {p['src']}", f"*{p['tr']}*", ""]
        else:
            out += [p["src"], "", *[f"> {ln}" for ln in p["tr"].splitlines() if ln.strip()], ""]
    return "\n".join(out).rstrip() + "\n"


def translate_paper(config: Config, registry: Registry, llm: LLMClient, paper_id: int, pdf: Path,
                    card_path: Path, taxonomy=None, service_id: str | None = None,
                    progress: Callable[[int, int], None] | None = None,
                    transport: httpx.BaseTransport | None = None) -> Path:
    row = registry.get_paper(paper_id)
    service = service_by_id(config, service_id)
    target = config.raw["translation"].get("target_language") or "zh"
    paras = paragraphs(pdf)
    if not paras:
        raise TranslationError("no text found in the PDF (scanned image?)")
    card = json.loads(row["card_json"] or "{}")
    terms = glossary(card, taxonomy, " ".join(p["src"] for p in paras))
    tr = Translator(config, registry, llm, service, transport)
    groups = chunks(paras)
    done = 0
    results: dict[int, tuple[str, str]] = {}

    def work(ids: list[int]) -> dict[int, tuple[str, str]]:
        return tr.translate_chunk(paras, ids, target, terms, paper_id)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for part in pool.map(work, groups):
            results.update(part)
            done += 1
            if progress:
                progress(done, len(groups))
    vias = Counter(v for _, v in results.values())
    result = {
        "paper_id": paper_id, "service": service.get("name"), "model": vias.most_common(1)[0][0],
        "target": target, "generated": date.today().isoformat(), "glossary": terms, "notes": tr.notes,
        "paragraphs": [dict(p, tr=results[i][0], via=results[i][1]) if i in results else dict(p, tr=p["src"], via="")
                       for i, p in enumerate(paras)],
    }
    stem = card_path.stem
    json_path = card_path.with_name(f"{stem}.translation.json")
    atomic_write(json_path, json.dumps(result, ensure_ascii=False, indent=1))
    meta = {"title": row["title"], "doi": row["doi"]}
    atomic_write(card_path.with_name(f"{stem}.translation.md"), render_md(meta, result, config.output_language))
    return json_path


def bilibili_cost_note() -> str:
    return "免费（bilibili 官方提供，将来是否收费未知）"


def estimate_chars(pdf: Path) -> int:
    return sum(len(p["src"]) for p in paragraphs(pdf))


def compute_service_cost(eff: dict[str, Any], pt: int, ct: int) -> float:  # pragma: no cover - helper for UI
    return compute_cost(eff, pt, ct)
