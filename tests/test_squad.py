"""Agent squads: lead plans a task board, members work in parallel with library tools, proposals wait
for the researcher, uncited sentences are dropped, the run stops at its cap and resumes."""
import json
import re
import threading
import time
from types import SimpleNamespace

import pytest

from papermap import evidence
from papermap.llm import LLMClient
from papermap.squad import cite, proposals, runtime, store

from .conftest import OWN_DOI
from .test_reader import client
from .test_web import _token

SENTENCE = "We profiled tumours with single-cell RNA sequencing and Visium spatial transcriptomics."


class Script:
    """A fake chat client. The lead's replies come from ``lead`` (a list); each member's from a
    function (step, user, script) -> dict. Every call is recorded."""

    def __init__(self, lead, members):
        self.lead = list(lead)
        self.members = members
        self.calls: list[tuple[str, str]] = []
        self.steps: dict[str, int] = {}
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.delay = 0.0

    def create(self, **kw):
        system, user = kw["messages"][0]["content"], kw["messages"][-1]["content"]
        if system.startswith("You lead"):
            role = "lead"
            out = self.lead.pop(0) if self.lead else {"note": "", "add_tasks": [], "finish": True}
        else:
            role = re.search(r"You are the '(\w+)'", system).group(1)
            task = re.search(r"YOUR TASK \((\w+)\)", user).group(1)
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                step = self.steps.get(task, 0)
                self.steps[task] = step + 1
            time.sleep(self.delay)
            out = self.members[role](task, step, user, self)
            with self.lock:
                self.active -= 1
        with self.lock:
            self.calls.append((role, user))
        usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=200, prompt_cache_hit_tokens=0)
        msg = SimpleNamespace(content=json.dumps(out, ensure_ascii=False))
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage, model=kw["model"])


def use(pipe, script):
    pipe.llm = LLMClient(pipe.config, pipe.registry, client=SimpleNamespace(chat=SimpleNamespace(completions=script)))


@pytest.fixture
def lib(config, registry, pdfs, monkeypatch):
    from papermap import chat

    monkeypatch.setattr(chat, "_subscription_ready", lambda config, pid: False)  # never this Mac's real sign-ins
    web, pipe, _ = client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    qid = evidence.create_question(registry, "成纤维细胞小生境是否促进肿瘤进展？")
    pipe.reindex(pid)
    return web, pipe, pid, qid


def check_script(pid, qid):
    plan = {"note": "先检索，再精读，最后写判断。", "add_tasks": [
        {"key": "s1", "member": "searcher", "title": "找相关论文", "instructions": f"Q{qid}", "blocked_by": []},
        {"key": "r1", "member": "reader", "title": "精读", "instructions": f"P{pid} for Q{qid}", "blocked_by": ["s1"]},
        {"key": "r2", "member": "reader", "title": "精读（同一篇，测并行）", "instructions": f"P{pid}", "blocked_by": ["s1"]},
        {"key": "w1", "member": "writer", "title": "写判断", "instructions": "", "blocked_by": ["r1", "r2", "zz"],
         "final": True}]}

    def searcher(task, step, user, s):
        if step == 0:
            return {"actions": [{"tool": "search_library", "args": {"query": "fibroblast"}},
                                {"tool": "question_evidence", "args": {"question_id": qid}}], "done": False}
        assert f"[P{pid}]" in user  # the tool's result came back in the next step
        return {"done": True, "result": {"summary": f"相关：[P{pid}] p.1"}}

    def reader(task, step, user, s):
        assert "相关：" in user  # the searcher's result is passed on
        if step == 0:
            return {"actions": [{"tool": "propose_evidence", "args": {
                "question_id": qid, "paper_id": pid, "quote": "Fibroblasts were absent from every sample we studied.",
                "stance": "opposes", "dimension": "findings", "note": "编造"}}]}
        if step == 1:
            assert "not found verbatim" in user
            return {"actions": [{"tool": "propose_evidence", "args": {
                "question_id": qid, "paper_id": pid, "quote": SENTENCE, "stance": "supports",
                "dimension": "methods", "note": "用了空间转录组", "page": 7}},
                {"tool": "delete_everything", "args": {}}]}
        assert "not available to you" in user
        return {"done": True, "result": {"summary": f"已提交一条摘录 [P{pid}]"}}

    def writer(task, step, user, s):
        if step == 0:
            return {"actions": [{"tool": "propose_judgment", "args": {
                "question_id": qid, "text": f"空间转录组定位到了成纤维细胞小生境 [P{pid}]。这一句没有任何出处所以应该被删除掉才对。"}}]}
        return {"done": True, "result": {"summary":
                f"## 结论\n\n空间转录组定位到了成纤维细胞小生境 [P{pid}]。这一句完全没有出处，应当被自动删掉才对吧。"
                f"编造的引用也会被去掉 [P99999]。\n\n- 要点一很长很长但是没有引用的条目内容"}}

    return Script([plan, {"note": "完成", "add_tasks": [], "finish": True}],
                  {"searcher": searcher, "reader": reader, "writer": writer})


