"""Minimal local web UI (FastAPI + Jinja2). Listens on loopback only."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import threading
from html import escape
from pathlib import Path
from urllib.parse import unquote

import markdown as md
import nh3
import yaml
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from ..config import OFFICIAL_TRANSLATION_SERVICES, PROTOCOLS, PROVIDERS, Config, ConfigError, normalize_reasoning_effort
from ..plugin_contributions import export_templates, export_text, translation_services
from ..plugins import render_snapshot
from .. import backup as backup_mod
from .. import kb_git, library, reading_progress
from .. import ocr
from .. import reader as reader_mod
from ..llm import check_connection, list_chatgpt_model_options, list_models
from ..pipeline import Pipeline
from ..registry import Registry, now_iso
from ..taxonomy import BUILTIN, TaxonomyError, builtin_text, parse_text, resolve_path
from ..i18n import L

HERE = Path(__file__).parent
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
LANGS = ("zh", "en", "bilingual")
SETTINGS_GROUPS = (
    (L("AI 服务"), (("models", L("模型与账号"), "settings"), ("translation", L("翻译"), "handbooks"))),
    (L("文献库"), (("import", L("导入与文件夹"), "inbox"), ("processing", L("论文处理"), "papers"),
                ("library", L("知识库位置"), "folder"), ("backup", L("备份与恢复"), "folder"),
                ("taxonomy", L("学科与版本"), "fields"))),
    (L("应用"), (("appearance", L("外观"), "settings"), ("extensions", L("插件与接口"), "skills"),
             ("application", L("后台运行"), "events"))),
)
SETTINGS_SECTIONS = {item[0]: (group, item[1]) for group, items in SETTINGS_GROUPS for item in items}


def create_app(config: Config, registry: Registry, pipeline: Pipeline) -> FastAPI:
    from ..plugin_hooks import PluginHooks
    from .plugin_admin import EXAMPLES, mount_admin
    from .plugin_api import mount_api

    hooks = PluginHooks(config, registry)
    app = FastAPI(title="PaperMap", docs_url=None, redoc_url=None, openapi_url=None, lifespan=hooks.lifespan)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["fromjson"] = lambda s: json.loads(s or "[]")

    def _hl(text: str, terms: list[str]):
        from markupsafe import Markup, escape

        out = str(escape(text))
        for t in sorted(terms, key=len, reverse=True):
            out = re.sub(re.escape(str(escape(t))), lambda m: f"<mark>{m.group(0)}</mark>", out, flags=re.I)
        return Markup(out)

    templates.env.filters["hl"] = _hl
    csrf_token = secrets.token_urlsafe(24)
    # cache-busting for static assets (app updates must not show stale CSS/JS)
    static_v = str(int(max(f.stat().st_mtime for f in (HERE / "static").iterdir())))
    templates.env.globals["static_v"] = static_v
    from .. import i18n

    def _lang() -> str:
        return i18n.config_language(config)

    def _gettext(text: str, **kw) -> str:
        out = i18n.translate(_lang(), text)
        return out.format(**kw) if kw else out

    def _finalize(value):
        # labels that Python passes to templates (status names, settings groups...) are translated
        # only on an exact catalog match; papers, notes and other user content never are
        if isinstance(value, i18n.L) and _lang() != i18n.SOURCE:
            return i18n.translate(_lang(), value)
        return value

    from markupsafe import Markup, escape as _escape

    # form values that are saved back (names, titles, notes) must never be translated
    templates.env.filters["keep"] = lambda v: Markup(_escape("" if v is None else v))
    def _fname(row) -> str:
        """A field's display name: English names for an English / Japanese interface."""
        try:
            en, zh = row["name_en"], row["name_zh"]
        except (KeyError, IndexError, TypeError):
            return ""
        return (en or zh or "") if _lang() in ("en", "de") else (zh or en or "")

    templates.env.globals["fname"] = _fname
    templates.env.globals["_"] = _gettext
    templates.env.globals["ui_lang"] = _lang
    templates.env.globals["html_lang"] = lambda: i18n.HTML_LANG[_lang()]
    templates.env.finalize = _finalize

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # Defend against DNS rebinding and cross-site form posts: this server can
        # change the API endpoint/key, so only same-origin local requests pass.
        try:
            host = request.url.hostname
        except ValueError:
            host = None
        if host not in ALLOWED_HOSTS:
            return HTMLResponse("Forbidden host", status_code=403)
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and origin != str(request.base_url).rstrip("/"):
            return HTMLResponse("Forbidden origin", status_code=403)
        if request.method == "GET":
            with render_snapshot():
                return await call_next(request)
        return await call_next(request)

    busy: set[str] = set()  # names of running background jobs

    def background(name: str, fn, *args) -> None:
        def run():
            busy.add(name)
            try:
                fn(*args)
            finally:
                busy.discard(name)
        threading.Thread(target=run, daemon=True).start()

    def md_html(text: str) -> str:
        text = _highlights(text)
        html = md.markdown(_wikilinks(text, registry), extensions=["extra", "sane_lists"])
        # to-do items written in the note editor ("- [ ] x" / "- [x] x")
        html = html.replace('src="../../attachments/', 'src="/attachments/').replace('src="../attachments/', 'src="/attachments/')
        html = re.sub(r"<li>(<p>)?\[([ xX])\] ", lambda m: f"<li>{m.group(1) or ''}{'☐' if m.group(2) == ' ' else '☑'} ", html)
        # external links open in the system browser (pywebview honours target=_blank)
        return re.sub(r'<a href="(https?://)', r'<a target="_blank" rel="noopener" href="\1', _clean_markdown_html(html))

    def render(request: Request, name: str, **ctx):
        is_subscription = config.effective_llm().get("billing") == "subscription"
        from ..themes import normalize as _theme

        ctx.update(
            request=request,
            theme=_theme(config.raw.get("appearance")),
            key_set=config.model_ready(),
            account_provider=config.llm.get("provider", "deepseek"),
            is_subscription=is_subscription,
            month_cost=registry.month_cost(),
            budget=float(config.llm.get("monthly_budget_usd") or 0),
            csrf=csrf_token,
            review_count=_review_count(registry, pipeline.new_field_threshold),
            tracking_badge=_tracking_badge(registry),
            collection_tree=registry.collection_tree(),
            unfiled_n=registry.one("SELECT count(*) AS n FROM papers WHERE status != 'ignored' AND id NOT IN "
                                   "(SELECT paper_id FROM paper_collections)")["n"],
            all_n=registry.one("SELECT count(*) AS n FROM papers WHERE status != 'ignored'")["n"],
            budget_ratio=(registry.month_cost() / float(config.llm.get("monthly_budget_usd") or 0))
            if float(config.llm.get("monthly_budget_usd") or 0) and not is_subscription else 0,
            busy=bool(busy),
        )
        return templates.TemplateResponse(request, name, ctx)

    def paper_rows(q: str = "", status: str = "", c: str = ""):
        sql, params = "SELECT * FROM papers WHERE status != 'ignored'", []
        if c == "none":
            sql += " AND id NOT IN (SELECT paper_id FROM paper_collections)"
        elif c.isdigit():  # a group shows its own papers and those of its subgroups
            ids = registry.collection_descendants(int(c))
            sql += f" AND id IN (SELECT paper_id FROM paper_collections WHERE collection_id IN ({','.join('?' * len(ids))}))"
            params += ids
        if status == "starred":
            sql += " AND starred=1"
        elif status.startswith("r:"):
            sql += " AND read_status=?"
            params.append(status[2:])
        elif status:
            sql += " AND status=?"
            params.append(status)
        if q.strip():
            like = f"%{q.strip()}%"
            sql += (" AND (title LIKE ? OR authors_json LIKE ? OR journal LIKE ? OR doi LIKE ? OR first_author LIKE ?"
                    " OR tags_json LIKE ?)")
            params += [like] * 6
        rows = registry.query(sql + " ORDER BY id DESC LIMIT 2000", tuple(params))
        labels: dict[int, list] = {}
        for r in registry.query(
                "SELECT pf.paper_id, f.name_zh, f.axis FROM paper_fields pf JOIN fields f ON f.id=pf.field_id "
                "WHERE pf.status='accepted' ORDER BY pf.is_primary DESC, pf.confidence DESC"):
            labels.setdefault(r["paper_id"], []).append(r)
        return rows, labels

    def pdf_file(row) -> Path | None:
        if row["kb_path"] and row["kb_path"].count("/") >= 2:
            p = library.pdf_path(config.knowledge_dir, row["kb_path"])
            if p.exists():
                return p
        for f in registry.paper_file_paths(row["id"]):
            if Path(f).exists():
                return Path(f)
        return None

    def paper_panel_ctx(paper_id: int) -> dict | None:
        row = registry.get_paper(paper_id)
        if row is None:
            return None
        body_html, notes_text = "", None
        if row["kb_path"]:
            path = config.knowledge_dir / row["kb_path"]
            if path.exists():
                body = _split_front(path.read_text("utf-8"))[1]
                body = re.sub(r"^#\s.*\n", "", body.lstrip("\n"), count=1)  # title shown in the header
                body = re.sub(r"^##\s+(我的笔记|My notes)\s*\n(.*\n)*?(?=^##\s|\Z)", "", body, flags=re.M)
                body_html = md_html(body)
            if row["kb_path"].count("/") >= 2:
                np_ = library.notes_path(config.knowledge_dir, row["kb_path"])
                notes_text = np_.read_text("utf-8") if np_.exists() else ""
        from .. import skills_runtime as sr

        translation, outputs = None, []
        skills = sr.discover(config)
        if row["kb_path"] and row["kb_path"].count("/") >= 2:
            card = config.knowledge_dir / row["kb_path"]
            tj = card.with_name(f"{card.stem}.translation.json")
            if tj.exists():
                try:
                    translation = json.loads(tj.read_text("utf-8"))
                except ValueError:
                    translation = None
            for sk in skills.values():
                f = sr.output_path(card, sk.id)
                if f.exists():
                    outputs.append({"id": sk.id, "label": sk.label,
                                    "html": md_html(_split_front(f.read_text("utf-8"))[1])})
        jobs = registry.query("SELECT * FROM jobs WHERE paper_id=? ORDER BY id DESC LIMIT 6", (paper_id,))
        tr_cfg = config.raw.get("translation") or {}
        from .. import notelinks

        from .. import updates as upd

        ucards = upd.for_paper(registry, paper_id)
        from .. import gaps as gapmod

        return {"gap_hits": gapmod.for_paper(registry, paper_id), "p": row, "body_html": body_html, "notes_text": notes_text, "has_pdf": pdf_file(row) is not None,
                "update_cards": ucards, "update_pending": sum(u["status"] == "pending" for u in ucards),
                "update_questions": upd.candidate_questions(registry, paper_id), "update_outcomes": upd.OUTCOMES,
                "backlinks": notelinks.backlinks(registry, config.knowledge_dir, paper_id),
                "translation": translation, "skill_outputs": outputs,
                "active_jobs": [j for j in jobs if j["status"] in ("queued", "running")],
                "failed_jobs": [j for j in jobs[:3] if j["status"] == "error"],
                "skills_ok": [sk for sk in skills.values() if sk.supported],
                "default_skill": sr.default_skill_id(config, skills),
                "tr_services": translation_services(config), "tr_default": tr_cfg.get("default", "bilibili"),
                "tr_ack": bool(tr_cfg.get("privacy_ack")),
                "plabels": registry.paper_field_rows(paper_id), "files": registry.paper_file_paths(paper_id),
                "ptags": json.loads(row["tags_json"] or "[]"),
                "pcollections": [{"id": r["id"], "path": registry.collection_path(r["id"])}
                                 for r in registry.paper_collections(paper_id)],
                "all_collections": _flat_tree(registry.collection_tree()),
                "dois": registry.paper_dois(paper_id),
                "events": registry.query("SELECT * FROM events WHERE paper_id=? ORDER BY id DESC LIMIT 20", (paper_id,))}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, status: str = "", q: str = "", c: str = "", id: int | None = None):
        papers, labels = paper_rows(q, status, c)
        bulk = pipeline.estimate() if pipeline.hold and pipeline.pending_count() else None
        selected = id if id is not None else (papers[0]["id"] if papers else None)
        rcounts = {r["read_status"]: r["n"] for r in registry.query(
            "SELECT read_status, count(*) AS n FROM papers WHERE status='done' GROUP BY read_status")}
        rcounts["starred"] = registry.one("SELECT count(*) AS n FROM papers WHERE starred=1")["n"]
        return render(request, "index.html", papers=papers, labels=labels, counts=registry.status_counts(), rcounts=rcounts,
                      status=status, q=q, c=c, bulk=bulk, pending_n=pipeline.pending_count(), selected=selected,
                      recent_reading=reading_progress.recent(registry) if not c else [],
                      plugin_exports=[{"id": key, "name": item["name"], "extension": item.get("extension", "md")}
                                      for key, item in export_templates(config).items()],
                      current_collection=(registry.one("SELECT * FROM collections WHERE id=?", (int(c),))
                                          if c.isdigit() else None),
                      **((paper_panel_ctx(selected) or {}) if selected else {}))

    @app.get("/papers/list", response_class=HTMLResponse)
    def papers_list(request: Request, status: str = "", q: str = "", c: str = "", selected: int | None = None):
        papers, labels = paper_rows(q, status, c)
        return templates.TemplateResponse(request, "_paper_list.html",
                                          {"papers": papers, "labels": labels, "selected": selected})

    @app.get("/api/state")
    def api_state():
        jobs = registry.one("SELECT count(*) AS n, max(updated_at) AS u FROM jobs WHERE status IN ('queued','running')")
        last_job = registry.one("SELECT max(updated_at) AS u FROM jobs")["u"] or ""
        return {"pending": pipeline.pending_count() + (jobs["n"] or 0),
                "busy": bool(busy) or hooks.running.is_set() or pipeline._lock.locked() or bool(jobs["n"]),
                "jobs_version": last_job,
                "papers": registry.one("SELECT count(*) AS n FROM papers")["n"],
                "last_event": registry.one("SELECT max(id) AS m FROM events")["m"] or 0,
                "review": _review_count(registry, pipeline.new_field_threshold)}

    @app.get("/paper/{paper_id}/panel", response_class=HTMLResponse)
    def paper_panel(request: Request, paper_id: int):
        ctx = paper_panel_ctx(paper_id)
        if ctx is None:
            raise HTTPException(404)
        return templates.TemplateResponse(request, "_paper_panel.html", {**ctx, "csrf": csrf_token})

    @app.get("/paper/{paper_id}/pdf")
    def paper_pdf(paper_id: int):
        row = registry.get_paper(paper_id)
        f = pdf_file(row) if row else None
        if f is None:
            raise HTTPException(404)
        return FileResponse(f, media_type="application/pdf", content_disposition_type="inline", filename=f.name)

    @app.get("/api/link-suggest")
    def link_suggest(q: str = ""):
        from .. import notelinks

        pipeline.taxonomy()
        return notelinks.suggest(registry, q)

    @app.post("/api/notes-preview")
    async def notes_preview(request: Request):
        body = await json_body(request)
        return {"html": md_html(str(body.get("text", "")))}

    @app.post("/paper/{paper_id}/notes")
    async def paper_notes(paper_id: int, request: Request):
        body = await request.json()
        _check_csrf(str(body.get("csrf", "")), csrf_token)
        row = registry.get_paper(paper_id)
        if row is None or not row["kb_path"] or row["kb_path"].count("/") < 2:
            raise HTTPException(404)
        from ..kb_writer import atomic_write

        atomic_write(library.notes_path(config.knowledge_dir, row["kb_path"]),
                     str(body.get("text", "")).replace("\r\n", "\n"))
        background("reindex", pipeline.reindex, paper_id)
        return {"ok": True}

    def _card_path(paper_id: int) -> tuple:
        row = registry.get_paper(paper_id)
        if row is None or not row["kb_path"] or not row["card_json"]:
            raise HTTPException(404)
        path = config.knowledge_dir / row["kb_path"]
        if not path.exists():
            raise HTTPException(404)
        return row, path

    @app.get("/paper/{paper_id}/card.md")
    def paper_card_md(paper_id: int):
        from ..kb_writer import editable_part

        row, path = _card_path(paper_id)
        return {"ok": True, "text": editable_part(path.read_text("utf-8"), config.output_language),
                "edited": bool(row["card_override"])}

    @app.post("/paper/{paper_id}/card")
    async def paper_card_save(paper_id: int, request: Request):
        """The user's own edit of a card (kept when the card is refreshed); reset=true restores the generated one."""
        body = await json_body(request)
        row, path = _card_path(paper_id)
        text = body.get("text")
        if body.get("reset"):
            override = None
        elif isinstance(text, str) and text.strip() and len(text) <= 200_000:
            override = text.replace("\r\n", "\n").strip()
        else:
            return {"ok": False, "message": "卡片内容不能为空，最多 20 万字符"}
        registry.update_paper(paper_id, card_override=override)
        pipeline._refresh_card(paper_id)
        background("reindex", pipeline.reindex, paper_id)
        pipeline.kb_commit(("Restore" if override is None else "Edit") + f" card #{paper_id}")
        ctx = paper_panel_ctx(paper_id)
        return {"ok": True, "html": ctx["body_html"] if ctx else "", "edited": override is not None}

    @app.post("/paper/{paper_id}/meta")
    async def paper_meta(paper_id: int, request: Request):
        """Reading status / star."""
        body = await json_body(request)
        if registry.get_paper(paper_id) is None:
            raise HTTPException(404)
        cols = {}
        if body.get("read_status") in ("unread", "reading", "read"):
            cols["read_status"] = body["read_status"]
        if "starred" in body:
            cols["starred"] = 1 if body["starred"] else 0
        if cols:
            sets = ", ".join(f"{k}=?" for k in cols)
            registry.execute(f"UPDATE papers SET {sets} WHERE id=?", (*cols.values(), paper_id))
            background("cards", pipeline._refresh_card, paper_id)
        return {"ok": True}

    def export_ids(ids: str, c: str) -> list[int]:
        if ids.strip():
            return [int(x) for x in ids.split(",") if x.strip().isdigit()]
        papers, _ = paper_rows("", "", c)
        return [r["id"] for r in papers if r["status"] == "done"]

    @app.get("/export.{fmt}")
    def export_refs(fmt: str, ids: str = "", c: str = ""):
        from fastapi.responses import Response

        from .. import citations_export as ce

        pids = export_ids(ids, c)
        extension = fmt
        if fmt == "bib":
            text, media = ce.bibtex(registry, pids), "application/x-bibtex"
        elif fmt == "ris":
            text, media = ce.ris(registry, pids), "application/x-research-info-systems"
        elif fmt.startswith("ext."):
            template = export_templates(config).get(fmt)
            if not template:
                raise HTTPException(404)
            extension = template.get("extension", "md")
            text = export_text(template, (registry.get_paper(pid) for pid in pids))
            media = "text/plain"
        else:
            raise HTTPException(404)
        return Response(text, media_type=media + "; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="papermap.{extension}"'})

    @app.post("/paper/{paper_id}/reveal")
    async def paper_reveal(paper_id: int, request: Request):
        body = await request.json()
        _check_csrf(str(body.get("csrf", "")), csrf_token)
        row = registry.get_paper(paper_id)
        if row is None:
            raise HTTPException(404)
        target = pdf_file(row) or (config.knowledge_dir / row["kb_path"] if row["kb_path"] else None)
        if target is None:
            raise HTTPException(404)
        _reveal(target)
        return {"ok": True}

    # ------------------------------------------------------------- collections
    def refresh_cards_later(paper_ids) -> None:
        ids = list(dict.fromkeys(paper_ids))
        if ids:
            background("cards", lambda: [pipeline._refresh_card(i) for i in ids])

    async def json_body(request: Request) -> dict:
        body = await request.json()
        _check_csrf(str(body.get("csrf", "")), csrf_token)
        return body

    def ids_of(body: dict) -> list[int]:
        return [int(x) for x in body.get("paper_ids", []) if str(x).isdigit()]

    @app.post("/api/collections")
    async def collection_create(request: Request):
        body = await json_body(request)
        parent = body.get("parent_id")
        try:
            cid = registry.create_collection(str(body.get("name", "")), int(parent) if parent else None)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        if ids_of(body):
            registry.add_to_collection(cid, ids_of(body))
            refresh_cards_later(ids_of(body))
        return {"ok": True, "id": cid}

    @app.post("/api/collections/{cid}/{action}")
    async def collection_action(cid: int, action: str, request: Request):
        body = await json_body(request)
        if registry.one("SELECT 1 FROM collections WHERE id=?", (cid,)) is None:
            raise HTTPException(404)
        try:
            if action == "rename":
                registry.rename_collection(cid, str(body.get("name", "")))
                refresh_cards_later(r["paper_id"] for r in registry.query(
                    "SELECT paper_id FROM paper_collections WHERE collection_id IN "
                    f"({','.join('?' * len(registry.collection_descendants(cid)))})",
                    tuple(registry.collection_descendants(cid))))
            elif action == "delete":
                refresh_cards_later(registry.delete_collection(cid))
            elif action == "move":
                parent = body.get("parent_id")
                registry.move_collection(cid, int(parent) if parent else None)
            elif action == "add":
                n = registry.add_to_collection(cid, ids_of(body))
                refresh_cards_later(ids_of(body))
                return {"ok": True, "added": n}
            elif action == "remove":
                registry.remove_from_collection(cid, ids_of(body))
                refresh_cards_later(ids_of(body))
            else:
                raise HTTPException(404)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True}

    # ------------------------------------------------------------------ search
    @app.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, q: str = "", kind: str = ""):
        from .. import search as srch

        groups, total = [], 0
        if q.strip():
            hits = srch.search(registry, q, {kind} if kind in srch.KINDS else None)
            total = len(hits)
            groups = srch.grouped(registry, hits)
        state = registry.one("SELECT count(*) AS n FROM search_state")["n"] if registry.one(
            "SELECT name FROM sqlite_master WHERE name='search_state'") else 0
        return render(request, "search.html", q=q, kind=kind, groups=groups, total=total, kinds=srch.KINDS,
                      terms=srch.query_terms(q), indexed=state)

    # ------------------------------------------------------------- comparison
    def compare_ids(ids: str, field: str, c: str) -> tuple[list[int], str]:
        if ids.strip():
            pids = [int(x) for x in ids.split(",") if x.strip().isdigit()]
            return pids, f"{len(pids)} 篇论文对比"
        if field:
            f = registry.field_by_slug(field)
            if f is None:
                raise HTTPException(404)
            return [r["paper_id"] for r in registry.field_paper_rows(f["id"])], f"{f['name_zh']} · 论文对比"
        if c.isdigit():
            papers, _ = paper_rows("", "done", c)
            g = registry.one("SELECT name FROM collections WHERE id=?", (int(c),))
            return [r["id"] for r in papers], f"{g['name'] if g else '分组'} · 论文对比"
        return [], "论文对比"

    @app.get("/compare", response_class=HTMLResponse)
    def compare_page(request: Request, ids: str = "", field: str = "", c: str = ""):
        from .. import compare as cmp

        pids, title = compare_ids(ids, field, c)
        items = cmp.rows(registry, pids)
        return render(request, "compare.html", items=items, title=title, columns=cmp.COLUMNS,
                      query=f"ids={ids}&field={field}&c={c}")

    @app.get("/compare.{fmt}")
    def compare_export(fmt: str, ids: str = "", field: str = "", c: str = ""):
        from fastapi.responses import Response

        from .. import compare as cmp

        pids, title = compare_ids(ids, field, c)
        items = cmp.rows(registry, pids)
        if fmt == "md":
            return Response(cmp.to_markdown(items, title), media_type="text/markdown; charset=utf-8",
                            headers={"Content-Disposition": 'attachment; filename="comparison.md"'})
        if fmt == "csv":
            return Response(cmp.to_csv(items), media_type="text/csv; charset=utf-8",
                            headers={"Content-Disposition": 'attachment; filename="comparison.csv"'})
        raise HTTPException(404)

    @app.post("/api/compare/summary")
    async def compare_summary(request: Request):
        from .. import compare as cmp

        body = await json_body(request)
        pids, title = compare_ids(str(body.get("ids", "")), str(body.get("field", "")), str(body.get("c", "")))
        items = cmp.rows(registry, pids)
        if len(items) < 2:
            return {"ok": False, "message": "至少需要两篇已处理的论文"}
        try:
            text, cost = await run_in_threadpool(cmp.summarize, config, pipeline.llm, items)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"{type(exc).__name__}: {str(exc)[:200]}"}
        rel = cmp.save(config.knowledge_dir, items, title, text)
        return {"ok": True, "html": md_html(text), "saved": rel, "cost": cost}

    # ---------------------------------------------------------------- figures
    def figure_row(fid: int):
        from .. import figures as figmod

        figmod.ensure_schema(registry)
        f = registry.one("SELECT * FROM figures WHERE id=?", (fid,))
        if f is None:
            raise HTTPException(404)
        return f

    @app.get("/api/figures/{paper_id}")
    def figures_list(paper_id: int):
        from .. import figures as figmod

        pdf = pipeline.pdf_for(paper_id)
        if pdf is not None:
            figmod.sync(registry, paper_id, pdf)
        figmod.ensure_schema(registry)
        return [{"id": f["id"], "label": f["label"], "page": f["page"], "legend": f["legend"],
                 "explanation_html": md_html(f["explanation"]) if f["explanation"] else ""}
                for f in registry.query("SELECT * FROM figures WHERE paper_id=? ORDER BY id", (paper_id,))]

    @app.get("/api/figure/{fid}.png")
    def figure_png(fid: int, scale: float = 2.0):
        from .. import figures as figmod

        f = figure_row(fid)
        pdf = pipeline.pdf_for(f["paper_id"])
        if pdf is None:
            raise HTTPException(404)
        scale = min((1.0, 2.0, 3.0), key=lambda s: abs(s - scale))
        return FileResponse(figmod.image(pdf, f, config.data_dir / "cache" / "figures", scale), media_type="image/png",
                            headers={"Cache-Control": "max-age=86400"})

    @app.post("/api/figure/{fid}/explain")
    async def figure_explain(fid: int, request: Request):
        from .. import figures as figmod

        await json_body(request)
        f = figure_row(fid)
        pdf = pipeline.pdf_for(f["paper_id"])
        png = figmod.image(pdf, f, config.data_dir / "cache" / "figures", 2.0).read_bytes()
        try:
            text = await run_in_threadpool(figmod.explain, config, registry, pipeline.llm, f, png)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"{type(exc).__name__}: {str(exc)[:240]}（当前模型可能不支持看图）"}
        return {"ok": True, "html": md_html(text)}

    @app.post("/api/figure/{fid}/to-notes")
    async def figure_to_notes(fid: int, request: Request):
        await json_body(request)
        f = figure_row(fid)
        row = registry.get_paper(f["paper_id"])
        if not f["explanation"] or not row["kb_path"] or row["kb_path"].count("/") < 2:
            return {"ok": False, "message": "还没有讲解"}
        from ..kb_writer import atomic_write

        notes = library.notes_path(config.knowledge_dir, row["kb_path"])
        old = notes.read_text("utf-8") if notes.exists() else ""
        atomic_write(notes, old.rstrip() + f"\n\n## {f['label']} 讲解（p.{f['page'] + 1}）\n\n{f['explanation'].strip()}\n")
        background("reindex", pipeline.reindex, f["paper_id"])
        return {"ok": True}

    @app.get("/figures", response_class=HTMLResponse)
    def figures_page(request: Request, paper: int | None = None):
        from .. import figures as figmod

        figmod.ensure_schema(registry)
        sql = ("SELECT f.id, f.label, f.page, f.legend, f.paper_id, p.first_author, p.year, p.title FROM figures f "
               "JOIN papers p ON p.id=f.paper_id WHERE p.status != 'ignored'")
        rows = registry.query(sql + (" AND f.paper_id=?" if paper else "") + " ORDER BY p.id DESC, f.id", (paper,) if paper else ())
        return render(request, "figures.html", rows=rows, paper=paper)

    # ----------------------------------------------------------------- Zotero
    @app.get("/zotero", response_class=HTMLResponse)
    def zotero_page(request: Request, dir: str = ""):
        from .. import zotero as z

        zdir = Path(dir).expanduser() if dir else z.default_dir()
        try:
            info = z.preview(zdir)
            err = ""
        except Exception as exc:  # noqa: BLE001
            info, err = None, str(exc)
        job = registry.one("SELECT * FROM jobs WHERE kind='zotero' ORDER BY id DESC LIMIT 1")
        return render(request, "zotero.html", info=info, err=err, zdir=str(zdir), job=job,
                      settings_groups=SETTINGS_GROUPS, settings_section="import",
                      settings_group_label=SETTINGS_SECTIONS["import"][0], settings_title="Zotero 导入",
                      result=json.loads(job["message"]) if job and job["status"] == "done" and (job["message"] or "").startswith("{") else None)

    @app.post("/api/zotero/import")
    async def zotero_import(request: Request):
        body = await json_body(request)
        opts = {"dir": str(body.get("dir", "")), "collections": bool(body.get("collections", True)),
                "tags": bool(body.get("tags", True)), "notes": bool(body.get("notes", True))}
        pipeline.jobs.submit("zotero", 0, json.dumps(opts))
        return {"ok": True}

    # -------------------------------------------------------------- citations
    @app.get("/citations", response_class=HTMLResponse)
    def citations_page(request: Request, min_citing: int = 2, paper: int | None = None):
        from .. import citations as ci

        ci.ensure_schema(registry)
        missing = ci.missing_key_papers(registry, max(1, min_citing))
        titles = {r["id"]: f"{r['first_author'] or '?'} {r['year'] or ''}" for r in registry.query("SELECT id, first_author, year FROM papers")}
        edges = [(a, b) for a, b in ci.library_edges(registry)]
        fetched = registry.one("SELECT count(*) AS n FROM refs_state")["n"]
        done = [r for r in registry.list_papers(status="done", limit=5000) if r["doi"]]
        return render(request, "citations.html", missing=missing, titles=titles, edges=edges, min_citing=min_citing,
                      fetched=fetched, papers=done, selected=paper)

    @app.get("/api/citations/{paper_id}/citing")
    async def citations_citing(paper_id: int):
        from .. import citations as ci

        try:
            return {"ok": True, "works": await run_in_threadpool(ci.citing_works, registry, pipeline.metadata, paper_id)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"{type(exc).__name__}: {str(exc)[:200]}"}

    @app.post("/api/citations/dismiss")
    async def citations_dismiss(request: Request):
        from .. import citations as ci

        body = await json_body(request)
        ci.ensure_schema(registry)
        registry.execute("INSERT OR IGNORE INTO dismissed_works VALUES (?,?)", (str(body.get("openalex_id", "")), now_iso()))
        return {"ok": True}

    @app.post("/api/citations/refresh")
    async def citations_refresh(request: Request):
        from .. import citations as ci

        await json_body(request)
        background("citations", ci.fetch_missing, registry, pipeline.metadata, 0)
        return {"ok": True}

    # --------------------------------------------------------- data resources
    @app.get("/resources", response_class=HTMLResponse)
    def resources_page(request: Request, kind: str = "", role: str = "", scope: str = ""):
        from .. import qa as qamod
        from .. import resources as res

        items = res.rows(registry, kind, role, qamod.scope_paper_ids(registry, scope) if scope else None)
        counts = {r["kind"]: r["n"] for r in registry.query("SELECT kind, count(*) AS n FROM data_resources GROUP BY kind")}
        pipeline.taxonomy()
        return render(request, "resources.html", items=items, kind=kind, role=role, scope=scope,
                      kinds=[(k, lbl) for k, lbl, _, _ in res.PATTERNS], counts=counts, roles=res.ROLE_LABELS,
                      groups=_flat_tree(registry.collection_tree()),
                      fields=[f for f in registry.active_fields() if registry.field_paper_rows(f["id"])])

    @app.get("/resources.csv")
    def resources_csv(kind: str = "", role: str = "", scope: str = ""):
        from fastapi.responses import Response

        from .. import qa as qamod
        from .. import resources as res

        items = res.rows(registry, kind, role, qamod.scope_paper_ids(registry, scope) if scope else None)
        return Response(res.to_csv(items), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="papermap-data-resources.csv"'})

    # --------------------------------------------------------------------- Q&A
    def answer_html(answer: str, sources: list[dict]) -> str:
        by = {x["id"]: x for x in sources}

        def link(m: re.Match) -> str:
            src = by.get(m.group(1))
            if src is None:
                return ""
            n = list(by).index(m.group(1)) + 1
            if src["kind"] in ("page", "annotation") and src["page"] is not None:
                href = f"/reader/{src['paper_id']}?page={src['page'] + 1}"
            else:
                href = f"/?id={src['paper_id']}"
            tip = f"{src['who']}{' p.' + str(src['page'] + 1) if src['page'] is not None else ''} — {src['title']}"
            return f'<a class="cite" href="{href}" title="{escape(tip, quote=True)}">{n}</a>'

        html = md.markdown(answer, extensions=["extra", "sane_lists"])
        return _clean_markdown_html(re.sub(r"\[(S\d+)\]", link, html))

    @app.get("/qa", response_class=HTMLResponse)
    def qa_page(request: Request, id: int | None = None):
        from .. import qa as qamod

        qamod.ensure_schema(registry)
        history = registry.query("SELECT id, question, created_at FROM qa_history ORDER BY id DESC LIMIT 50")
        current = None
        if id:
            row = registry.one("SELECT * FROM qa_history WHERE id=?", (id,))
            if row:
                sources = json.loads(row["sources_json"])
                current = {"row": row, "html": answer_html(row["answer"], sources), "sources": sources}
        pipeline.taxonomy()
        return render(request, "qa.html", history=history, current=current,
                      groups=_flat_tree(registry.collection_tree()),
                      fields=[f for f in registry.active_fields() if registry.field_paper_rows(f["id"])])

    @app.post("/api/qa")
    async def api_qa(request: Request):
        from .. import qa as qamod

        body = await json_body(request)
        try:
            r = await run_in_threadpool(qamod.ask, config, registry, pipeline.llm, str(body.get("question", "")),
                                        str(body.get("scope", "")))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"{type(exc).__name__}: {str(exc)[:300]}"}
        return {"ok": True, "id": r["id"], "html": answer_html(r["answer"], r["sources"]), "sources": r["sources"],
                "keywords": r["keywords"], "cost": r["cost"], "model": r["model"]}

    # ------------------------------------------------------------------ reader
    def reader_pdf(paper_id: int) -> Path:
        row = registry.get_paper(paper_id)
        f = pdf_file(row) if row else None
        if f is None:
            raise HTTPException(404)
        return f

    @app.get("/reader/{paper_id}", response_class=HTMLResponse)
    def reader_page(request: Request, paper_id: int, embed: int = 0, page: int | None = None, q: str = "",
                    evidence: int | None = None):
        if not 0 < paper_id < 2**63:
            raise HTTPException(404)
        if evidence is not None and 0 < evidence < 2**63:
            # Preserve exported links when a pending DOI is merged, even if SQLite reuses the old paper ID.
            moved = registry.one("SELECT paper_id FROM evidence_entries WHERE id=? AND merged_from=? AND paper_id!=?",
                                 (evidence, paper_id, paper_id))
            if moved:
                return RedirectResponse(request.url.replace(path=f"/reader/{moved['paper_id']}"), status_code=307)
        reader_pdf(paper_id)
        if embed and request.headers.get("sec-fetch-dest") == "document":
            return RedirectResponse(request.url.remove_query_params("embed"), status_code=307,
                                    headers={"Vary": "Sec-Fetch-Dest"})
        response = render(request, "reader.html", p=registry.get_paper(paper_id), embed=embed, start_page=page,
                          find=q, colors=reader_mod.COLORS)
        if embed:
            response.headers.add_vary_header("Sec-Fetch-Dest")
        return response

    @app.get("/api/reading/recent")
    def recent_reading():
        return {"recent": reading_progress.recent(registry)}

    @app.post("/api/reader/{paper_id}/progress")
    def save_reading_progress(paper_id: int, body: dict):
        _check_csrf(str(body.get("csrf", "")), csrf_token)
        path = reader_pdf(paper_id)
        doc, lock = reader_mod.DOCS.get(path)
        with lock:
            count = doc.page_count
        try:
            written = reading_progress.save(registry, paper_id, body, count)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"ok": True, "saved": written, "recent": reading_progress.recent(registry)}

    @app.get("/api/reader/{paper_id}/info")
    def reader_info(paper_id: int):
        from ..evidence import document_revision

        path = reader_pdf(paper_id)
        data = reader_mod.info(path)
        data.update(revision=document_revision(path), ocr_available=ocr.available())
        data["annotations"] = [dict(r, rects=json.loads(r["rects_json"])) for r in registry.annotations(paper_id)]
        return data

    @app.get("/api/reader/{paper_id}/page/{n}.png")
    def reader_png(paper_id: int, n: int, scale: float = 2.0):
        try:
            f = reader_mod.render(reader_pdf(paper_id), n, scale, config.data_dir / "cache" / "render")
        except IndexError:
            raise HTTPException(404) from None
        return FileResponse(f, media_type="image/png", headers={"Cache-Control": "max-age=86400"})

    def text_layer(paper_id: int, n: int, *, force: bool = False):
        from ..search import index_ocr_page

        path = reader_pdf(paper_id)
        try:
            layer = reader_mod.page_layer(path, n, ocr.cache_for(path, config.data_dir), force=force)
        except IndexError:
            raise HTTPException(404) from None
        if layer["source"] == "ocr":
            index_ocr_page(registry, paper_id, n, layer["words"])
        return layer

    @app.get("/api/reader/{paper_id}/page/{n}/words")
    def reader_words(paper_id: int, n: int, details: bool = False):
        layer = text_layer(paper_id, n)
        return layer if details else layer["words"]

    @app.post("/api/reader/{paper_id}/page/{n}/ocr")
    def reader_ocr(paper_id: int, n: int, body: dict):
        _check_csrf(str(body.get("csrf", "")), csrf_token)
        try:
            return {"ok": True, **text_layer(paper_id, n, force=True)}
        except ocr.OCRError as exc:
            return {"ok": False, "message": str(exc)}

    @app.get("/api/reader/{paper_id}/search")
    def reader_search(paper_id: int, q: str = ""):
        path = reader_pdf(paper_id)
        return reader_mod.search(path, q, cache=ocr.cache_for(path, config.data_dir))

    def annotations_changed(paper_id: int) -> None:
        def run():
            reader_mod.write_annotations_md(registry, config.knowledge_dir, paper_id, config.output_language)
            pipeline.reindex(paper_id)

        background("annotations", run)

    @app.post("/api/reader/{paper_id}/annotations")
    async def annotation_add(paper_id: int, request: Request):
        body = await json_body(request)
        reader_pdf(paper_id)
        rects = reader_mod.clean_rects(body.get("rects"))
        if not rects:
            return {"ok": False, "message": "没有选中任何文字"}
        color = body.get("color") if body.get("color") in reader_mod.COLORS else "yellow"
        aid = registry.add_annotation(paper_id, int(body.get("page", 0)), json.dumps(rects),
                                      str(body.get("text", ""))[:4000], color, str(body.get("note", ""))[:4000])
        annotations_changed(paper_id)
        return {"ok": True, "id": aid}

    @app.post("/api/annotations/{ann_id}/{action}")
    async def annotation_action(ann_id: int, action: str, request: Request):
        body = await json_body(request)
        row = registry.one("SELECT * FROM annotations WHERE id=?", (ann_id,))
        if row is None:
            raise HTTPException(404)
        if action == "delete":
            registry.delete_annotation(ann_id)
        elif action == "update":
            cols = {}
            if "note" in body:
                cols["note"] = str(body["note"])[:4000]
            if body.get("color") in reader_mod.COLORS:
                cols["color"] = body["color"]
            if cols:
                registry.update_annotation(ann_id, **cols)
        else:
            raise HTTPException(404)
        annotations_changed(row["paper_id"])
        return {"ok": True}

    @app.post("/api/translate-text")
    async def translate_text(request: Request):
        """Quick translation of a selection in the reader (default translation service)."""
        from .. import translate as tr

        body = await json_body(request)
        text = " ".join(str(body.get("text", "")).split())
        if not text:
            return {"ok": False, "message": "没有选中文字"}
        if len(text) > 4000:
            return {"ok": False, "message": "选中文字超过 4000 个字符，请缩短选区，或在论文详情中选择「翻译全文」。"}
        trc = config.raw["translation"]
        try:
            svc = tr.service_by_id(config, None)
        except tr.TranslationError as exc:
            return {"ok": False, "message": str(exc)}
        if svc.get("kind") == "bilibili" and not trc.get("privacy_ack"):
            if not body.get("ack"):
                return {"ok": False, "need_ack": True}
            trc["privacy_ack"] = True
            config.save()
        target = trc.get("target_language") or "zh"
        translator = tr.Translator(config, registry, pipeline.llm, svc)
        try:
            with translator.http:
                out, via = await run_in_threadpool(translator.translate_text, text, target, [], body.get("paper_id"))
        except tr.TranslationError as exc:
            return {"ok": False, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"翻译失败：{type(exc).__name__}: {str(exc)[:200]}"}
        return {"ok": True, "translation": out, "model": via}

    @app.post("/paper/{paper_id}/translate")
    async def paper_translate(paper_id: int, request: Request):
        from ..translate import TranslationError

        body = await json_body(request)
        sid = str(body.get("service") or "")
        tr = config.raw["translation"]
        svc = next((x for x in translation_services(config) if x.get("id") == (sid or tr.get("default"))), None)
        if svc is None:
            return {"ok": False, "message": "找不到这个翻译服务"}
        if svc.get("kind") == "bilibili" and not tr.get("privacy_ack"):
            if not body.get("ack"):
                return {"ok": False, "need_ack": True}
            tr["privacy_ack"] = True
            config.save()
        try:
            pipeline.submit_translation(paper_id, sid)
        except (ValueError, TranslationError) as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True}

    @app.post("/paper/{paper_id}/update")
    async def paper_update(paper_id: int, request: Request):
        body = await json_body(request)
        qid = body.get("question_id")
        try:
            pipeline.submit_update(paper_id, int(qid) if str(qid).isdigit() else 0)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True}

    # ---------------------------------------------------------------- evidence gaps (stage 7 task 5)
    def _gap_terms(qid: int, text: str, terms) -> tuple[list[str], str]:
        from .. import gaps as gapmod

        mine = gapmod.clean_terms(terms or [])
        if mine:
            return mine, "user"
        if config.model_ready():  # papers are mostly English; gaps are often written in Chinese
            q = registry.one("SELECT title FROM evidence_questions WHERE id=?", (qid,))
            try:
                return pipeline.gap_terms(q["title"] if q else "", text), "model"
            except Exception as exc:  # noqa: BLE001 - fall back to the free terms
                registry.log("warning", f"Search terms for an evidence gap not generated: {exc}")
        return gapmod.auto_terms(text), "auto"

    @app.post("/api/evidence/questions/{qid}/gaps")
    async def gap_create(qid: int, request: Request):
        from .. import gaps as gapmod

        body = await json_body(request)
        text = body.get("text")
        try:
            if not isinstance(text, str) or not text.strip():
                raise ValueError("请写下缺少的证据。")
            terms, origin = await run_in_threadpool(_gap_terms, qid, text, body.get("terms"))
            gid = gapmod.add_gap(registry, qid, text, terms, origin)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        found = gapmod.scan_library(registry, gid)
        return {"ok": True, "id": gid, "terms": gapmod.get_gap(registry, gid)["terms"], "found": found}

    @app.post("/api/gaps/{gid}")
    async def gap_action(gid: int, request: Request):
        from .. import gaps as gapmod

        body = await json_body(request)
        action = str(body.get("action", ""))
        g = gapmod.get_gap(registry, gid)
        if g is None:
            return {"ok": False, "message": "这个缺口不存在。"}
        try:
            if action == "edit":
                terms = body.get("terms")
                origin = "user"
                if body.get("regenerate"):
                    terms, origin = await run_in_threadpool(_gap_terms, g["question_id"], body.get("text") or g["text"], [])
                gapmod.update_gap(registry, gid, body.get("text"), terms, origin)
                return {"ok": True, "terms": gapmod.get_gap(registry, gid)["terms"], "found": gapmod.scan_library(registry, gid)}
            if action in ("close", "reopen"):
                pid = body.get("paper_id")
                gapmod.set_gap_status(registry, gid, "closed" if action == "close" else "open",
                                      int(pid) if str(pid).isdigit() else None)
                return {"ok": True, "found": gapmod.scan_library(registry, gid) if action == "reopen" else 0}
            if action == "delete":
                gapmod.delete_gap(registry, gid)
                return {"ok": True}
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": False, "message": "无效的操作"}

    @app.post("/api/gap-hits/{hid}")
    async def gap_hit_action(hid: int, request: Request):
        from .. import gaps as gapmod

        body = await json_body(request)
        action = str(body.get("action", ""))
        try:
            if action == "update":  # ask the model what the paper changes (task 3) — separate from the reminder
                h = registry.one("SELECT h.paper_id, g.question_id FROM gap_hits h JOIN question_gaps g "
                                 "ON g.id=h.gap_id WHERE h.id=?", (hid,))
                if h is None:
                    raise ValueError("这条提醒不存在。")
                pipeline.submit_update(h["paper_id"], h["question_id"])
                gapmod.decide_hit(registry, hid, "seen")
                return {"ok": True, "paper_id": h["paper_id"]}
            gapmod.decide_hit(registry, hid, action)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True}

    @app.post("/api/updates/{card_id}")
    async def api_update_decide(card_id: int, request: Request):
        from .. import updates as upd

        body = await json_body(request)
        try:
            res = upd.decide(registry, card_id, str(body.get("action", "")), body.get("text") or "",
                             str(body.get("outcome") or ""))
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True, **res}

    @app.post("/paper/{paper_id}/skill")
    async def paper_skill(paper_id: int, request: Request):
        body = await json_body(request)
        try:
            pipeline.submit_skill(paper_id, str(body.get("skill", "")))
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True}

    @app.post("/api/import")
    async def api_import(request: Request):
        """PDFs dropped onto the window or chosen with the import button.
        When a group is selected (field 'collection'), imported papers join it."""
        from ..pdf_import import publish_pdf, staged_file

        form = await request.form()
        _check_csrf(str(form.get("csrf", "")), csrf_token)
        coll = str(form.get("collection", "") or "")
        coll_id = int(coll) if coll.isdigit() and registry.one("SELECT 1 FROM collections WHERE id=?", (int(coll),)) else None
        items = form.getlist("files")
        saved = []
        for item in items:
            if not hasattr(item, "filename") or not (item.filename or "").lower().endswith(".pdf"):
                continue
            with staged_file(config.imported_dir) as tmp:
                with tmp.open("wb") as fh:
                    while chunk := await item.read(1 << 20):
                        fh.write(chunk)
                saved.append(publish_pdf(tmp, item.filename))
        if saved:
            registry.log("info", f"Imported {len(saved)} PDF(s) into the library")
            from ..pdf_extract import sha256_file

            shas = [sha256_file(p) for p in saved]

            def run_import():
                for p, sha in zip(saved, shas):
                    pipeline.handle_path(p)
                    f = registry.file_by_sha(sha)
                    if coll_id and f is not None and f["paper_id"]:
                        registry.add_to_collection(coll_id, [f["paper_id"]])
                        pipeline._refresh_card(f["paper_id"])

            background("import", run_import)
        return {"ok": True, "count": len(saved), "skipped": len(items) - len(saved)}

    @app.post("/bulk/start")
    def bulk_start(csrf: str = Form(...)):
        _check_csrf(csrf, csrf_token)
        pipeline.hold = False
        background("bulk", pipeline.process_pending)
        return RedirectResponse("/", status_code=303)

    # ------------------------------------------------------------------- costs
    @app.get("/costs", response_class=HTMLResponse)
    def costs(request: Request):
        month = registry.one("SELECT strftime('%Y-%m','now') AS m")["m"]
        by_purpose = registry.query(
            "SELECT purpose, count(*) AS n, sum(prompt_tokens) AS pin, sum(completion_tokens) AS pout, "
            "sum(cost_usd) AS cost, sum(1-ok) AS failed FROM llm_calls WHERE substr(created_at,1,7)=? "
            "GROUP BY purpose ORDER BY cost DESC", (month,))
        by_day = registry.query(
            "SELECT substr(created_at,1,10) AS day, count(*) AS n, sum(cost_usd) AS cost FROM llm_calls "
            "WHERE created_at >= date('now','-30 day') GROUP BY day ORDER BY day")
        by_month = registry.query(
            "SELECT substr(created_at,1,7) AS month, count(*) AS n, sum(cost_usd) AS cost, "
            "sum(prompt_tokens) AS pin, sum(completion_tokens) AS pout FROM llm_calls GROUP BY month ORDER BY month DESC LIMIT 12")
        recent = registry.query(
            "SELECT c.*, p.title FROM llm_calls c LEFT JOIN papers p ON p.id=c.paper_id ORDER BY c.id DESC LIMIT 50")
        papers_done = registry.one("SELECT count(*) AS n FROM papers WHERE status='done'")["n"]
        plan_usage = registry.query(
            "SELECT model, count(*) AS n, sum(prompt_tokens) AS pin, sum(completion_tokens) AS pout, "
            "sum(1-ok) AS failed, max(created_at) AS last FROM llm_calls "
            "WHERE billing='subscription' AND substr(created_at,1,7)=? GROUP BY model ORDER BY n DESC", (month,))
        total = registry.one("SELECT sum(cost_usd) AS c FROM llm_calls")["c"] or 0
        max_day = max([d["cost"] or 0 for d in by_day] + [1e-9])
        return render(request, "costs.html", by_purpose=by_purpose, by_day=by_day, by_month=by_month,
                      recent=recent, max_day=max_day, per_paper=(total / papers_done) if papers_done else 0,
                      eff=config.effective_llm(), est10=pipeline.estimate(10), plan_usage=plan_usage)

    @app.get("/paper/{paper_id}")
    def paper(paper_id: int):
        if registry.get_paper(paper_id) is None:
            raise HTTPException(404)
        return RedirectResponse(f"/?id={paper_id}", status_code=307)

    @app.post("/paper/{paper_id}/retry")
    def retry(paper_id: int, csrf: str = Form(...)):
        _check_csrf(csrf, csrf_token)
        if registry.get_paper(paper_id) is None:
            raise HTTPException(404)
        registry.update_paper(paper_id, status="new")
        threading.Thread(target=pipeline.process_pending, daemon=True).start()
        return RedirectResponse(f"/?id={paper_id}", status_code=303)

    # ------------------------------------------------------------ knowledge map
    @app.get("/map", response_class=HTMLResponse)
    def knowledge_map(request: Request):
        return render(request, "map.html")

    @app.get("/api/graph")
    def graph():
        pipeline.taxonomy()
        fields = {f["id"]: f for f in registry.query("SELECT * FROM fields WHERE status IN ('active','retired')")}
        links = registry.query(
            "SELECT pf.paper_id, pf.field_id, pf.is_primary, p.title, p.year, p.first_author, p.journal "
            "FROM paper_fields pf JOIN papers p ON p.id=pf.paper_id "
            "WHERE pf.status='accepted' AND p.status='done'"
        )
        lang = config.output_language
        counts: dict[int, int] = {}
        papers: dict[int, dict] = {}
        by_paper: dict[int, list[int]] = {}
        for ln in links:
            counts[ln["field_id"]] = counts.get(ln["field_id"], 0) + 1
            by_paper.setdefault(ln["paper_id"], []).append(ln["field_id"])
            papers[ln["paper_id"]] = {
                "id": f"p{ln['paper_id']}", "kind": "paper", "url": f"/paper/{ln['paper_id']}",
                "label": f"{ln['first_author'] or '?'} {ln['year'] or ''}",
                "title": ln["title"] or "", "journal": ln["journal"] or "",
            }
        nodes = [
            {"id": f"f{fid}", "kind": f["axis"], "url": f"/field/{f['slug']}",
             "label": f["name_en"] if lang == "en" or _lang() in ("en", "de") else f["name_zh"],
             "title": f["name_zh"] if lang == "en" or _lang() in ("en", "de") else f["name_en"], "count": counts.get(fid, 0),
             "retired": f["status"] == "retired"}
            for fid, f in fields.items() if counts.get(fid) or f["status"] == "active"
        ]
        edges = [{"source": f"p{ln['paper_id']}", "target": f"f{ln['field_id']}", "primary": bool(ln["is_primary"])}
                 for ln in links]
        co: dict[tuple[int, int], int] = {}
        for fids in by_paper.values():
            for i, a in enumerate(sorted(fids)):
                for b in sorted(fids)[i + 1:]:
                    co[(a, b)] = co.get((a, b), 0) + 1
        field_edges = [{"source": f"f{a}", "target": f"f{b}", "weight": w} for (a, b), w in co.items()]
        from .. import citations as ci

        cite_edges = [{"source": f"p{a}", "target": f"p{b}"} for a, b in ci.library_edges(registry)
                      if a in papers and b in papers]
        from .. import questions as qmod

        q_nodes, q_edges = qmod.graph_parts(registry)
        shown = {n["id"] for n in nodes}
        q_edges = [e for e in q_edges if e["target"] in shown]
        q_nodes = [n for n in q_nodes if any(e["source"] == n["id"] for e in q_edges)]
        return {"nodes": nodes + list(papers.values()) + q_nodes, "paper_edges": edges, "field_edges": field_edges,
                "cite_edges": cite_edges, "question_edges": q_edges}

    @app.get("/fields", response_class=HTMLResponse)
    def fields_index(request: Request):
        pipeline.taxonomy()
        rows = registry.query(
            "SELECT f.*, (SELECT count(*) FROM paper_fields pf JOIN papers p ON p.id=pf.paper_id "
            " WHERE pf.field_id=f.id AND pf.status='accepted' AND p.status='done') AS n "
            "FROM fields f WHERE f.status IN ('active','retired') ORDER BY f.axis, f.sort_order"
        )
        return render(request, "fields.html", rows=rows)

    @app.get("/field/{slug}", response_class=HTMLResponse)
    def field_page(request: Request, slug: str, created: int = 0, hb_started: int = 0, cons_started: int = 0):
        pipeline.taxonomy()
        f = registry.field_by_slug(slug)
        if f is None:
            raise HTTPException(404)
        path = config.knowledge_dir / "fields" / f"{slug}.md"
        if not path.exists():
            pipeline.refresh_fields({slug})
        body_html = md_html(_split_front(path.read_text("utf-8"))[1]) if path.exists() else ""
        from .. import questions as qmod

        suggested = qmod.field_questions(registry, f["id"], "suggested")
        for q in suggested:
            q["papers"] = qmod.paper_labels(registry, q["source"].get("paper_ids", []))
        from .. import updates as upd

        changes, unchanged_n = upd.confirmed_for_field(registry, f["id"])
        return render(request, "field.html", f=f, body_html=body_html, rel=f"fields/{slug}.md",
                      changes=changes, unchanged_n=unchanged_n, update_outcomes=upd.OUTCOMES,
                      field_questions=qmod.field_questions(registry, f["id"]), suggested_questions=suggested,
                      suggest_busy=("questions:" + slug) in busy,
                      has_handbook=(config.knowledge_dir / "handbooks" / f"{slug}.md").exists(),
                      created=created, hb_started=hb_started or ("handbook" in busy),
                      n_papers=len(registry.field_paper_rows(f["id"])),
                      cons_started=cons_started or ("consolidate" in busy))

    @app.post("/api/fields/{slug}/suggest-questions")
    async def api_suggest_questions(slug: str, body: dict):
        _check_csrf(str(body.get("csrf", "")), csrf_token)
        from .. import questions as qmod

        if registry.field_by_slug(slug) is None:
            raise HTTPException(404)
        key = "questions:" + slug
        if key in busy:
            return {"ok": False, "message": "正在提议，请稍候"}
        busy.add(key)
        try:
            created = await run_in_threadpool(qmod.suggest, pipeline.llm, config, registry, slug)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            return {"ok": False, "message": f"{type(exc).__name__}: {str(exc)[:300]}"}
        finally:
            busy.discard(key)
        return {"ok": True, "created": len(created)}

    # ------------------------------------------------------------------ review
    @app.get("/review", response_class=HTMLResponse)
    def review(request: Request, error: str = ""):
        pipeline.taxonomy()
        pipeline.migrate_legacy_proposals()
        unverified = registry.list_papers(status="doi_unverified", limit=1000)
        flagged = registry.query("SELECT * FROM papers WHERE status='done' AND needs_review=1 ORDER BY id DESC")
        items = [(p, registry.paper_field_rows(p["id"])) for p in flagged]
        pending = [(r, registry.pending_papers(r["id"])) for r in registry.pending_fields("open")]
        from .. import gaps as gapmod, updates as upd

        return render(request, "review.html", unverified=unverified, items=items, pending=pending,
                      update_cards=upd.pending(registry), update_outcomes=upd.OUTCOMES,
                      gap_hits=gapmod.new_hits(registry),
                      threshold=pipeline.new_field_threshold,
                      all_fields=registry.active_fields(), error=error,
                      accept=float(config.classification.get("accept_confidence", 0.6)))

    @app.post("/review/label")
    def review_label(csrf: str = Form(...), paper_id: int = Form(...), slug: str = Form(...),
                     action: str = Form(...), back: str = Form("/review")):
        _check_csrf(csrf, csrf_token)
        if action not in ("accept", "reject", "add"):
            raise HTTPException(400)
        try:
            pipeline.set_label(paper_id, slug, action)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse(back if back.startswith("/") else "/review", status_code=303)

    @app.post("/review/paper-ok")
    def review_paper_ok(csrf: str = Form(...), paper_id: int = Form(...)):
        """Accept the current labels as they are (clears the review flag)."""
        _check_csrf(csrf, csrf_token)
        for r in registry.paper_field_rows(paper_id):
            if r["status"] == "suggested":
                pipeline.set_label(paper_id, r["slug"], "reject")
        registry.update_paper(paper_id, needs_review=0)
        return RedirectResponse("/review", status_code=303)

    @app.post("/review/doi")
    def review_doi(csrf: str = Form(...), paper_id: int = Form(...), action: str = Form(...), doi: str = Form("")):
        _check_csrf(csrf, csrf_token)
        if action == "ignore":
            registry.update_paper(paper_id, status="ignored", status_detail="Ignored on the review page")
            return RedirectResponse("/review", status_code=303)
        try:
            if action == "use":
                # validate quickly so errors show on the page; processing runs in the background
                from ..doi import normalize

                if not normalize(doi):
                    raise ValueError("这不是有效的 DOI")
            background("doi", pipeline.resolve_unverified, paper_id, doi if action == "use" else None)
        except ValueError as exc:
            return RedirectResponse(f"/review?error={_q(str(exc))}", status_code=303)
        return RedirectResponse("/review", status_code=303)

    @app.post("/review/pending")
    def review_pending(csrf: str = Form(...), pending_id: int = Form(...), action: str = Form(...),
                       slug: str = Form(""), name_zh: str = Form(""), name_en: str = Form(""),
                       description: str = Form(""), axis: str = Form(""), target: str = Form(""),
                       make_handbook: str = Form("")):
        _check_csrf(csrf, csrf_token)
        try:
            if action == "establish":
                new_slug = pipeline.establish_pending(pending_id, slug or None, name_zh or None, name_en or None,
                                                      description or None, axis or None)
                if make_handbook:
                    background("handbook", _safe(pipeline.generate_handbook), new_slug)
                return RedirectResponse(f"/field/{new_slug}?created=1", status_code=303)
            if action == "merge":
                pipeline.merge_pending(pending_id, target)
                return RedirectResponse(f"/field/{target}", status_code=303)
            if action == "reject":
                pipeline.reject_pending(pending_id)
                return RedirectResponse("/review", status_code=303)
        except (ValueError, TaxonomyError) as exc:
            return RedirectResponse(f"/review?error={_q(str(exc))}", status_code=303)
        raise HTTPException(400)

    # --------------------------------------------------------------- handbooks
    @app.get("/handbooks", response_class=HTMLResponse)
    def handbooks_index(request: Request):
        pipeline.taxonomy()
        d = config.knowledge_dir / "handbooks"
        rows = []
        for f in sorted(d.glob("*.md")) if d.exists() else []:
            field = registry.field_by_slug(f.stem)
            front = _split_front(f.read_text("utf-8"))[0]
            rows.append({"slug": f.stem, "name": field["name_zh"] if field else f.stem,
                         "name_en": field["name_en"] if field else "", "papers": front.get("papers"),
                         "generated": front.get("generated")})
        return render(request, "handbooks.html", rows=rows)

    @app.get("/handbook/{slug}", response_class=HTMLResponse)
    def handbook_page(request: Request, slug: str):
        path = config.knowledge_dir / "handbooks" / f"{slug}.md"
        if not path.exists():
            raise HTTPException(404)
        return render(request, "handbook.html", slug=slug, body_html=md_html(_split_front(path.read_text("utf-8"))[1]),
                      rel=f"handbooks/{slug}.md", f=registry.field_by_slug(slug))

    @app.post("/field/{slug}/consolidate")
    def field_consolidate(slug: str, csrf: str = Form(...)):
        _check_csrf(csrf, csrf_token)
        if registry.field_by_slug(slug) is None:
            raise HTTPException(404)
        background("consolidate", _safe(pipeline.consolidate_field), slug)
        return RedirectResponse(f"/field/{slug}?cons_started=1", status_code=303)

    @app.get("/concepts", response_class=HTMLResponse)
    def concepts_index(request: Request):
        d = config.knowledge_dir / "concepts"
        rows = []
        for f in sorted(d.glob("*.md")) if d.exists() else []:
            front = _split_front(f.read_text("utf-8"))[0]
            rows.append({"slug": f.stem, "name": front.get("name", f.stem), "papers": front.get("papers", 0)})
        rows.sort(key=lambda r: -int(r["papers"] or 0))
        return render(request, "concepts.html", rows=rows)

    def _kb_notes_path(rel: str) -> Path:
        if not re.fullmatch(r"(concepts|fields|handbooks)/[\w.\-]+\.md", rel or "") or ".." in rel:
            raise HTTPException(404)
        path = config.knowledge_dir / rel
        if not path.is_file():
            raise HTTPException(404)
        return path

    def _notes_hints() -> set[str]:
        from .. import handbook as hb, kb_fields as kf

        hints = {"（写下用法、参数心得、踩过的坑…）", "(usage notes)"}
        for mod in (hb, kf):
            for h in getattr(mod, "H", {}).values():
                if isinstance(h, dict) and h.get("notes_hint"):
                    hints.add(h["notes_hint"])
        return hints

    @app.get("/api/kb/notes")
    def kb_notes_get(rel: str):
        from ..kb_writer import read_user_section

        text = read_user_section(_kb_notes_path(rel))
        if text is None:
            raise HTTPException(404)
        placeholder = text.strip() in _notes_hints()
        return {"ok": True, "text": "" if placeholder else text, "hint": text.strip() if placeholder else "",
                "html": "" if placeholder or not text.strip() else md_html(text)}

    @app.post("/api/kb/notes")
    async def kb_notes_save(request: Request):
        from ..kb_writer import read_user_section, write_user_section

        body = await json_body(request)
        text = body.get("text")
        if not isinstance(text, str) or len(text) > 100_000:
            return {"ok": False, "message": "笔记最多 10 万个字符"}
        path = _kb_notes_path(str(body.get("rel", "")))
        try:
            write_user_section(path, text)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        pipeline.kb_commit(f"Edit notes in {body.get('rel')}")
        note = read_user_section(path) or ""
        return {"ok": True, "html": md_html(note) if note.strip() else ""}

    # Images pasted / dropped into a note live in knowledge/attachments/ and are written into the
    # Markdown as ../attachments/<name>, so the note also shows them when opened in Obsidian.
    @app.post("/api/kb/attachments")
    async def kb_attachment_upload(request: Request):
        _check_csrf(request.headers.get("x-csrf", ""), csrf_token)
        data = await request.body()
        if not data or len(data) > ATTACH_MAX:
            return {"ok": False, "message": "图片最大 20 MB"}
        ext = _image_ext(data)
        if not ext:
            return {"ok": False, "message": "只支持 PNG、JPEG、GIF、WebP 图片"}
        stem = re.sub(r"[^\w\-]+", "-", Path(unquote(request.headers.get("x-filename", "")) or "image").stem,
                      flags=re.A).strip("-")[:40] or "image"
        name = f"{stem}-{hashlib.sha256(data).hexdigest()[:10]}.{ext}"
        folder = config.knowledge_dir / "attachments"
        folder.mkdir(parents=True, exist_ok=True)
        if not (folder / name).exists():  # same picture pasted twice is stored once
            (folder / name).write_bytes(data)
        return {"ok": True, "src": f"../attachments/{name}", "name": name, "alt": stem}

    @app.get("/attachments/{name}")
    def kb_attachment(name: str):
        f = config.knowledge_dir / "attachments" / name
        ext = name.rsplit(".", 1)[-1].lower()
        if not re.fullmatch(r"[\w\-]+\.(png|jpe?g|gif|webp)", name, flags=re.A) or not f.is_file():
            raise HTTPException(404)
        return FileResponse(f, media_type=ATTACH_TYPES[ext], headers={"Cache-Control": "max-age=86400",
                                                                       "X-Content-Type-Options": "nosniff"})

    @app.get("/concept/{slug}", response_class=HTMLResponse)
    def concept_page(request: Request, slug: str):
        path = config.knowledge_dir / "concepts" / f"{slug}.md"
        if not path.exists() or not re.fullmatch(r"[a-z0-9-]+", slug):
            raise HTTPException(404)
        return render(request, "concept.html", body_html=md_html(_split_front(path.read_text("utf-8"))[1]),
                      rel=f"concepts/{slug}.md")

    @app.post("/handbook/{slug}/generate")
    def handbook_generate(slug: str, csrf: str = Form(...)):
        _check_csrf(csrf, csrf_token)
        if registry.field_by_slug(slug) is None:
            raise HTTPException(404)
        background("handbook", _safe(pipeline.generate_handbook), slug)
        return RedirectResponse(f"/field/{slug}?hb_started=1", status_code=303)

    # ---------------------------------------------------------------- taxonomy
    @app.get("/taxonomy", response_class=HTMLResponse)
    def taxonomy_page(request: Request, saved: int = 0, error: str = "", started: int = 0):
        spec = str(config.raw.get("taxonomy_file") or "default")
        path = resolve_path(spec, config.data_dir)
        error_now = ""
        try:
            tax = pipeline.taxonomy()
        except TaxonomyError as exc:
            tax, error_now = None, str(exc)
        text = path.read_text("utf-8") if path is not None and path.exists() else builtin_text(spec if spec in BUILTIN else "default")
        return render(request, "taxonomy.html", tax=tax, spec=spec, is_builtin=path is None, text=text,
                      builtin=BUILTIN, saved=saved, error=error or error_now, started=started,
                      stale=pipeline.stale_classification_count(),
                      unclassified=registry.one("SELECT count(*) AS n FROM papers WHERE status='done' AND classify_key IS NULL")["n"],
                      custom_path=config.data_dir / "taxonomy.yaml")

    @app.post("/taxonomy/preset")
    def taxonomy_preset(csrf: str = Form(...), preset: str = Form(...)):
        _check_csrf(csrf, csrf_token)
        if preset not in BUILTIN and preset != "taxonomy.yaml":
            raise HTTPException(400)
        config.raw["taxonomy_file"] = preset
        config.save()
        pipeline.taxonomy()
        background("fields", pipeline.refresh_fields)
        return RedirectResponse("/taxonomy?saved=1", status_code=303)

    @app.post("/taxonomy/save")
    def taxonomy_save(csrf: str = Form(...), yaml_text: str = Form(...)):
        _check_csrf(csrf, csrf_token)
        yaml_text = yaml_text.replace("\r\n", "\n").replace("\r", "\n")  # browsers submit CRLF
        try:
            parse_text(yaml_text)
        except TaxonomyError as exc:
            return RedirectResponse(f"/taxonomy?error={_q(str(exc))}", status_code=303)
        target = config.data_dir / "taxonomy.yaml"
        tmp = target.with_suffix(".yaml.tmp")
        tmp.write_text(yaml_text, "utf-8")
        tmp.replace(target)
        config.raw["taxonomy_file"] = "taxonomy.yaml"
        config.save()
        pipeline.taxonomy()
        background("fields", pipeline.refresh_fields)
        return RedirectResponse("/taxonomy?saved=1", status_code=303)

    @app.post("/taxonomy/reclassify")
    def taxonomy_reclassify(csrf: str = Form(...), mode: str = Form("stale")):
        _check_csrf(csrf, csrf_token)
        if mode not in ("stale", "all", "unclassified"):
            raise HTTPException(400)
        background("classify", pipeline.classify_papers, mode)
        return RedirectResponse("/taxonomy?started=1", status_code=303)

    @app.post("/api/app/show")
    def app_show():
        """A second launch of the desktop app asks this instance to show its window."""
        fn = getattr(app.state, "show_window", None)
        if fn is None:
            return {"ok": False}
        fn()
        return {"ok": True}

    @app.post("/api/translation/services")
    async def tr_service_add(request: Request):
        body = await json_body(request)
        name, base, model = (str(body.get(k, "")).strip() for k in ("name", "base_url", "model"))
        if not (name and base and model):
            return {"ok": False, "message": "名称、Base URL 和模型名都要填"}
        tr = config.raw["translation"]
        sid = "custom-" + secrets.token_hex(3)
        tr.setdefault("services", []).append({
            "id": sid, "name": name, "kind": "custom", "base_url": base, "model": model,
            "api_key": str(body.get("api_key", "")).strip(),
            "protocol": body.get("protocol") if body.get("protocol") in PROTOCOLS else "openai",
            "mode": body.get("mode") if body.get("mode") in ("translate", "polish", "custom") else "translate",
            "prompt": str(body.get("prompt", "")),
            "price_input_per_1m": float(body.get("price_in") or 0), "price_output_per_1m": float(body.get("price_out") or 0),
        })
        config.save()
        return {"ok": True, "id": sid}

    @app.post("/api/translation/services/{sid}/delete")
    async def tr_service_delete(sid: str, request: Request):
        await json_body(request)
        tr = config.raw["translation"]
        if sid.startswith("ext."):
            return {"ok": False, "message": "请在扩展设置里停用所属插件"}
        if sid in ("bilibili", "main") or sid in OFFICIAL_TRANSLATION_SERVICES:
            return {"ok": False, "message": "内置服务不能删除"}
        tr["services"] = [x for x in tr.get("services", []) if x.get("id") != sid]
        if tr.get("default") == sid:
            tr["default"] = "bilibili"
        config.save()
        return {"ok": True}

    # ------------------------------------------------------------------ skills
    @app.get("/skills", response_class=HTMLResponse)
    def skills_page(request: Request, saved: int = 0):
        from .. import skills_runtime as sr

        skills = sr.discover(config)
        return render(request, "skills.html", skills=sorted(skills.values(), key=lambda k: (not k.supported, k.source != "builtin", k.label)),
                      default_skill=sr.default_skill_id(config, skills), sources=sr.SOURCES,
                      auto=bool((config.raw.get("skills") or {}).get("auto_new_papers")), saved=saved,
                      skills_dir=config.data_dir / "skills")

    @app.post("/skills")
    def skills_save(csrf: str = Form(...), default: str = Form(""), auto: str = Form("")):
        _check_csrf(csrf, csrf_token)
        sk = config.raw.setdefault("skills", {})
        sk["default"] = default
        sk["auto_new_papers"] = bool(auto)
        config.save()
        return RedirectResponse("/skills?saved=1", status_code=303)

    @app.post("/api/skills/install")
    async def skills_install(request: Request):
        from .. import skills_runtime as sr

        body = await json_body(request)
        try:
            sk = sr.install(config, Path(str(body.get("path", ""))))
        except (ValueError, OSError) as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "id": sk.id, "supported": sk.supported, "reason": sk.reason}

    @app.post("/shutdown")
    def shutdown(csrf: str = Form(...)):
        """Quit button for the packaged app (no terminal to press Ctrl+C in).
        Work in progress is resumed on the next start."""
        _check_csrf(csrf, csrf_token)
        threading.Timer(0.5, lambda: os._exit(0)).start()
        return HTMLResponse("<p style='font-family:sans-serif;padding:40px'>PaperMap 已退出。可以关闭这个页面。</p>")

    @app.get("/events", response_class=HTMLResponse)
    def events(request: Request):
        return render(request, "events.html", events=registry.recent_events(500))

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request, saved: int = 0, welcome: int = 0, section: str = "models"):
        from ..claude_cli import model_options as claude_model_options

        section = section if section in SETTINGS_SECTIONS else "models"
        llm = config.llm
        eff = config.effective_llm()
        custom = llm.get("custom") or {}
        official_env = PROVIDERS["deepseek"]["api_key_env"]
        tr_services = []
        for svc in translation_services(config):
            public = {field: svc[field] for field in ("id", "name", "kind", "model", "plugin") if field in svc}
            if svc.get("id") in OFFICIAL_TRANSLATION_SERVICES and svc.get("kind") == svc["id"]:
                public.update(official=True, has_api_key=bool(svc.get("api_key")), has_app_id=bool(svc.get("app_id")))
            tr_services.append(public)
        return render(
            request, "settings.html", cfg=config, saved=saved, langs=LANGS,
            providers=PROVIDERS, protocols=PROTOCOLS, provider=llm.get("provider", "deepseek"),
            eff=eff, custom=custom, presets=config.model_presets(), preset_state=llm.get("presets") or {},
            official_key_state=("env" if os.environ.get(official_env) else "local" if llm.get("api_key") else ""),
            custom_key_state="local" if custom.get("api_key") else "",
            inbox_text="\n".join(str(p) for p in config.raw.get("inbox_dirs") or []),
            exclude_dirs=", ".join(config.exclude_dir_names),
            exclude_patterns=", ".join(config.exclude_patterns),
            git_available=kb_git.available(), welcome=welcome,
            kb_dir=str(config.knowledge_dir), kb_default=(config.knowledge_dir == config.data_dir / "knowledge"),
            data_dir=str(config.data_dir),
            palettes=__import__("papermap.themes", fromlist=["x"]).PALETTES,
            appearances=__import__("papermap.themes", fromlist=["x"]).APPEARANCES,
            ui_languages=i18n.LANGUAGES,
            swatches=__import__("papermap.themes", fromlist=["x"]).SWATCH,
            backups=backup_mod.list_backups(config)[:30], backup_cfg=backup_mod.settings(config),
            backup_dir=str(backup_mod.backup_dir(config)),
            tr={**config.raw["translation"], "services": tr_services}, bilibili_models=__import__("papermap.translate", fromlist=["x"]).BILIBILI_MODELS,
            protocols_all=PROTOCOLS,
            claude_model_options=claude_model_options(),
            settings_section=section, settings_groups=SETTINGS_GROUPS,
            settings_group_label=SETTINGS_SECTIONS[section][0], settings_title=SETTINGS_SECTIONS[section][1],
        )

    @app.post("/settings")
    async def save_settings(
        request: Request,
        csrf: str = Form(...),
        provider: str = Form("deepseek"),
        official_api_key: str = Form(""),
        official_model: str = Form(""),
        clear_official_key: str = Form(""),
        custom_base_url: str = Form(""),
        custom_api_key: str = Form(""),
        clear_custom_key: str = Form(""),
        custom_protocol: str = Form("openai"),
        custom_model: str = Form(""),
        custom_price_in: float = Form(0),
        custom_price_cache: str = Form(""),
        custom_price_out: float = Form(0),
        custom_multiplier: float = Form(1.0),
        custom_peak: str = Form(""),
        custom_billing: str = Form(""),
        output_language: str = Form("zh"),
        monthly_budget_usd: float = Form(5),
        inbox_dirs: str = Form(""),
        exclude_dirs: str = Form(""),
        exclude_patterns: str = Form(""),
        contact_email: str = Form(""),
        openalex_api_key: str = Form(""),
        clear_openalex_key: str = Form(""),
        tracking_enabled: str = Form(""),
        tracking_model_rank: str = Form(""),
        tr_default: str = Form("bilibili"),
        tr_bilibili_model: str = Form("Index-Translate-35B-A3B"),
        tr_target: str = Form("zh"),
        tr_fallback: str = Form(""),
        tr_auto: str = Form(""),
        new_field_threshold: int = Form(3),
        embedding_model: str = Form(""),
        knowledge_git: str = Form(""),
    ):
        _check_csrf(csrf, csrf_token)
        form = await request.form()
        reasoning_values = {}
        for pid in ("chatgpt_plan", "claude_cli"):
            field = f"p_{pid}_reasoning_effort"
            if field in form:
                try:
                    reasoning_values[pid] = normalize_reasoning_effort(form[field])
                except ConfigError as exc:
                    raise HTTPException(422, str(exc)) from exc
        raw = config.raw
        from ..translate import BILIBILI_MODELS

        presets = config.model_presets()
        if provider.startswith("ext.") and provider not in presets and provider != config.llm.get("provider"):
            raise HTTPException(409, "模型插件已停用或接口已更新，请刷新设置")
        tr = raw["translation"]
        if any(x.get("id") == tr_default for x in translation_services(config)):
            tr["default"] = tr_default
        for svc in tr.get("services", []):
            if svc.get("kind") == "bilibili" and tr_bilibili_model in BILIBILI_MODELS:
                svc["model"] = tr_bilibili_model
            sid = svc.get("id")
            if sid in OFFICIAL_TRANSLATION_SERVICES and svc.get("kind") == sid:
                fields = ("app_id", "api_key") if sid == "niutrans" else ("api_key",)
                for field in fields:
                    value = str(form.get(f"tr_{sid}_{field}", "")).strip()
                    if form.get(f"tr_{sid}_clear"):
                        svc.pop(field, None)
                    elif value:
                        svc[field] = value
        tr["target_language"] = tr_target if tr_target in ("zh", "en") else "zh"
        tr["fallback_to_main"] = bool(tr_fallback)
        tr["auto_new_papers"] = bool(tr_auto)
        raw["new_field_threshold"] = max(1, new_field_threshold)
        raw.setdefault("embedding", {})["model"] = embedding_model.strip()
        raw["knowledge_git"] = bool(knowledge_git)
        form_b = await request.form()
        if form_b.get("backup_form"):
            b = raw.setdefault("backup", {})
            b["enabled"] = bool(form_b.get("backup_enabled"))
            try:
                b["keep"] = max(1, min(365, int(str(form_b.get("backup_keep") or 14))))
            except ValueError:
                b["keep"] = 14
            b["dir"] = str(form_b.get("backup_dir") or "").strip()
        llm = raw["llm"]
        llm["provider"] = provider if provider in (*PROVIDERS, *presets, "custom") or provider == llm.get("provider") else "deepseek"
        store = llm.setdefault("presets", {})
        for pid in presets:
            st = store.get(pid) or {}
            f = {k: str(form.get(f"p_{pid}_{k}", "")).strip() for k in ("key", "model", "in", "hit", "out", "clear")}
            if pid in ("chatgpt_plan", "claude_cli"):
                st.pop("api_key", None)
            elif f["clear"]:
                st.pop("api_key", None)
            elif f["key"]:
                st["api_key"] = f["key"]
            if f["model"] or (pid == "chatgpt_plan" and f"p_{pid}_model" in form):
                st["model"] = f["model"]
            if pid in reasoning_values:
                st["reasoning_effort"] = reasoning_values[pid]
            for k, name in (("in", "price_input_per_1m"), ("hit", "price_cache_hit_per_1m"), ("out", "price_output_per_1m")):
                if f"p_{pid}_{k}" in form:
                    try:
                        st[name] = max(0.0, float(f[k])) if f[k] else None
                    except ValueError:
                        st[name] = None
            if st:
                store[pid] = st
        if official_model in PROVIDERS["deepseek"]["models"]:
            llm["model"] = official_model
        if clear_official_key:
            llm["api_key"] = ""
        elif official_api_key.strip():
            llm["api_key"] = official_api_key.strip()
        c = llm.setdefault("custom", {})
        c["base_url"] = custom_base_url.strip()
        c["protocol"] = custom_protocol if custom_protocol in PROTOCOLS else "openai"
        c["model"] = custom_model.strip()
        c["price_input_per_1m"] = max(0.0, custom_price_in)
        c["price_output_per_1m"] = max(0.0, custom_price_out)
        try:
            c["price_cache_hit_per_1m"] = max(0.0, float(custom_price_cache)) if custom_price_cache.strip() else None
        except ValueError:
            c["price_cache_hit_per_1m"] = None
        c["price_multiplier"] = custom_multiplier if custom_multiplier > 0 else 1.0
        c["peak_schedule"] = "deepseek" if custom_peak else "none"
        c["billing"] = "subscription" if custom_billing else "token"
        if clear_custom_key:
            c["api_key"] = ""
        elif custom_api_key.strip():
            c["api_key"] = custom_api_key.strip()
        llm["monthly_budget_usd"] = max(0.0, monthly_budget_usd)
        if output_language in LANGS:
            raw["output_language"] = output_language
        raw["inbox_dirs"] = [ln.strip() for ln in inbox_dirs.splitlines() if ln.strip()]
        raw["exclude"]["dir_names"] = _csv(exclude_dirs)
        raw["exclude"]["filename_patterns"] = _csv(exclude_patterns)
        raw["metadata"]["contact_email"] = contact_email.strip()
        if clear_openalex_key:
            raw["metadata"]["openalex_api_key"] = ""
        elif openalex_api_key.strip():
            raw["metadata"]["openalex_api_key"] = openalex_api_key.strip()
        pipeline.metadata.openalex_key = raw["metadata"].get("openalex_api_key", "")
        if "tracking_form" in form:
            tr = raw.setdefault("tracking", {})
            tr["enabled"] = bool(tracking_enabled)
            tr["model_rank"] = bool(tracking_model_rank)
        config.validate()
        path = config.save()
        try:
            path.chmod(0o600)
        except OSError:
            pass
        pipeline.llm.reset_client()
        threading.Thread(target=pipeline.process_pending, daemon=True).start()
        target = "/settings?saved=1"
        if "settings_section" in form:
            section = str(form["settings_section"])
            target += "&section=" + (section if section in SETTINGS_SECTIONS else "models")
        return RedirectResponse(target, status_code=303)

    def _form_llm(body: dict) -> dict:
        """Effective LLM settings from the (unsaved) form; blank key = stored key."""
        _check_csrf(str(body.get("csrf", "")), csrf_token)
        llm = copy.deepcopy(config.llm)
        provider = body.get("provider", "deepseek")
        presets = config.model_presets()
        if str(provider).startswith("ext.") and provider not in presets:
            raise HTTPException(409, "模型插件已停用或接口已更新，请刷新设置")
        llm["provider"] = provider if provider in (*PROVIDERS, *presets, "custom") else "deepseek"
        if llm["provider"] in presets:
            st = llm.setdefault("presets", {}).setdefault(llm["provider"], {})
            if str(body.get("model", "")).strip() or (llm["provider"] == "chatgpt_plan" and "model" in body):
                st["model"] = str(body["model"]).strip()
            if llm["provider"] in ("chatgpt_plan", "claude_cli") and "reasoning_effort" in body:
                try:
                    st["reasoning_effort"] = normalize_reasoning_effort(body["reasoning_effort"])
                except ConfigError as exc:
                    raise HTTPException(422, str(exc)) from exc
            if llm["provider"] in ("chatgpt_plan", "claude_cli"):
                st.pop("api_key", None)
            elif str(body.get("api_key", "")).strip():
                st["api_key"] = str(body["api_key"]).strip()
        elif llm["provider"] == "custom":
            c = llm.setdefault("custom", {})
            c["base_url"] = str(body.get("base_url", "")).strip()
            c["protocol"] = body.get("protocol") if body.get("protocol") in PROTOCOLS else "openai"
            c["model"] = str(body.get("model", "")).strip()
            if str(body.get("api_key", "")).strip():
                c["api_key"] = str(body["api_key"]).strip()
        else:
            if body.get("model") in PROVIDERS[llm["provider"]]["models"]:
                llm["model"] = body["model"]
            if str(body.get("api_key", "")).strip():
                llm["api_key"] = str(body["api_key"]).strip()
        return config.resolve_llm(llm)

    async def _account_action(request: Request, provider: str, action: str):
        body = await request.json()
        _check_csrf(str(body.get("csrf", "")), csrf_token)
        if provider not in ("chatgpt_plan", "claude_cli"):
            raise HTTPException(404)
        if action == "cancel" and provider != "chatgpt_plan":
            raise HTTPException(404)
        try:
            client = _account_client(provider, config.data_dir)
            method = "start" if action == "login" and provider == "chatgpt_plan" else action
            if method == "start":
                data = await run_in_threadpool(client.start, consent=bool(body.get("consent")))
            elif method == "status" and provider == "claude_cli" and body.get("refresh"):
                data = await run_in_threadpool(client.status, refresh=True)
            else:
                data = await run_in_threadpool(getattr(client, method))
            if action == "cancel":
                data = {**await run_in_threadpool(client.status), "message": "已取消登录等待。"}
            # Never pass through credentials returned by an auth implementation.
            allowed = ("ready", "available", "authenticated", "message", "status", "auth_method",
                       "subscription_type", "billing", "models", "url", "expires_at", "pending",
                       "connected", "email", "error", "error_code", "needs_consent")
            result = {k: data[k] for k in allowed if k in data}
            result["ok"] = bool(data.get("ok", True))
            if provider == "chatgpt_plan" and data.get("error_code") in ("scope_missing", "access_denied"):
                result["needs_consent"] = True
            if action == "logout":
                pipeline.llm.reset_client()
            return result
        except Exception as exc:  # noqa: BLE001
            if provider == "chatgpt_plan":
                from ..chatgpt_auth import ChatGPTAuthError

                if isinstance(exc, ChatGPTAuthError):
                    return {"ok": False, "error": str(exc), "message": str(exc), "error_code": exc.code,
                            "needs_consent": exc.code in ("scope_missing", "access_denied"), "pending": exc.code == "login_pending"}
            registry.log("warning", f"{provider} {action} failed ({type(exc).__name__})")
            return {"ok": False, "message": f"连接操作失败（{type(exc).__name__}），请重试或查看活动日志"}

    @app.post("/api/accounts/{provider}/status")
    async def account_status(provider: str, request: Request):
        return await _account_action(request, provider, "status")

    @app.post("/api/accounts/{provider}/login")
    async def account_login(provider: str, request: Request):
        return await _account_action(request, provider, "login")

    @app.post("/api/accounts/{provider}/logout")
    async def account_logout(provider: str, request: Request):
        return await _account_action(request, provider, "logout")

    @app.post("/api/accounts/{provider}/cancel")
    async def account_cancel(provider: str, request: Request):
        return await _account_action(request, provider, "cancel")

    @app.get("/i18n/{code}.js")
    def i18n_js(code: str):
        code = i18n.normalize(code)
        body = "window.PM_T=" + json.dumps(i18n.catalog(code), ensure_ascii=False) + ";"
        return Response(body, media_type="text/javascript", headers={"Cache-Control": "max-age=86400"})

    @app.post("/api/appearance")
    async def api_appearance(body: dict):
        _check_csrf(str(body.get("csrf", "")), csrf_token)
        from ..themes import normalize

        config.raw["appearance"] = normalize({**(config.raw.get("appearance") or {}), **body})
        config.save()
        return {"ok": True, **config.raw["appearance"]}

    @app.post("/api/backup/create")
    async def api_backup_create(body: dict):
        _check_csrf(str(body.get("csrf", "")), csrf_token)
        try:
            path = await run_in_threadpool(backup_mod.create, config, registry, "manual")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"备份失败：{exc}"}
        registry.log("info", f"Backup saved: {path.name}")
        return {"ok": True, "name": path.name}

    @app.post("/api/backup/restore")
    async def api_backup_restore(body: dict):
        _check_csrf(str(body.get("csrf", "")), csrf_token)
        running = registry.one("SELECT count(*) AS n FROM jobs WHERE status IN ('queued','running')")["n"]
        if running or busy:
            return {"ok": False, "message": "还有翻译、解读或批量任务在运行，请等它们完成后再恢复"}
        try:
            res = await run_in_threadpool(backup_mod.restore, config, registry, str(body.get("name", "")),
                                          pipeline._lock)
        except (ValueError, OSError) as exc:
            return {"ok": False, "message": str(exc)}
        pipeline.llm._client = None
        report = await run_in_threadpool(backup_mod.check_library, config, registry)
        return {"ok": True, **res, "report": report}

    @app.get("/api/backup/check")
    async def api_backup_check():
        return {"ok": True, "report": await run_in_threadpool(backup_mod.check_library, config, registry)}

    @app.post("/api/knowledge/move")
    async def api_knowledge_move(request: Request):
        """Move the knowledge folder. ``confirm`` false = only report where it would go."""
        body = await request.json()
        _check_csrf(str(body.get("csrf", "")), csrf_token)
        default = config.data_dir / "knowledge"
        chosen = Path(str(body.get("path") or "").strip()) if not body.get("reset") else default
        current = config.knowledge_dir
        try:
            target = chosen if body.get("reset") else library.resolve_move_target(current, chosen)
            if body.get("reset") and (target.resolve() == current.resolve() or (target.exists() and any(target.iterdir()))):
                raise ValueError("默认位置已经在用，或那里已经有文件")
            for inbox in config.inbox_dirs:
                if target.resolve().is_relative_to(inbox.resolve()):
                    raise ValueError(f"新位置在自动导入文件夹 {inbox} 里面，里面的 PDF 会被反复扫描，请换一个位置")
        except (ValueError, OSError) as exc:
            return {"ok": False, "message": str(exc)}
        n_files = sum(1 for _ in current.rglob("*") if _.is_file()) if current.exists() else 0
        if not body.get("confirm"):
            return {"ok": True, "target": str(target), "files": n_files}
        running = registry.one("SELECT count(*) AS n FROM jobs WHERE status IN ('queued','running')")["n"]
        if running or busy:
            return {"ok": False, "message": "还有翻译、解读或批量任务在运行，请等它们完成后再移动"}

        def run():
            with pipeline._lock:  # no paper is written while the folder moves
                n, how = library.move_knowledge(current, target)
                config.raw["knowledge_dir"] = "" if target.resolve() == default.resolve() else str(target)
                config.save()
                config.ensure_dirs()
            return n, how
        try:
            n, how = await run_in_threadpool(run)
        except OSError as exc:
            registry.log("error", f"moving the knowledge base failed: {exc}")
            return {"ok": False, "message": f"移动失败：{exc}"}
        registry.log("info", f"knowledge base moved to {target} ({n} files, {how})")
        return {"ok": True, "target": str(target), "files": n}

    @app.post("/api/llm/models")
    async def api_models(request: Request):
        eff = _form_llm(await request.json())
        try:
            if eff["provider"] == "chatgpt_plan":
                options = await run_in_threadpool(list_chatgpt_model_options, eff)
                return {"ok": True, "models": [o["id"] for o in options], "model_options": options}
            models = await run_in_threadpool(list_models, eff)
            if eff["provider"] == "claude_cli":
                from ..claude_cli import model_option, model_options

                options = model_options()
                if eff["model"] and eff["model"] not in {o["id"] for o in options}:
                    options.append(model_option(eff["model"]))
                return {"ok": True, "models": models, "model_options": options}
            return {"ok": True, "models": models}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"{type(exc).__name__}: {str(exc)[:300]}"}

    @app.post("/api/llm/test")
    async def api_test(request: Request):
        eff = _form_llm(await request.json())
        return await run_in_threadpool(check_connection, eff)

    @app.get("/extensions/guide", response_class=HTMLResponse)
    def extensions_guide(request: Request):
        guide = (EXAMPLES / "GUIDE.md").read_text("utf-8")
        return render(request, "extensions_guide.html", guide=md_html(guide), settings_groups=SETTINGS_GROUPS,
                      settings_section="extensions")

    mount_api(app, config, registry, pipeline, background)
    from .evidence_routes import mount_evidence

    mount_evidence(app, config, registry, render, csrf_token, reader_pdf)
    from .tracking_routes import mount_tracking

    mount_tracking(app, config, registry, pipeline, render, csrf_token)
    mount_admin(app, config, registry, hooks, csrf_token, templates)
    return app


