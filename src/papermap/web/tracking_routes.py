"""New-literature page: candidates with their reasons, watches, sources (stage 7 task 6)."""
from __future__ import annotations

import secrets
import threading

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import tracking


def mount_tracking(app, config, registry, pipeline, render, csrf_token):
    tracking.ensure_schema(registry)

    def check(body: dict) -> None:
        csrf = body.get("csrf")
        if not isinstance(csrf, str) or not secrets.compare_digest(csrf.encode(), csrf_token.encode()):
            raise HTTPException(403, "请刷新页面后重试。")

    @app.get("/tracking", response_class=HTMLResponse)
    def tracking_page(request: Request, status: str = "new"):
        if status not in tracking.STATUSES:
            status = "new"
        try:
            tracking.sync_watches(registry, pipeline.taxonomy())
        except Exception:  # noqa: BLE001 - a broken taxonomy must not hide the page
            tracking.sync_watches(registry, None)
        return render(request, "tracking.html", status=status, items=tracking.candidates(registry, status),
                      counts=tracking.counts(registry), statuses=tracking.STATUSES, watches=tracking.watches(registry),
                      sources=tracking.source_status(registry), kinds=tracking.KINDS,
                      running=tracking._run_lock.locked(),  # noqa: SLF001
                      settings=tracking.settings(config), last_run=tracking._state(registry, "last_run"),  # noqa: SLF001
                      has_key=bool((config.raw.get("metadata") or {}).get("openalex_api_key")),
                      digest=tracking.digest_for_page(registry))

    @app.post("/api/tracking/run")
    def tracking_run(body: dict):
        check(body)
        if tracking._run_lock.locked():  # noqa: SLF001
            return {"ok": False, "message": "正在检查中，请稍候"}
        threading.Thread(target=lambda: tracking.run(pipeline, force=bool(body.get("force"))), daemon=True).start()
        return {"ok": True}

    @app.get("/api/tracking/status")
    def tracking_status():
        return {"running": tracking._run_lock.locked(), "badge": tracking.badge(registry),  # noqa: SLF001
                "last_run": tracking._state(registry, "last_run")}  # noqa: SLF001

    @app.post("/api/tracking/candidates/{cid}")
    def tracking_candidate(cid: int, body: dict):
        check(body)
        action = str(body.get("action", ""))
        try:
            if action == "take":
                return tracking.take_in(pipeline, cid)
            tracking.decide(registry, cid, action)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True}

    @app.post("/api/tracking/watches/{wid}")
    def tracking_watch(wid: int, body: dict):
        check(body)
        try:
            tracking.set_watch(registry, wid, body.get("enabled"), body.get("query"), bool(body.get("delete")))
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True}

    @app.get("/api/tracking/authors")
    def tracking_author_search(q: str = ""):
        q = " ".join(q.split())[:100]
        if len(q) < 3:
            return {"ok": True, "authors": []}
        md = pipeline.metadata
        try:
            body = md._get_json("https://api.openalex.org/authors",  # noqa: SLF001
                                {"search": q, "per_page": 8, **md._openalex_params()}, cache=True)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"OpenAlex：{exc}"}
        out = []
        for a in (body or {}).get("results", []):
            inst = (a.get("last_known_institutions") or [{}])[0] or {}
            out.append({"id": (a.get("id") or "").rsplit("/", 1)[-1], "name": a.get("display_name", ""),
                        "institution": inst.get("display_name", ""), "works": a.get("works_count", 0),
                        "orcid": a.get("orcid") or ""})
        return {"ok": True, "authors": out}

    @app.post("/api/tracking/authors")
    def tracking_follow(body: dict):
        check(body)
        try:
            wid = tracking.follow_author(registry, str(body.get("id", "")), str(body.get("name", "")))
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "id": wid}
