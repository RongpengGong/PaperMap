"""Taxonomy (classification scheme) loading and validation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

AXES = ("method", "topic")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
BUILTIN = {
    "liver_omics": "肝病 × 组学（作者预设）",
    "default": "通用生物医学（起步）",
}


class TaxonomyError(Exception):
    pass


@dataclass(frozen=True)
class Category:
    slug: str
    axis: str
    name_zh: str
    name_en: str
    description: str
    synonyms: tuple[str, ...] = ()

    def label(self, language: str = "zh") -> str:
        return self.name_en if language == "en" else self.name_zh


@dataclass
class Taxonomy:
    name: str
    categories: list[Category]
    source: str = ""
    by_slug: dict[str, Category] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.by_slug = {c.slug: c for c in self.categories}

    def axis(self, axis: str) -> list[Category]:
        return [c for c in self.categories if c.axis == axis]

    @property
    def fingerprint(self) -> str:
        """Changes whenever something that affects classification changes."""
        payload = [[c.slug, c.axis, c.description, list(c.synonyms)] for c in self.categories]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()[:12]


def parse(data: Any, source: str = "") -> Taxonomy:
    """Validate a taxonomy mapping; raises TaxonomyError with readable messages."""
    if not isinstance(data, dict):
        raise TaxonomyError("taxonomy must be a YAML mapping with 'method' and 'topic' lists")
    problems: list[str] = []
    cats: list[Category] = []
    seen: set[str] = set()
    for axis in AXES:
        items = data.get(axis)
        if items is None:
            problems.append(f"missing '{axis}' list")
            continue
        if not isinstance(items, list) or not items:
            problems.append(f"'{axis}' must be a non-empty list")
            continue
        for i, it in enumerate(items, 1):
            where = f"{axis} #{i}"
            if not isinstance(it, dict):
                problems.append(f"{where}: must be a mapping")
                continue
            slug = str(it.get("slug", "")).strip()
            where = f"{axis} '{slug or i}'"
            if not SLUG_RE.match(slug):
                problems.append(f"{where}: slug must be lowercase letters/digits joined by '-'")
            elif slug in seen:
                problems.append(f"{where}: duplicate slug")
            seen.add(slug)
            for key in ("name_zh", "name_en", "description"):
                if not str(it.get(key, "")).strip():
                    problems.append(f"{where}: missing {key}")
            syn = it.get("synonyms") or []
            if not isinstance(syn, list):
                problems.append(f"{where}: synonyms must be a list")
                syn = []
            cats.append(Category(
                slug=slug, axis=axis, name_zh=str(it.get("name_zh", "")).strip(),
                name_en=str(it.get("name_en", "")).strip(),
                description=re.sub(r"\s+", " ", str(it.get("description", ""))).strip(),
                synonyms=tuple(str(s).strip() for s in syn if str(s).strip()),
            ))
    if problems:
        raise TaxonomyError("; ".join(problems))
    return Taxonomy(name=str(data.get("name") or source or "taxonomy"), categories=cats, source=source)


def parse_text(text: str, source: str = "") -> Taxonomy:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TaxonomyError(f"YAML syntax error: {exc}") from exc
    return parse(data, source)


def builtin_text(name: str) -> str:
    return resources.files("papermap.taxonomies").joinpath(f"{name}.yaml").read_text("utf-8")


def resolve_path(spec: str, data_dir: Path) -> Path | None:
    """A file path for ``spec``, or None when it names a built-in preset."""
    stem = Path(spec).stem
    if spec in BUILTIN or (stem in BUILTIN and not Path(spec).expanduser().is_absolute()
                           and not (data_dir / spec).exists()):
        return None
    p = Path(spec).expanduser()
    return p if p.is_absolute() else (data_dir / p)


def load(spec: str, data_dir: Path) -> Taxonomy:
    """Load a built-in preset by name (``liver_omics``) or a YAML file path.

    Relative paths are resolved against the data folder. For backward
    compatibility, ``taxonomies/default.yaml`` maps to the built-in preset.
    """
    path = resolve_path(spec, data_dir)
    if path is None:
        name = spec if spec in BUILTIN else Path(spec).stem
        return parse_text(builtin_text(name), source=name)
    if not path.exists():
        raise TaxonomyError(f"taxonomy file not found: {path}")
    return parse_text(path.read_text("utf-8"), source=str(path))
