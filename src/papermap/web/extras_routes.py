"""Quick jump (⌘K), "what to read today", the status check page and the user guide."""
from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import HTMLResponse

from .. import backup as backup_mod


def _age_hours(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600


def _dir_size(path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def mount_extras(app, config, registry, pipeline, render, tr, fname):
    # ------------------------------------------------------------ ⌘K quick jump
    @app.get("/api/jump")
    def jump(q: str = ""):
        q = " ".join(q.split())[:80]
        out: list[dict] = []
        if not q:
            for r in registry.query("SELECT id, title, first_author, year FROM papers WHERE status='done' "
                                    "AND reading_at IS NOT NULL ORDER BY reading_at DESC LIMIT 6"):
                out.append({"kind": "paper", "label": r["title"], "hint": f"{r['first_author'] or ''} {r['year'] or ''}",
                            "url": f"/?id={r['id']}"})
            return {"ok": True, "items": out}
        like = f"%{q}%"
        for r in registry.query(
                "SELECT id, title, first_author, year, journal FROM papers WHERE status != 'ignored' AND "
                "(title LIKE ? OR first_author LIKE ? OR doi LIKE ? OR journal LIKE ?) ORDER BY year DESC LIMIT 8",
                (like, like, like, like)):
            out.append({"kind": "paper", "label": r["title"], "hint": f"{r['first_author'] or ''} {r['year'] or ''} · {r['journal'] or ''}",
                        "url": f"/?id={r['id']}"})
        for f in registry.query("SELECT slug, name_zh, name_en, axis FROM fields WHERE status='active' AND "
                                "(name_zh LIKE ? OR name_en LIKE ? OR slug LIKE ?) LIMIT 5", (like, like, like)):
            out.append({"kind": "field", "label": fname(f), "hint": f["name_en"] or "", "url": f"/field/{f['slug']}"})
        try:
            for x in registry.query("SELECT id, title FROM evidence_questions WHERE title LIKE ? LIMIT 5", (like,)):
                out.append({"kind": "question", "label": x["title"], "hint": tr("研究问题"), "url": f"/evidence?question={x['id']}"})
        except Exception:  # noqa: BLE001 - older library without questions
            pass
        low = q.lower()
        for sub, kind, url, hint in (("concepts", "tool", "/concept/", tr("工具")), ("handbooks", "handbook", "/handbook/", tr("手册"))):
            d = config.knowledge_dir / sub
            if d.is_dir():
                for p in sorted(d.glob("*.md")):
                    if low in p.stem.lower():
                        out.append({"kind": kind, "label": p.stem, "hint": hint, "url": url + p.stem})
                        if sum(1 for o in out if o["kind"] == kind) >= 4:
                            break
        return {"ok": True, "items": out[:24]}

    # ------------------------------------------------------------ what to read today
    def today_items(limit: int = 5) -> list[dict]:
        items: list[dict] = []
        seen: set[int] = set()

        def add(pid, title, why, url=None, kind="paper"):
            if pid in seen or len(items) >= limit:
                return
            seen.add(pid)
            items.append({"id": pid, "title": title, "why": why, "url": url or f"/?id={pid}", "kind": kind})

        for r in registry.query("SELECT id, title FROM papers WHERE status='done' AND read_status='reading' "
                                "ORDER BY reading_at DESC LIMIT 2"):
            add(r["id"], r["title"], tr("读到一半，接着读"), f"/?id={r['id']}&read=1")
        try:
            for r in registry.query("SELECT DISTINCT p.id, p.title FROM version_checks c JOIN papers p ON p.id=c.paper_id "
                                    "WHERE c.decided IS NULL LIMIT 2"):
                add(r["id"], r["title"], tr("出了新版本，有摘录要核对"))
            for r in registry.query("SELECT DISTINCT p.id, p.title FROM gap_hits h JOIN papers p ON p.id=h.paper_id "
                                    "WHERE h.status='new' LIMIT 2"):
                add(r["id"], r["title"], tr("可能回答了你记下的证据缺口"))
        except Exception:  # noqa: BLE001
            pass
        try:
            for r in registry.query("SELECT p.id, p.title FROM paper_purposes pp JOIN papers p ON p.id=pp.paper_id "
                                    "WHERE pp.tag='toread' AND p.status='done' AND p.read_status != 'read' "
                                    "ORDER BY p.starred DESC, pp.created_at LIMIT 3"):
                add(r["id"], r["title"], tr("你标了「待精读」"))
        except Exception:  # noqa: BLE001
            pass
        for r in registry.query("SELECT id, title FROM papers WHERE status='done' AND starred=1 AND read_status='unread' "
                                "ORDER BY id DESC LIMIT 3"):
            add(r["id"], r["title"], tr("星标了还没读"))
        try:
            for r in registry.query("SELECT id, title, model_reason FROM track_candidates WHERE status='new' AND score >= 7 "
                                    "ORDER BY score DESC LIMIT 2"):
                add(-r["id"], r["title"], (r["model_reason"] or tr("新文献里得分最高")), f"/tracking#cand-{r['id']}", "new")
        except Exception:  # noqa: BLE001
            pass
        return items

    app.state.today_items = today_items

    # ------------------------------------------------------------ status check
    @app.get("/status", response_class=HTMLResponse)
    def status_page(request: Request):
        checks: list[dict] = []

        def add(group, label, level, detail, link=""):
            checks.append({"group": group, "label": label, "level": level, "detail": detail, "link": link})

        # model
        ready = config.model_ready()
        eff = config.effective_llm()
        add(tr("模型"), tr("主模型"), "ok" if ready else "error",
            f"{eff.get('preset_label') or eff.get('provider')} · {eff.get('model') or '—'}" if ready else tr("还没有连接，新论文只登记不生成卡片"),
            "/settings")
        spent, budget = registry.month_cost(), float(config.llm.get("monthly_budget_usd") or 0)
        if budget and eff.get("billing") != "subscription":
            add(tr("模型"), tr("本月费用"), "warn" if spent >= 0.8 * budget else "ok", f"${spent:.3f} / ${budget:.0f}", "/costs")
        # library
        n_done = registry.one("SELECT count(*) AS n FROM papers WHERE status='done'")["n"]
        n_err = registry.one("SELECT count(*) AS n FROM papers WHERE status='error'")["n"]
        n_new = registry.one("SELECT count(*) AS n FROM papers WHERE status IN ('new','extracting')")["n"]
        add(tr("文献库"), tr("论文"), "warn" if n_err else "ok",
            f"{n_done} {tr('篇已完成')}" + (f" · {n_new} {tr('篇等待处理')}" if n_new else "") + (f" · {n_err} {tr('篇出错')}" if n_err else ""),
            "/?status=error" if n_err else "/")
        try:
            indexed = registry.one("SELECT count(DISTINCT paper_id) AS n FROM search_state")["n"]
            add(tr("文献库"), tr("全文索引"), "ok" if indexed >= n_done else "warn",
                f"{indexed} / {n_done} {tr('篇已建索引')}" + ("" if indexed >= n_done else " · " + tr("后台会补上")), "/search")
        except Exception:  # noqa: BLE001
            pass
        db = config.db_path
        wal = db.with_name(db.name + "-wal")
        size = db.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
        add(tr("文献库"), tr("数据库"), "ok", f"{size / 1048576:.1f} MB · {db}")
        kb_size = _dir_size(config.knowledge_dir)
        free = shutil.disk_usage(config.knowledge_dir).free
        add(tr("文献库"), tr("知识库文件夹"), "warn" if free < 2 * 1024 ** 3 else "ok",
            f"{kb_size / 1048576:.0f} MB · {tr('磁盘剩余')} {free / 1024 ** 3:.1f} GB")
        trash_dir = config.data_dir / "trash"
        if trash_dir.exists():
            add(tr("文献库"), tr("回收站"), "ok", f"{_dir_size(trash_dir) / 1048576:.0f} MB")
        # safety
        backups = backup_mod.list_backups(config)
        age = (datetime.now(timezone.utc) - backups[0]["when"]).total_seconds() / 3600 if backups else None
        add(tr("数据安全"), tr("数据库备份"), "error" if age is None else "warn" if age > 72 else "ok",
            tr("还没有备份") if age is None else f"{tr('最近一次')} {age:.0f} {tr('小时前')} · {len(backups)} {tr('份')}",
            "/settings?section=backup")
        ms = backup_mod.mirror_state(config)
        mcfg = backup_mod.settings(config)
        if mcfg["mirror_enabled"]:
            h = _age_hours(ms.get("last"))
            add(tr("数据安全"), tr("知识库同步"), "error" if h is None else "warn" if h > 72 else "ok",
                tr("还没有同步过") if h is None else f"{tr('最近一次')} {h:.0f} {tr('小时前')} · {ms.get('target', '')}",
                "/settings?section=backup")
        else:
            add(tr("数据安全"), tr("知识库同步"), "warn", tr("没有打开：卡片、笔记和 PDF 副本只有一份"), "/settings?section=backup")
        # background work
        from .. import tracking

        tracking.ensure_schema(registry)
        last = _age_hours(tracking._state(registry, "last_run"))  # noqa: SLF001
        errs = sum(s.get("errs") or 0 for s in tracking.source_status(registry))
        add(tr("后台任务"), tr("新文献追踪"), "warn" if errs or (last is not None and last > 72) else "ok",
            (tr("还没有运行") if last is None else f"{tr('最近一次')} {last:.0f} {tr('小时前')}") + (f" · {errs} {tr('个订阅出错')}" if errs else ""),
            "/tracking")
        failed = registry.one("SELECT count(*) AS n FROM jobs WHERE status='error' AND updated_at > datetime('now', '-7 days')")["n"]
        running = registry.one("SELECT count(*) AS n FROM jobs WHERE status IN ('queued','running')")["n"]
        add(tr("后台任务"), tr("翻译与技能任务"), "warn" if failed else "ok",
            f"{running} {tr('个进行中')} · {failed} {tr('个最近 7 天失败')}")
        errors = registry.one("SELECT count(*) AS n FROM events WHERE level='error' AND created_at > datetime('now', '-7 days')")["n"]
        add(tr("后台任务"), tr("错误日志"), "warn" if errors else "ok", f"{errors} {tr('条最近 7 天的错误')}", "/events")
        worst = "error" if any(c["level"] == "error" for c in checks) else "warn" if any(c["level"] == "warn" for c in checks) else "ok"
        groups: dict[str, list] = {}
        for c in checks:
            groups.setdefault(c["group"], []).append(c)
        return render(request, "status.html", groups=groups, worst=worst)

    # ------------------------------------------------------------ guide
    @app.get("/guide", response_class=HTMLResponse)
    def guide_page(request: Request):
        return render(request, "guide.html")
