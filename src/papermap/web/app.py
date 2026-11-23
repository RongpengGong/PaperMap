"""Minimal local web UI (FastAPI + Jinja2). Listens on loopback only."""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import threading
from pathlib import Path

import markdown as md
import yaml
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from ..config import PROTOCOLS, PROVIDERS, Config, effective_llm
from .. import kb_git
from ..llm import check_connection, list_models
from ..pipeline import Pipeline
from ..registry import Registry
from ..taxonomy import BUILTIN, TaxonomyError, builtin_text, parse_text, resolve_path

HERE = Path(__file__).parent
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
LANGS = ("zh", "en", "bilingual")


def create_app(config: Config, registry: Registry, pipeline: Pipeline) -> FastAPI:
    app = FastAPI(title="PaperMap", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["fromjson"] = lambda s: json.loads(s or "[]")
    csrf_token = secrets.token_urlsafe(24)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # Defend against DNS rebinding and cross-site form posts: this server can
        # change the API endpoint/key, so only same-origin local requests pass.
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in ALLOWED_HOSTS:
            return HTMLResponse("Forbidden host", status_code=403)
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and re.sub(r"^https?://", "", origin).rsplit(":", 1)[0] not in ALLOWED_HOSTS:
                return HTMLResponse("Forbidden origin", status_code=403)
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
        return md.markdown(_wikilinks(text, registry), extensions=["extra", "sane_lists"])

    def render(request: Request, name: str, **ctx):
        ctx.update(
            request=request,
            key_set=bool(config.api_key()),
            month_cost=registry.month_cost(),
            budget=float(config.llm.get("monthly_budget_usd") or 0),
            csrf=csrf_token,
            review_count=_review_count(registry, pipeline.new_field_threshold),
            budget_ratio=(registry.month_cost() / float(config.llm.get("monthly_budget_usd") or 0))
            if float(config.llm.get("monthly_budget_usd") or 0) else 0,
            busy=bool(busy),
        )
        return templates.TemplateResponse(request, name, ctx)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, status: str | None = None):
        papers = registry.list_papers(status=status or None, limit=1000)
        bulk = pipeline.estimate() if pipeline.hold and pipeline.pending_count() else None
        return render(request, "index.html", papers=papers, counts=registry.status_counts(), status=status,
                      bulk=bulk, pending_n=pipeline.pending_count())

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
        total = registry.one("SELECT sum(cost_usd) AS c FROM llm_calls")["c"] or 0
        max_day = max([d["cost"] or 0 for d in by_day] + [1e-9])
        return render(request, "costs.html", by_purpose=by_purpose, by_day=by_day, by_month=by_month,
                      recent=recent, max_day=max_day, per_paper=(total / papers_done) if papers_done else 0,
                      eff=config.effective_llm(), est10=pipeline.estimate(10))

    @app.get("/paper/{paper_id}", response_class=HTMLResponse)
    def paper(request: Request, paper_id: int):
        row = registry.get_paper(paper_id)
        if row is None:
            raise HTTPException(404)
        front, body_html = {}, ""
        if row["kb_path"]:
            path = config.knowledge_dir / row["kb_path"]
            if path.exists():
                front, body = _split_front(path.read_text("utf-8"))
                body_html = md_html(body)
        return render(
            request, "paper.html", p=row, front=front, body_html=body_html,
            labels=registry.paper_field_rows(paper_id),
            files=registry.paper_file_paths(paper_id), dois=registry.paper_dois(paper_id),
            events=registry.query("SELECT * FROM events WHERE paper_id=? ORDER BY id DESC LIMIT 50", (paper_id,)),
        )

    @app.post("/paper/{paper_id}/retry")
    def retry(paper_id: int, csrf: str = Form(...)):
        _check_csrf(csrf, csrf_token)
        if registry.get_paper(paper_id) is None:
            raise HTTPException(404)
        registry.update_paper(paper_id, status="new")
        threading.Thread(target=pipeline.process_pending, daemon=True).start()
        return RedirectResponse(f"/paper/{paper_id}", status_code=303)

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
             "label": f["name_en"] if lang == "en" else f["name_zh"],
             "title": f["name_en"] if lang != "en" else f["name_zh"], "count": counts.get(fid, 0),
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
        return {"nodes": nodes + list(papers.values()), "paper_edges": edges, "field_edges": field_edges}

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
        return render(request, "field.html", f=f, body_html=body_html, rel=f"fields/{slug}.md",
                      has_handbook=(config.knowledge_dir / "handbooks" / f"{slug}.md").exists(),
                      created=created, hb_started=hb_started or ("handbook" in busy),
                      n_papers=len(registry.field_paper_rows(f["id"])),
                      cons_started=cons_started or ("consolidate" in busy))

    # ------------------------------------------------------------------ review
    @app.get("/review", response_class=HTMLResponse)
    def review(request: Request, error: str = ""):
        pipeline.taxonomy()
        pipeline.migrate_legacy_proposals()
        unverified = registry.list_papers(status="doi_unverified", limit=1000)
        flagged = registry.query("SELECT * FROM papers WHERE status='done' AND needs_review=1 ORDER BY id DESC")
        items = [(p, registry.paper_field_rows(p["id"])) for p in flagged]
        pending = [(r, registry.pending_papers(r["id"])) for r in registry.pending_fields("open")]
        return render(request, "review.html", unverified=unverified, items=items, pending=pending,
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
    def settings(request: Request, saved: int = 0, welcome: int = 0):
        llm = config.llm
        eff = config.effective_llm()
        custom = llm.get("custom") or {}
        official_env = PROVIDERS["deepseek"]["api_key_env"]
        return render(
            request, "settings.html", cfg=config, saved=saved, langs=LANGS,
            providers=PROVIDERS, protocols=PROTOCOLS, provider=llm.get("provider", "deepseek"),
            eff=eff, custom=custom,
            official_key_state=("env" if os.environ.get(official_env) else "local" if llm.get("api_key") else ""),
            custom_key_state="local" if custom.get("api_key") else "",
            inbox_text="\n".join(str(p) for p in config.raw.get("inbox_dirs") or []),
            exclude_dirs=", ".join(config.exclude_dir_names),
            exclude_patterns=", ".join(config.exclude_patterns),
            git_available=kb_git.available(), welcome=welcome,
        )

    @app.post("/settings")
    def save_settings(
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
        output_language: str = Form("zh"),
        monthly_budget_usd: float = Form(5),
        inbox_dirs: str = Form(""),
        exclude_dirs: str = Form(""),
        exclude_patterns: str = Form(""),
        contact_email: str = Form(""),
        new_field_threshold: int = Form(3),
        embedding_model: str = Form(""),
        knowledge_git: str = Form(""),
    ):
        _check_csrf(csrf, csrf_token)
        raw = config.raw
        raw["new_field_threshold"] = max(1, new_field_threshold)
        raw.setdefault("embedding", {})["model"] = embedding_model.strip()
        raw["knowledge_git"] = bool(knowledge_git)
        llm = raw["llm"]
        llm["provider"] = provider if provider in (*PROVIDERS, "custom") else "deepseek"
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
        config.validate()
        path = config.save()
        try:
            path.chmod(0o600)
        except OSError:
            pass
        pipeline.llm._client = None  # pick up new provider / key
        threading.Thread(target=pipeline.process_pending, daemon=True).start()
        return RedirectResponse("/settings?saved=1", status_code=303)

    def _form_llm(body: dict) -> dict:
        """Effective LLM settings from the (unsaved) form; blank key = stored key."""
        _check_csrf(str(body.get("csrf", "")), csrf_token)
        llm = copy.deepcopy(config.llm)
        provider = body.get("provider", "deepseek")
        llm["provider"] = provider if provider in (*PROVIDERS, "custom") else "deepseek"
        if llm["provider"] == "custom":
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
        return effective_llm(llm)

    @app.post("/api/llm/models")
    async def api_models(request: Request):
        eff = _form_llm(await request.json())
        try:
            models = await run_in_threadpool(list_models, eff)
            return {"ok": True, "models": models}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"{type(exc).__name__}: {str(exc)[:300]}"}

    @app.post("/api/llm/test")
    async def api_test(request: Request):
        eff = _form_llm(await request.json())
        return await run_in_threadpool(check_connection, eff)

    return app


def _review_count(registry: Registry, threshold: int = 3) -> int:
    row = registry.one(
        "SELECT (SELECT count(*) FROM papers WHERE status='doi_unverified') + "
        "(SELECT count(*) FROM papers WHERE status='done' AND needs_review=1) AS n")
    ready = sum(1 for r in registry.pending_fields("open") if r["n_papers"] >= threshold)
    return (int(row["n"]) if row else 0) + ready


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
