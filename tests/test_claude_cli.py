"""Offline CLI contract tests: no real process, account, or inference is used."""

import json
import base64
import subprocess
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from papermap import claude_cli as cc
from papermap.llm import BudgetExceeded, LLMError, LLMNotConfigured


AUTH = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"}
SIGNED_OUT = {"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_PROFILE",
                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                "CLAUDE_CODE_SIMPLE", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    cc._client.cache_clear()


def mock_cli(monkeypatch, auth=None, answer=None, returncode=0, setup_auth=None):
    calls = []
    monkeypatch.setattr(cc, "find_claude", lambda executable=None: "/mock/claude")

    def run(args, **kwargs):
        nonlocal auth
        assert kwargs["shell"] is False
        assert Path(kwargs["cwd"]).is_dir()
        assert not list(Path(kwargs["cwd"]).iterdir())
        calls.append((args, kwargs))
        if args[-2:] == ["auth", "status"]:
            value = AUTH if auth is None else auth
            if setup_auth is not None and "CLAUDE_CODE_OAUTH_TOKEN" not in json.loads(args[args.index("--settings") + 1])["env"]:
                value = setup_auth
            return SimpleNamespace(returncode=0 if value.get("loggedIn") else 1, stdout=json.dumps(value), stderr="")
        if args[-2:] == ["auth", "logout"]:
            auth = SIGNED_OUT
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        value = answer or {"type": "result", "subtype": "success", "is_error": False, "result": '{"answer": "中文"}',
                           "usage": {"input_tokens": 10, "output_tokens": 6, "cache_creation_input_tokens": 4,
                                     "cache_read_input_tokens": 20},
                           "modelUsage": {"claude-sonnet-example": {}}, "total_cost_usd": 0.12}
        return SimpleNamespace(returncode=returncode, stdout=json.dumps(value), stderr="")

    monkeypatch.setattr(cc.subprocess, "run", run)
    return calls


def test_finder_native_install_is_found_without_path(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cc.sys, "platform", "darwin")
    monkeypatch.setattr(cc.shutil, "which", lambda _: None)
    native = tmp_path / ".local" / "bin" / "claude"
    native.parent.mkdir(parents=True)
    native.write_text("fixture only")
    native.chmod(0o700)
    assert cc.find_claude() == str(native.resolve())


def test_windows_native_path_and_batch_shim_boundary(tmp_path, monkeypatch):
    native = tmp_path / "claude.exe"
    native.write_text("fixture only")
    shim = tmp_path / "claude.cmd"
    shim.write_text("fixture only")
    monkeypatch.setattr(cc.sys, "platform", "win32")
    monkeypatch.setattr(cc.shutil, "which", lambda _: str(native))
    assert cc.find_claude() == str(native)
    assert cc.find_claude(str(shim)) is None


def test_status_is_cached_and_only_returns_public_fields(monkeypatch):
    calls = mock_cli(monkeypatch, auth={**AUTH, "email": "private@example.com", "orgId": "private-org", "accessToken": "secret"})
    cli = cc.get_cli()
    state = cli.status()
    assert state["ready"] and state["billing"] == "subscription"
    assert state["subscription_type"] == "max"
    assert "private" not in json.dumps(state) and "secret" not in json.dumps(state)
    assert cc.get_cli() is cli
    cli.status()
    assert len(calls) == 1
    cli.status(refresh=True)
    assert len(calls) == 2


@pytest.mark.parametrize("method", ["api_key", "api_key_helper", "third_party", "oauth_token", "unknown"])
def test_non_subscription_identity_never_sends_prompt(monkeypatch, method):
    calls = mock_cli(monkeypatch, auth={**AUTH, "authMethod": method})
    cli = cc.ClaudeCliClient()
    assert not cli.status()["ready"]
    with pytest.raises(LLMNotConfigured):
        cli.create(model="sonnet", messages=[{"role": "user", "content": "PRIVATE PAPER"}])
    assert all(args[-2:] == ["auth", "status"] for args, _ in calls)


@pytest.mark.parametrize("key,value", [("ANTHROPIC_API_KEY", "secret"), ("ANTHROPIC_AUTH_TOKEN", "secret"),
                                      ("CLAUDE_CODE_OAUTH_TOKEN", "secret"), ("CLAUDE_CODE_USE_VERTEX", "1"),
                                      ("ANTHROPIC_BASE_URL", "https://proxy.example"), ("CLAUDE_CODE_SIMPLE", "1"),
                                      ("ANTHROPIC_MODEL", "fixture-proxy-model"), ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "fixture-haiku-proxy")])
