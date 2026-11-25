import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from papermap import antigravity_cli as agy
from papermap.llm import BudgetExceeded, LLMError, LLMNotConfigured


@pytest.fixture
def fake_agy(tmp_path, monkeypatch):
    script = tmp_path / "fake_agy.py"
    script.write_text(
        textwrap.dedent("""
        import json, os, sys, time
        assert sys.argv[1:] == ["acp", "--sandbox", "--include-default-customizations=false"]
        assert not any(os.getenv(key) for key in ["GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_BASE_URL"])
        assert os.path.basename(os.getcwd()).startswith("papermap-agy-")
        def send(event):
            print(json.dumps({"jsonrpc": "2.0", **event}), flush=True)
        def option(name, current, values):
            return {"id": name, "currentValue": current,
                    "options": [{"value": v, "name": v.title()} for v in values]}
        def chunk(text, sid="abc", kind="agent_message_chunk"):
            send({"method":"session/update", "params":{"sessionId":sid, "update":{
                "sessionUpdate":kind, "content":{"type":"text","text":text}}}})
        behavior = os.getenv("PAPERMAP_TEST_ACP", "")
        for line in sys.stdin:
            req = json.loads(line)
            method = req["method"]
            params = req["params"]
            with open(os.environ["PAPERMAP_TEST_LOG"], "a", encoding="utf8") as log:
                print(json.dumps([method, params]), file=log)
            if method == "initialize":
                assert not any(params["clientCapabilities"]["fs"].values())
                assert not params["clientCapabilities"]["terminal"]
                res = {"protocolVersion": 1, "agentCapabilities":{"promptCapabilities":{"image":True}}}
            elif method == "session/new":
                assert not params["mcpServers"]
                if behavior == "no_login":
                    send({"id":req["id"], "error":{"code":-32000,"message":"Authentication required"}})
                    continue
                tier = {} if behavior == "no_tier" else {"name": "Enterprise" if behavior == "business" else "Google AI Pro", "id":"tier"}
                res = {"sessionId":"abc", "_meta":{"antigravity":{"tier":tier}},
                       "configOptions":[option("model","glow",["glow","flash"]),
                                        option("thought_level","high",["low","high"]),
                                        option("mode","auto",["default","auto"])],
                       "models":{"availableModels":[{"modelId":"glow-high"},{"modelId":"flash-low"},
                                                   {"modelId":"flash-xhigh"}]}}
                if behavior == "old_models":
                    res.pop("configOptions")
                    res["models"]["currentModelId"] = "glow-high"
            elif method in {"session/set_config_option", "session/set_model"}:
                if params.get("value") == "bad":
                    send({"id":req["id"],"error":{"code":-32602, "message":"unsupported"}})
                    continue
                res = {"configOptions":[option("thought_level", "low", ["low"])]}
            elif method == "session/prompt":
                if behavior == "hang":
                    time.sleep(30)
                if behavior == "quota":
                    send({"id":req["id"], "error":{"code":-32603, "message":"RESOURCE_EXHAUSTED"}})
                    continue
                if behavior == "quit": sys.exit(0)
                if behavior == "permission":
                    send({"id":88, "method":"session/request_permission","params":{"sessionId":"abc"}})
                    assert json.loads(sys.stdin.readline())["result"]["outcome"] == {"outcome":"cancelled"}
                    send({"id":89, "method":"terminal/create","params":{"sessionId":"abc"}})
                    assert json.loads(sys.stdin.readline())["error"]["code"] == -32601
                if behavior == "tool":
                    chunk("", kind="tool_call")
                chunk("wrong session", sid="other")
                chunk("private reasoning", kind="agent_thought_chunk")
                chunk("Paper")
                chunk("Map")
                res = {"stopReason":"cancelled" if behavior == "cancel" else "end_turn",
                       "usage":{"inputTokens":15,"outputTokens":2}}
            else:
                raise AssertionError(method)
            send({"id":req["id"], "result":res})
    """),
        encoding="utf8",
    )
    log = tmp_path / "log.ndjson"
    monkeypatch.setenv("PAPERMAP_TEST_LOG", str(log))
    monkeypatch.setenv("GEMINI_API_KEY", "test-only")
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "test-only")
    popen = subprocess.Popen
    monkeypatch.setattr(
        agy.subprocess, "Popen", lambda cmd, **kw: popen([sys.executable, str(script), *cmd[1:]], **kw)
    )
    return agy.AntigravityCliClient(executable=sys.executable, timeout_s=5), log


def logged(log):
    return [json.loads(line) for line in log.read_text(encoding="utf8").splitlines()]


