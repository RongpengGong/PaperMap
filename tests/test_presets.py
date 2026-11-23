"""One-click provider presets and flat-rate (subscription) billing."""
from types import SimpleNamespace

from papermap import config as cfgmod
from papermap.llm import LLMClient, compute_cost

from .test_web import BASE_FORM, _token, client


def test_subscription_preset_costs_nothing():
    cfg = cfgmod.from_dict({"llm": {"provider": "minimax_mplan", "presets": {"minimax_mplan": {"api_key": "mk"}}}})
    eff = cfg.effective_llm()
    assert (eff["base_url"], eff["protocol"], eff["model"], eff["api_key"]) == (
        "https://api.minimax.cn/v1", "openai", "MiniMax-M3.1-Flash", "mk")
    assert eff["billing"] == "subscription" and not eff["json_mode"]
    assert compute_cost(eff, 100_000, 50_000) == 0.0


def test_api_preset_prices_default_and_override():
    cfg = cfgmod.from_dict({"llm": {"provider": "anthropic", "presets": {"anthropic": {"model": "claude-sonnet-5-5"}}}})
    eff = cfg.effective_llm()
    assert eff["protocol"] == "anthropic" and eff["no_temperature"]
    assert (eff["price_input_per_1m"], eff["price_output_per_1m"]) == (2.0, 10.0)
    cfg = cfgmod.from_dict({"llm": {"provider": "zhipu", "presets": {"zhipu": {"model": "glm-x", "price_input_per_1m": 1}}}})
    eff = cfg.effective_llm()
    assert eff["price_input_per_1m"] == 1.0 and eff["price_cache_hit_per_1m"] == 1.0 and eff["billing"] == "token"


class _Fake:
    def __init__(self):
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        msg = SimpleNamespace(content='<think>hmm</think>Here it is: {"a": 1}')
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], model=kw["model"],
                               usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


def test_plan_calls_logged_without_cost(config, registry):
    config.raw["llm"].update(provider="ollama_cloud", presets={"ollama_cloud": {"api_key": "ok"}})
    fake = _Fake()
    res = LLMClient(config, registry, client=fake).chat_json("s", "u", lambda d: [], "extract")
    assert res.data == {"a": 1}
    assert "response_format" not in fake.calls[0]  # Ollama cloud has no JSON mode
    row = registry.one("SELECT billing, cost_usd, prompt_tokens FROM llm_calls")
    assert (row["billing"], row["cost_usd"], row["prompt_tokens"]) == ("subscription", 0.0, 10)
    page = client(config, registry).get("/costs").text
    assert "本月包月套餐用量" in page and "当前用的是包月套餐" in page


def test_anthropic_preset_sends_no_temperature(config, registry):
    config.raw["llm"].update(provider="anthropic", presets={"anthropic": {"api_key": "ak"}})
    fake = _Fake()
    LLMClient(config, registry, client=fake).chat_text("s", "u", "qa")
    assert "temperature" not in fake.calls[0] and fake.calls[0]["max_tokens"] >= 16000


def test_settings_saves_preset_key(config, registry):
    c = client(config, registry)
    page = c.get("/settings").text
    assert "MiniMax M Plan" in page and "Ollama Cloud" in page and "Claude Pro / Max" in page
    c.post("/settings", data={"csrf": _token(c), "provider": "moonshot", "p_moonshot_key": "sk-kimi",
                              "p_moonshot_model": "kimi-x", "p_moonshot_in": "0.5", "p_moonshot_out": "2",
                              **BASE_FORM})
    eff = config.effective_llm()
    assert (eff["provider"], eff["api_key"], eff["model"], eff["price_output_per_1m"]) == ("moonshot", "sk-kimi", "kimi-x", 2.0)
    assert "sk-kimi" not in c.get("/settings").text
    # blank key keeps the stored one; custom relay settings are untouched
    c.post("/settings", data={"csrf": _token(c), "provider": "moonshot", **BASE_FORM})
    assert config.effective_llm()["api_key"] == "sk-kimi"


def test_knowledge_dir_move(config, registry, tmp_path):
    from papermap import library

    kb = config.knowledge_dir
    (kb / "papers" / "p1").mkdir(parents=True)
    (kb / "papers" / "p1" / "p1.md").write_text("card")
    c = client(config, registry)
    tok = _token(c)
    # a non-empty folder gets a PaperMap subfolder; nested targets are refused
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "mine.md").write_text("x")
    pre = c.post("/api/knowledge/move", json={"csrf": tok, "path": str(vault)}).json()
    assert pre["ok"] and pre["target"] == str(vault / "PaperMap") and pre["files"] == 1
    assert not c.post("/api/knowledge/move", json={"csrf": tok, "path": str(kb / "papers")}).json()["ok"]
    r = c.post("/api/knowledge/move", json={"csrf": tok, "path": str(vault), "confirm": True}).json()
    assert r["ok"]
    assert config.knowledge_dir == vault / "PaperMap"
    assert (vault / "PaperMap" / "papers" / "p1" / "p1.md").read_text() == "card" and not kb.exists()
    assert "knowledge_dir" in config.path.read_text()
    # back to the default location
    r = c.post("/api/knowledge/move", json={"csrf": tok, "reset": True, "confirm": True}).json()
    assert r["ok"] and config.knowledge_dir == config.data_dir / "knowledge" and config.raw["knowledge_dir"] == ""
    assert (kb / "papers" / "p1" / "p1.md").exists()
    # cross-volume path (copy + verify + delete)
    src, dst = tmp_path / "a", tmp_path / "b"
    (src / "x").mkdir(parents=True)
    (src / "x" / "f.txt").write_text("hi")
    import os
    orig = os.rename
    os.rename = lambda *a: (_ for _ in ()).throw(OSError("cross-device"))
    try:
        n, how = library.move_knowledge(src, dst)
    finally:
        os.rename = orig
    assert (n, how) == (1, "copied") and (dst / "x" / "f.txt").read_text() == "hi" and not src.exists()
