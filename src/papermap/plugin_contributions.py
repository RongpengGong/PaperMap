"""Version 1 declarative contributions. Only data and plain-text templates."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from pathlib import Path
from string import Template
from urllib.parse import urlsplit

from .config import PROTOCOLS

FIELDS = {
    "providers": {"id", "name", "base_url", "protocol", "group", "default_model", "models"},
    "translations": {"id", "name", "provider", "mode", "prompt"},
    "skills": {"id", "name", "description", "prompt_file"},
    "exports": {"id", "name", "template", "extension"},
}
PAPER_FIELDS = ("id", "title", "first_author", "journal", "year", "doi")
CARD_FIELDS = ("research_question", "one_sentence", "key_findings", "limitations")
SLUG = re.compile(r"[a-z][a-z0-9-]{0,62}")


def _text(item, key, limit=256, *, required=True):
    value = item.get(key, "")
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f"Invalid contribution field: {key}")
    return value.strip()


def _endpoint(value):
    parts = urlsplit(value)
    if (not parts.hostname or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or "\\" in value or any(ord(c) <= 32 for c in value)):
        raise ValueError("Invalid provider base_url")
    # Accessing port also validates its syntax and range.
    if parts.port == 0:
        raise ValueError("Invalid provider port")
    if parts.scheme == "https":
        return
    try:
        loopback = ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        loopback = parts.hostname == "localhost"
    if parts.scheme != "http" or not loopback:
        raise ValueError("Provider base_url must use HTTPS or loopback HTTP")


def _prompt_file(root, item, files):
    name = _text(item, "prompt_file")
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts or name != rel.as_posix() or rel.suffix.lower() != ".md":
        raise ValueError("Skill prompt_file must be a relative Markdown file")
    if files is not None:
        content = files.get(name)
        if content is None:
            raise ValueError(f"Missing plugin prompt: {name}")
    else:
        path = root / rel
        if path.resolve() != root.resolve() / rel or not path.is_file() or path.stat().st_size > 128 * 1024:
            raise ValueError("Skill prompt_file must be a regular Markdown file within the plugin")
        content = path.read_bytes()
    if len(content) > 128 * 1024:
        raise ValueError("Skill prompt_file must be at most 128 KiB")
    return content.decode("utf-8")


def _validate_provider(item):
    _endpoint(_text(item, "base_url", 2048))
    if item.get("protocol", "openai") not in tuple(PROTOCOLS):
        raise ValueError("Unsupported plugin provider protocol")
    if item.get("group", "api") not in ("api", "subscription"):
        raise ValueError("Unsupported plugin provider group")
    _text(item, "default_model")
    models = item.get("models", {})
    if not isinstance(models, dict) or len(models) > 100:
        raise ValueError("Provider models must be a mapping of model IDs to prices")
    for name, model in models.items():
        if (not isinstance(name, str) or not name.strip() or not isinstance(model, dict)
                or model.keys() - {"in", "hit", "out", "label"}):
            raise ValueError("Invalid provider model")
        _text(model, "label", required=False)
        for key in ("in", "hit", "out"):
            value = model.get(key, 0)
            if type(value) not in (float, int) or value < 0 or value > 10**9 or not math.isfinite(value):
                raise ValueError(f"Invalid price for {name}")


def validate_contributions(root: Path, contributions: dict, *, files=None) -> dict[str, str]:
    """Validate the manifest and decode prompts from the approved package snapshot."""
    if contributions.keys() - FIELDS.keys():
        raise ValueError("Unknown contribution section")
    prompts = {}
    providers = set()
    for section, allowed in FIELDS.items():
        rows = contributions.get(section, [])
        if not isinstance(rows, list) or len(rows) > 64:
            raise ValueError(f"{section} must be a list of at most 64 contributions")
        seen = set()
        for item in rows:
            if not isinstance(item, dict):
                raise ValueError(f"Each {section} entry must be a mapping")
            if item.keys() - allowed:
                raise ValueError(f"Unknown field in {section}: {item.keys() - allowed}")
            sid = _text(item, "id")
            if not SLUG.fullmatch(sid) or sid != item["id"] or sid in seen:
                raise ValueError(f"Invalid or duplicate {section} ID: {sid}")
            seen.add(sid)
            _text(item, "name")
            if section == "providers":
                _validate_provider(item)
                providers.add(sid)
            elif section == "translations":
                if item.get("provider", "main") not in ("main", *providers):
                    raise ValueError("Translation provider must be main or a provider in this plugin")
                if item.get("mode", "translate") not in ("translate", "custom", "polish"):
                    raise ValueError("Unknown translation mode")
                _text(item, "prompt", 16384, required=item.get("mode") == "custom")
            elif section == "skills":
                _text(item, "description", 4096, required=False)
                prompts[item["prompt_file"]] = _prompt_file(root, item, files)
            else:
                t = Template(_text(item, "template", 8192))
                # Template.is_valid / get_identifiers require Python 3.11.
                try:
                    t.substitute(dict.fromkeys((*PAPER_FIELDS, *CARD_FIELDS), ""))
                except (KeyError, ValueError) as exc:
                    raise ValueError(f"Invalid export template: {exc}") from exc
                if item.get("extension", "md") not in ("txt", "md", "bib", "ris"):
                    raise ValueError("Export extension must be txt, md, bib or ris")
    return prompts


def entries(config, section, only=None):
    from .plugins import enabled_plugins

    for plugin in enabled_plugins(config, only):
        for item in plugin.contributions.get(section, []):
            yield plugin, item


def _id(plugin, item):
    return f"ext.{plugin.id}.{item['id']}"


def _provider_id(plugin, item):
    # Bind credentials and persisted choices to the original destination.
    dest = item.get("protocol", "openai") + "\n" + item["base_url"].strip().rstrip("/")
    return _id(plugin, item) + "." + hashlib.sha256(dest.encode()).hexdigest()[:16]


def provider_presets(config, only=None):
    result = {}
    for plugin, item in entries(config, "providers", only):
        models = {}
        for mid, m in item.get("models", {}).items():
            models[mid] = {"label": m.get("label") or mid, "in": m.get("in", 0),
                           "hit": m.get("hit", m.get("in", 0)), "out": m.get("out", 0)}
        base = item["base_url"].strip().rstrip("/")
        result[_provider_id(plugin, item)] = {
            "label": item["name"], "group": item.get("group", "api"), "base_url": base,
            "protocol": item.get("protocol", "openai"), "default_model": item["default_model"],
            "models": models, "plugin": plugin.name, "note": f"{plugin.name} · {base}",
        }
    return result


def translation_services(config):
    services = list(config.raw["translation"].get("services", []))
    for plugin, item in entries(config, "translations"):
        pid = item.get("provider", "main")
        svc = {"id": _id(plugin, item), "name": item["name"], "plugin": plugin.name, "kind": "main",
               "mode": item.get("mode", "translate"), "prompt": item.get("prompt", "")}
        if pid != "main":
            provider = next(p for p in plugin.contributions["providers"] if p["id"] == pid)
            svc["kind"] = "plugin"
            svc["provider"] = _provider_id(plugin, provider)
            svc["id"] += "." + svc["provider"].rsplit(".", 1)[1]
        services.append(svc)
    return services


def prompt_skills(config):
    from .skills_runtime import Skill

    for plugin, item in entries(config, "skills"):
        path = plugin.root / item["prompt_file"]
        yield Skill(id=_id(plugin, item), name=item["name"], description=str(item.get("description", "")),
                    source="plugin", path=path.parent, body=plugin.prompt_files[item["prompt_file"]])


def export_templates(config):
    return {_id(plugin, item): item for plugin, item in entries(config, "exports")}


def export_text(template, rows):
    from .citations_export import _bib_escape

    t = Template(template["template"])
    output = []
    for row in rows:
        if row is None:
            continue
        record = {k: "" if row[k] is None else str(row[k]) for k in PAPER_FIELDS}
        card = json.loads(row["card_json"] or "{}")
        for field in CARD_FIELDS:
            value = card.get(field) or ""
            record[field] = "; ".join(map(str, value)) if isinstance(value, list) else str(value)
        if template.get("extension") == "bib":
            record = {k: _bib_escape(v) for k, v in record.items()}
        elif template.get("extension") == "ris":
            record = {k: " ".join(v.splitlines()) for k, v in record.items()}
        output.append(t.substitute(record))
    return "\n".join(output) + "\n"