def test_catalog_status_cache_and_refresh(fake_agy):
    cli, log = fake_agy
    state = cli.status()
    assert state["ready"] and state["authenticated"] and state["subscription_type"] == "Google AI Pro"
    assert [m["id"] for m in state["models"]] == ["glow", "flash"]
    assert [m["id"] for m in state["models"] if m["is_current"]] == ["glow"]
    assert cli.model_options()[1]["reasoning_efforts"] == ["low", "xhigh"]
    state["models"].clear()
    assert cli.model_options()[0]["default_reasoning_effort"] == "high"
    assert len(logged(log)) == 2
    cli.status(refresh=True)
    assert len(logged(log)) == 4


def test_create_selects_published_variant_and_serializes_task(fake_agy, monkeypatch):
    cli, log = fake_agy
    monkeypatch.setenv("PAPERMAP_TEST_ACP", "permission")
    messages = [
        {"role": "system", "content": "Return evidence."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Figure A"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,YWJj"}},
            ],
        },
    ]
    result = cli.create("flash", messages, reasoning_effort="xhigh", response_format={"type": "json_object"})
    assert result.model == "flash"
    assert result.choices[0].message.content == "PaperMap"
    assert result.usage.prompt_tokens == 15 and result.usage.completion_tokens == 2
    calls = logged(log)
    assert [(p["configId"], p["value"]) for m, p in calls if m == "session/set_config_option"] == [
        ("mode", "default"),
        ("model", "flash"),
        ("model", "flash-xhigh"),
    ]
    prompt = calls[-1][1]["prompt"]
    assert "JSON object" in prompt[0]["text"] and "Return evidence." in prompt[0]["text"]
    assert prompt[1] == {"type": "image", "mimeType": "image/png", "data": "YWJj"}


def test_streams_only_this_sessions_answer(fake_agy):
    cli, _ = fake_agy
    chunks = list(cli.create("auto", [{"role": "user", "content": "a"}], stream=True))
    assert [r.choices[0].delta.content for r in chunks if r.choices] == ["Paper", "Map"]
    assert chunks[-1].usage.completion_tokens == 2 and chunks[-1].model == "glow"


def test_legacy_model_selection(fake_agy, monkeypatch):
    cli, log = fake_agy
    monkeypatch.setenv("PAPERMAP_TEST_ACP", "old_models")
    assert [o["id"] for o in cli.model_options()] == ["glow-high", "flash-low", "flash-xhigh"]
    assert [m["id"] for m in cli.model_options() if m["is_current"]] == ["glow-high"]
    cli.create("flash-low", [{"role": "user", "content": "a"}])
    assert any(m == "session/set_model" and p["modelId"] == "flash-low" for m, p in logged(log))


@pytest.mark.parametrize(
    ("behavior", "error", "message"),
    [
        ("quota", BudgetExceeded, "上限"),
        ("cancel", LLMError, "取消"),
        ("tool", LLMError, "工具"),
        ("quit", LLMError, "提前退出"),
        ("no_login", LLMNotConfigured, "登录"),
        ("no_tier", LLMNotConfigured, "套餐"),
        ("business", LLMNotConfigured, "企业"),
    ],
)
def test_failures_do_not_return_partial_answers(fake_agy, monkeypatch, behavior, error, message):
    cli, _ = fake_agy
    monkeypatch.setenv("PAPERMAP_TEST_ACP", behavior)
    with pytest.raises(error, match=message):
        cli.create("auto", [{"role": "user", "content": "a"}])


def test_timeout_and_cleanup(fake_agy, monkeypatch):
    cli, log = fake_agy
    cli.timeout_s = 0.6
    monkeypatch.setenv("PAPERMAP_TEST_ACP", "hang")
    with pytest.raises(LLMError, match="超时"):
        cli.create("auto", [{"role": "user", "content": "a"}])
    cwd = next(p["cwd"] for m, p in logged(log) if m == "session/new")
    assert not Path(cwd).exists()


def test_rejected_model_does_not_send_prompt(fake_agy):
    cli, log = fake_agy
    with pytest.raises(LLMError, match="模型"):
        cli.create("bad", [{"role": "user", "content": "a"}])
    assert all(m != "session/prompt" for m, p in logged(log))


def test_missing_binary(tmp_path):
    cli = agy.AntigravityCliClient(executable=str(tmp_path / "missing"))
    assert not cli.status()["available"]
    with pytest.raises(LLMNotConfigured):
        cli.model_options()
    assert not cli.start_login()["ok"]
