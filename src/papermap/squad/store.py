"""Squad runs, their task board, the event log the board shows, and the proposals the user reviews."""
from __future__ import annotations

import json

from ..registry import Registry, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS squad_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template TEXT NOT NULL, title TEXT NOT NULL, goal TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',      -- queued / running / stopping / stopped / done / error
    message TEXT NOT NULL DEFAULT '',
    cap_usd REAL NOT NULL DEFAULT 0, estimate_usd REAL NOT NULL DEFAULT 0, spent_usd REAL NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0, lead_turns INTEGER NOT NULL DEFAULT 0,
    result_md TEXT NOT NULL DEFAULT '', result_path TEXT NOT NULL DEFAULT '', dropped INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS squad_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES squad_runs(id) ON DELETE CASCADE,
    key TEXT NOT NULL, member TEXT NOT NULL, title TEXT NOT NULL, instructions TEXT NOT NULL DEFAULT '',
    blocked_by_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'queued',      -- queued / running / done / error / cancelled
    final INTEGER NOT NULL DEFAULT 0, steps INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (run_id, key)
);
CREATE TABLE IF NOT EXISTS squad_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES squad_runs(id) ON DELETE CASCADE,
    task_key TEXT NOT NULL DEFAULT '', who TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '', delivered INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_squad_events_run ON squad_events(run_id, id);
CREATE TABLE IF NOT EXISTS squad_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES squad_runs(id) ON DELETE CASCADE,
    task_key TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, payload_json TEXT NOT NULL, dedupe TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',     -- pending / accepted / rejected / failed
    result_json TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, decided_at TEXT,
    UNIQUE (run_id, dedupe)
);
CREATE TABLE IF NOT EXISTS squad_works (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES squad_runs(id) ON DELETE CASCADE,
    rec_json TEXT NOT NULL, dedupe TEXT NOT NULL, UNIQUE (run_id, dedupe)
);
"""

ACTIVE = ("queued", "running", "stopping")


MIGRATIONS = {"squad_runs": {"usage_json": "TEXT NOT NULL DEFAULT '{}'"},
              "squad_tasks": {"model": "TEXT NOT NULL DEFAULT ''"}}


def ensure_schema(registry: Registry) -> None:
    with registry._lock:  # noqa: SLF001
        registry.conn.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in registry.conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    registry.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        registry.conn.commit()


def create_run(registry: Registry, template: str, title: str, goal: str, params: dict, cap_usd: float,
               estimate_usd: float) -> int:
    ensure_schema(registry)
    now = now_iso()
    cur = registry.execute(
        "INSERT INTO squad_runs(template,title,goal,params_json,cap_usd,estimate_usd,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (template, title, goal, json.dumps(params, ensure_ascii=False), cap_usd, estimate_usd, now, now))
    return int(cur.lastrowid)


def get_run(registry: Registry, run_id: int) -> dict | None:
    ensure_schema(registry)
    r = registry.one("SELECT * FROM squad_runs WHERE id=?", (run_id,))
    if r is None:
        return None
    d = dict(r)
    d["params"] = json.loads(d.pop("params_json") or "{}")
    d["usage"] = json.loads(d.get("usage_json") or "{}")
    return d


def runs(registry: Registry, limit: int = 100) -> list[dict]:
    ensure_schema(registry)
    rows = registry.query(
        "SELECT r.*, (SELECT count(*) FROM squad_proposals p WHERE p.run_id=r.id AND p.status='pending') AS pending, "
        "(SELECT count(*) FROM squad_tasks t WHERE t.run_id=r.id) AS n_tasks, "
        "(SELECT count(*) FROM squad_tasks t WHERE t.run_id=r.id AND t.status IN ('done','error','cancelled')) AS n_done "
        "FROM squad_runs r ORDER BY r.id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def set_run(registry: Registry, run_id: int, **cols) -> None:
    cols["updated_at"] = now_iso()
    sets = ", ".join(f"{k}=?" for k in cols)
    registry.execute(f"UPDATE squad_runs SET {sets} WHERE id=?", (*cols.values(), run_id))


def add_cost(registry: Registry, run_id: int, task_key: str, cost: float, channel: dict | None = None) -> float:
    """Charge one model call to the run (and the task); returns the run's new total. ``channel``
    ({label, billing}) counts the call per model for the run's usage line."""
    with registry.transaction() as db:
        db.execute("UPDATE squad_runs SET spent_usd=spent_usd+?, calls=calls+1, updated_at=? WHERE id=?",
                   (cost, now_iso(), run_id))
        if channel:
            row = db.execute("SELECT usage_json FROM squad_runs WHERE id=?", (run_id,)).fetchone()
            usage = json.loads(row["usage_json"] or "{}")
            u = usage.setdefault(channel["label"], {"calls": 0, "usd": 0.0, "billing": channel["billing"]})
            u["calls"] += 1
            u["usd"] = round(u["usd"] + cost, 6)
            db.execute("UPDATE squad_runs SET usage_json=? WHERE id=?", (json.dumps(usage, ensure_ascii=False), run_id))
        if task_key:
            db.execute("UPDATE squad_tasks SET cost_usd=cost_usd+?, model=? WHERE run_id=? AND key=?",
                       (cost, (channel or {}).get("label", ""), run_id, task_key))
        return float(db.execute("SELECT spent_usd FROM squad_runs WHERE id=?", (run_id,)).fetchone()[0])