ATTACH_MAX = 20 * 1024 * 1024
ATTACH_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def _image_ext(data: bytes) -> str:
    """File type from the bytes themselves, never from the name the browser sent."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return ""


def _highlights(text: str) -> str:
    """Obsidian-style ==highlight== (outside code) becomes <mark>; Python-Markdown has no syntax for it."""
    parts = re.split(r"(```.*?```|`[^`\n]*`)", text, flags=re.S)
    return "".join(p if i % 2 else re.sub(r"==(?=\S)([^=\n]*?\S)==", r"<mark>\1</mark>", p) for i, p in enumerate(parts))


def _clean_markdown_html(html: str) -> str:
    # Documents, model output and prompt plugins all share the local UI origin.
    # Inline colour from the note editor survives as span/mark style, limited to colour properties.
    return nh3.clean(html, tags={"p", "br", "hr", "strong", "b", "em", "i", "u", "s", "del", "blockquote", "code", "pre",
                                 "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "a", "sup", "sub", "mark", "span",
                                 "table", "thead", "tbody", "tr", "td", "th", "dl", "dt", "dd", "abbr", "img"},
                     attributes={"a": {"href", "title", "class"}, "img": {"src", "alt", "title"},
                                 "span": {"style"}, "mark": {"style"},
                                 "td": {"colspan", "rowspan"}, "th": {"colspan", "rowspan"}},
                     filter_style_properties={"color", "background-color"},
                     url_schemes={"http", "https", "mailto"}, link_rel=None)


def _account_client(provider: str, data_dir: Path):
    if provider == "chatgpt_plan":
        from ..chatgpt_auth import get_auth

        return get_auth(data_dir)
    from ..claude_cli import get_cli

    return get_cli(data_dir)


def _flat_tree(tree: list[dict]) -> list[dict]:
    out = []
    for node in tree:
        out.append(node)
        out += _flat_tree(node["children"])
    return out


def _reveal(path: Path) -> None:
    import subprocess
    import sys

    if sys.platform == "darwin":
        subprocess.Popen(["/usr/bin/open", "-R", str(path)])
    elif os.name == "nt":
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])


def _tracking_badge(registry: Registry) -> int:
    from ..tracking import badge

    try:
        return badge(registry)
    except Exception:  # noqa: BLE001 - the sidebar must render even if tracking tables are broken
        return 0


def _review_count(registry: Registry, threshold: int = 3) -> int:
    row = registry.one(
        "SELECT (SELECT count(*) FROM papers WHERE status='doi_unverified') + "
        "(SELECT count(*) FROM papers WHERE status='done' AND needs_review=1) AS n")
    ready = sum(1 for r in registry.pending_fields("open") if r["n_papers"] >= threshold)
    from ..updates import pending_count

    from ..gaps import new_count

    return (int(row["n"]) if row else 0) + ready + pending_count(registry) + new_count(registry)


def _safe(fn):
    """Background jobs log their own errors; swallow so the thread ends quietly."""
    def run(*args):
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            pass
    return run


def _q(s: str) -> str:
    from urllib.parse import quote

    return quote(s[:500])


_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def _wikilinks(text: str, registry: Registry) -> str:
    """Turn Obsidian-style [[fields/x|Label]] / [[papers/y|Label]] into web links."""

    def repl(m: re.Match) -> str:
        target, label = m.group(1).strip(), (m.group(2) or m.group(1)).strip()
        if target.startswith("fields/"):
            return f"[{label}](/field/{target.split('/', 1)[1]})"
        if target.startswith("concepts/"):
            return f"[{label}](/concept/{target.split('/', 1)[1]})"
        if target.startswith("handbooks/"):
            return f"[{label}](/handbook/{target.split('/', 1)[1]})"
        if target.startswith("papers/"):
            row = registry.one("SELECT id FROM papers WHERE kb_path=?", (target + ".md",))
            if row:
                return f"[{label}](/paper/{row['id']})"
        return label

    return _WIKILINK.sub(repl, text)


def _check_csrf(given: str, expected: str) -> None:
    if not secrets.compare_digest(given or "", expected):
        raise HTTPException(403, "Invalid form token; reload the page")


def _csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def _split_front(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    try:
        return yaml.safe_load(m.group(1)) or {}, m.group(2)
    except yaml.YAMLError:
        return {}, text