def test_check_squad_end_to_end(lib, registry, config):
    web, pipe, pid, qid = lib
    script = check_script(pid, qid)
    script.delay = 0.2
    use(pipe, script)
    rid = store.create_run(registry, "check", "核对", "", {"question_ids": [qid], "parallel": 4}, 0, 0)
    runtime.SquadRunner(pipe).execute(rid)
    run = store.get_run(registry, rid)
    assert run["status"] == "done", run["message"]
    roles_in_order = [r for r, _ in script.calls]
    assert roles_in_order[0] == "lead"
    # readers start only after the searcher finished, and the two readers ran at the same time
    assert roles_in_order.index("reader") > max(i for i, r in enumerate(roles_in_order) if r == "searcher")
    assert script.max_active == 2
    assert roles_in_order.index("writer") > max(i for i, r in enumerate(roles_in_order) if r == "reader")
    # nothing was written to the library; proposals wait for the researcher
    assert not evidence.get_entries(registry, qid)
    props = store.proposals(registry, rid)
    kinds = sorted(p["kind"] for p in props)
    assert kinds == ["evidence", "judgment"]  # the fabricated quote was refused, both readers' real one merged
    ev = next(p for p in props if p["kind"] == "evidence")
    assert ev["payload"]["page"] == 0 and ev["payload"]["rects"]  # wrong page hint corrected, anchored
    judg = next(p for p in props if p["kind"] == "judgment")
    assert "没有任何出处" not in judg["payload"]["text"] and judg["payload"]["dropped"] == 1
    # result: uncited and invented-citation sentences removed, file written
    assert "完全没有出处" not in run["result_md"] and "P99999" not in run["result_md"] and "要点一" not in run["result_md"]
    assert f"/paper/{pid}" in run["result_md"] and run["dropped"] >= 2
    assert (config.knowledge_dir / run["result_path"]).exists()
    assert run["spent_usd"] > 0 and run["calls"] == len(script.calls)
    purposes = {r["purpose"] for r in registry.query("SELECT purpose FROM llm_calls")}
    assert {"squad_lead", "squad_searcher", "squad_reader", "squad_writer"} <= purposes

    # the researcher accepts (editing the note) and rejects
    res = proposals.decide(pipe, ev["id"], "accept", {"note": "我改过的笔记", "stance": "supports"})
    entry = evidence.get_entries(registry, qid)[0]
    assert res["status"] == "accepted" and entry["note"] == "我改过的笔记" and entry["quote"] == SENTENCE
    assert json.loads(entry["rects_json"]) == ev["payload"]["rects"]
    proposals.decide(pipe, judg["id"], "accept", {})
    assert f"/paper/{pid}" in registry.one("SELECT judgment FROM evidence_questions WHERE id=?", (qid,))["judgment"]
    with pytest.raises(ValueError):
        proposals.decide(pipe, ev["id"], "reject")


