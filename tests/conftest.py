"""Shared fixtures: tiny generated PDFs, fake metadata service, fake LLM."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from papermap import config as cfgmod
from papermap.registry import Registry

OWN_DOI = "10.1234/demo.2024.001"
OWN_TITLE = "Spatial transcriptomics reveals fibroblast niches in liver cancer"
REF_DOIS = ["10.1038/s41586-020-0001-1", "10.1016/j.cell.2019.01.002", "10.1126/science.abc1234"]
BODY = " ".join(["We profiled tumours with single-cell RNA sequencing and Visium spatial transcriptomics."] * 10)


def make_pdf(path: Path, pages: list[str], title_meta: str = "", subject: str = "") -> Path:
    doc = pymupdf.open()
    for i, text in enumerate(pages):
        page = doc.new_page()
        if i == 0 and text.startswith("#"):
            title, _, rest = text[1:].partition("\n")
            page.insert_textbox(pymupdf.Rect(50, 50, 550, 130), title, fontsize=18)
            page.insert_textbox(pymupdf.Rect(50, 140, 550, 800), rest, fontsize=9)
        else:
            page.insert_textbox(pymupdf.Rect(50, 50, 550, 800), text, fontsize=9)
    doc.set_metadata({"title": title_meta, "subject": subject})
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def pdfs(tmp_path: Path) -> dict[str, Path]:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    out = {}
    out["paper"] = make_pdf(
        inbox / "paper.pdf",
        [f"#{OWN_TITLE}\nhttps://doi.org/{OWN_DOI}\nAbstract\n{BODY}\nResults\n{BODY}",
         "Methods\n" + BODY + "\nReferences\n" + "\n".join(f"{i+1}. Smith, J. et al. Title. J (2020). doi:{d}" for i, d in enumerate(REF_DOIS))],
    )
    # Page 1 has no own DOI; references with DOIs are on page 2.
    out["refs_only"] = make_pdf(
        inbox / "refs_only.pdf",
        [f"#An unrelated study of bile duct organoids in mice\n{BODY}",
         "References\n" + "\n".join(f"{i+1}. Doe, A. Paper. Journal (2019). {d}" for i, d in enumerate(REF_DOIS))],
    )
    out["nodoi"] = make_pdf(
        inbox / "thesis.pdf",
        [f"#Doctoral thesis on hepatic stellate cell activation\n{BODY}", BODY],
    )
    out["supp"] = make_pdf(
        inbox / "nature_article_reporting.pdf",
        ["Supplementary Information\nSupplementary Figures 1-12 and Tables 1-4\n" + BODY],
    )
    return out


class FakeMetadata:
    """Stands in for MetadataClient; counts calls."""

    def __init__(self, works: dict[str, dict] | None = None):
        self.works = works or {
            OWN_DOI: {"doi": OWN_DOI, "title": OWN_TITLE, "authors": [{"family": "Zhang", "given": "Wei", "name": "Wei Zhang"}],
                      "year": 2024, "journal": "Journal of Demo Hepatology", "journal_abbrev": "J Demo Hepatol",
                      "type": "journal-article", "abstract": "", "relations": {}, "openalex_id": "W1"},
        }
        for d in REF_DOIS:
            self.works.setdefault(d, {"doi": d, "title": f"Some cited work {d}", "authors": [], "year": 2019,
                                       "journal": "Cell", "type": "journal-article", "relations": {}})
        self.calls = 0
        self.search_results: list[dict] = []

    def lookup(self, doi):
        self.calls += 1
        w = self.works.get(doi)
        return dict(w) if w else None

    def crossref_search(self, title, rows=5):
        self.calls += 1
        return list(self.search_results)


CARD = {
    "one_sentence": "Fibroblast niches organize the liver tumour microenvironment.",
    "research_question": "How are fibroblasts spatially organized?",
    "design": {"summary": "Single-cell + spatial", "sample_size": "n=12", "species": ["human"],
               "tissues": ["liver"], "platforms": ["Visium"], "cohorts": []},
    "methods": ["scRNA-seq", "spatial transcriptomics"],
    "tools": ["Seurat", "cell2location"],
    "key_findings": ["Finding one", "Finding two", "Finding three"],
    "limitations": ["Small cohort"],
}


CLASSIFY = {
    "methods": [
        {"slug": "spatial-transcriptomics", "confidence": 0.95, "primary": True, "note": "Visium niches", "reason": "r"},
        {"slug": "scrna-seq", "confidence": 0.9, "primary": False, "note": "", "reason": "r"},
        {"slug": "wet-lab-mechanism", "confidence": 0.45, "primary": False, "note": "", "reason": "weak"},
    ],
    "topics": [{"slug": "tumor-microenvironment", "confidence": 0.8, "reason": "r"}],
    "proposed_new": [{"axis": "topic", "name_en": "Glioma", "name_zh": "胶质瘤", "description": "d", "reason": "r"}],
}


class FakeCompletions:
    """Extraction calls consume ``outputs`` (then CARD); classification calls
    consume ``classify_outputs`` (then CLASSIFY). Counted separately."""

    def __init__(self, outputs, classify_outputs=(), handbook_outputs=()):
        self.handbook_outputs = list(handbook_outputs)
        self.handbook_calls = 0
        self.outputs = list(outputs)
        self.classify_outputs = list(classify_outputs)
        self.calls = 0
        self.classify_calls = 0
        self.last_system = ""

    def create(self, **kwargs):
        system = kwargs["messages"][0]["content"]
        self.last_system = system
        if "study handbook" in system:
            self.handbook_calls += 1
            content = self.handbook_outputs.pop(0) if self.handbook_outputs else json.dumps(HANDBOOK)
        elif "You classify" in system:
            self.classify_calls += 1
            content = self.classify_outputs.pop(0) if self.classify_outputs else json.dumps(CLASSIFY)
        else:
            self.calls += 1
            content = self.outputs.pop(0) if self.outputs else json.dumps(CARD)
        usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=200, prompt_cache_hit_tokens=0)
        msg = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage, model=kwargs["model"])


HANDBOOK = {
    "overview": "Overview text.",
    "A": {
        "representative": [{"text": "Study one mapped niches", "refs": ["P1"]},
                           {"text": "Invented claim", "refs": ["P9"]},
                           {"text": "Uncited claim", "refs": []}],
        "methods": [{"text": "Visium + scRNA-seq integration", "refs": ["P1", "P2"]}],
        "findings": [{"text": "Fibroblasts sit at margins", "refs": ["P2"]}],
    },
    "B": {"core_concepts": [{"term": "Spot", "explanation": "55 um capture area"}],
          "workflow": ["QC", "Deconvolution"], "tools": [{"name": "cell2location", "purpose": "deconvolution"}],
          "pitfalls": ["Spot is not a cell"], "reading_order": ["Start with a review"]},
}


def fake_llm_client(outputs=(), classify_outputs=(), handbook_outputs=()):
    completions = FakeCompletions(outputs, classify_outputs, handbook_outputs)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


@pytest.fixture
def config(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg = cfgmod.from_dict({"inbox_dirs": [str(tmp_path / "inbox")], "data_dir": str(tmp_path / "data"),
                            "taxonomy_file": "liver_omics"},
                           path=tmp_path / "data" / "config.yaml")
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def registry(config):
    reg = Registry(config.db_path)
    yield reg
    reg.close()