def test_browser_uses_official_route_for_status_and_inference_without_altering_shell(monkeypatch, key, value):
    calls = mock_cli(monkeypatch)
    monkeypatch.setenv(key, value)
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.invalid")
    cli = cc.ClaudeCliClient()
    assert cli.status()["ready"]
    cli.create(model="sonnet", messages=[{"role": "user", "content": "fixture"}])
    assert cc.os.environ[key] == value
    for args, kwargs in calls:
        env = kwargs["env"]
        assert key not in env and env["HTTPS_PROXY"] == "https://proxy.invalid"
        overlay = json.loads(args[args.index("--settings") + 1])
        assert overlay["disableAllHooks"] and overlay["apiKeyHelper"] == ""
        assert overlay["env"][key] == ("https://api.anthropic.com" if key == "ANTHROPIC_BASE_URL" else "")
        assert overlay["env"]["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"


def test_setup_token_is_used_only_with_first_party_and_a_matching_inference_environment(monkeypatch):
    setup = {**AUTH, "authMethod": "oauth_token", "subscriptionType": None}
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fixture-token")
    for provider, ready in [("firstParty", True), ("vertex", False)]:
        calls = mock_cli(monkeypatch, auth=SIGNED_OUT, setup_auth={**setup, "apiProvider": provider})
        cli = cc.ClaudeCliClient()
        assert cli.status()["ready"] == ready
        if ready:
            cli.create(model="sonnet", messages=[{"role": "user", "content": "fixture"}])
            args, opts = calls[-1]
            assert opts["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "fixture-token"
            assert "CLAUDE_CODE_OAUTH_TOKEN" not in json.loads(args[args.index("--settings") + 1])["env"]
        assert all("fixture-token" not in str(args) for args, _ in calls)


def test_inference_refreshes_identity_after_a_cached_ready_status(monkeypatch):
    calls = mock_cli(monkeypatch)
    cli = cc.ClaudeCliClient()
    assert cli.status()["ready"]
    mock_cli(monkeypatch, auth={"loggedIn": False, "authMethod": "none"})
    with pytest.raises(LLMNotConfigured):
        cli.create(model="sonnet", messages=[{"role": "user", "content": "paper"}])
    assert len(calls) == 1


def test_reasoning_catalog_uses_official_aliases_and_explicit_versions(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-6")
    choices = {m["id"]: m["reasoning_efforts"] for m in cc.model_options()}
    assert choices["sonnet"] == list(cc.EFFORT_LEVELS)
    assert cc.model_option("claude-sonnet-4-6")["reasoning_efforts"] == ["low", "medium", "high", "max"]
    assert cc.model_option("claude-opus-5-5[1m]")["reasoning_efforts"] == list(cc.EFFORT_LEVELS)
    assert cc.model_option("claude-haiku-4-5-20251001")["reasoning_efforts"] == []
    assert not cc.model_option("future-model")["reasoning_efforts_known"]


def test_explicit_claude_effort_reaches_child_and_default_inherits(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "low")
    calls = mock_cli(monkeypatch)
    cli = cc.get_cli()
    request = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hello"}]}
    cli.create(**request, reasoning_effort="max")
    flags, opts = calls[-1]
    assert flags[flags.index("--effort") + 1] == "max"
    assert "CLAUDE_CODE_EFFORT_LEVEL" not in opts["env"]
    cli.create(**request)
    flags, opts = calls[-1]
    assert "--effort" not in flags and opts["env"]["CLAUDE_CODE_EFFORT_LEVEL"] == "low"
    assert cc.os.environ["CLAUDE_CODE_EFFORT_LEVEL"] == "low"


@pytest.mark.parametrize("model,effort", [("claude-opus-4-6", "xhigh"), ("claude-opus-5-5", "ultra")])
def test_unsupported_claude_effort_does_not_run_cli(monkeypatch, model, effort):
    calls = mock_cli(monkeypatch)
    with pytest.raises(LLMError, match="推理强度"):
        cc.get_cli().create(model=model, messages=[{"role": "user", "content": "paper"}], reasoning_effort=effort)
    assert not calls


def test_text_json_context_and_tokens_pass_through_stdin_only(monkeypatch):
    calls = mock_cli(monkeypatch)
    cli = cc.ClaudeCliClient(timeout_s=42)
    answer = cli.chat.completions.create(model="sonnet", messages=[{"role": "system", "content": "TASK SCHEMA"},
                                                                   {"role": "user", "content": "PRIVATE PAPER 中文"}],
                                         max_tokens=1500, response_format={"type": "json_object"})
    assert answer.choices[0].message.content == '{"answer": "中文"}'
    assert answer.model == "claude-sonnet-example"
    assert (answer.usage.prompt_tokens, answer.usage.completion_tokens, answer.usage.prompt_cache_hit_tokens) == (34, 6, 20)
    args, opts = calls[-1]
    assert "TASK SCHEMA" in opts["input"] and "PRIVATE PAPER 中文" in opts["input"]
    assert "ONLY a valid JSON object" in opts["input"]
    assert "PRIVATE" not in str(args) and "TASK SCHEMA" not in str(args)
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--setting-sources") + 1] == "user"
    assert "--safe-mode" in args and "--bare" not in args
    assert "--strict-mcp-config" in args and "--no-session-persistence" in args
    assert args[args.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert args[args.index("--disallowedTools") + 1] == "mcp__*"
    assert opts["timeout"] == 42 and opts["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "1500"
    assert not Path(opts["cwd"]).exists()


def test_remote_images_are_rejected_before_any_cli_calls(monkeypatch):
    calls = mock_cli(monkeypatch)
    with pytest.raises(LLMError, match="不读取远程"):
        cc.ClaudeCliClient().create(model="sonnet", messages=[{"role": "user", "content": [
            {"type": "text", "text": "explain"}, {"type": "image_url", "image_url": {"url": "https://example.org/private.png"}}]}])
    assert calls == []


def test_inline_png_uses_documented_user_message_and_no_file_tools(monkeypatch):
    calls = mock_cli(monkeypatch)
    original_run = cc.subprocess.run
    def run(args, **kwargs):
        if args[-2:] == ["auth", "status"]:
            return original_run(args, **kwargs)
        assert not list(Path(kwargs["cwd"]).iterdir())
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stderr="", stdout="\n".join(json.dumps(x) for x in [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "Figure explanation",
             "usage": {"input_tokens": 19, "output_tokens": 8}},
        ]))
    monkeypatch.setattr(cc.subprocess, "run", run)
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture pixels").decode()
    answer = cc.ClaudeCliClient().create(model="sonnet", messages=[{"role": "user", "content": [
        {"type": "text", "text": "Describe the supplied figure"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + png}}]}])
    assert answer.choices[0].message.content == "Figure explanation"
    args, options = calls[-1]
    assert args[args.index("--input-format") + 1] == "stream-json"
    assert args[args.index("--output-format") + 1] == "stream-json" and "--verbose" in args
    assert args[args.index("--tools") + 1] == ""
    assert png not in str(args)
    assert options["input"].endswith("\n") and len(options["input"].splitlines()) == 1
    msg = json.loads(options["input"])
    assert msg["type"] == "user" and msg["parent_tool_use_id"] is None
    image = msg["message"]["content"][1]
    assert image == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png}}
    assert not Path(options["cwd"]).exists()


