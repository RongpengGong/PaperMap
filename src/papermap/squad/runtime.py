"""Runs a squad: the lead plans a task board, members work through it in parallel, the writer's report
becomes the result. One worker per library (file lock), so two app windows never run the same squad.

The model protocol is plain JSON over ``chat_json`` (no provider-specific tool calling), so every
configured model works, including subscription CLIs. Each call is charged to the run; the run stops
at its cap. Follow-ups from the researcher reach the lead (or one member) on its next turn.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime

from ..jobs import _worker_lock
from ..llm import BudgetExceeded, LLMError, LLMNotConfigured, compute_cost
from ..registry import now_iso
from . import cite, roles, store, tools
from . import messages as M

log = logging.getLogger(__name__)

MAX_TASKS = 40
MAX_LEAD_TURNS = 12
MAX_CALLS = 200            # also bounds subscription runs, whose calls cost nothing here
DEFAULT_PARALLEL = 4
HISTORY_CHARS = 45000
DONEISH = ("done", "error", "cancelled")
LANGS = {"zh": "Simplified Chinese (keep gene, method and software names in English)", "en": "English",
         "bilingual": "Simplified Chinese with key English terms in parentheses", "de": "German"}


class Stopped(Exception):
    pass


class CapReached(Exception):
    pass


LEAD_SYSTEM = """You lead a small team of research assistants inside PaperMap, a researcher's own literature library.
You do not read papers yourself: you plan the work as a task board, hand each task to one member, and decide when the team is done.

Reply with ONE JSON object:
{{"note": "one or two sentences to the researcher about the plan or what changed",
 "add_tasks": [{{"key": "short unique id such as s1 or r3", "member": "member key", "title": "short title",
               "instructions": "exactly what to do, with the paper ids (P#), question ids (Q#) or offsets involved",
               "blocked_by": ["keys of tasks that must finish first"], "final": false}}],
 "cancel_tasks": ["keys of queued tasks that are no longer needed"],
 "finish": false}}

Rules:
- Tasks run in parallel unless blocked_by says otherwise; keep them independent where you can.
- blocked_by may only name existing tasks or tasks listed earlier in the same reply.
- Exactly one task has "final": true, given to the '{final}' member and blocked by the work it summarises; its report is the team's result.
- At most {max_tasks} tasks in total. Do not repeat work that is already done; read the board.
- After results come in you may add follow-up tasks (for example a second reader when a search found more papers).
- Set "finish": true only when every task is done, failed or cancelled and nothing more is worth doing.
- The researcher may send follow-up messages; follow them.
- Write note, titles and instructions in {lang}.

YOUR TEAM:
{roster}

THE ASSIGNMENT:
{brief}
"""

MEMBER_SYSTEM = """You are the '{member}' on a research team inside PaperMap, a researcher's own literature library.
{brief}

You work in steps. Each reply is ONE JSON object, either
{{"actions": [{{"tool": "tool name", "args": {{...}}}}], "done": false}}
(one to four tool calls; you see their results in the next step), or, when your task is complete,
{{"done": true, "result": {{"summary": "your report in Markdown"}}}}

TOOLS:
{catalog}