def test_cap_stops_the_run_and_follow_up_resumes_it(lib, registry):
    web, pipe, pid, qid = lib
    script = check_script(pid, qid)
    use(pipe, script)
    rid = store.create_run(registry, "check", "核对", "", {"question_ids": [qid], "parallel": 1}, 0.0000001, 0)
    runtime.SquadRunner(pipe).execute(rid)
    run = store.get_run(registry, rid)
    assert run["status"] == "stopped" and run["calls"] == 1  # the lead planned, then the cap stopped it
    assert [t["status"] for t in store.tasks(registry, rid)] == ["queued"] * 4
    # the researcher raises the cap and says something: the lead hears it, the board continues
    store.set_run(registry, rid, cap_usd=0)
    store.log(registry, rid, "user", "user", "只看人类样本", "lead", delivered=False)
    script.lead.insert(0, {"note": "收到，只看人类样本", "add_tasks": []})
    store.set_run(registry, rid, status="running")
    runtime.SquadRunner(pipe).execute(rid)
    assert store.get_run(registry, rid)["status"] == "done"
    lead_prompts = [u for r, u in script.calls if r == "lead"]
    assert "只看人类样本" in lead_prompts[1]
    assert not store.pending_messages(registry, rid, "lead")


def test_interrupted_run_resumes_without_redoing_finished_tasks(lib, registry):
    web, pipe, pid, qid = lib
    script = check_script(pid, qid)
    use(pipe, script)
    rid = store.create_run(registry, "check", "核对", "", {"question_ids": [qid]}, 0, 0)
    sq = runtime.Squad(pipe, rid)
    sq.lead_turn()  # the plan was made before the app closed ...
    store.set_task(registry, rid, "s1", status="done", result={"summary": f"相关：[P{pid}] p.1"})
    store.set_task(registry, rid, "r1", status="running")  # ... while a reader was working
    store.set_run(registry, rid, status="running")
    runner = runtime.SquadRunner(pipe)
    runner.recover()
    for _ in range(100):
        if store.get_run(registry, rid)["status"] == "done":
            break
        time.sleep(0.1)
    assert store.get_run(registry, rid)["status"] == "done"
    roles_called = [r for r, _ in script.calls]
    assert "searcher" not in roles_called and roles_called.count("lead") == 2  # plan + wrap-up only
    assert script.steps.get("r1") == 3  # the interrupted reader started over


def test_stop_and_tools_refuse_bad_ids(lib, registry):
    web, pipe, pid, qid = lib
    from papermap.squad import tools

    rid = store.create_run(registry, "review", "综述", "成纤维细胞", {}, 0, 0)
    ctx = tools.Ctx(pipe, rid, "x", "reader")
    assert tools.call(ctx, "read_card", {"paper_id": 99999}, ["read_card"]).startswith("ERROR: no library paper")
    assert "[p.1]" in tools.call(ctx, "read_pages", {"paper_id": pid, "start": 1, "end": 1}, ["read_pages"])
    assert "Q" in tools.call(ctx, "library_overview", {}, ["library_overview"])
    assert tools.call(ctx, "propose_gap", {"question_id": 424242, "text": "x"}, ["propose_gap"]).startswith("ERROR")
    runtime.stop(pipe, rid)
    assert store.get_run(registry, rid)["status"] == "stopped"


def test_cite_check_keeps_structure():
    class R:
        def one(self, sql, params):
            return {"x": 1} if params[0] in (1, 2) else None

        def get_paper(self, n):
            return {"first_author": "Li", "year": 2020} if n in (1, 2) else None

    text = ("# 标题\n\n| a | b |\n|---|---|\n\n短句：\n\n这是一句有引用的话 [P1]。这是一句没有任何引用的长句子，需要被删除才对。"
            "\n\nAnother sentence with support [P2]. An unsupported English sentence that should go away.")
    out, dropped = cite.check(R(), 1, text)
    assert "# 标题" in out and "| a | b |" in out and "短句：" in out
    assert "有引用的话 [P1]" in out and "没有任何引用" not in out and "unsupported" not in out
    assert dropped == 2