def test_image_stream_requires_successful_final_result(monkeypatch):
    mock_cli(monkeypatch)
    original_run = cc.subprocess.run
    def run(args, **kwargs):
        if args[-2:] == ["auth", "status"]:
            return original_run(args, **kwargs)
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}}))
    monkeypatch.setattr(cc.subprocess, "run", run)
    with pytest.raises(LLMError, match="有效 JSON"):
        cc.ClaudeCliClient().create(model="sonnet", messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,cGl4ZWxz"}}]}])


def test_failed_cli_json_is_not_treated_as_generated_content(monkeypatch):
    mock_cli(monkeypatch, answer={"subtype": "success", "is_error": True, "result": "You've hit your limit", "usage": {}})
    with pytest.raises(BudgetExceeded):
        cc.ClaudeCliClient().create(model="sonnet", messages=[{"role": "user", "content": "x"}])
    mock_cli(monkeypatch, answer={"subtype": "error", "is_error": True, "result": "Not logged in"})
    with pytest.raises(LLMNotConfigured):
        cc.ClaudeCliClient().create(model="sonnet", messages=[{"role": "user", "content": "x"}])


def test_cli_stderr_is_not_exposed_and_timeouts_are_actionable(monkeypatch):
    mock_cli(monkeypatch)
    cli = cc.ClaudeCliClient()
    cli.status()
    def broken(args, **kwargs):
        if args[-2:] == ["auth", "status"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(AUTH), stderr="")
        return SimpleNamespace(returncode=1, stdout="broken", stderr="private token and paper content")
    monkeypatch.setattr(cc.subprocess, "run", broken)
    with pytest.raises(LLMError) as err:
        cli.create(model="sonnet", messages=[{"role": "user", "content": "x"}])
    assert "private" not in str(err.value)
    def timed_out(args, **kwargs):
        if args[-2:] == ["auth", "status"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(AUTH), stderr="")
        raise subprocess.TimeoutExpired(args, 1)
    monkeypatch.setattr(cc.subprocess, "run", timed_out)
    with pytest.raises(LLMError, match="超时"):
        cli.create(model="sonnet", messages=[{"role": "user", "content": "x"}])


def test_login_opens_the_official_terminal_only_after_explicit_call(monkeypatch, tmp_path):
    calls = mock_cli(monkeypatch)
    cli = cc.ClaudeCliClient()
    cli.status()
    assert len(calls) == 1
    launches = []
    monkeypatch.setattr(cc.sys, "platform", "darwin")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config ' with spaces"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fixture-private-token")
    def launch(args, **kwargs):
        path = Path(args[-1])
        assert path.suffix == ".command" and (path.stat().st_mode & 0o777) == 0o700
        assert (path.parent.stat().st_mode & 0o777) == 0o700
        launches.append((args[:-1], kwargs, path.read_text()))
        path.unlink()
        path.parent.rmdir()
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(cc.subprocess, "run", launch)
    assert cli.login()["ok"]
    cmd, opts, script = launches[0]
    assert cmd == ["/usr/bin/open", "-a", "Terminal"]
    assert "auth login --claudeai" in script and "fixture-private-token" not in script
    assert shlex.join(cc._cli_args("/mock/claude", browser=True)) in script
    assert shlex.quote(cc.os.environ["CLAUDE_CONFIG_DIR"]) in script
    assert launches[0][1]["shell"] is False


def test_logout_uses_official_command_and_warns_shared_login(monkeypatch):
    calls = mock_cli(monkeypatch)
    out = cc.ClaudeCliClient().logout()
    assert out["ok"] and "共用" in out["message"]
    assert any(args[-2:] == ["auth", "logout"] for args, _ in calls)


@pytest.mark.parametrize("browser", [True, False])
def test_logout_never_claims_an_external_token_has_been_removed(monkeypatch, browser):
    token = {**AUTH, "authMethod": "oauth_token"}
    calls = mock_cli(monkeypatch, auth=AUTH if browser else SIGNED_OUT, setup_auth=token)
    state = cc.ClaudeCliClient().logout()
    assert not state["ok"] and state["ready"] and state["auth_method"] == "oauth_token"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in state["message"]
    assert any(args[-2:] == ["auth", "logout"] for args, _ in calls) == browser


@pytest.mark.parametrize("details,expected,message", [
    ("You've hit your 5-hour limit · resets 12:10pm (Europe/Berlin) · out of usage credits",
     BudgetExceeded, "12:10（Europe/Berlin）"),
    ("hit your limit · resets 7am (bad/secret-value)", BudgetExceeded, "订阅额度暂不可用"),
    ("API Error: 401 authentication_error other private information", LLMNotConfigured, "登录已失效"),
    ("Issue with the selected model. You may not have access.", LLMError, "所选 Claude 模型"),
    ("Unable to connect - certificate failure", LLMError, "网络、代理和证书"),
])
def test_cli_error_classification_without_echoing_payload(details, expected, message):
    with pytest.raises(expected, match=message) as exc:
        cc.ClaudeCliClient._raise_error(details)
    assert "secret-value" not in str(exc.value) and "private information" not in str(exc.value)
    if "Europe/Berlin" in details:
        assert (exc.value.reset_time, exc.value.reset_zone) == ("12:10", "Europe/Berlin")