# ------------------------------------------------------------------ tasks
def tasks(registry: Registry, run_id: int) -> list[dict]:
    out = []
    for r in registry.query("SELECT * FROM squad_tasks WHERE run_id=? ORDER BY id", (run_id,)):
        d = dict(r)
        d["blocked_by"] = json.loads(d.pop("blocked_by_json") or "[]")
        d["result"] = json.loads(d["result_json"]) if d["result_json"] else None
        out.append(d)
    return out


def add_task(registry: Registry, run_id: int, key: str, member: str, title: str, instructions: str,
             blocked_by: list[str], final: bool = False) -> None:
    now = now_iso()
    registry.execute(
        "INSERT INTO squad_tasks(run_id,key,member,title,instructions,blocked_by_json,final,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, key, member, title, instructions, json.dumps(blocked_by), int(final), now, now))


def set_task(registry: Registry, run_id: int, key: str, **cols) -> None:
    if "result" in cols:
        cols["result_json"] = json.dumps(cols.pop("result"), ensure_ascii=False)
    cols["updated_at"] = now_iso()
    sets = ", ".join(f"{k}=?" for k in cols)
    registry.execute(f"UPDATE squad_tasks SET {sets} WHERE run_id=? AND key=?", (*cols.values(), run_id, key))


# ------------------------------------------------------------------ events
def log(registry: Registry, run_id: int, who: str, kind: str, text: str, task_key: str = "",
        data: dict | None = None, delivered: bool = True) -> int:
    cur = registry.execute(
        "INSERT INTO squad_events(run_id,task_key,who,kind,text,data_json,delivered,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, task_key, who, kind, text[:20000], json.dumps(data, ensure_ascii=False) if data else "",
         int(delivered), now_iso()))
    return int(cur.lastrowid)


def events(registry: Registry, run_id: int, after: int = 0, limit: int = 500) -> list[dict]:
    rows = registry.query("SELECT * FROM squad_events WHERE run_id=? AND id>? ORDER BY id LIMIT ?", (run_id, after, limit))
    return [dict(r) for r in rows]


def take_messages(registry: Registry, run_id: int, target: str) -> list[str]:
    """Undelivered follow-ups from the user to ``target`` ('lead' or a task key), marked delivered."""
    with registry.transaction() as db:
        rows = db.execute("SELECT id, text FROM squad_events WHERE run_id=? AND kind='user' AND delivered=0 "
                          "AND task_key=? ORDER BY id", (run_id, target)).fetchall()
        if rows:
            db.execute(f"UPDATE squad_events SET delivered=1 WHERE id IN ({','.join('?' * len(rows))})",
                       [r["id"] for r in rows])
    return [r["text"] for r in rows]


def pending_messages(registry: Registry, run_id: int, target: str) -> bool:
    return registry.one("SELECT 1 FROM squad_events WHERE run_id=? AND kind='user' AND delivered=0 AND task_key=?",
                        (run_id, target)) is not None


