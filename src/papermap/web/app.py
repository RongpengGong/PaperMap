"""Minimal local web UI (FastAPI + Jinja2). Listens on loopback only."""

from __future__ import annotations

import json
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

from ..config import Config
from ..pipeline import Pipeline
from ..registry import Registry

HERE = Path(__file__).parent
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
LANGS = ("zh", "en", "bilingual")


def create_app(config: Config, registry: Registry, pipeline: Pipeline) -> FastAPI:
    app = FastAPI(title="PaperMap", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
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

    def render(request: Request, name: str, **ctx):
        ctx.update(
            request=request,
            key_set=bool(config.api_key()),
            month_cost=registry.month_cost(),
            budget=float(config.llm.get("monthly_budget_usd") or 0),
            csrf=csrf_token,
        )
        return templates.TemplateResponse(request, name, ctx)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, status: str | None = None):
        papers = registry.list_papers(status=status or None, limit=1000)
        return render(request, "index.html", papers=papers, counts=registry.status_counts(), status=status)

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
                body_html = md.markdown(body, extensions=["extra", "sane_lists"])
        return render(
            request, "paper.html", p=row, front=front, body_html=body_html,
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

    @app.get("/events", response_class=HTMLResponse)
    def events(request: Request):
        return render(request, "events.html", events=registry.recent_events(500))

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request, saved: int = 0):
        key_source = ""
        env = config.llm.get("api_key_env") or ""
        import os

        if env and os.environ.get(env):
            key_source = f"environment variable {env}"
        elif config.llm.get("api_key"):
            key_source = "local config file"
        return render(
            request, "settings.html", cfg=config, saved=saved, key_source=key_source, langs=LANGS,
            inbox_text="\n".join(str(p) for p in config.raw.get("inbox_dirs") or []),
            exclude_dirs=", ".join(config.exclude_dir_names),
            exclude_patterns=", ".join(config.exclude_patterns),
        )

    @app.post("/settings")
    def save_settings(
        csrf: str = Form(...),
        api_key: str = Form(""),
        clear_key: str = Form(""),
        base_url: str = Form(...),
        model: str = Form(...),
        output_language: str = Form("zh"),
        monthly_budget_usd: float = Form(5),
        inbox_dirs: str = Form(""),
        exclude_dirs: str = Form(""),
        exclude_patterns: str = Form(""),
        contact_email: str = Form(""),
    ):
        _check_csrf(csrf, csrf_token)
        raw = config.raw
        if clear_key:
            raw["llm"]["api_key"] = ""
        elif api_key.strip():
            raw["llm"]["api_key"] = api_key.strip()
        raw["llm"]["base_url"] = base_url.strip()
        raw["llm"]["model"] = model.strip()
        raw["llm"]["monthly_budget_usd"] = max(0.0, monthly_budget_usd)
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
        pipeline.llm._client = None  # pick up new key / endpoint
        threading.Thread(target=pipeline.process_pending, daemon=True).start()
        return RedirectResponse("/settings?saved=1", status_code=303)

    return app


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