def test_web_start_board_and_remote_refusal(lib, registry):
    web, pipe, pid, qid = lib
    script = check_script(pid, qid)
    use(pipe, script)
    tok = _token(web)
    assert "智能体小队" in web.get("/squads").text
    est = web.post("/api/squads/estimate", json={"csrf": tok, "template": "check", "question_ids": [qid]}).json()
    assert est["ok"] and est["calls"] > 10
    bad = web.post("/api/squads/start", json={"csrf": tok, "template": "review", "goal": ""}).json()
    assert not bad["ok"]
    r = web.post("/api/squads/start", json={"csrf": tok, "template": "check", "question_ids": [qid], "cap_usd": 0}).json()
    assert r["ok"]
    rid = r["id"]
    for _ in range(100):
        if store.get_run(registry, rid)["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    state = web.get(f"/api/squads/{rid}/state").json()
    assert state["run"]["status"] == "done" and state["tasks"] and state["events"] and state["result_html"]
    page = web.get(f"/squad/{rid}").text
    assert "任务板" in page and "/static/squad.js" in page
    assert "智能体小队的提议" in web.get("/review").text
    pending = [p for p in state["proposals"] if p["status"] == "pending"]
    from papermap import remote_web

    remote = {remote_web.HEADER: remote_web.SECRET}
    assert web.post(f"/api/squads/proposals/{pending[0]['id']}", json={"csrf": tok, "action": "accept"},
                    headers=remote).status_code == 403
    assert web.post(f"/api/squads/{rid}/message", json={"csrf": tok, "text": "hi"}, headers=remote).status_code == 403
    assert web.get(f"/squad/{rid}", headers=remote).status_code == 200  # the phone can watch
    ok = web.post(f"/api/squads/proposals/{pending[0]['id']}", json={"csrf": tok, "action": "reject"}).json()
    assert ok["ok"] and ok["status"] == "rejected"


def test_triage_squad_proposes_take_in_and_skip(lib, registry):
    web, pipe, pid, qid = lib
    from papermap import tracking

    tracking.ensure_schema(registry)
    now = "2026-11-25T00:00:00+00:00"
    ids = []
    for i, title in enumerate(["Fibroblast niches drive liver tumour growth in a human cohort",
                               "A survey of bird migration routes over the Alps"]):
        cur = registry.execute("INSERT INTO track_candidates(doi,title,title_key,abstract,year,created_at,updated_at) "
                               "VALUES (?,?,?,?,?,?,?)", (f"10.9/x{i}", title, tracking.title_key(title), "abs", 2026, now, now))
        ids.append(cur.lastrowid)
    plan = {"note": "分两批", "add_tasks": [
        {"key": "c1", "member": "screener", "title": "前 10 篇", "instructions": "offset 0"},
        {"key": "w", "member": "writer", "title": "分诊说明", "blocked_by": ["c1"], "final": True}]}

    def screener(task, step, user, s):
        if step == 0:
            return {"actions": [{"tool": "tracking_candidates", "args": {"limit": 10}}]}
        if step == 1:
            assert f"[C{ids[0]}]" in user and f"[C{ids[1]}]" in user
            return {"actions": [
                {"tool": "propose_take_in", "args": {"candidate_id": ids[0], "reason": f"回答 Q{qid}", "read_first": "Fig. 2"}},
                {"tool": "propose_skip", "args": {"candidate_id": ids[1], "reason": "与研究无关"}}]}
        return {"done": True, "result": {"summary": f"收 [C{ids[0]}]，跳过 [C{ids[1]}]"}}

    def writer(task, step, user, s):
        return {"done": True, "result": {"summary": f"先读这篇关于成纤维细胞小生境的新研究 [C{ids[0]}]。"}}

    use(pipe, Script([plan], {"screener": screener, "writer": writer}))
    rid = store.create_run(registry, "triage", "分诊", "", {"limit": 10, "available": 2}, 0, 0)
    runtime.SquadRunner(pipe).execute(rid)
    run = store.get_run(registry, rid)
    assert run["status"] == "done" and "/tracking" in run["result_md"]
    props = {p["kind"]: p for p in store.proposals(registry, rid)}
    assert set(props) == {"take_in", "skip"} and props["take_in"]["payload"]["read_first"] == "Fig. 2"
    proposals.decide(pipe, props["skip"]["id"], "accept")
    assert registry.one("SELECT status FROM track_candidates WHERE id=?", (ids[1],))["status"] == "dismissed"
    assert registry.one("SELECT status FROM track_candidates WHERE id=?", (ids[0],))["status"] == "new"


def test_review_squad_lead_follow_up_tasks_and_outside_search_failure(lib, registry):
    web, pipe, pid, qid = lib
    plan = {"note": "检索", "add_tasks": [{"key": "s", "member": "searcher", "title": "检索"}]}
    more = {"note": "补一篇精读和综述", "add_tasks": [
        {"key": "r", "member": "reader", "title": "精读", "instructions": f"P{pid}", "blocked_by": ["s"]},
        {"key": "w", "member": "writer", "title": "写综述", "blocked_by": ["r"], "final": True},
        {"key": "w2", "member": "writer", "title": "第二个终稿", "final": True}]}  # invalid: two finals
    more_ok = {"note": "补一篇精读和综述", "add_tasks": more["add_tasks"][:2]}

    def searcher(task, step, user, s):
        if step == 0:
            return {"actions": [{"tool": "list_papers", "args": {}}, {"tool": "external_search", "args": {"query": "fibroblast"}}]}
        assert f"[P{pid}]" in user and "OpenAlex search failed" in user
        return {"done": True, "result": {"summary": f"[P{pid}]"}}

    def reader(task, step, user, s):
        return {"done": True, "result": {"summary": f"设计：Visium [P{pid}]"}}

    def writer(task, step, user, s):
        assert "设计：Visium" in user
        return {"done": True, "result": {"summary": f"## 背景\n\n空间转录组研究定位了成纤维细胞的小生境 [P{pid}]。"}}

    pipe.metadata._get_json = lambda *a, **k: None  # OpenAlex unreachable
    pipe.metadata._openalex_params = lambda: {}
    script = Script([plan, more, more_ok, {"note": "好了", "finish": True}],
                    {"searcher": searcher, "reader": reader, "writer": writer})
    use(pipe, script)
    rid = store.create_run(registry, "review", "综述", "成纤维细胞", {"max_papers": 2}, 0, 0)
    runtime.SquadRunner(pipe).execute(rid)
    run = store.get_run(registry, rid)
    assert run["status"] == "done", run["message"]
    assert [t["key"] for t in store.tasks(registry, rid)] == ["s", "r", "w"]
    assert "小生境" in run["result_md"]


def test_squad_from_the_page_drawer(lib, registry, config):
    web, pipe, pid, qid = lib
    from papermap import chat

    script = check_script(pid, qid)
    use(pipe, script)
    tok = _token(web)
    body = {"csrf": tok, "key": "/evidence", "search": f"?question={qid}", "title": "证据笔记",
            "page_text": "成纤维细胞小生境 页面文字", "text": "这些证据够不够支持这个问题？"}
    prev = web.post("/api/page-chat/squad", json=body).json()
    assert prev["ok"] and prev["preview"] and prev["template"] == "check" and prev["questions"]
    assert not store.runs(registry)  # nothing starts before the researcher confirms
    started = web.post("/api/page-chat/squad", json={**body, "confirm": True, "cap_usd": 0}).json()
    rid = started["id"]
    msgs = chat.load(chat.page_store(config.data_dir, "/evidence"))["messages"]
    assert [m["role"] for m in msgs] == ["user", "squad"] and msgs[1]["run_id"] == rid
    for _ in range(100):
        if store.get_run(registry, rid)["status"] == "done":
            break
        time.sleep(0.1)
    run = store.get_run(registry, rid)
    assert run["status"] == "done" and run["params"]["page_context"].startswith("成纤维细胞")
    lead_prompt = next(u for r, u in script.calls if r == "lead")
    assert "页面文字" in lead_prompt
    brief = web.get(f"/api/squads/{rid}/brief").json()
    assert brief["status"] == "done" and brief["result_html"] and brief["pending"] == 2
    # the report joined the conversation, so a normal follow-up question can discuss it
    msgs = chat.load(chat.page_store(config.data_dir, "/evidence"))["messages"]
    assert "小生境" in msgs[1]["content"]
    got = web.get("/api/page-chat?key=/evidence").json()
    assert got["messages"][1]["role"] == "squad" and got["messages"][1]["run_id"] == rid
    # while a squad runs, the drawer talks to its lead instead of starting another one
    store.set_run(registry, rid, status="running")
    real_start = runtime.SquadRunner.start
    runtime.SquadRunner.start = lambda self, run_id: None
    try:
        fol = web.post("/api/page-chat/squad", json={**body, "text": "多看人类样本"}).json()
    finally:
        runtime.SquadRunner.start = real_start
    assert fol["followed"] == rid and store.pending_messages(registry, rid, "lead")
    assert len(store.runs(registry)) == 1


def test_page_plan_picks_the_squad_for_the_page(lib, registry):
    web, pipe, pid, qid = lib
    tok = _token(web)
    base = {"csrf": tok, "title": "t", "page_text": "", "text": "比较这个学科里的研究"}
    field = registry.query("SELECT slug FROM fields WHERE status='active' LIMIT 1")[0]["slug"]
    r = web.post("/api/page-chat/squad", json={**base, "key": f"/field/{field}"}).json()
    assert r["template"] == "review" and r["field"] == field
    r = web.post("/api/page-chat/squad", json={**base, "key": "/tracking"}).json()
    assert not r["ok"]  # no candidates yet: a clear message instead of an empty squad
    r = web.post("/api/page-chat/squad", json={**base, "key": "/map"}).json()
    assert r["template"] == "review"
    from papermap import remote_web

    assert web.post("/api/page-chat/squad", json={**base, "key": "/map"},
                    headers={remote_web.HEADER: remote_web.SECRET}).status_code == 403


def test_deep_reading_squad_from_the_paper_chat(lib, registry, config):
    web, pipe, pid, qid = lib
    from papermap import chat

    plan = {"note": "分头精读", "add_tasks": [
        {"key": "r1", "member": "reader", "title": "设计与方法"},
        {"key": "r2", "member": "reader", "title": "结果与图表"},
        {"key": "c1", "member": "critic", "title": "局限", "blocked_by": ["r1", "r2"]},
        {"key": "k1", "member": "connector", "title": "对照文献库", "blocked_by": ["r1", "r2"]},
        {"key": "w1", "member": "writer", "title": "精读报告", "blocked_by": ["c1", "k1"], "final": True}]}

    def reader(task, step, user, s):
        assert f"[P{pid}]" in s.lead_system  # the lead was told which paper
        if step == 0:
            return {"actions": [{"tool": "read_pages", "args": {"paper_id": pid, "start": 1, "end": 1}}]}
        return {"done": True, "result": {"summary": f"用了 Visium p.1 [P{pid}]"}}

    def critic(task, step, user, s):
        return {"done": True, "result": {"summary": f"样本量小 [P{pid}]"}}

    def connector(task, step, user, s):
        if step == 0:
            return {"actions": [{"tool": "propose_evidence", "args": {
                "question_id": qid, "paper_id": pid, "quote": SENTENCE, "stance": "supports", "dimension": "methods",
                "note": "方法相关"}}]}
        return {"done": True, "result": {"summary": f"和问题 Q{qid} 相关 [P{pid}]"}}

    def writer(task, step, user, s):
        return {"done": True, "result": {"summary": f"## 精读\n\n这篇用空间转录组定位了成纤维细胞小生境 [P{pid}]。"}}

    script = Script([plan], {"reader": reader, "critic": critic, "connector": connector, "writer": writer})
    original = script.create

    def create(**kw):
        if kw["messages"][0]["content"].startswith("You lead"):
            script.lead_system = kw["messages"][0]["content"]
        return original(**kw)

    use(pipe, SimpleNamespace(create=create))
    tok = _token(web)
    body = {"csrf": tok, "text": "这篇的结论站得住吗？"}
    prev = web.post(f"/api/paper/{pid}/chat/squad", json=body).json()
    assert prev["ok"] and prev["template"] == "deep" and "关联员" in prev["members"]
    started = web.post(f"/api/paper/{pid}/chat/squad", json={**body, "confirm": True, "cap_usd": 0}).json()
    rid = started["id"]
    for _ in range(100):
        if store.get_run(registry, rid)["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    run = store.get_run(registry, rid)
    assert run["status"] == "done", run["message"]
    assert {p["kind"] for p in store.proposals(registry, rid)} == {"evidence"}
    _, card = pipe._paper_files(pid)
    msgs = chat.load(chat.chat_path(card))["messages"]
    assert [m["role"] for m in msgs] == ["user", "squad"] and "小生境" in msgs[1]["content"]
    got = web.get(f"/api/paper/{pid}/chat").json()
    assert got["messages"][1]["run_id"] == rid
    # the squads page does not offer it as a type of its own (it needs a paper)
    assert "单篇深读" not in web.get("/squads").text.split("运行记录")[0]


def _with_custom_model(config):
    config.raw["llm"]["api_key"] = "test-only"
    config.raw["llm"]["custom"] = {"base_url": "http://127.0.0.1:9/v1", "model": "deepseek-v4.1-flash", "api_key": "x",
                                   "protocol": "openai", "price_input_per_1m": 0.01, "price_output_per_1m": 0.02}
    from papermap.squad import models

    models.forget()
    return "custom|deepseek-v4.1-flash"


def test_roster_settings_and_recommendation(lib, registry, config):
    web, pipe, pid, qid = lib
    from papermap.squad import models

    key = _with_custom_model(config)
    roster = {m["key"]: m for m in models.roster(config)}
    assert roster[key]["vendor"] == "DeepSeek" and roster[key]["billing"] == "token" and roster["main"]["main"]
    rec = models.recommend(config, "balanced")
    assert rec["searcher"][0]["model"] == key and rec["screener"][0]["model"] == key  # simple: cheapest paid
    assert rec["writer"][0]["model"] == "main" and rec["reader"][0]["model"] == "main"
    assert models.recommend(config, "economy")["reader"][0]["model"] == key
    assert models.recommend(config, "quality")["searcher"][0]["model"] == "main"
    with pytest.raises(ValueError):
        models.validate(config, {"roles": {"reader": {"candidates": [{"model": "nope|x"}]}}})
    clean = models.validate(config, {"budget": "economy", "parallel": 9, "roles": {
        "searcher": {"candidates": [{"model": key, "effort": "bogus"}, {"model": key, "effort": ""}], "extra": "只搜英文", "max_steps": "3"}},
        "disabled": {"deep": ["connector", "writer"]}})
    assert clean["parallel"] == 4 and clean["roles"]["searcher"]["candidates"] == [{"model": key, "effort": ""}]
    assert clean["roles"]["searcher"]["max_steps"] == 3 and clean["disabled"] == {"deep": ["connector"]}

    tok = _token(web)
    page = web.get("/squads/settings").text
    assert "模型名册" in page and "deepseek-v4.1-flash" in page and "DeepSeek" in page
    assert web.post("/api/squads/settings", json={"csrf": tok, **clean}).json()["ok"]
    assert config.raw["squad"]["roles"]["searcher"]["extra"] == "只搜英文"
    from papermap import remote_web

    assert web.post("/api/squads/settings", json={"csrf": tok, **clean},
                    headers={remote_web.HEADER: remote_web.SECRET}).status_code == 403
    est = runtime.estimate(config, "check", {"question_ids": [qid], "max_papers": 2})
    by = {r["label"]: r for r in est["by_model"]}
    paid = next(r for r in by.values() if "deepseek-v4.1-flash" in r["label"])
    assert paid["roles"] == ["searcher"] and paid["usd"] > 0 and paid["billing"] == "token"


def test_roles_use_their_own_models_and_fall_back(lib, registry, config, monkeypatch):
    web, pipe, pid, qid = lib
    from papermap import llm as llm_mod
    from papermap.squad import models

    key = _with_custom_model(config)
    config.raw["squad"] = models.validate(config, {"roles": {
        "searcher": {"candidates": [{"model": key}], "extra": "只用英文检索词"},
        "critic": {"candidates": [{"model": key}]}}, "disabled": {"check": ["critic"]}})
    script = check_script(pid, qid)
    use(pipe, script)
    side = Script([], {"searcher": lambda task, step, user, s: (
        {"actions": [{"tool": "search_library", "args": {"query": "fibroblast"}}]} if step == 0
        else {"done": True, "result": {"summary": f"相关：[P{pid}] p.1"}})})
    seen_system = []

    def make_client(eff, timeout):
        assert eff["model"] == "deepseek-v4.1-flash"

        def create(**kw):
            seen_system.append(kw["messages"][0]["content"])
            return side.create(**kw)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(llm_mod, "make_client", make_client)
    rid = store.create_run(registry, "check", "核对", "", {"question_ids": [qid]}, 0, 0)
    runtime.SquadRunner(pipe).execute(rid)
    run = store.get_run(registry, rid)
    assert run["status"] == "done", run["message"]
    assert [r for r, _ in side.calls] == ["searcher", "searcher"]  # the searcher ran on its own model
    assert "searcher" not in [r for r, _ in script.calls]
    assert "只用英文检索词" in seen_system[0]
    assert any(u for r, u in script.calls if r == "lead")
    assert set(run["usage"]) >= {"自定义接口 · deepseek-v4.1-flash"}
    tasks = {t["key"]: t for t in store.tasks(registry, rid)}
    assert tasks["s1"]["model"] == "自定义接口 · deepseek-v4.1-flash"

    # the paid model breaks: the searcher falls back to the main model and says so
    def broken(eff, timeout):
        def create(**kw):
            raise RuntimeError("quota used up")
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(llm_mod, "make_client", broken)
    script2 = check_script(pid, qid)
    use(pipe, script2)
    rid2 = store.create_run(registry, "check", "核对", "", {"question_ids": [qid]}, 0, 0)
    runtime.SquadRunner(pipe).execute(rid2)
    assert store.get_run(registry, rid2)["status"] == "done"
    assert "searcher" in [r for r, _ in script2.calls]
    falls = [e for e in store.events(registry, rid2) if e["kind"] == "fallback"]
    assert falls and "quota used up" in falls[0]["data_json"]


def test_disabled_role_leaves_the_roster_and_scorecards_count(lib, registry, config):
    web, pipe, pid, qid = lib
    from papermap.squad import models, roles

    s = models.validate(config, {"disabled": {"deep": ["critic"]}, "roles": {"writer": {"extra": "800 字以内"}}})
    t = roles.configured("deep", s)
    assert "critic" not in t.members and "800 字以内" in t.members["writer"].brief
    assert "{paper}" in t.lead_brief
    script = check_script(pid, qid)
    use(pipe, script)
    rid = store.create_run(registry, "check", "核对", "", {"question_ids": [qid]}, 0, 0)
    runtime.SquadRunner(pipe).execute(rid)
    cards = {c["role"]: c for c in store.scorecards(registry)}
    assert cards["reader"]["refused"] == 2  # both readers first copied a sentence that is not in the PDF
    assert cards["writer"]["dropped"] >= 1 and cards["reader"]["pending"] >= 1
