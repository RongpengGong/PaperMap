"""Agent squads (/squads): start a squad, watch its task board, send follow-ups, review its proposals."""
from __future__ import annotations

from datetime import date
from urllib.parse import parse_qs

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import chat, evidence
from ..registry import now_iso
from ..squad import proposals as props
from ..squad import evaluate, models, roles, runtime, store

STATUS = {"queued": "排队中", "running": "进行中", "stopping": "正在停止", "stopped": "已停止", "done": "已完成",
          "error": "出错", "cancelled": "已取消"}
PROPOSAL_KINDS = {"evidence": "证据摘录", "judgment": "判断草稿", "gap": "证据缺口", "question": "新研究问题",
                  "take_in": "收进文献库", "skip": "跳过候选", "collect": "收录库外论文"}


def mount_squads(app, config, registry, pipeline, render, json_body, md_html, tr):
    store.ensure_schema(registry)
    runner = runtime.squad_runner(pipeline)
    app.state.squad_pending = lambda: store.pending_count(registry)

    def local_only(request: Request) -> None:
        if getattr(request.state, "remote", False):
            raise HTTPException(403, tr("手机上暂时只能查看小队，发起、追加指令和确认请在电脑上操作。"))

    def problem(message: str, status: int = 400):
        return JSONResponse({"ok": False, "message": tr(str(message))}, status_code=status)

    def available() -> int:
        from .. import tracking

        tracking.ensure_schema(registry)
        return int(registry.one("SELECT count(*) AS n FROM track_candidates WHERE status='new'")["n"])

    def templates_view() -> list[dict]:
        return [{"key": t.key, "label": tr(str(t.label)), "description": tr(str(t.description)),
                 "members": [tr(str(roles.LEAD))] + [tr(str(m.label)) for m in t.members.values()]}
                for t in roles.TEMPLATES.values() if t.key not in roles.PAPER_ONLY]

    def params_from(body: dict) -> tuple[str, str, str, dict]:
        key = body.get("template")
        if key not in roles.TEMPLATES:
            raise ValueError("请选择小队类型。")
        goal = str(body.get("goal") or "").strip()
        if len(goal) > 2000:
            raise ValueError("目标太长了（最多 2000 字）。")
        defaults = models.settings(config)
        params: dict = {"parallel": max(1, min(4, int(body.get("parallel") or defaults["parallel"])))}
        if key == "review":
            if not goal:
                raise ValueError("请写下综述的题目。")
            params["max_papers"] = max(2, min(12, int(body.get("max_papers") or 8)))
            params["external"] = bool(body.get("external", defaults["external"]))
            title = goal.splitlines()[0][:80]
        elif key == "deep":
            row = registry.get_paper(int(body.get("paper_id") or 0))
            if row is None or row["status"] != "done":
                raise ValueError("这篇论文还没有卡片，暂时不能精读。")
            params["paper_id"] = row["id"]
            title = tr("单篇深读") + "：" + (row["title"] or "")[:70]
        elif key == "check":
            ids = [int(x) for x in (body.get("question_ids") or []) if str(x).isdigit()][:5]
            rows = registry.query(f"SELECT id, title FROM evidence_questions WHERE status='active' AND id IN "
                                  f"({','.join('?' * len(ids))})", tuple(ids)) if ids else []
            if not rows:
                raise ValueError("请至少选择一个研究问题。")
            params["question_ids"] = [r["id"] for r in rows]
            params["max_papers"] = max(2, min(10, int(body.get("max_papers") or 6)))
            title = tr("证据核对") + "：" + rows[0]["title"][:60] + (" " + tr("等 {n} 个问题", n=len(rows)) if len(rows) > 1 else "")
        else:
            n = available()
            if not n:
                raise ValueError("现在没有待看的新文献候选。先在「新文献」里运行一次追踪。")
            params["limit"] = max(5, min(60, int(body.get("limit") or 30)))
            params["available"] = n
            title = tr("新文献分诊") + " " + date.today().isoformat()
        return key, title, goal, params

    @app.get("/squads", response_class=HTMLResponse)
    def squads_page(request: Request):
        rows = store.runs(registry)
        for r in rows:
            r["status_label"] = tr(STATUS.get(r["status"], r["status"]))
            r["template_label"] = tr(str(roles.TEMPLATES[r["template"]].label)) if r["template"] in roles.TEMPLATES else r["template"]
        return render(request, "squads.html", runs=rows, squad_templates=templates_view(), squad_defaults=models.settings(config),
                      questions=evidence.questions(registry), candidates=available())

    @app.post("/api/squads/estimate")
    async def squad_estimate(request: Request):
        body = await json_body(request)
        try:
            key, _, _, params = params_from(body)
        except ValueError as exc:
            return problem(exc)
        return {"ok": True, **runtime.estimate(config, key, params, params.get("available", 0))}

    @app.post("/api/squads/start")
    async def squad_start(request: Request):
        local_only(request)
        body = await json_body(request)
        try:
            key, title, goal, params = params_from(body)
            cap = float(body.get("cap_usd") if body.get("cap_usd") not in (None, "") else models.settings(config)["default_cap_usd"])
            if not 0 <= cap <= 50:
                raise ValueError("费用上限需要在 0–50 美元之间。")
        except (ValueError, TypeError) as exc:
            return problem(exc)
        est = runtime.estimate(config, key, params, params.get("available", 0))
        rid = store.create_run(registry, key, title, goal, params, cap, est["usd"])
        await run_in_threadpool(runner.start, rid)
        return {"ok": True, "id": rid, "url": f"/squad/{rid}"}

    def get_run(rid: int) -> dict:
        run = store.get_run(registry, rid) if 0 < rid < 2**63 else None
        if run is None:
            raise HTTPException(404, tr("没有这次小队运行。"))
        return run

    @app.get("/squad/{rid}", response_class=HTMLResponse)
    def squad_page(request: Request, rid: int):
        run = get_run(rid)
        t = roles.TEMPLATES.get(run["template"])
        members = {k: tr(str(m.label)) for k, m in (t.members.items() if t else [])}
        members["lead"] = tr(str(roles.LEAD))
        return render(request, "squad_run.html", run=run, template_label=tr(str(t.label)) if t else run["template"],
                      member_labels=members, status_labels={k: tr(v) for k, v in STATUS.items()},
                      proposal_kinds={k: tr(v) for k, v in PROPOSAL_KINDS.items()},
                      stances={k: tr(str(v)) for k, v in evidence.STANCES.items()},
                      dimensions={k: tr(str(v)) for k, v in evidence.DIMENSIONS.items()})

    @app.get("/api/squads/{rid}/state")
    def squad_state(rid: int, after: int = 0):
        run = get_run(rid)
        tasks = store.tasks(registry, rid)
        for t in tasks:
            t["summary_html"] = md_html(t["result"]["summary"]) if t["result"] and t["result"].get("summary") else ""
            t.pop("result_json", None)
        events = store.events(registry, rid, after)
        for e in events:
            e.pop("delivered", None)
            if e["who"] == "system":
                e["text"] = tr(e["text"])
        run["message"] = tr(run["message"]) if run["message"] else ""
        return {"run": {k: run[k] for k in ("id", "status", "message", "cap_usd", "spent_usd", "estimate_usd", "calls",
                                            "title", "goal", "result_path", "dropped")},
                "result_html": md_html(run["result_md"]) if run["result_md"] else "",
                "tasks": tasks, "events": events, "proposals": store.proposals(registry, rid)}

    @app.post("/api/squads/{rid}/message")
    async def squad_message(request: Request, rid: int):
        local_only(request)
        body = await json_body(request)
        get_run(rid)
        try:
            await run_in_threadpool(runtime.follow_up, pipeline, rid, str(body.get("to") or "lead"),
                                    str(body.get("text") or ""))
        except ValueError as exc:
            return problem(exc)
        return {"ok": True}

    @app.post("/api/squads/{rid}/cap")
    async def squad_cap(request: Request, rid: int):
        local_only(request)
        body = await json_body(request)
        get_run(rid)
        try:
            cap = float(body.get("cap_usd"))
            if not 0 <= cap <= 50:
                raise ValueError
        except (TypeError, ValueError):
            return problem("费用上限需要在 0–50 美元之间。")
        store.set_run(registry, rid, cap_usd=cap)
        return {"ok": True}

    @app.post("/api/squads/{rid}/stop")
    async def squad_stop(request: Request, rid: int):
        local_only(request)
        await json_body(request)
        get_run(rid)
        runtime.stop(pipeline, rid)
        return {"ok": True}

    @app.post("/api/squads/proposals/{pid}")
    async def squad_proposal(request: Request, pid: int):
        local_only(request)
        body = await json_body(request)
        edits = body.get("edits") if isinstance(body.get("edits"), dict) else {}
        try:
            res = await run_in_threadpool(props.decide, pipeline, pid, str(body.get("action") or ""), edits)
        except ValueError as exc:
            return problem(exc)
        return {"ok": True, **res}

    # ---------------------------------------------------------------- from the page "Ask AI" drawer
    def page_plan(key: str, search: str, text: str, title: str, page_text: str) -> dict:
        """Which squad fits the page the researcher is on, and with what parameters."""
        qs = parse_qs(search.lstrip("?"))
        base = {"template": "review", "goal": text, "max_papers": 6, "external": True}
        if key.startswith("/evidence"):
            qid = qs.get("question", [""])[0]
            rows = evidence.questions(registry)
            q = next((r for r in rows if str(r["id"]) == qid), rows[0] if rows else None)
            if q is not None:
                base = {"template": "check", "goal": text, "question_ids": [q["id"]], "max_papers": 6}
        elif key.startswith("/field/"):
            base["field"] = key.split("/")[2]
        elif key.startswith("/tracking"):
            base = {"template": "triage", "goal": text, "limit": 30}
        base["page_title"] = title[:200]
        base["page_context"] = page_text[:4000]
        return base

    def active_run(store_path) -> int | None:
        data = chat.load(store_path)
        for m in reversed(data["messages"]):
            if m.get("role") == "squad":
                run = store.get_run(registry, int(m.get("run_id") or 0))
                return run["id"] if run and run["status"] in store.ACTIVE else None
        return None

    @app.post("/api/page-chat/squad")
    async def page_squad(request: Request):
        local_only(request)
        body = await json_body(request)
        key = str(body.get("key") or "").split("?")[0][:200]
        if not key.startswith("/"):
            raise HTTPException(400)
        text = str(body.get("text") or "").strip()
        if not text or len(text) > 2000:
            return problem("请写下要交给小队的问题（最多 2000 字）。")
        path = chat.page_store(config.data_dir, key)
        plan = page_plan(key, str(body.get("search") or ""), text, str(body.get("title") or ""),
                         str(body.get("page_text") or ""))
        return await chat_squad(path, text, plan, {"chat_key": key}, body)

    @app.post("/api/paper/{paper_id}/chat/squad")
    async def paper_squad(paper_id: int, request: Request):
        """Deep reading of one paper from its own AI chat; the report joins that paper's conversation."""
        local_only(request)
        body = await json_body(request)
        text = str(body.get("text") or "").strip()
        if not text or len(text) > 2000:
            return problem("请写下要交给小队的问题（最多 2000 字）。")
        try:
            _, card = pipeline._paper_files(paper_id)  # noqa: SLF001
        except ValueError:
            return problem("这篇论文还没有卡片，暂时不能精读。")
        plan = {"template": "deep", "goal": text, "paper_id": paper_id}
        return await chat_squad(chat.chat_path(card), text, plan, {"chat_paper": paper_id}, body)

    async def chat_squad(path, text: str, plan: dict, link: dict, body: dict):
        """Shared by the page drawer and the paper chat: follow-up to a running squad, preview, or start."""
        running = active_run(path)
        if running:  # while a squad works, the chat talks to its lead
            try:
                await run_in_threadpool(runtime.follow_up, pipeline, running, "lead", text)
            except ValueError as exc:
                return problem(exc)
            chat.append(path, {"role": "user", "content": text, "at": now_iso(), "squad_to": running})
            return {"ok": True, "followed": running}
        try:
            tkey, title, goal, params = params_from({**plan, "parallel": 4})
        except (ValueError, TypeError) as exc:
            return problem(exc)
        params.update({k: plan[k] for k in ("field", "page_title", "page_context") if plan.get(k)})
        t = roles.TEMPLATES[tkey]
        est = runtime.estimate(config, tkey, params, params.get("available", 0))
        if not body.get("confirm"):
            questions = [r["title"] for r in registry.query(
                f"SELECT title FROM evidence_questions WHERE id IN ({','.join('?' * len(params.get('question_ids') or []))})",
                tuple(params.get("question_ids") or []))] if params.get("question_ids") else []
            return {"ok": True, "preview": True, "template": tkey, "label": tr(str(t.label)),
                    "description": tr(str(t.description)), "questions": questions, "field": params.get("field", ""),
                    "members": [tr(str(roles.LEAD))] + [tr(str(m.label)) for m in t.members.values()], **est,
                    "cap_usd": models.settings(config)["default_cap_usd"]}
        try:
            cap = float(body.get("cap_usd") if body.get("cap_usd") not in (None, "") else models.settings(config)["default_cap_usd"])
            if not 0 <= cap <= 50:
                raise ValueError
        except (TypeError, ValueError):
            return problem("费用上限需要在 0–50 美元之间。")
        params.update(link)
        rid = store.create_run(registry, tkey, text.splitlines()[0][:80] if tkey == "review" else title, goal,
                               params, cap, est["usd"])
        chat.append(path, {"role": "user", "content": text, "at": now_iso(), "squad_to": rid},
                    {"role": "squad", "run_id": rid, "content": "", "at": now_iso()})
        await run_in_threadpool(runner.start, rid)
        return {"ok": True, "id": rid, "url": f"/squad/{rid}"}

    @app.get("/api/squads/{rid}/brief")
    def squad_brief(rid: int):
        """A compact view for the chat drawer's progress card."""
        run = get_run(rid)
        t = roles.TEMPLATES.get(run["template"])
        label = (lambda k: tr(str(t.members[k].label)) if t and k in t.members else k)
        tasks = [{"key": x["key"], "member": label(x["member"]), "title": x["title"], "status": x["status"]}
                 for x in store.tasks(registry, rid)]
        pending = len(store.proposals(registry, rid, "pending"))
        return {"ok": True, "id": rid, "status": run["status"], "status_label": tr(STATUS.get(run["status"], run["status"])),
                "template_label": tr(str(t.label)) if t else run["template"], "title": run["title"],
                "spent_usd": run["spent_usd"], "cap_usd": run["cap_usd"], "message": tr(run["message"]) if run["message"] else "",
                "tasks": tasks, "pending": pending,
                "result_html": chat.page_links(md_html(run["result_md"])) if run["result_md"] else ""}

    # ---------------------------------------------------------------- squad settings: who uses which model
    def role_labels() -> dict[str, str]:
        out = {"lead": tr(str(roles.LEAD))}
        for t in roles.TEMPLATES.values():
            for k, m in t.members.items():
                out.setdefault(k, tr(str(m.label)))
        return out

    def role_uses() -> dict[str, list[str]]:
        uses: dict[str, list[str]] = {"lead": [tr(str(t.label)) for t in roles.TEMPLATES.values()]}
        for t in roles.TEMPLATES.values():
            for k in t.members:
                uses.setdefault(k, []).append(tr(str(t.label)))
        return uses

    @app.get("/squads/settings", response_class=HTMLResponse)
    async def squad_settings_page(request: Request):
        roster = await run_in_threadpool(models.roster, config, True)
        s = models.settings(config)
        labels = role_labels()
        warning = models.same_maker_warning(config, s)
        if warning:
            known = {m["key"]: m for m in roster}
            warning = tr(warning, vendor=(known.get(s["roles"]["critic"]["candidates"][0]["model"]) or {}).get("vendor", ""))
        estimates = []
        for t in roles.TEMPLATES.values():
            params = dict(t.params)
            if t.key == "check":
                params["question_ids"] = [1]
            est = await run_in_threadpool(runtime.estimate, config, t.key, params, 30)
            estimates.append({"label": tr(str(t.label)), **est})
        cards = store.scorecards(registry)
        for c in cards:
            c["role_label"] = labels.get(c["role"], c["role"])
        review = evaluate.latest(registry)
        return render(request, "squad_settings.html", roster=roster, s=s, role_order=models.ROLE_ORDER,
                      review=review, es=evaluate.eval_settings(config), review_due=evaluate.due(registry),
                      period=evaluate.PERIOD_DAYS,
                      role_labels=labels, role_uses=role_uses(), tiers={k: tr(str(v)) for k, v in models.TIERS.items()},
                      role_tiers=models.ROLE_TIERS, budgets={k: tr(str(v)) for k, v in models.BUDGETS.items()},
                      optional=sorted(models.OPTIONAL), templates=[{"key": t.key, "label": tr(str(t.label)),
                                                                     "members": list(t.members)} for t in roles.TEMPLATES.values()],
                      warning=warning, estimates=estimates, scorecards=cards)

    @app.post("/api/squads/settings")
    async def squad_settings_save(request: Request):
        local_only(request)
        body = await json_body(request)
        try:
            clean = await run_in_threadpool(models.validate, config, body)
        except ValueError as exc:
            return problem(exc)
        if (config.raw.get("squad") or {}).get("eval"):
            clean["eval"] = config.raw["squad"]["eval"]  # the model-review settings are saved separately
        config.raw["squad"] = clean
        config.save()
        return {"ok": True}

    @app.post("/api/squads/recommend")
    async def squad_recommend(request: Request):
        body = await json_body(request)
        rec = await run_in_threadpool(models.recommend, config, str(body.get("budget") or ""))
        return {"ok": True, "roles": rec}

    @app.post("/api/squads/models/refresh")
    async def squad_models_refresh(request: Request):
        await json_body(request)
        models.forget()
        await run_in_threadpool(models.roster, config, True)
        return {"ok": True}


    # ---------------------------------------------------------------- two-week model review
    @app.post("/api/squads/review/settings")
    async def squad_review_settings(request: Request):
        local_only(request)
        body = await json_body(request)
        known = {m["key"]: m for m in await run_in_threadpool(models.roster, config)}
        evaluator = []
        for c in (body.get("evaluator") or [])[:3]:
            key, effort = str(c.get("model") or ""), str(c.get("effort") or "")
            if key not in known:
                return problem("有一个角色选了还没有配置好的模型，请重新检查订阅后再选。")
            evaluator.append({"model": key, "effort": effort if effort in known[key]["efforts"] else ""})
        sources = []
        for line in str(body.get("sources") or "").splitlines()[:12]:
            name, _, url = line.rpartition("|") if "|" in line else ("", "", line)
            url = url.strip()
            if url.startswith(("http://", "https://")):
                sources.append({"name": name.strip() or url, "url": url})
        squad = dict(config.raw.get("squad") or {})
        squad["eval"] = {"evaluator": evaluator, "sources": sources, "notes": str(body.get("notes") or "")[:20000],
                         "auto": bool(body.get("auto", True))}
        config.raw["squad"] = squad
        config.save()
        return {"ok": True}

    @app.post("/api/squads/review/start")
    async def squad_review_start(request: Request):
        local_only(request)
        await json_body(request)
        return {"ok": True, "id": evaluate.start(pipeline)}

    @app.get("/api/squads/review/latest")
    def squad_review_latest():
        rev = evaluate.latest(registry)
        return {"ok": True, "review": rev}

    @app.post("/api/squads/review/{rid}/decide")
    async def squad_review_decide(request: Request, rid: int):
        local_only(request)
        body = await json_body(request)
        try:
            res = await run_in_threadpool(evaluate.decide, config, registry, rid, str(body.get("role") or ""),
                                          str(body.get("action") or ""))
        except ValueError as exc:
            return problem(exc)
        return {"ok": True, **res}
