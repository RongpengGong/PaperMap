"""Periodic reviews (/digest): weekly and monthly reports, gap pushes, goals and the group-meeting outline."""
from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from .. import digest


def mount_digest(app, config, registry, pipeline, render, json_body, md_html, tr):
    digest.ensure_schema(registry)

    def today_done():
        if not digest.settings(config)["daily"]:
            return None
        try:
            return digest.today(config, registry)
        except Exception:  # noqa: BLE001 - never break the home page
            return None

    app.state.today_done = today_done

    def view(kind: str, period: str) -> dict:
        """A stored report, or a live preview of a period that has no report yet."""
        rep = digest.find(registry, kind, period)
        live = rep is None or period in (digest.current_period("week"), digest.current_period("month"))
        data = digest.build(config, registry, kind, period) if live else rep["data"]
        # feedback given after the report was written shows on its pushes
        fb = digest.feedback_map(registry)
        for p in data["pushes"]:
            f = fb.get(p["key"])
            p["state"] = f["action"] if f and (f["action"] != "resolved" or f["signature"] == p["signature"]) else None
        if kind == "week" and not live:
            data["goals"] = digest.goal_progress(registry, period, digest.activity(
                config, registry, *digest.period_range(kind, period)))
        out = {"kind": kind, "period": period, "data": data, "report": rep, "live": live,
               "summary_html": md_html(rep["summary"]) if rep and rep["summary"] else "",
               "outline": digest.read_outline(config, rep) if rep else ""}
        if kind == "week":
            out["next_goals"] = digest.goals(registry, data["next_period"])
        start, end = digest.period_range(kind, period)
        out["estimate"] = (digest.estimate_summary(config, registry, digest.activity(config, registry, start, end))
                           if config.model_ready() else {"calls": 0, "cost": 0.0})
        return out

    @app.get("/digest", response_class=HTMLResponse)
    def digest_page(request: Request, kind: str = "week", period: str = ""):
        if kind not in digest.KINDS:
            kind = "week"
        period = period or digest.current_period(kind)
        try:
            digest.period_range(kind, period)
        except ValueError:
            raise HTTPException(404)
        tabs = [("week", digest.current_period("week"), tr("本周至今")),
                ("week", digest.previous_period("week"), tr("上周")),
                ("month", digest.current_period("month"), tr("本月至今")),
                ("month", digest.previous_period("month"), tr("上月"))]
        return render(request, "digest.html", v=view(kind, period), tabs=tabs, history=digest.history(registry),
                      st=digest.settings(config), model_ready=config.model_ready(),
                      push_kinds=digest.PUSH_KINDS, actions=digest.ACTIONS,
                      period_label=digest.period_label(kind, period))

    @app.post("/api/digest/generate")
    async def api_generate(request: Request):
        body = await json_body(request)
        kind, period = str(body.get("kind") or ""), str(body.get("period") or "")
        try:
            digest.period_range(kind, period)
            rep = await run_in_threadpool(digest.generate, config, registry, pipeline.llm, kind, period,
                                          bool(body.get("model")))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": tr(str(exc))}
        return {"ok": True, "id": rep["id"], "path": rep["kb_path"]}

    @app.post("/api/digest/feedback")
    async def api_feedback(request: Request):
        body = await json_body(request)
        try:
            out = digest.give_feedback(registry, str(body.get("key") or ""), str(body.get("action") or ""),
                                       str(body.get("signature") or ""))
        except ValueError as exc:
            return {"ok": False, "message": tr(str(exc))}
        return {"ok": True, **out}

    @app.post("/api/digest/goals")
    async def api_goal_add(request: Request):
        body = await json_body(request)
        try:
            gid = digest.add_goal(registry, str(body.get("period") or ""), str(body.get("text") or ""))
        except ValueError as exc:
            return {"ok": False, "message": tr(str(exc))}
        return {"ok": True, "id": gid}

    @app.post("/api/digest/goals/{gid}")
    async def api_goal_set(gid: int, request: Request):
        body = await json_body(request)
        try:
            digest.set_goal(registry, gid, body.get("status"), bool(body.get("delete")))
        except ValueError as exc:
            return {"ok": False, "message": tr(str(exc))}
        return {"ok": True}

    @app.post("/api/digest/settings")
    async def api_settings(request: Request):
        body = await json_body(request)
        return {"ok": True, "settings": digest.save_settings(config, body)}

    @app.post("/api/digest/{rid}/outline")
    async def api_outline(rid: int, request: Request):
        body = await json_body(request)
        try:
            if "text" in body:
                path = digest.save_outline(config, registry, rid, body["text"])
                return {"ok": True, "path": path}
            out = await run_in_threadpool(digest.outline, config, registry, rid)
        except ValueError as exc:
            return {"ok": False, "message": tr(str(exc))}
        return {"ok": True, **out}

    def _download(rel: str | None, name: str) -> Response:
        if not rel:
            raise HTTPException(404)
        try:
            text = (config.knowledge_dir / rel).read_text("utf-8")
        except OSError:
            raise HTTPException(404)
        from urllib.parse import quote

        return Response(text, media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    @app.get("/digest/{rid}.md")
    def report_md(rid: int):
        rep = digest.get(registry, rid)
        return _download(rep and rep["kb_path"], f"{rep['period'] if rep else 'report'}.md")

    @app.get("/digest/{rid}/outline.md")
    def outline_md(rid: int):
        rep = digest.get(registry, rid)
        return _download(rep and rep["outline_path"], (rep["outline_path"] or "outline.md").split("/")[-1] if rep else "x")
