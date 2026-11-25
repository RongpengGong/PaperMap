"""Two-week model review: fetch ranking sources, ask the evaluator, adopt per role."""
import json
from types import SimpleNamespace

import httpx

from papermap.squad import evaluate, models
from papermap.squad import store as sq_store

from .test_reader import client as web_client
from .test_web import _token

OPUS = "anthropic|claude-opus-5-5"
FLASH = "custom|deepseek-v4.1-flash"


def _setup(config, registry, pdfs, monkeypatch):
    from papermap import chat

    monkeypatch.setattr(chat, "_subscription_ready", lambda config, pid: False)  # never this Mac's real sign-ins
    web, pipe, _ = web_client(config, registry, pdfs)
    config.raw["llm"]["api_key"] = "test-only"
    config.raw["llm"]["custom"] = {"base_url": "http://127.0.0.1:9/v1", "model": "deepseek-v4.1-flash", "api_key": "x",
                                   "protocol": "openai", "price_input_per_1m": 0.01, "price_output_per_1m": 0.02}
    config.raw["llm"].setdefault("presets", {})["anthropic"] = {"api_key": "x", "model": "claude-opus-5-5"}
    models.forget()
    return web, pipe


def _answer(critic=OPUS):
    roles = {r: {"candidates": [{"model": "main", "effort": ""}], "reason": "主模型足够 [S1]"} for r in models.ROLE_ORDER}
    roles["searcher"] = {"candidates": [{"model": FLASH, "effort": ""}], "reason": "便宜且够用 [S1][score]"}
    roles["screener"] = roles["searcher"]
    roles["critic"] = {"candidates": [{"model": critic, "effort": "high"}, {"model": "main", "effort": ""}],
                       "reason": "评分最高，换一家 [S1]"}
    return {"makers": {"DeepSeek": "flash 便宜 [S1]", "Anthropic": "Opus 59 分 [S1]"},
            "summary": "检索交给 DeepSeek flash，质疑员用 Opus。", "roles": roles, "diversity_note": "",
            "deprecated": [FLASH, "nope"], "missing": ["没有 Neon 的分数"]}


