from fastapi.testclient import TestClient

from papermap import compare as cmp
from papermap.web.app import create_app

from .test_newfields import add_paper
from .test_pipeline import make_pipeline
from .test_web import _token


def test_compare_table_exports_and_summary(config, registry):
    pipe, comp = make_pipeline(config, registry)
    ids = [add_paper(registry, i) for i in range(2)]
    items = cmp.rows(registry, ids)
    assert [i["who"] for i in items] == ["Auth0 2020", "Auth1 2021"] and items[0]["platforms"] == "Visium"
    md = cmp.to_markdown(items, "T")
    assert "| 论文 | 研究问题" in md and "| Auth0 2020 |" in md
    assert cmp.to_csv(items).startswith("﻿paper,doi,研究问题")
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    page = c.get(f"/compare?ids={ids[0]},{ids[1]}").text
    assert "Auth1 2021" in page and "生成对比总结" in page
    assert "Visium" in c.get(f"/compare.csv?ids={ids[0]},{ids[1]}").text
    j = c.post("/api/compare/summary", json={"csrf": _token(c), "ids": f"{ids[0]},{ids[1]}"}).json()
    assert j["ok"] and (config.knowledge_dir / j["saved"]).exists()
    assert c.post("/api/compare/summary", json={"csrf": _token(c), "ids": str(ids[0])}).json()["ok"] is False
