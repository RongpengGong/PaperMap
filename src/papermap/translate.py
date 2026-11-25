"""Full-text paragraph-aligned translation (CLAUDE.md §14.4).

Services include bilibili Index-Translate, NiuTrans, Zhipu, Tencent, the main
LLM, and custom OpenAI/Anthropic/Gemini-compatible endpoints. Dedicated MT
APIs receive individual paragraphs, while chat models receive numbered chunks.
A glossary keeps gene names, tools and taxonomy terms untranslated.
Output: <stem>.translation.md (Obsidian) + <stem>.translation.json (app view).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from datetime import date
from itertools import groupby
from pathlib import Path
from typing import Any, Callable, NamedTuple

import httpx
import pymupdf
import yaml

from . import __version__, ocr
from .config import Config, OFFICIAL_TRANSLATION_SERVICES, SIGN_IN_PROVIDERS, effective_llm
from .evidence import document_revision
from .kb_writer import atomic_write
from .llm import LLMClient, LLMHTTPError, compute_cost, make_client, strip_think
from .registry import Registry
from .translation_coverage import coverage_gaps, reporting_form_page

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


def require_credentials(service: dict) -> None:
    kind = service["kind"]
    if kind not in OFFICIAL_TRANSLATION_SERVICES:
        return
    required = {"api_key": "API Key"}
    if kind == "niutrans":
        required["app_id"] = "App ID"
    missing = [label for key, label in required.items() if not (service.get(key) or "").strip()]
    if missing:
        raise TranslationError(f"请在「设置 → AI 服务 → 翻译」填写{service['name']}的{'、'.join(missing)}。")


# ------------------------------------------------------------- paragraphs
def _clean(text: str) -> str:
    text = re.sub(r"-\n(?=[a-z])", "", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _is_heading(t: str) -> bool:
    words = t.split()
    return (len(t) < 90 and 1 <= len(words) <= 12 and not t.endswith((".", ",", ";", ":"))
            and t[:1].isupper() and not re.search(r"\b(19|20)\d{2}\b", t))


_CAPTION_ID = (r"(?:(?:extended\s+data|supplementary|supplemental|suppl\.?)\s+)?"
               r"(?:fig(?:ure)?\.?|table)\s*(?:S\s*[-.]?\s*)?\d+[a-z]?\b")
_LEGEND_RE = re.compile(_CAPTION_ID, re.I)
_CAPTION_ONLY = re.compile(_CAPTION_ID + r"(?:\s*\(continued\))?[.:|]?", re.I)
_REFS_HEAD = re.compile(r"(?:\d+(?:\.\d+)*[.)]?\s+)?(?:references(?:\s+(?:and|&)\s+notes)?"
                        r"|bibliography|literature cited)[.:]?", re.I)
_METHODS = r"(?:(?:\d+(?:\.\d+)*[.)]?\s+)?(?:online methods|materials (?:and|&) methods|experimental procedures|star\s*[★⋆*☆]?\s*methods|methods))"
_METHODS_HEAD = re.compile(_METHODS + r"[.:]?", re.I)
_CAPTION_LOCATION = re.compile(r"\(?see (?:the )?(?:figure|table) on (?:the )?(?:previous|next|following) page[.)]*", re.I)
_AFTER_REFS = re.compile(_METHODS + r"|data availability|code availability|"
                         r"acknowledg(e)?ments|author contributions|supplementary information|extended data", re.I)


_APPENDIX_LEAD = re.compile(r"extended data (?:fig(?:ure)?\.?|table)\s*\d|supplementary information\b", re.I)


class _Line(NamedTuple):
    text: str
    bold: bool
    reference_cell: bool = False


def _mark_reference_cells(lines: list[_Line], boxes: list[tuple]) -> list[_Line]:
    """A label beside another cell/page number differs from a heading beside a marginal line number."""
    for i, line in enumerate(lines):
        if not _REFS_HEAD.fullmatch(line.text.strip()):
            continue
        x0, y0, x1, y1 = boxes[i]
        for other, (left, top, right, bottom) in zip(lines, boxes):
            if right > x0 and left < x1:
                continue
            if right <= x0 and other.text.strip().isdigit() and bottom - top < 0.75 * (y1 - y0):
                continue  # Small line numbers in the left margin of a numbered manuscript.
            if min(y1, bottom) - max(y0, top) > 0.7 * min(y1 - y0, bottom - top):
                lines[i] = line._replace(reference_cell=True)
                break
    return lines


def _ocr_lines(words: list) -> list[_Line]:
    lines = [_Line(line, False) for line in ocr.lines(words)]
    if any(_REFS_HEAD.fullmatch(line.text.strip()) for line in lines):
        boxes = []
        for _, group in groupby(words, key=lambda word: word[5]):
            box = pymupdf.Rect()
            for word in group:
                box |= pymupdf.Rect(word[:4])
            boxes.append(tuple(box))
        _mark_reference_cells(lines, boxes)
    return lines


def _caption_lead(lines: list[_Line]) -> str:
    first = lines[0].text.strip()
    if len(lines) > 1 and _CAPTION_LOCATION.fullmatch(first):
        return lines[1].text.strip()
    return first


def _continues_caption(previous: dict, block: dict) -> bool:
    lead = _caption_lead(previous["lines"])
    if not (previous.get("caption_continuation") or _LEGEND_RE.match(lead)):
        return False
    first = _caption_lead(block["lines"])
    if block["chars"] < 25 or _LEGEND_RE.match(first):
        return False
    left, top, _, _ = block["bbox"]
    x0, _, _, bottom = previous["bbox"]
    size = max(previous["size"], block["size"])
    if abs(left - x0) > 2 * size or not -0.4 * size <= top - bottom <= 1.5 * size:
        return False
    if len(previous["lines"]) == 1 and _CAPTION_ONLY.fullmatch(lead):
        return True
    # An unfinished caption may wrap into another PDF block of the same type.
    return (abs(previous["size"] - block["size"]) <= 0.4 and top - bottom <= size
            and not re.search(r"[.!?。！？][)”’]?$", previous["lines"][-1].text.rstrip())
            and (first[:1].islower() or first.startswith("(")))


def _blocks(pdf: str | Path, cache: Path | None = None, *, automatic_ocr: bool = True) -> list[list[dict]]:
    """Per page: text blocks with their dominant font size and boldness."""
    pages = []
    with pymupdf.open(pdf) as doc:
        auto = automatic_ocr and cache is not None and ocr.scanned_document(doc)
        for i in range(doc.page_count):
            blocks = []
            page = doc[i]
            found = ocr.page_words(page, cache, automatic=auto) if cache else None
            if found:
                lines = _ocr_lines(found)
                pages.append([{"lines": lines, "size": 11., "bold": False,
                               "chars": sum(len(line.text) for line in lines)}])
                continue
            for b in page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES).get("blocks", []):
                if b.get("type") != 0:
                    continue
                lines, boxes, sizes, bold = [], [], Counter(), 0
                native_lines = b.get("lines", [])
                for ln in native_lines:
                    text = "".join(sp["text"] for sp in ln.get("spans", []))
                    if not text.strip():
                        continue
                    lb = 0
                    for sp in ln["spans"]:
                        n = len(sp["text"].strip())
                        sizes[round(sp["size"], 1)] += n
                        if sp.get("flags", 0) & 16 or re.search(r"bold|semibold|black|heavy", sp.get("font", ""), re.I):
                            lb += n
                    boxes.append(ln["bbox"])
                    lines.append(_Line(text, lb >= max(1, len(text.strip())) * 0.6))
                    bold += lb
                total = sum(sizes.values())
                if total:
                    block = {"lines": _mark_reference_cells(lines, boxes), "size": sizes.most_common(1)[0][0],
                             "bold": bold >= total * 0.6, "chars": total, "bbox": b["bbox"]}
                    if blocks and _continues_caption(blocks[-1], block):
                        previous = blocks[-1]
                        if len(previous["lines"]) == 1 and _CAPTION_ONLY.fullmatch(_caption_lead(previous["lines"])):
                            blocks.pop()
                            block["lines"] = previous["lines"] + block["lines"]
                            block["chars"] += previous["chars"]
                        else:
                            block["caption_continuation"] = True
                    blocks.append(block)
            pages.append(blocks)
    return pages


# A plain/OCR heading needs bibliographic evidence. These are entry prefixes,
# not patterns for deleting individual lines of prose containing citations.
_ENTRY_START = re.compile(
    r"^(?:(?:\[\d+\]|\d+[.)])\s*[^\W\d_]|"
    r"[^\W\d_][\w’'.-]*(?:[ -][^\W\d_][\w’'.-]*){0,3},\s*[A-Z](?:\.|[, ])|"
    r"[^\W\d_][\w’'.-]+\s+[A-Z]{1,4}(?:,|\.\s)|"
    r"[^\W\d_][\w’'.-]+(?: et al\.| and [^\W\d_][\w’'.-]+)?[, ]+\((?:19|20)\d{2}[a-z]?\))"
)


_ENTRY_NUMBER = re.compile(r"(?:\[\d+\]|\d+[.)])")
_CAPTION_LABEL = re.compile(_CAPTION_ID + r"(?:$|[.:|–—]|\s+[|–—])", re.I)


def _after_bibliography(text: str, styled: bool, block_lead: bool) -> bool:
    section = _AFTER_REFS.match(text)
    if not section:
        return False
    tail = text[section.end():].strip()
    return (not tail or tail[:1] in (":", ".", "–", "—", "|")
            or (block_lead and (styled or bool(_APPENDIX_LEAD.match(text)))))


def _article_blocks(pages: list[list[dict]], body: float) -> list[list[dict]]:
    """Exclude the bibliography, retaining later sections and captions emitted out of visual order."""
    blocks = [(p, b) for p, page in enumerate(pages) for b in page if b["lines"]]
    article: list[list[dict]] = [[] for _ in pages]
    in_refs = False
    methods = False
    for index, (page_no, block) in enumerate(blocks):
        lines = block["lines"]
        start = None if in_refs and not block.get("caption_continuation") else 0
        for i, line in enumerate(lines):
            text = line.text.strip()
            if in_refs:
                if _after_bibliography(text, line.bold or block["size"] > body + 0.8, i == 0):
                    in_refs = False
                    methods = bool(_METHODS_HEAD.fullmatch(text))
                    if start is None:
                        start = i
                elif _CAPTION_LABEL.match(text):
                    # One PDF block may hold a list of separate supplemental captions.
                    if start is not None and start < i:
                        article[page_no].append(dict(block, lines=lines[start:i], methods=methods))
                    start = i
                elif start is not None and (_ENTRY_NUMBER.fullmatch(text) or _ENTRY_START.match(text)):
                    # OCR can put the next citation in the same block as the caption.
                    article[page_no].append(dict(block, lines=lines[start:i], methods=methods))
                    start = None
                continue
            if (i == 0 or line.bold or "bbox" not in block) and _METHODS_HEAD.fullmatch(text):
                methods = True
            if line.reference_cell or not _REFS_HEAD.fullmatch(text):
                continue
            # A PDF can place a heading, an editorial note, a separate number
            # and its citation in one block. OCR similarly groups a whole page.
            following = lines[i+1:i+3]
            if len(following) < 2 and index + 1 < len(blocks):
                following += blocks[index + 1][1]["lines"][:2-len(following)]
            if (len(lines) == 1 or line.bold
                    or any(_ENTRY_NUMBER.fullmatch(n.text.strip()) or _ENTRY_START.match(n.text.strip())
                           for n in following)):
                if start is not None and start < i:
                    article[page_no].append(dict(block, lines=lines[start:i], methods=methods))
                start = None
                in_refs = True
                methods = False
        if start is not None:
            article[page_no].append(dict(block, lines=lines[start:], methods=methods))
    return article


def paragraphs(pdf: str | Path, cache: Path | None = None, *, automatic_ocr: bool = True) -> list[dict]:
    """Reading-order paragraphs of the article text.

    Drops running headers/footers, the reference list and the small text
    inside figures (axis labels etc.); keeps figure legends.
    """
    pages = _blocks(pdf, cache, automatic_ocr=automatic_ocr)
    pages = [[] if reporting_form_page(line.text for b in page for line in b["lines"]) else page for page in pages]
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
    counts = Counter(k for page in pages for k in {key(line.text) for b in page for line in b["lines"] if len(line.text) < 200})
    limit = max(3, int(len(pages) * 0.3))
    repeated = {k for k, n in counts.items() if n >= limit and not _REFS_HEAD.fullmatch(k)
                and not _AFTER_REFS.fullmatch(k)} if len(pages) >= 4 else set()
    if repeated:
        for page in pages:
            for block in page:
                block["lines"] = [line for line in block["lines"]
                                  if key(line.text) not in repeated or _LEGEND_RE.match(line.text.strip())]
    out: list[dict] = []
    method_type_size = None
    for page_number, page in enumerate(_article_blocks(pages, body)):
        for b in page:
            raw = "\n".join(line.text for line in b["lines"])
            first = b["lines"][0].text.strip()
            legend = bool(b.get("caption_continuation") or _LEGEND_RE.match(_caption_lead(b["lines"])))
            method_heading = len(b["lines"]) == 1 and bool(_METHODS_HEAD.fullmatch(first))
            # Journals often typeset the complete methods section in smaller type.
            if not b["methods"] or _METHODS_HEAD.fullmatch(first):
                method_type_size = None
            method_text = False
            if b["methods"] and len(re.findall(r"\b[^\W\d_]{2,}\b", raw)) >= 5:
                if method_type_size is None and not b["bold"]:
                    method_type_size = b["size"]
                method_text = method_type_size is not None and b["size"] >= method_type_size - 0.6
            if not legend and not method_heading and not method_text and b["size"] < body - 1.4:
                continue  # text inside figures, footnotes, small print
            heading_like = (method_heading or b["bold"] or b["size"] > body + 0.8) and len(raw) < 160 and not raw.rstrip().endswith(".")
            if heading_like and not legend:
                t = _clean(raw)
                if 6 <= len(t) <= 160 and re.search(r"[A-Za-z]{3,}", t):  # not panel letters like "d e"
                    out.append({"kind": "heading", "src": t, "page": page_number, "endPage": page_number})
                continue
            lines = list(b["lines"])
            head = []  # bold first line(s) of a body block = inline subsection heading
            while lines and lines[0].bold and not legend and len(lines) > 1 and len(lines[0].text) < 120:
                head.append(lines.pop(0).text)
            if head and re.search(r"[A-Za-z]{3,}", " ".join(head)):
                out.append({"kind": "heading", "src": _clean("\n".join(head)), "page": page_number, "endPage": page_number})
            for piece in re.split(r"\n\s*\n", "\n".join(line.text for line in lines)):
                t = _clean(piece)
                if len(t) >= 25:
                    out.append({"kind": "para", "src": t, "page": page_number, "endPage": page_number})
    return [dict(p, kind="meta") if p["kind"] in ("para", "heading") and is_meta(p["src"]) else p for p in _merge(out)]


def _merge(out: list[dict]) -> list[dict]:
    merged: list[dict] = []  # re-join paragraphs split across columns/pages
    for p in out:
        prev = merged[-1] if merged else None
        if (prev and prev["kind"] == "para" and p["kind"] == "para" and not _LEGEND_RE.match(p["src"])
                and not re.search(r"[.!?:)\]]$", prev["src"]) and p["src"][:1].islower()):
            prev["src"] = (prev["src"][:-1] if prev["src"].endswith("-") else prev["src"] + " ") + p["src"]
            prev["endPage"] = p["endPage"]
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
    return found if len(parts) == 2 * len(ids) + 1 and set(found) == set(ids) and all(found.values()) else None


# --------------------------------------------------------------- services
def service_by_id(config: Config, sid: str | None) -> dict:
    tr = config.raw["translation"]
    sid = sid or tr.get("default") or "bilibili"
    from .plugin_contributions import translation_services

    for s in translation_services(config):
        if s.get("id") == sid:
            return s
    raise TranslationError(f"unknown translation service '{sid}'")


def _digest(value) -> str:
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def _route(settings: dict) -> str:
    # URLs and provider-specific request options may contain credentials.
    return _digest([settings.get("base_url", ""), settings.get("protocol", "openai"),
                    settings.get("extra_body") or {}])


def _model_settings(config: Config, provider: str | None = None, model: str | None = None) -> dict:
    llm = dict(config.llm)
    if provider:
        llm["provider"] = provider
    if model:
        llm["model"] = model
        presets = llm.get("presets") or {}
        llm["presets"] = {**presets, provider: {**presets.get(provider, {}), "model": model}}
    return config.resolve_llm(llm)


def _pin_model(eff: dict) -> dict:
    return {"provider": eff["provider"], "model": eff["model"], "route": _route(eff),
            "reasoning_effort": eff.get("reasoning_effort", "")}


def _restore_model(config: Config, pin: dict) -> dict:
    eff = _model_settings(config, pin["provider"], pin["model"])
    if _route(eff) != pin["route"]:
        raise TranslationError("原模型的服务地址或请求配置已经改变，请重新提交翻译。")
    return {**eff, "model": pin["model"], "reasoning_effort": pin["reasoning_effort"]}


def translation_options(config: Config, service: dict, pdf: Path) -> dict:
    kind = service["kind"]
    options = {"service": {k: service[k] for k in ("id", "name", "kind", "model", "mode", "prompt", "provider")
                           if k in service},
               "revision": document_revision(pdf), "route": _route(service), "prompt_version": PROMPT_VERSION,
               "target": config.raw["translation"].get("target_language") or "zh",
               "output_language": config.output_language}
    if kind in OFFICIAL_TRANSLATION_SERVICES:
        options["service"]["model"] = OFFICIAL_TRANSLATION_SERVICES[kind]["model"]
    elif kind == "bilibili":
        options["service"]["model"] = service.get("model") or "Index-Translate-35B-A3B"
    if kind == "main" or config.raw["translation"].get("fallback_to_main", True):
        options["main"] = _pin_model(config.effective_llm())
    if kind == "plugin":
        options["plugin"] = _pin_model(_model_settings(config, service["provider"]))
    return options


class Translator:
    def __init__(self, config: Config, registry: Registry, llm: LLMClient, service: dict,
                 transport: httpx.BaseTransport | None = None, *, options: dict | None = None):
        self.config, self.registry, self.llm, self.service = config, registry, llm, service
        self.options = options
        self.main_eff = None
        self.plugin_eff = None
        if service["kind"] == "main" and options:
            self.main_eff = _restore_model(config, options["main"])
        if service["kind"] == "plugin" and options:
            self.plugin_eff = _restore_model(config, options["plugin"])
        self.http = httpx.Client(timeout=180, transport=transport,
                                 headers={"User-Agent": f"PaperMap/{__version__}"})
        self.notes: list[str] = []
        self.failures = 0

    @property
    def model(self) -> str:
        kind = self.service["kind"]
        if kind in OFFICIAL_TRANSLATION_SERVICES:
            return OFFICIAL_TRANSLATION_SERVICES[kind]["model"]
        if kind == "bilibili":
            return self.service.get("model") or "Index-Translate-35B-A3B"
        if kind == "main":
            return (self.main_eff or self.config.effective_llm())["model"]
        if kind == "plugin":
            return self._provider_settings()["model"]
        return self.service.get("model", "")

    def _provider_settings(self) -> dict:
        return self.plugin_eff or _model_settings(self.config, self.service["provider"])

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
            return self.llm.chat_text("", prompt, purpose="translate", paper_id=paper_id, max_tokens=8192,
                                      eff=self.main_eff).raw
        if kind == "plugin":
            eff = self._provider_settings()
            return self.llm.chat_text("", prompt, purpose="translate", paper_id=paper_id, max_tokens=8192, eff=eff).raw
        s = self.service
        eff = effective_llm({"provider": "custom", "custom": {
            "base_url": s.get("base_url", ""), "api_key": s.get("api_key", ""), "protocol": s.get("protocol", "openai"),
            "model": s.get("model", ""), "price_input_per_1m": s.get("price_input_per_1m", 0),
            "price_output_per_1m": s.get("price_output_per_1m", 0)}})
        return self.llm.chat_text("", prompt, purpose="translate", paper_id=paper_id, max_tokens=8192,
                                  eff=eff, client=make_client(eff)).raw

    def _official(self, prompt: str, source: str, target: str, terms: list[str], paper_id: int | None) -> str:
        from . import niutrans, translation_models

        kind = self.service["kind"]
        key = self.service["api_key"]
        if kind == "niutrans":
            out = niutrans.translate(source, target, self.service["app_id"], key, client=self.http, terms=terms)
            pt, ct = 0, 0  # This service meters characters rather than tokens.
        elif kind == "tencent":
            out, pt, ct = translation_models.tencent_translate(self.http, source, target, key, terms)
        else:
            out, pt, ct = translation_models.zhipu_translate(self.http, prompt, key)
        billing = "token" if kind == "zhipu" else "external"
        self.registry.record_llm_call(paper_id, "translate", self.model, pt, ct, 0.0, True, billing=billing)
        return out

    def call(self, prompt: str, paper_id: int | None, *, source: str = "", target: str = "zh",
             terms: list[str] | None = None) -> tuple[str, str]:
        """Returns (text, via). Falls back to the main LLM if the service fails."""
        kind = self.service["kind"]
        require_credentials(self.service)
        try:
            if self.failures >= MAX_SERVICE_FAILURES:
                raise LLMHTTPError(0, "service skipped after repeated failures")
            if kind in OFFICIAL_TRANSLATION_SERVICES:
                text = self._official(prompt, source, target, terms or [], paper_id)
            elif kind == "bilibili":
                text = self._bilibili(prompt, paper_id)
            else:
                text = self._llm(prompt, paper_id, kind)
            self.failures = 0
            return text, self.model
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            if kind == "main":
                raise
            if self.options is None:
                enabled = self.config.raw["translation"].get("fallback_to_main", True)
                fallback_ready = enabled and self.config.model_ready()
            elif "main" in self.options:
                self.main_eff = _restore_model(self.config, self.options["main"])
                fallback_ready = self.main_eff["api_key"] or self.main_eff["provider"] in SIGN_IN_PROVIDERS
            else:
                fallback_ready = False
            if fallback_ready:
                log.warning("translation service failed (%s); falling back to main model", exc)
                note = f"部分段落 {self.service.get('name')} 没有响应，改用主模型翻译"
                if note not in self.notes:
                    self.notes.append(note)
                model = (self.main_eff or self.config.effective_llm())["model"]
                return self._llm(prompt, paper_id, "main"), model + " (fallback)"
            raise

    def translate_text(self, text: str, target: str, terms: list[str], paper_id: int | None,
                       *, numbered: bool = False) -> tuple[str, str]:
        local_terms = [term for term in terms if term in text]
        prompt = build_prompt(text, target, self.service.get("mode", "translate"),
                              self.service.get("prompt", ""), local_terms, numbered)
        return self.call(prompt, paper_id, source=text, target=target, terms=local_terms)

    def translate_chunk(self, paras: list[dict], ids: list[int], target: str, terms: list[str],
                        paper_id: int | None, save: Callable[[dict], None] | None = None) -> dict[int, tuple[str, str]]:
        if len(ids) > 1 and self.service["kind"] not in ("niutrans", "tencent"):
            text = "\n\n".join(f"[{n + 1}] {paras[i]['src']}" for n, i in enumerate(ids))
            answer, via = self.translate_text(text, target, terms, paper_id, numbered=True)
            parsed = parse_numbered(answer, list(range(1, len(ids) + 1)))
            if parsed is not None:
                part = {i: (parsed[n + 1], via) for n, i in enumerate(ids)}
                if save:
                    save(part)
                return part
        out = {}
        for i in ids:  # alignment failed (or single paragraph): one by one
            answer, via = self.translate_text(paras[i]["src"], target, terms, paper_id)
            out[i] = (answer.strip(), via)
            if save:
                save({i: out[i]})
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
    if result.get("review_pages"):
        pages = page_ranges(result["review_pages"])
        out += [f"> PDF 第 {pages} 页可能漏译，请对照原文核对。" if zh
                else f"> Check PDF pages {pages} for possible omissions.", ""]
    for p in result["paragraphs"]:
        if p["kind"] == "meta":
            out += [p["src"], ""]
        elif p["kind"] == "heading":
            out += [f"## {p['src']}", f"*{p['tr']}*", ""]
        else:
            out += [p["src"], "", *[f"> {ln}" for ln in p["tr"].splitlines() if ln.strip()], ""]
    return "\n".join(out).rstrip() + "\n"


def _translation_settings(config, registry, pdf, service_id, job):
    # Old queued jobs only have a service id. Pin the rest before their first request.
    options = json.loads(job["options_json"]) if job and job["options_json"] else None
    service = service_by_id(config, options["service"]["id"] if options else service_id)
    if options is None:
        options = translation_options(config, service, pdf)
        if job:
            registry.execute("UPDATE jobs SET arg=?, options_json=? WHERE id=?",
                             (service["id"], json.dumps(options, ensure_ascii=False, sort_keys=True), job["id"]))
    saved = options["service"]
    if options["revision"] != document_revision(pdf) or options["prompt_version"] != PROMPT_VERSION:
        raise TranslationError("PDF 版本或翻译提示词版本已经改变，请重新提交翻译。")
    if (service["kind"] != saved["kind"] or _route(service) != options["route"] or
            (saved["kind"] in OFFICIAL_TRANSLATION_SERVICES and
             saved["model"] != OFFICIAL_TRANSLATION_SERVICES[saved["kind"]]["model"])):
        raise TranslationError("原翻译服务已经变更，请重新提交翻译。")
    # Only credentials and connection details come from current settings.
    service = {**{k: service[k] for k in ("api_key", "app_id", "base_url", "protocol", "price_input_per_1m",
                                          "price_output_per_1m") if k in service}, **saved}
    require_credentials(service)
    return service, options


def _load_parts(registry, job_id, paras, terms):
    if job_id is None:
        return terms, {}
    results = {}
    digest = _digest([(p["kind"], p["src"]) for p in paras])
    with registry.transaction() as conn:
        previous = conn.execute("SELECT * FROM translation_inputs WHERE job_id=?", (job_id,)).fetchone()
        if previous and previous["paragraphs_digest"] == digest:
            terms = json.loads(previous["glossary_json"])
            for part in conn.execute("SELECT * FROM translation_parts WHERE job_id=?", (job_id,)).fetchall():
                i = part["paragraph"]
                if 0 <= i < len(paras) and paras[i]["kind"] != "meta" and part["text"].strip():
                    results[i] = (part["text"], part["model"])
                else:
                    conn.execute("DELETE FROM translation_parts WHERE job_id=? AND paragraph=?", (job_id, i))
        else:
            # OCR or extraction can change paragraph numbers even for the same PDF.
            conn.execute("DELETE FROM translation_parts WHERE job_id=?", (job_id,))
            conn.execute("INSERT OR REPLACE INTO translation_inputs VALUES (?,?,?)",
                         (job_id, digest, json.dumps(terms, ensure_ascii=False)))
    return terms, results


def page_ranges(pages: list[int]) -> str:
    """Compact sorted, 1-based physical page numbers for the reader."""
    ranges: list[list[int]] = []
    for page in sorted(set(pages)):
        if ranges and page == ranges[-1][-1] + 1:
            ranges[-1][1] = page
        else:
            ranges.append([page, page])
    return ", ".join(str(start) if start == end else f"{start}–{end}" for start, end in ranges)


def _translated_rows(saved):
    return [p for p in saved if isinstance(p, dict) and isinstance(p.get("src"), str) and p["src"].strip()
            and isinstance(p.get("tr"), str) and p["tr"].strip()]


def saved_translation_gaps(pdf: Path, saved: list[dict], cache: Path) -> list[int]:
    """Check legacy files even when they carry no page offsets or have different paragraph breaks."""
    translated = _translated_rows(saved)
    missing = set(coverage_gaps(pdf, translated, cache=cache))
    expected = [p for p in paragraphs(pdf, cache=cache, automatic_ocr=False) if p["kind"] != "meta"]
    if not expected:
        raise TranslationError("无法从 PDF 提取可核对的正文。")

    def key(text):
        return "".join(unicodedata.normalize("NFKC", text).split())

    original = [key(p["src"]) for p in translated if p.get("kind") != "meta"]
    current = [key(p["src"]) for p in expected]
    for op, a, b, c, d in SequenceMatcher(None, original, current, autojunk=False).get_opcodes():
        if op in ("equal", "delete"):
            continue
        # Accept split or joined paragraphs, in order, without reusing the same saved passage twice.
        fragment = "".join(original[a:b])
        offset = 0
        for i in range(c, d):
            at = fragment.find(current[i], offset)
            if at >= 0:
                offset = at + len(current[i])
            else:
                missing.update(range(expected[i]["page"] + 1, expected[i]["endPage"] + 2))
    return sorted(missing)


def _reuse_previous(json_path: Path, paper_id: int, options: dict, paras: list[dict], results: dict) -> int:
    """补译: paragraphs whose source text is unchanged keep the earlier translation, so repairing an old
    translation with omissions only pays for the passages that were missing or are split differently."""
    try:
        old = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return 0
    if (not isinstance(old, dict) or old.get("paper_id", paper_id) != paper_id or not isinstance(old.get("paragraphs"), list)
            or old.get("revision") not in (None, options["revision"]) or old.get("target", options["target"]) != options["target"]):
        return 0

    def key(text):
        return "".join(unicodedata.normalize("NFKC", text).split())

    known = {key(p["src"]): (p["tr"], p.get("via") or old.get("model") or "")
             for p in _translated_rows(old["paragraphs"]) if p.get("kind") != "meta" and p["tr"].strip() != p["src"].strip()}
    n = 0
    for i, p in enumerate(paras):
        if p["kind"] == "meta" or i in results:
            continue
        hit = known.get(key(p["src"]))
        if hit:
            results[i] = hit
            n += 1
    return n


def translate_paper(config: Config, registry: Registry, llm: LLMClient, paper_id: int, pdf: Path,
                    card_path: Path, taxonomy=None, service_id: str | None = None,
                    progress: Callable[[int, int], None] | None = None,
                    transport: httpx.BaseTransport | None = None, *, job: dict | None = None,
                    current_pdf: Callable[[], Path] | None = None) -> Path:
    row = registry.get_paper(paper_id)
    service, options = _translation_settings(config, registry, pdf, service_id, job)
    target = options["target"]
    cache = ocr.cache_for(pdf, config.data_dir)
    paras = paragraphs(pdf, cache=cache)
    if not paras:
        raise TranslationError("no text found in the PDF (scanned image?)")
    review_pages = coverage_gaps(pdf, paras, cache=cache)
    json_path = card_path.with_suffix(".translation.json")
    if review_pages and json_path.exists():
        try:
            old = json.loads(json_path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            old = None
        if (isinstance(old, dict) and old.get("paper_id", paper_id) == paper_id
                and old.get("revision") in (None, options["revision"]) and isinstance(old.get("paragraphs"), list)):
            translated = _translated_rows(old["paragraphs"])
            newly_missing = set(review_pages).difference(coverage_gaps(pdf, translated, cache=cache)) if translated else set()
            if newly_missing:
                raise TranslationError(f"此次提取可能漏掉 PDF 第 {page_ranges(sorted(newly_missing))} 页的内容，已保留原译文。")
    card = json.loads(row["card_json"] or "{}")
    terms = glossary(card, taxonomy, " ".join(p["src"] for p in paras))
    job_id = job["id"] if job else None
    terms, results = _load_parts(registry, job_id, paras, terms)
    reused = _reuse_previous(json_path, paper_id, options, paras, results) if options.get("reuse_previous") else 0
    groups = chunks(paras)
    todo = [[i for i in group if i not in results] for group in groups]
    expected = {i for i, p in enumerate(paras) if p["kind"] != "meta"}
    total = len(expected)
    if progress:
        progress(len(results), total)
    mutex = threading.Lock()
    remaining = iter(group for group in todo if group)
    errors = []
    tr = Translator(config, registry, llm, service, transport, options=options)
    if reused:
        tr.notes.append(f"补译：沿用旧译 {reused} 段，本次翻译 {total - reused} 段")

    def save(part):
        if not part.keys() <= expected or any(not text.strip() for text, _ in part.values()):
            raise TranslationError("翻译服务返回了缺失或无效的译文段落。")
        if job_id is not None:
            with registry.transaction() as conn:
                conn.executemany("INSERT INTO translation_parts(job_id,paragraph,text,model) VALUES (?,?,?,?)",
                                 [(job_id, i, text, model) for i, (text, model) in part.items()])
        with mutex:
            results.update(part)
            if progress:
                progress(len(results), total)

    def work() -> None:
        while True:
            with mutex:
                ids = next(remaining, None) if not errors else None
            if ids is None:
                return
            try:
                tr.translate_chunk(paras, ids, target, terms, paper_id, save)
            except Exception as exc:
                with mutex:
                    errors.append(exc)
                return

    # A ThreadPoolExecutor joins all scheduled groups during interpreter shutdown.
    # Daemon workers let the desktop actually quit while a provider request is in flight.
    with tr.http:
        workers = [threading.Thread(target=work, daemon=True, name="translation")
                   for _ in range(min(WORKERS, sum(bool(ids) for ids in todo)) - 1)]
        for thread in workers:
            thread.start()
        work()
        for thread in workers:
            thread.join()
    if errors:
        raise errors[0]
    if results.keys() != expected:
        raise TranslationError(f"段落校验未通过：应有 {total} 段译文，实有 {len(results)} 段。")
    if document_revision(current_pdf() if current_pdf else pdf) != options["revision"]:
        raise TranslationError("翻译期间 PDF 已经改变，请重新提交翻译。")
    vias = Counter(results[i][1] for i in sorted(results))
    if not tr.notes and any(v.endswith(" (fallback)") for v in vias):
        tr.notes.append(f"部分段落 {service.get('name')} 没有响应，改用主模型翻译")
    result = {
        "paper_id": paper_id, "service": service.get("name"), "model": vias.most_common(1)[0][0] if vias else tr.model,
        "target": target, "revision": options["revision"], "generated": date.today().isoformat(), "glossary": terms, "notes": tr.notes,
        "review_pages": review_pages,
        "paragraphs": [dict(p, tr=p["src"], via="") if p["kind"] == "meta" else dict(p, tr=results[i][0], via=results[i][1])
                       for i, p in enumerate(paras)],
    }
    stem = card_path.stem
    atomic_write(json_path, json.dumps(result, ensure_ascii=False, indent=1))
    meta = {"title": row["title"], "doi": row["doi"]}
    atomic_write(card_path.with_name(f"{stem}.translation.md"), render_md(meta, result, options["output_language"]))
    return json_path


def bilibili_cost_note() -> str:
    return "免费（bilibili 官方提供，将来是否收费未知）"


def estimate_chars(pdf: Path) -> int:
    return sum(len(p["src"]) for p in paragraphs(pdf))


def compute_service_cost(eff: dict[str, Any], pt: int, ct: int) -> float:  # pragma: no cover - helper for UI
    return compute_cost(eff, pt, ct)