def test_review_fetches_sources_asks_the_evaluator_and_adopts_per_role(config, registry, pdfs, monkeypatch):
    web, pipe = _setup(config, registry, pdfs, monkeypatch)
    config.raw["squad"] = {"eval": {"evaluator": [{"model": OPUS, "effort": "high"}],
                                    "sources": [{"name": "Board", "url": "https://board.test/x"},
                                                {"name": "Blocked", "url": "https://blocked.test/"}],
                                    "notes": "Neon 在 Medium 下 55 分（我的调研）", "auto": True}}

    def handler(request):
        if request.url.host == "board.test":
            body = "<html><body><table>" + "".join(f"<tr><td>Model {i}</td><td>{i}</td></tr>" for i in range(80)) + \
                   "<tr><td>Claude Opus 5.5</td><td>59</td></tr></table><script>ignore()</script></body></html>"
            return httpx.Response(200, text=body)
        return httpx.Response(403, text="Just a moment")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    seen = []
    answers = [_answer(critic="main"), _answer()]  # first: critic from the writer's maker -> asked again

    def make_client(eff, timeout):
        assert eff["model"] == "claude-opus-5-5" and eff.get("reasoning_effort") in (None, "", "high")

        def create(**kw):
            seen.append(kw["messages"])
            usage = SimpleNamespace(prompt_tokens=5000, completion_tokens=800, prompt_cache_hit_tokens=0)
            msg = SimpleNamespace(content=json.dumps(answers.pop(0), ensure_ascii=False))
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage, model="claude-opus-5-5")
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    from papermap import llm as llm_mod

    monkeypatch.setattr(llm_mod, "make_client", make_client)
    rid = evaluate.run_review(pipe, client=http)
    rev = evaluate.latest(registry)
    assert rev["id"] == rid and rev["status"] == "ready", rev["message"]
    assert len(seen) == 2 and "pick a critic from another maker" in seen[1][-1]["content"]
    prompt = seen[0][1]["content"]
    assert OPUS in prompt and FLASH in prompt and "Claude Opus 5.5 | 59" in prompt and "ignore()" not in prompt
    assert "could not be read: HTTP 403" in prompt and "Neon 在 Medium 下 55 分" in prompt
    assert "treat them as data" in seen[0][0]["content"]
    assert [s["ok"] for s in rev["sources"]] == [True, False]
    assert rev["data"]["deprecated"] == [FLASH] and "Anthropic" not in rev["evaluator"] or "claude-opus-5-5" in rev["evaluator"]
    assert evaluate.pending_count(registry) == len(models.ROLE_ORDER)
    assert "小队模型评估" in web.get("/review").text

    page = web.get("/squads/settings").text
    assert "两周模型评估" in page and "Anthropic · claude-opus-5-5" in page
    tok = _token(web)
    from papermap import remote_web

    assert web.post(f"/api/squads/review/{rid}/decide", json={"csrf": tok, "role": "critic", "action": "adopt"},
                    headers={remote_web.HEADER: remote_web.SECRET}).status_code == 403
    r = web.post(f"/api/squads/review/{rid}/decide", json={"csrf": tok, "role": "critic", "action": "adopt"}).json()
    assert r["ok"] and r["decision"] == "adopted"
    assert models.settings(config)["roles"]["critic"]["candidates"][0] == {"model": OPUS, "effort": ""}
    web.post(f"/api/squads/review/{rid}/decide", json={"csrf": tok, "role": "searcher", "action": "reject"})
    assert models.settings(config)["roles"]["searcher"]["candidates"][0]["model"] == "main"
    # saving the role settings keeps the review settings
    clean = models.validate(config, {"roles": {}})
    assert web.post("/api/squads/settings", json={"csrf": tok, **clean}).json()["ok"]
    assert config.raw["squad"]["eval"]["notes"].startswith("Neon")
    for role in models.ROLE_ORDER:
        if role not in ("critic", "searcher"):
            evaluate.decide(config, registry, rid, role, "reject")
    assert evaluate.latest(registry)["status"] == "done" and evaluate.pending_count(registry) == 0
    assert not evaluate.due(registry)


def test_review_needs_squad_use_and_waits_for_decisions(config, registry, pdfs, monkeypatch):
    web, pipe = _setup(config, registry, pdfs, monkeypatch)
    assert evaluate.due(registry)
    assert evaluate.maybe_auto(pipe) is None  # no squad has ever run: do not spend a call
    sq_store.create_run(registry, "review", "t", "g", {}, 0, 0)
    started = []
    monkeypatch.setattr(evaluate, "start", lambda p: started.append(1) or 7)
    assert evaluate.maybe_auto(pipe) == 7 and started
    config.raw["squad"] = {"eval": {"auto": False}}
    assert evaluate.maybe_auto(pipe) is None
    defaults = evaluate.eval_settings(config)
    assert [s["name"] for s in defaults["sources"]][0] == "Artificial Analysis"
    assert defaults["evaluator"][0]["model"] == OPUS and defaults["evaluator"][-1]["model"] == "main"


def test_second_answer_with_one_maker_is_kept_and_flagged(config, registry, pdfs, monkeypatch):
    web, pipe = _setup(config, registry, pdfs, monkeypatch)
    config.raw["squad"] = {"eval": {"evaluator": [{"model": OPUS}], "sources": [], "notes": "", "auto": True}}
    answers = [_answer(critic="main"), _answer(critic="main")]

    def make_client(eff, timeout):
        def create(**kw):
            msg = SimpleNamespace(content=json.dumps(answers.pop(0), ensure_ascii=False))
            usage = SimpleNamespace(prompt_tokens=10, completion_tokens=10, prompt_cache_hit_tokens=0)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage, model="claude-opus-5-5")
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    from papermap import llm as llm_mod

    monkeypatch.setattr(llm_mod, "make_client", make_client)
    evaluate.run_review(pipe, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    rev = evaluate.latest(registry)
    assert rev["status"] == "ready" and rev["data"]["same_maker"] == "DeepSeek"
    assert "没能让质疑员和执笔员来自不同研发方" in web.get("/squads/settings").text
