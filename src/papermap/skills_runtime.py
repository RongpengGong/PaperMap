"""Agent-skill interface (CLAUDE.md §14.5).

A skill is a folder with SKILL.md (YAML frontmatter: name, description) and
optional reference Markdown files. PaperMap runs prompt-only skills with the
configured LLM on the paper's full text. Skills that ship executable scripts
are listed but refused: PaperMap never runs code from a skill.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import date
from importlib import resources
from pathlib import Path

import yaml

from .config import Config
from .kb_writer import atomic_write
from .llm import LLMClient
from .pdf_extract import extract_full_text
from .plugins import _linked
from . import ocr
from .registry import Registry

SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".ts", ".rb", ".pl", ".bat", ".ps1", ".exe", ".command"}
MAX_REFERENCE_CHARS = 60000
MAX_PAPER_CHARS = 200000
FALLBACK_DEFAULT = "papermap-close-reading"
_INSTALL_LOCK = threading.Lock()


@dataclass
class Skill:
    id: str
    name: str
    description: str
    path: Path
    source: str
    body: str = ""
    references: list[tuple[str, str]] = field(default_factory=list)
    supported: bool = True
    reason: str = ""

    @property
    def label(self) -> str:
        return {"nature-paper-xray": "精读 X 光", "papermap-close-reading": "精读"}.get(self.id, self.name)


def skill_dirs(config: Config) -> list[tuple[str, Path]]:
    builtin = Path(str(resources.files("papermap.skills")))
    return [("builtin", builtin), ("papermap", config.data_dir / "skills")]


def _parse(folder: Path, source: str) -> Skill | None:
    md = folder / "SKILL.md"
    if _linked(folder) or _linked(md) or not md.is_file():
        return None
    text = md.read_text("utf-8", errors="replace")
    front, body = {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if m:
        try:
            metadata = yaml.safe_load(m.group(1))
            if isinstance(metadata, dict):
                front = metadata
        except yaml.YAMLError:
            front = {}
        body = m.group(2)
    sk = Skill(id=folder.name, name=str(front.get("name") or folder.name),
               description=re.sub(r"\s+", " ", str(front.get("description") or "")).strip(),
               path=folder, source=source, body=body.strip())
    scripts = [p for p in folder.rglob("*") if p.suffix.lower() in SCRIPT_SUFFIXES and not _linked(p) and p.is_file()
               and not any(part in ("tests", "evals", "test") for part in p.relative_to(folder).parts)]
    if scripts:
        sk.supported = False
        sk.reason = f"需要运行脚本（{', '.join(sorted({p.name for p in scripts})[:3])}），PaperMap 不执行技能里的代码"
    return sk


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root)
    except (OSError, RuntimeError):
        return False


def _references(sk: Skill) -> list[tuple[str, str]]:
    if sk.source == "plugin":
        return []  # The manifest grants access only to its specified prompt file.
    refs, total = [], 0
    root = sk.path.parent.resolve()
    ref_dir = sk.path / "references"
    files = sorted(ref_dir.rglob("*.md")) if _inside(ref_dir, root) and ref_dir.is_dir() else []
    # relative files mentioned in SKILL.md / manifest, e.g. ../nature-shared/core/x.md
    manifest = sk.path / "manifest.yaml"
    manifest_text = _read(manifest) if _inside(manifest, root) else ""
    for rel in re.findall(r"(\.\.?/[\w\-./]+\.md)", sk.body + "\n" + manifest_text):
        files.append(sk.path / rel)
    seen = set()
    for p in files:
        if not _inside(p, root) or not p.is_file():
            continue
        resolved = p.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        t = _read(p)
        if total + len(t) > MAX_REFERENCE_CHARS:
            break
        refs.append((p.name, t))
        total += len(t)
    return refs


def _read(p: Path) -> str:
    try:
        return p.read_text("utf-8", errors="replace")
    except OSError:
        return ""


def discover(config: Config) -> dict[str, Skill]:
    """All skills; later sources override earlier ones with the same id."""
    found: dict[str, Skill] = {}
    with _INSTALL_LOCK:
        for source, d in skill_dirs(config):
            if _linked(d) or not d.is_dir():
                continue
            for folder in sorted(d.iterdir()):
                if folder.name.startswith((".", "_")) or _linked(folder) or not folder.is_dir():
                    continue
                sk = _parse(folder, source)
                if sk is not None:
                    found[sk.id] = sk
    from .plugin_contributions import prompt_skills

    for skill in prompt_skills(config):
        found[skill.id] = skill
    return found


def default_skill_id(config: Config, skills: dict[str, Skill]) -> str | None:
    chosen = (config.raw.get("skills") or {}).get("default") or ""
    if chosen in skills and skills[chosen].supported:
        return chosen
    if FALLBACK_DEFAULT in skills and skills[FALLBACK_DEFAULT].supported:
        return FALLBACK_DEFAULT
    return next((s.id for s in skills.values() if s.supported), None)


def require_skill(config: Config, skill_id: str) -> Skill:
    skill = discover(config).get(skill_id)
    if skill is None:
        raise ValueError("请选择已安装且可运行的 PaperMap 技能。")
    if not skill.supported:
        raise ValueError(skill.reason)
    return skill


def install(config: Config, src: Path) -> Skill:
    """Copy a skill folder into <data>/skills."""
    src = Path(src).expanduser()
    if not src.name or src.name.startswith((".", "_")) or _linked(src):
        raise ValueError("请选择独立的技能文件夹。")
    md = src / "SKILL.md"
    if _linked(md) or not md.is_file():
        raise ValueError("这个文件夹里没有 SKILL.md，不是一个技能")
    root = config.data_dir / "skills"
    dest = root / src.name
    if _linked(root) or _linked(dest):
        raise ValueError("请选择独立的技能文件夹。")
    resolved_src = src.resolve()
    resolved_dest = dest.resolve()
    if root.resolve().is_relative_to(resolved_src) or resolved_dest in resolved_src.parents:
        raise ValueError("请选择独立的技能文件夹。")
    if resolved_src == resolved_dest:
        existing = _parse(dest, "papermap")
        if existing is None:
            raise ValueError("这个文件夹里没有 SKILL.md，不是一个技能")
        return existing
    root.mkdir(parents=True, exist_ok=True)

    def ignored(folder, names):
        return [n for n in names if n in {".git", "__pycache__", "node_modules"} or _linked(Path(folder) / n)]

    with tempfile.TemporaryDirectory(dir=root, prefix=".install-") as tmp:
        incoming = Path(tmp) / "incoming" / src.name
        shutil.copytree(src, incoming, ignore=ignored)
        skill = _parse(incoming, "papermap")
        if skill is None:
            raise ValueError("这个文件夹里没有 SKILL.md，不是一个技能")
        with _INSTALL_LOCK:
            previous = Path(tmp) / "previous"
            if dest.exists():
                descriptor = dest / "SKILL.md"
                if _linked(dest) or _linked(descriptor) or not descriptor.is_file():
                    raise ValueError("请选择独立的技能文件夹。")
                dest.rename(previous)
            try:
                incoming.rename(dest)
            except OSError:
                if previous.exists():
                    previous.rename(dest)
                raise
        skill.path = dest
        return skill


def output_path(card_path: Path, skill_id: str) -> Path:
    return card_path.with_name(f"{card_path.stem}.{skill_id}.md")


def run(skill: Skill, config: Config, registry: Registry, llm: LLMClient, paper_id: int, pdf: Path,
        card_path: Path) -> Path:
    if not skill.supported:
        raise ValueError(skill.reason)
    row = registry.get_paper(paper_id)
    language = {"zh": "Simplified Chinese (keep gene, software and technical terms in English)",
                "en": "English", "bilingual": "Simplified Chinese with English key terms in parentheses"}.get(
        config.output_language, "Simplified Chinese")
    system = [
        f'You are running the agent skill "{skill.name}" inside PaperMap, a local literature app.',
        "Environment limits: you cannot run tools, scripts or searches, and cannot open files. Ignore any "
        "instruction to load files, run scripts, call other skills or search the web: the reference files are "
        "already included below and the paper's full text is in the user message. If the skill needs something "
        "that is not available here, say so briefly and continue with what you have.",
        f"Write the skill's deliverable as Markdown in {language}. Do not wrap it in a code block.",
        "", "===== SKILL.md =====", skill.body,
    ]
    for name, text in _references(skill):
        system += ["", f"===== reference: {name} =====", text]
    text = extract_full_text(pdf, MAX_PAPER_CHARS, cache=ocr.cache_for(pdf, config.data_dir))
    user = (f"Title: {row['title']}\nJournal: {row['journal'] or ''} ({row['year'] or ''})\nDOI: {row['doi'] or 'none'}\n"
            f"Scope: main text of the PDF as extracted ({len(text):,} characters; figures are not visible, "
            "only their legends).\n\n===== PAPER TEXT =====\n" + text)
    res = llm.chat_text("\n".join(system), user, purpose=f"skill:{skill.id}", paper_id=paper_id, max_tokens=16000)
    front = {"skill": skill.id, "skill_name": skill.name, "title": row["title"], "doi": row["doi"],
             "model": res.model, "generated": date.today().isoformat()}
    head = (f"> 由技能「{skill.label}」生成（模型 {res.model}），基于 PDF 全文文字；图片内容不可见。内容需结合原文核实。"
            if config.output_language != "en" else
            f"> Generated by the skill \"{skill.name}\" ({res.model}) from the PDF text; figures were not visible. Verify against the paper.")
    out = output_path(card_path, skill.id)
    atomic_write(out, "---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False, width=10**6).strip()
                 + "\n---\n\n" + head + "\n\n" + res.raw.strip() + "\n")
    return out
