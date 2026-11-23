import time

import pytest
from fastapi.testclient import TestClient

from papermap.web.app import create_app

from .conftest import OWN_DOI
from .test_newfields import add_paper
from .test_pipeline import make_pipeline
from .test_web import _token


def test_registry_groups(registry):
    a, b = add_paper(registry, 1), add_paper(registry, 2)
    liver = registry.create_collection("肝癌")
    sc = registry.create_collection("单细胞", liver)
    assert registry.add_to_collection(sc, [a, b, 999]) == 2
    assert registry.add_to_collection(liver, [a]) == 1 and registry.add_to_collection(liver, [a]) == 0
    tree = registry.collection_tree()
    assert tree[0]["name"] == "肝癌" and tree[0]["children"][0]["name"] == "单细胞"
    assert registry.collection_path(sc) == "肝癌 / 单细胞"
    assert {r["id"] for r in registry.paper_collections(a)} == {liver, sc}
    with pytest.raises(ValueError):
        registry.move_collection(liver, sc)  # no cycles
    with pytest.raises(ValueError):
        registry.create_collection("  ")
    assert sorted(registry.delete_collection(liver)) == [a, b]
    assert registry.collections() == [] and registry.get_paper(a) is not None  # papers stay


def test_group_api_and_filter(config, registry, pdfs):
    pipe, _ = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    other = add_paper(registry, 5)
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    tok = _token(c)
    j = c.post("/api/collections", json={"csrf": tok, "name": "空间组学", "paper_ids": [pid]}).json()
    assert j["ok"]
    gid = j["id"]
    sub = c.post("/api/collections", json={"csrf": tok, "name": "Visium", "parent_id": gid}).json()["id"]
    assert c.post(f"/api/collections/{sub}/add", json={"csrf": tok, "paper_ids": [other]}).json()["added"] == 1
    home = c.get("/").text
    assert "空间组学" in home and "Visium" in home and "未分组" in home
    # a group shows its own and its subgroups' papers
    in_group = c.get(f"/papers/list?c={gid}").text
    assert f'data-id="{pid}"' in in_group and f'data-id="{other}"' in in_group
    assert f'data-id="{other}"' in c.get(f"/papers/list?c={sub}").text
    assert f'data-id="{pid}"' not in c.get(f"/papers/list?c={sub}").text
    assert f'data-id="{pid}"' not in c.get("/papers/list?c=none").text
    panel = c.get(f"/paper/{pid}/panel").text
    assert "空间组学" in panel and "data-remove-group" in panel
    # card frontmatter lists the groups (refreshed in the background)
    card = config.knowledge_dir / registry.get_paper(pid)["kb_path"]
    for _ in range(50):
        if "空间组学" in card.read_text():
            break
        time.sleep(0.05)
    assert "collections:\n- 空间组学" in card.read_text()
    assert c.post(f"/api/collections/{gid}/move", json={"csrf": tok, "parent_id": sub}).json()["ok"] is False
    assert c.post(f"/api/collections/{sub}/rename", json={"csrf": tok, "name": "Visium HD"}).json()["ok"]
    assert c.post(f"/api/collections/{gid}/remove", json={"csrf": tok, "paper_ids": [pid]}).json()["ok"]
    assert f'data-id="{pid}"' in c.get("/papers/list?c=none").text
    assert c.post(f"/api/collections/{gid}/delete", json={"csrf": tok}).json()["ok"]
    assert registry.collections() == []
    assert c.post("/api/collections", json={"csrf": "bad", "name": "x"}).status_code == 403


def test_import_into_selected_group(config, registry, pdfs):
    pipe, _ = make_pipeline(config, registry)
    config.ensure_dirs()
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    tok = _token(c)
    gid = registry.create_collection("待读")
    r = c.post("/api/import", data={"csrf": tok, "collection": str(gid)},
               files=[("files", ("x.pdf", pdfs["paper"].read_bytes(), "application/pdf"))])
    assert r.json()["count"] == 1
    for _ in range(100):
        p = registry.paper_by_doi(OWN_DOI)
        if p and registry.paper_collections(p["id"]):
            break
        time.sleep(0.05)
    assert [g["name"] for g in registry.paper_collections(registry.paper_by_doi(OWN_DOI)["id"])] == ["待读"]