# ------------------------------------------------------------------ proposals and outside works
def add_proposal(registry: Registry, run_id: int, task_key: str, kind: str, payload: dict, dedupe: str) -> tuple[int, bool]:
    """Store a proposal; the same proposal again (same run) updates the pending one."""
    now = now_iso()
    with registry.transaction() as db:
        old = db.execute("SELECT id, status FROM squad_proposals WHERE run_id=? AND dedupe=?", (run_id, dedupe)).fetchone()
        if old:
            if old["status"] == "pending":
                db.execute("UPDATE squad_proposals SET payload_json=?, task_key=? WHERE id=?",
                           (json.dumps(payload, ensure_ascii=False), task_key, old["id"]))
            return int(old["id"]), False
        cur = db.execute("INSERT INTO squad_proposals(run_id,task_key,kind,payload_json,dedupe,created_at) "
                         "VALUES (?,?,?,?,?,?)", (run_id, task_key, kind, json.dumps(payload, ensure_ascii=False),
                                                  dedupe, now))
        return int(cur.lastrowid), True


def proposals(registry: Registry, run_id: int | None = None, status: str | None = None) -> list[dict]:
    ensure_schema(registry)
    sql, params = "SELECT * FROM squad_proposals WHERE 1=1", []
    if run_id is not None:
        sql += " AND run_id=?"
        params.append(run_id)
    if status:
        sql += " AND status=?"
        params.append(status)
    out = []
    for r in registry.query(sql + " ORDER BY id", params):
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload_json"))
        d["result"] = json.loads(d["result_json"]) if d["result_json"] else None
        out.append(d)
    return out


def pending_count(registry: Registry) -> int:
    ensure_schema(registry)
    r = registry.one("SELECT count(*) AS n FROM squad_proposals WHERE status='pending'")
    return int(r["n"]) if r else 0


def add_work(registry: Registry, run_id: int, rec: dict) -> int:
    key = (rec.get("doi") or rec.get("openalex_id") or rec.get("title", "")).lower()[:300]
    with registry.transaction() as db:
        old = db.execute("SELECT id FROM squad_works WHERE run_id=? AND dedupe=?", (run_id, key)).fetchone()
        if old:
            return int(old["id"])
        cur = db.execute("INSERT INTO squad_works(run_id,rec_json,dedupe) VALUES (?,?,?)",
                         (run_id, json.dumps(rec, ensure_ascii=False), key))
        return int(cur.lastrowid)


def get_work(registry: Registry, run_id: int, wid: int) -> dict | None:
    r = registry.one("SELECT rec_json FROM squad_works WHERE run_id=? AND id=?", (run_id, wid))
    return json.loads(r["rec_json"]) if r else None


def scorecards(registry: Registry, days: int = 14) -> list[dict]:
    """How each role did with each model over the last ``days``: calls, money, quotes the PDF check
    refused, sentences removed for lacking a source, and the researcher's verdict on its proposals."""
    ensure_schema(registry)
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = registry.query(
        "SELECT t.run_id, t.key, t.member, t.model, t.steps, t.cost_usd, t.status, t.final, r.dropped "
        "FROM squad_tasks t JOIN squad_runs r ON r.id=t.run_id WHERE r.created_at>=? AND t.status!='queued'", (since,))
    refused = {(r["run_id"], r["task_key"]): r["n"] for r in registry.query(
        "SELECT run_id, task_key, count(*) AS n FROM squad_events WHERE kind='action' AND created_at>=? "
        "AND data_json LIKE '%not found verbatim%' GROUP BY run_id, task_key", (since,))}
    verdicts: dict[tuple, dict] = {}
    for r in registry.query("SELECT run_id, task_key, status, count(*) AS n FROM squad_proposals "
                            "WHERE created_at>=? GROUP BY run_id, task_key, status", (since,)):
        verdicts.setdefault((r["run_id"], r["task_key"]), {})[r["status"]] = r["n"]
    out: dict[tuple, dict] = {}
    for r in rows:
        k = (r["member"], r["model"] or "?")
        c = out.setdefault(k, {"role": r["member"], "model": r["model"] or "?", "tasks": 0, "steps": 0, "usd": 0.0,
                               "errors": 0, "refused": 0, "dropped": 0, "accepted": 0, "rejected": 0, "pending": 0})
        c["tasks"] += 1
        c["steps"] += r["steps"]
        c["usd"] = round(c["usd"] + r["cost_usd"], 4)
        c["errors"] += r["status"] == "error"
        c["refused"] += refused.get((r["run_id"], r["key"]), 0)
        c["dropped"] += r["dropped"] if r["final"] else 0
        v = verdicts.get((r["run_id"], r["key"]), {})
        c["accepted"] += v.get("accepted", 0)
        c["rejected"] += v.get("rejected", 0)
        c["pending"] += v.get("pending", 0)
    return sorted(out.values(), key=lambda c: (c["role"], -c["tasks"]))
