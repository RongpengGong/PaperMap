"""Interface language: catalogs cover every UI string, pages switch, user content stays as written."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from papermap import evidence, i18n
from papermap.web.app import create_app

from .test_pipeline import make_pipeline
from .test_web import _token

ROOT = Path(__file__).resolve().parents[1]


def test_catalogs_cover_all_keys(tmp_path):
    out = tmp_path / "keys.json"
    subprocess.run([sys.executable, str(ROOT / "packaging/i18n/extract_keys.py"), str(out)], cwd=ROOT, check=True,
                   capture_output=True)
    keys = {k for k in json.loads(out.read_text("utf-8")) if not k.startswith("^")}  # skip regex literals
    for code in ("zh-TW", "en", "de"):
        i18n.catalog.cache_clear()
        missing = sorted(keys - set(i18n.catalog(code)))
        assert not missing, f"{code} lacks {missing[:10]}"


@pytest.mark.parametrize("language", ["en", "de", "zh-TW"])
def test_antigravity_status_and_model_errors_follow_interface_language(config, registry, monkeypatch, language):
    from unittest.mock import Mock
    from papermap.llm import LLMNotConfigured

    pipe, _ = make_pipeline(config, registry)
    client = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    config.raw["appearance"]["language"] = language
    body = {"csrf": _token(client), "provider": "antigravity_acp", "model": "auto"}
    source = "请在官方终端运行 agy acp --login，完成 Google 账号登录后刷新。"
    localized = i18n.translate(language, source)
    assert localized != source
    cli = Mock()
    cli.status.return_value = {"ready": False, "message": source}
    cli.model_options.side_effect = LLMNotConfigured(source)
    monkeypatch.setattr("papermap.web.app._account_client", lambda *_: cli)
    reply = {"ok": False, "message": source}
    monkeypatch.setattr("papermap.web.app.check_connection", lambda *_: reply.copy())
    for route in ("/api/accounts/antigravity_acp/status", "/api/llm/models", "/api/llm/test"):
        assert client.post(route, json=body).json()["message"] == localized
    reply["ok"] = True
    assert client.post("/api/llm/test", json=body).json()["message"] == source


def test_switch_language_and_keep_user_text(config, registry):
    pipe, _ = make_pipeline(config, registry)
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    qid = evidence.create_question(registry, "设置")  # a user title that happens to equal a UI word
    tok = _token(c)
    assert 'lang="zh-Hans"' in c.get("/").text
    assert c.post("/api/appearance", json={"csrf": tok, "language": "en"}).json()["language"] == "en"
    for path in ("/", "/settings?section=appearance", "/evidence", "/review", "/map", "/costs", "/fields", "/search"):
        r = c.get(path)
        assert r.status_code == 200, path
    page = c.get("/settings?section=appearance").text
    assert 'lang="en"' in page and "Interface language" in page and "Sakura pink" in page
    assert "/i18n/en.js" in page and "Settings" in page
    js = c.get("/i18n/en.js").text
    assert js.startswith("window.PM_T=") and '"保存":"Save"' in js.replace(" ", "")
    nb = c.get(f"/evidence?question={qid}").text
    assert re.search(r'<option value="%d"[^>]*>设置' % qid, nb)  # the user's own title is not translated
    c.post("/api/appearance", json={"csrf": tok, "language": "de"})
    assert "Sprache der Oberfläche" in c.get("/settings?section=appearance").text
    c.post("/api/appearance", json={"csrf": tok, "language": "zh-TW"})
    assert "介面語言" in c.get("/settings?section=appearance").text or "界面語言" in c.get("/settings?section=appearance").text
    c.post("/api/appearance", json={"csrf": tok, "language": "xx"})
    assert config.raw["appearance"]["language"] == "system"