Rules:
- Only state what tools showed you. Cite library papers as [P#], excerpts as [E#], candidates as [C#], outside works as [W#].
- Tools that start with propose_ only file a suggestion for the researcher; they change nothing by themselves.
- Stay within your task; do not do other members' work.
- Write your report in {lang}; keep quotations in their original language.
"""


def _lang(config) -> str:
    return LANGS.get(config.output_language, LANGS["zh"])


# ------------------------------------------------------------------ cost estimate
def estimate(config, template: str, params: dict, available: int = 0) -> dict:
    t = roles.TEMPLATES[template]
    lead_calls = 3
    if template == "review":
        calls = 6 + params.get("max_papers", 8) * 4 + 5 + 3
    elif template == "check":
        calls = len(params.get("question_ids") or [1]) * (5 + params.get("max_papers", 6) * 5 + 5) + 4
    elif template == "deep":
        calls = 7 + 7 + 6 + 8 + 4
    else:
        n = min(params.get("limit", 30), available) if available else params.get("limit", 30)
        calls = math.ceil(max(n, 1) / 10) * 4 + 2
    eff = config.effective_llm()
    usd = lead_calls * compute_cost(eff, 5000, 900) + calls * compute_cost(eff, 9000, 900)
    return {"calls": lead_calls + calls, "usd": round(usd, 4), "subscription": eff.get("billing") == "subscription",
            "model": eff.get("model", ""), "template": t.key}


# ------------------------------------------------------------------ the worker
class SquadRunner:
    def __init__(self, pipeline):
        self.pipeline = pipeline

    @property
    def registry(self):
        return self.pipeline.registry

    def recover(self) -> None:
        """App start: interrupted runs continue where they stopped (finished tasks are kept)."""
        store.ensure_schema(self.registry)
        self.schedule()

    def start(self, run_id: int) -> None:
        store.set_run(self.registry, run_id, status="queued", message="")
        self.schedule()

    def schedule(self) -> None:
        store.ensure_schema(self.registry)
        lock = None
        try:
            with self.registry.transaction() as db:
                lock = _worker_lock(self.registry.db_path, "squad")
                if lock:
                    # We own the worker: anything 'running' belonged to a process that is gone.
                    db.execute("UPDATE squad_runs SET status='queued', updated_at=? WHERE status='running'", (now_iso(),))
                    db.execute("UPDATE squad_runs SET status='stopped', updated_at=? WHERE status='stopping'", (now_iso(),))
                    db.execute("UPDATE squad_tasks SET status='queued' WHERE status='running' AND run_id IN "
                               "(SELECT id FROM squad_runs WHERE status IN ('queued','stopped'))")
                    if not db.execute("SELECT 1 FROM squad_runs WHERE status='queued' LIMIT 1").fetchone():
                        lock.close()
                        lock = None
            if lock:
                threading.Thread(target=self._work, args=(lock,), name="squad", daemon=True).start()
                lock = None
        finally:
            if lock:
                lock.close()

    def _work(self, lock) -> None:
        try:
            while True:
                with self.registry.transaction() as db:
                    row = db.execute("SELECT id FROM squad_runs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                    if row is None:
                        lock.close()
                        return
                    db.execute("UPDATE squad_runs SET status='running', message='', updated_at=? WHERE id=?",
                               (now_iso(), row["id"]))
                self.execute(row["id"])
        except sqlite3.ProgrammingError as exc:  # the library was closed under us (app exit, tests)
            if "closed database" not in str(exc).lower():
                log.exception("squad worker failed")
        except Exception:  # noqa: BLE001 - never kill the app over a squad
            log.exception("squad worker failed")
        finally:
            lock.close()

    def execute(self, run_id: int) -> None:
        reg = self.registry
        try:
            Squad(self.pipeline, run_id).run()
        except Stopped:
            store.set_run(reg, run_id, status="stopped", message=M.STOPPED)
            store.log(reg, run_id, "system", "note", M.STOPPED_NOTE)
        except CapReached:
            store.set_run(reg, run_id, status="stopped", message=M.CAPPED)
            store.log(reg, run_id, "system", "note", M.CAPPED_NOTE)
        except (BudgetExceeded, LLMNotConfigured) as exc:
            store.set_run(reg, run_id, status="error", message=str(exc)[:500])
            store.log(reg, run_id, "system", "error", str(exc)[:2000])
        except Exception as exc:  # noqa: BLE001
            log.exception("squad run %s failed", run_id)
            store.set_run(reg, run_id, status="error", message=f"{type(exc).__name__}: {exc}"[:500])
            store.log(reg, run_id, "system", "error", f"{type(exc).__name__}: {exc}"[:2000])
            reg.log("error", f"Squad run #{run_id} failed: {exc}")


# ------------------------------------------------------------------ one run
class Squad:
    def __init__(self, pipeline, run_id: int):
        self.pipeline = pipeline
        self.reg = pipeline.registry
        self.config = pipeline.config
        self.llm = pipeline.llm
        self.run_id = run_id
        self.info = store.get_run(self.reg, run_id)
        self.t = roles.TEMPLATES[self.info["template"]]
        self.params = {**self.t.params, **self.info["params"]}
        self.abort: Exception | None = None

    # -- guards
    def guard(self) -> None:
        if self.abort is not None:
            raise self.abort
        run = store.get_run(self.reg, self.run_id)
        if run["status"] == "stopping":
            raise Stopped()
        if run["cap_usd"] > 0 and run["spent_usd"] >= run["cap_usd"]:
            raise CapReached()
        if run["calls"] >= MAX_CALLS:
            raise CapReached()

    def ask(self, system: str, user: str, validate, purpose: str, key: str):
        self.guard()
        res = self.llm.chat_json(system, user, validate, purpose=purpose, max_tokens=4000)
        store.add_cost(self.reg, self.run_id, key, res.cost_usd)
        return res

    # -- main loop
    def run(self) -> None:
        reg = self.reg
        if not store.tasks(reg, self.run_id):
            store.log(reg, self.run_id, "system", "note", M.STARTED)
            self.lead_turn()
        parallel = int(self.params.get("parallel") or DEFAULT_PARALLEL)
        running: dict[Future, str] = {}
        with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="squad-member") as pool:
            while True:
                if self.abort is None:
                    try:
                        self.guard()
                    except (Stopped, CapReached) as exc:
                        self.abort = exc
                if self.abort is not None:
                    if running:
                        wait(list(running))
                        self._collect(running, all_done=True)
                    raise self.abort
                if store.pending_messages(reg, self.run_id, "lead"):
                    self.lead_turn()
                board = store.tasks(reg, self.run_id)
                status = {t["key"]: t["status"] for t in board}
                ready = [t for t in board if t["status"] == "queued" and t["key"] not in running.values()
                         and all(status.get(d, "done") in DONEISH for d in t["blocked_by"])]
                for task in ready[: max(0, parallel - len(running))]:
                    store.set_task(reg, self.run_id, task["key"], status="running")
                    running[pool.submit(self.member_task, task)] = task["key"]
                if running:
                    wait(list(running), timeout=1.5, return_when=FIRST_COMPLETED)
                    self._collect(running)
                    continue
                queued = [t for t in board if t["status"] == "queued"]
                if queued:  # blocked by something that can never finish (should not happen): release them
                    for t in queued:
                        store.set_task(reg, self.run_id, t["key"], blocked_by_json="[]")
                    continue
                run = store.get_run(reg, self.run_id)
                if run["lead_turns"] >= MAX_LEAD_TURNS:
                    break
                added, finish = self.lead_turn(wave_end=True)
                if finish or not added:
                    break
        self.finalize()

    def _collect(self, running: dict[Future, str], all_done: bool = False) -> None:
        for fut in [f for f in running if f.done()]:
            key = running.pop(fut)
            exc = fut.exception()
            if exc is None:
                continue
            if isinstance(exc, (Stopped, CapReached, BudgetExceeded, LLMNotConfigured)):
                store.set_task(self.reg, self.run_id, key, status="queued")  # resumes from scratch later
                if self.abort is None:
                    self.abort = exc
            else:
                store.set_task(self.reg, self.run_id, key, status="error", result={"summary": f"失败：{exc}"})
                store.log(self.reg, self.run_id, key, "error", f"{type(exc).__name__}: {exc}"[:2000], key)

    # -- the lead
    def _board_text(self) -> str:
        lines = []
        for t in store.tasks(self.reg, self.run_id):
            line = (f"- {t['key']} [{t['status']}] {t['member']}{' (final)' if t['final'] else ''}: {t['title']}"
                    + (f" (after {', '.join(t['blocked_by'])})" if t["blocked_by"] else ""))
            if t["result"] and t["result"].get("summary"):
                line += "\n    RESULT: " + t["result"]["summary"][:1500].replace("\n", "\n    ")
            lines.append(line)
        return "\n".join(lines) or "(empty: plan the work)"

    def _brief(self) -> str:
        p = self.params
        extra = {
            "max_papers": p.get("max_papers", 8),
            "external": "Also search outside literature (external_search)." if p.get("external", True)
            else "Do not search outside the library.",
            "questions": "; ".join(f"Q{q['id']}: {q['title']}" for q in self.reg.query(
                f"SELECT id, title FROM evidence_questions WHERE id IN ({','.join('?' * len(p.get('question_ids') or []))})",
                tuple(p.get("question_ids") or []))) if p.get("question_ids") else "",
            "limit": p.get("limit", 30),
            "paper": self._paper_label(p.get("paper_id")),
            "scope": (f"Focus on the library papers of the field '{p['field']}' (list_papers with field=\"{p['field']}\")."
                      if p.get("field") else ""),
            "available": p.get("available", "?"),
        }
        return self.t.lead_brief.format(**extra)

    def _paper_label(self, pid) -> str:
        row = self.reg.get_paper(int(pid)) if pid else None
        return f"[P{row['id']}] {row['title']} ({row['first_author'] or '?'} {row['year'] or ''})" if row else ""

    def lead_turn(self, wave_end: bool = False) -> tuple[int, bool]:
        reg = self.reg
        board = store.tasks(reg, self.run_id)
        have = {t["key"] for t in board}
        has_final = any(t["final"] and t["status"] != "cancelled" for t in board)
        members = set(self.t.members)

        def validate(d: dict) -> list[str]:
            new, seen = [], set(have)
            final_count = int(has_final)
            for x in d.get("add_tasks") or []:
                if not isinstance(x, dict):
                    return ["each item of add_tasks must be an object"]
                key = str(x.get("key", "")).strip()[:20]
                if not key or key in seen:
                    return [f"task key '{key}' is empty or already used"]
                if x.get("member") not in members:
                    return [f"member must be one of {sorted(members)}"]
                deps = [str(k) for k in (x.get("blocked_by") or []) if str(k) in seen]
                final = bool(x.get("final"))
                if final:
                    final_count += 1
                    if x["member"] != self.t.final_member:
                        return [f"the final task must go to '{self.t.final_member}'"]
                seen.add(key)
                new.append({"key": key, "member": x["member"], "title": str(x.get("title", "")).strip()[:200] or key,
                            "instructions": str(x.get("instructions", "")).strip()[:3000], "blocked_by": deps,
                            "final": final})
            if final_count > 1:
                return ["only one task may be final"]
            if len(have) + len(new) > MAX_TASKS:
                return [f"at most {MAX_TASKS} tasks in total"]
            d["add_tasks"] = new
            d["cancel_tasks"] = [str(k) for k in (d.get("cancel_tasks") or []) if str(k) in have]
            d["finish"] = bool(d.get("finish"))
            d["note"] = str(d.get("note", "")).strip()[:1000]
            return []

        msgs = store.take_messages(reg, self.run_id, "lead")
        run = store.get_run(reg, self.run_id)
        cap = f"${run['cap_usd']:.2f}" if run["cap_usd"] > 0 else "none"
        user = "\n".join([
            f"GOAL FROM THE RESEARCHER: {run['goal'] or '(see assignment)'}",
            f"MONEY SPENT SO FAR: ${run['spent_usd']:.3f} of cap {cap}",
            *([f"", f"THE RESEARCHER ASKED THIS WHILE LOOKING AT THE PAGE \"{self.params.get('page_title', '')}\":",
               str(self.params["page_context"])[:4000]] if self.params.get("page_context") else []),
            "", "LIBRARY:", tools.library_overview(tools.Ctx(self.pipeline, self.run_id), {})[:6000],
            "", "TASK BOARD:", self._board_text(),
            *(["", "FOLLOW-UP MESSAGES FROM THE RESEARCHER (newest last):"] + [f"- {m}" for m in msgs] if msgs else []),
            "", ("All current tasks have finished. Add follow-up tasks if they are clearly worth it, otherwise "
                 "set finish to true." if wave_end else "Plan or adjust the board."),
        ])
        system = LEAD_SYSTEM.format(final=self.t.final_member, max_tasks=MAX_TASKS, lang=_lang(self.config),
                                    roster=roles.roster(self.t), brief=self._brief())
        res = self.ask(system, user, validate, "squad_lead", "")
        d = res.data
        for x in d["add_tasks"]:
            store.add_task(reg, self.run_id, x["key"], x["member"], x["title"], x["instructions"], x["blocked_by"], x["final"])
        for k in d["cancel_tasks"]:
            reg.execute("UPDATE squad_tasks SET status='cancelled', updated_at=? WHERE run_id=? AND key=? "
                        "AND status='queued'", (now_iso(), self.run_id, k))
        reg.execute("UPDATE squad_runs SET lead_turns=lead_turns+1 WHERE id=?", (self.run_id,))
        summary = d["note"] or (M.PLANNED if d["add_tasks"] else "")
        store.log(reg, self.run_id, "lead", "plan", summary, "", {
            "added": [{k: x[k] for k in ("key", "member", "title")} for x in d["add_tasks"]],
            "cancelled": d["cancel_tasks"], "finish": d["finish"]})
        return len(d["add_tasks"]), d["finish"]

    # -- members
    def member_task(self, task: dict) -> None:
        reg = self.reg
        member = self.t.members.get(task["member"])
        if member is None:
            store.set_task(reg, self.run_id, task["key"], status="error", result={"summary": "unknown member"})
            return
        ctx = tools.Ctx(self.pipeline, self.run_id, task["key"], member.key)
        system = MEMBER_SYSTEM.format(member=member.key, brief=member.brief, catalog=tools.catalog(member.tools),
                                      lang=_lang(self.config))
        board = {t["key"]: t for t in store.tasks(reg, self.run_id)}
        inputs = []
        for dep in task["blocked_by"]:
            t = board.get(dep)
            if t and t["result"] and t["result"].get("summary"):
                inputs.append(f"### {dep} ({t['member']}): {t['title']}\n{t['result']['summary'][:6000]}")
        history: list[tuple[str, str]] = []
        notes: list[str] = []

        def validate(d: dict) -> list[str]:
            if d.get("done"):
                res = d.get("result")
                if not isinstance(res, dict) or not isinstance(res.get("summary"), str) or not res["summary"].strip():
                    return ["when done is true, result.summary must be a non-empty string"]
                return []
            acts = d.get("actions")
            if not isinstance(acts, list) or not acts:
                return ["give 1-4 actions, or set done to true with result.summary"]
            ok = [a for a in acts if isinstance(a, dict) and isinstance(a.get("tool"), str)][:4]
            if not ok:
                return ["each action needs a 'tool' name and an 'args' object"]
            d["actions"] = ok
            return []

        store.log(reg, self.run_id, member.key, "start", task["title"], task["key"])
        for step in range(member.max_steps):
            notes += store.take_messages(reg, self.run_id, task["key"])
            last = step == member.max_steps - 1
            user = self._member_prompt(task, inputs, notes, history, step, member.max_steps, last)
            res = self.ask(system, user, validate, f"squad_{member.key}", task["key"])
            store.set_task(reg, self.run_id, task["key"], steps=step + 1)
            d = res.data
            if d.get("done"):
                summary = d["result"]["summary"].strip()
                store.set_task(reg, self.run_id, task["key"], status="done", result={"summary": summary[:20000]})
                store.log(reg, self.run_id, member.key, "result", summary[:4000], task["key"])
                return
            if last:
                break
            for a in d["actions"]:
                obs = tools.call(ctx, a["tool"], a.get("args") or {}, member.tools)
                brief = json.dumps(a.get("args") or {}, ensure_ascii=False)[:300]
                store.log(reg, self.run_id, member.key, "action", f"{a['tool']} {brief}", task["key"],
                          {"observation": obs[:1500]})
                history.append((f"{a['tool']}({brief})", obs))
        summary = "（步数用完，未写完报告。）\n\n" + "\n".join(f"- {h[0]}" for h in history[-8:])
        store.set_task(reg, self.run_id, task["key"], status="done", result={"summary": summary})
        store.log(reg, self.run_id, member.key, "result", summary, task["key"])

    def _member_prompt(self, task, inputs, notes, history, step, max_steps, last) -> str:
        parts = [f"TEAM GOAL: {self.info['goal'] or self.t.label}", "",
                 f"YOUR TASK ({task['key']}): {task['title']}", task["instructions"] or ""]
        if inputs:
            parts += ["", "RESULTS OF EARLIER TASKS YOU DEPEND ON:"] + inputs
        if notes:
            parts += ["", "MESSAGES FROM THE RESEARCHER (follow them):"] + [f"- {n}" for n in notes]
        if history:
            parts += ["", "YOUR PREVIOUS ACTIONS AND THEIR RESULTS:"]
            budget, block = HISTORY_CHARS, []
            for call, obs in reversed(history):
                text = obs if budget > 4000 else obs[:600] + ("…" if len(obs) > 600 else "")
                budget -= len(text)
                block.append(f">>> {call}\n{text}")
            parts += list(reversed(block))
        parts += ["", f"Step {step + 1} of {max_steps}."
                  + (" This is your LAST step: reply with done=true and your report now." if last else "")]
        return "\n".join(parts)

    # -- the result
    def finalize(self) -> None:
        reg = self.reg
        board = store.tasks(reg, self.run_id)
        final = [t for t in board if t["final"] and t["status"] == "done" and t["result"]]
        if final:
            raw = final[-1]["result"]["summary"]
        else:
            raw = "\n\n".join(f"## {self.t.members[t['member']].label if t['member'] in self.t.members else t['member']}"
                              f" · {t['title']}\n\n{t['result']['summary']}"
                              for t in board if t["status"] == "done" and t["result"])
        text, dropped = cite.check(reg, self.run_id, raw)
        body = cite.readable(reg, self.run_id, text)
        run = store.get_run(reg, self.run_id)
        n_prop = len(store.proposals(reg, self.run_id, "pending"))
        head = (f"# {run['title']}\n\n> PaperMap 小队 · {self.t.label} · {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                f" · 模型生成，结论需核对" + (f" · 删去 {dropped} 句没有出处的内容" if dropped else "") + "\n\n")
        if run["goal"]:
            head += f"**目标**：{run['goal']}\n\n"
        folder = self.config.knowledge_dir / "squads"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{datetime.now().strftime('%Y-%m-%d')}-squad-{self.run_id}-{self.t.key}.md"
        path.write_text(head + body + "\n", encoding="utf-8")
        rel = path.relative_to(self.config.knowledge_dir).as_posix()
        store.set_run(reg, self.run_id, status="done", result_md=body, result_path=rel, dropped=dropped,
                      finished_at=now_iso(), message="")
        store.log(reg, self.run_id, "system", "done", M.DONE, data={"path": rel, "pending": n_prop})
        store_path = None
        if self.params.get("chat_key"):
            from .. import chat

            store_path = chat.page_store(self.config.data_dir, self.params["chat_key"])
        elif self.params.get("chat_paper"):
            from .. import chat

            try:
                store_path = chat.chat_path(self.pipeline._paper_files(int(self.params["chat_paper"]))[1])  # noqa: SLF001
            except ValueError:
                store_path = None
        if store_path is not None:
            try:
                chat.set_squad_result(store_path, self.run_id, text + (f"\n\n（{n_prop} 条提议待确认）" if n_prop else ""))
            except OSError:
                pass
        try:
            self.pipeline.kb_commit(f"Squad run #{self.run_id}")
        except Exception:  # noqa: BLE001 - optional
            pass
        try:
            from .. import notify

            notify.send("PaperMap", f"小队「{run['title'][:40]}」已完成" + (f"，{n_prop} 条提议待确认" if n_prop else ""))
        except Exception:  # noqa: BLE001
            pass


def follow_up(pipeline, run_id: int, to: str, text: str) -> None:
    """A message from the researcher. A finished or stopped run starts again so the lead can act on it."""
    reg = pipeline.registry
    run = store.get_run(reg, run_id)
    if run is None:
        raise ValueError(M.NO_RUN)
    text = (text or "").strip()
    if not text or len(text) > 2000:
        raise ValueError(M.FOLLOW_UP_LEN)
    keys = {t["key"]: t for t in store.tasks(reg, run_id)}
    if to != "lead" and (to not in keys or keys[to]["status"] in DONEISH):
        to = "lead"  # a finished member cannot hear it any more; the lead can start a follow-up task
    store.log(reg, run_id, "user", "user", text, to, delivered=False)
    if run["status"] in ("done", "stopped", "error"):
        squad_runner(pipeline).start(run_id)


def stop(pipeline, run_id: int) -> None:
    reg = pipeline.registry
    reg.execute("UPDATE squad_runs SET status=CASE status WHEN 'queued' THEN 'stopped' ELSE 'stopping' END, "
                "updated_at=? WHERE id=? AND status IN ('queued','running')", (now_iso(), run_id))


def squad_runner(pipeline) -> SquadRunner:
    runner = getattr(pipeline, "_squad_runner", None)
    if runner is None:
        runner = SquadRunner(pipeline)
        pipeline._squad_runner = runner  # noqa: SLF001
    return runner
