"""Writing from the library, the manuscript citation check, the clippings notebook, card formatting."""
import io
import time
import zipfile

from papermap import backup, citecheck, clips, docx_out, trash, tracking

from .conftest import OWN_DOI, OWN_TITLE
from .test_reader import client as reader_client
from .test_web import _token


def _wait(registry, run_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if registry.one("SELECT status FROM cite_runs WHERE id=?", (run_id,))["status"] != "running":
            return
        time.sleep(0.05)


def test_write_paragraph_from_library_and_export(config, registry, pdfs):
    c, pipe, _ = reader_client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    tok = _token(c)
    assert c.post("/api/purposes", json={"csrf": tok, "paper_ids": [pid], "tag": "intro"}).json()["ok"]
    r = c.post("/api/write", json={"csrf": tok, "topic": "fibroblast niches", "scope": "u:intro", "style": "author_year"}).json()
    assert r["ok"] and r["cited"] == [pid]
    assert "(Zhang, 2024)" in r["markdown"] and "[P99]" not in r["markdown"] and "[P1]" not in r["markdown"]
    assert OWN_TITLE in r["markdown"]  # reference list
    r2 = c.post(f"/api/write/{r['id']}/style", json={"csrf": tok, "style": "numbered"}).json()
    assert "[1]" in r2["markdown"]
    docx = c.get(f"/write/{r['id']}.docx")
    assert docx.status_code == 200 and zipfile.ZipFile(io.BytesIO(docx.content)).read("word/document.xml")
    assert "@" in c.get(f"/write/{r['id']}.bib").text
    assert not c.post("/api/write", json={"csrf": tok, "topic": "x", "scope": "u:none"}).json()["ok"]
    # the draft goes straight to the citation check
    chk = c.post(f"/api/write/{r['id']}/check", json={"csrf": tok}).json()
    _wait(registry, chk["run"])
    verdicts = {i["verdict"] for i in citecheck.items(registry, chk["run"])}
    assert verdicts == {"supports"}


def test_citecheck_docx_numbered_and_author_year(config, registry, pdfs):
    c, pipe, _ = reader_client(config, registry, pdfs)
    tok = _token(c)
    doc = docx_out.build([
        ("p", "Tumours were profiled with Visium spatial transcriptomics [1]. A different claim was never used here [2]."),
        ("p", "Fibroblast niches were mapped (Zhang et al., 2024)."),
        ("h2", "References"),
        ("p", f"1. Zhang W, et al. {OWN_TITLE}. J Demo Hepatol. 2024. doi:{OWN_DOI}"),
        ("p", "2. Nobody A. A paper that is not in this library at all. Nature. 2020."),
    ])
    up = c.post("/api/citecheck/upload", data={"csrf": tok}, files={"file": ("thesis.docx", doc)}).json()
    assert up["ok"]
    s = citecheck.summary(registry, up["id"])
    assert s["refs"] == {"entries": 2, "matched": 1} and s["counts"] == {"pending": 2, "not_in_library": 1}
    assert "开始核对" in c.get(f"/citecheck?id={up['id']}").text
    config.raw["llm"]["api_key"] = "sk-test"  # the fake model stands in for the connected one
    assert c.post(f"/api/citecheck/{up['id']}/start", json={"csrf": tok}).json()["ok"]
    _wait(registry, up["id"])
    rows = citecheck.items(registry, up["id"])
    sup = [r for r in rows if r["verdict"] == "supports"]
    assert len(sup) == 2 and all(r["page"] == 0 and r["quote"] for r in sup)  # quote found verbatim, on p.1
    assert "[1]" not in sup[0]["sentence"]  # markers removed from the shown sentence
    assert "支持" in c.get(f"/citecheck/{up['id']}.md").text
    assert not c.post("/api/citecheck/upload", data={"csrf": tok}, files={"file": ("x.pdf", b"%PDF")}).json()["ok"]


def test_citecheck_rejects_quotes_not_in_the_paper(config, registry, pdfs, monkeypatch):
    c, pipe, comp = reader_client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    paper = registry.get_paper(pid)
    refs = [{"rid": "R1", "paper": paper, "passages": [{"page": 2, "text": "Completely different words."}]}]
    out = citecheck.check_sentence(pipe.llm, "English", "Visium was used.", refs)
    assert out[0]["verdict"] == "unclear" and out[0]["quote"] == ""


def test_clips_and_digest(config, registry, pdfs):
    c, pipe, _ = reader_client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    tok = _token(c)
    a = c.post("/api/clips", json={"csrf": tok, "text": "Fibroblasts sit at margins", "paper_id": pid, "page": 1, "source": "pdf"}).json()
    assert a["ok"]
    assert c.post("/api/clips", json={"csrf": tok, "text": "Fibroblasts sit at margins", "paper_id": pid}).json()["id"] == a["id"]
    b = c.post("/api/clips", json={"csrf": tok, "text": "AI said something useful", "source": "chat", "url": "https://evil"}).json()
    assert registry.one("SELECT url FROM clips WHERE id=?", (b["id"],))["url"] == ""
    assert not c.post("/api/clips", json={"csrf": tok, "text": " "}).json()["ok"]
    d = c.post("/api/clips-digest", json={"csrf": tok}).json()
    assert d["ok"]
    page = c.get("/clips").text
    assert f'href="#clip-{a["id"]}"' in page and "C9999" not in page and "值得追问" in page
    assert list((config.knowledge_dir / "clips").glob("*.md"))
    assert c.post(f"/api/clips/{b['id']}/delete", json={"csrf": tok}).json()["ok"]
    assert clips.maybe_auto_digest(pipe) is None  # just organised, nothing new


def test_card_format_selection(config, registry, pdfs):
    c, pipe, _ = reader_client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    tok = _token(c)
    r = c.post(f"/paper/{pid}/card/format", json={"csrf": tok, "text": "Finding two", "kind": "highlight", "color": "#fff3a3"}).json()
    assert r["ok"] and r["edited"] and "Finding two</mark>" in r["html"]
    r = c.post(f"/paper/{pid}/card/format", json={"csrf": tok, "text": "Finding one", "kind": "bold"}).json()
    assert "<strong>Finding one</strong>" in r["html"]
    assert len(c.get(f"/paper/{pid}/card/versions").json()["versions"]) == 2  # each change can be undone
    assert not c.post(f"/paper/{pid}/card/format", json={"csrf": tok, "text": "nowhere in card", "kind": "bold"}).json()["ok"]
    assert not c.post(f"/paper/{pid}/card/format", json={"csrf": tok, "text": "Finding one", "kind": "color", "color": "red;x"}).json()["ok"]


def test_mirror_trash_purge_and_weekly_digest(config, registry, pdfs, tmp_path):
    c, pipe, _ = reader_client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    target = tmp_path / "drive" / "PaperMap"
    target.parent.mkdir()
    config.raw.setdefault("backup", {}).update({"mirror_enabled": True, "mirror_dir": str(target)})
    backup.create(config, registry, "manual")
    st = backup.mirror(config, registry)
    assert st["copied"] > 0 and (target / "knowledge").is_dir() and list((target / "database").glob("*.zip"))
    assert backup.mirror(config, registry)["copied"] == 0  # only changes are copied
    assert not backup.mirror_due(config)
    # trash: expiring reminder, then emptied after the configured days
    trash.delete_paper(pipe, pid)
    config.raw["trash_days"] = 30
    row = registry.get_paper(pid)
    old = (config.data_dir / row["trash_path"])
    aged = old.with_name("20200101-000000_" + old.name.split("_", 1)[1])
    old.rename(aged)
    registry.update_paper(pid, trash_path=str(aged.relative_to(config.data_dir)))
    assert trash.expiring(config, registry)[0]["id"] == pid
    assert trash.purge(config, registry) == 1 and not aged.exists()
    assert registry.get_paper(pid)["status"] == "ignored" and registry.get_paper(pid)["trash_path"] is None
    # weekly digest of new literature: sent once, then not again within the week
    tracking.ensure_schema(registry)
    registry.execute("INSERT INTO track_candidates(doi, title, title_key, score, created_at, updated_at) VALUES "
                     "('10.1/x', 'A new paper', 'anewpaper', 9, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')")
    sent = []
    assert tracking.maybe_digest(config, registry, send=lambda t, b: sent.append((t, b)))
    assert sent and "A new paper" in sent[0][1]
    assert tracking.maybe_digest(config, registry, send=lambda t, b: sent.append(1)) is None and len(sent) == 1
    assert "本周简报" in c.get("/tracking").text


def test_sentences_split_in_chinese_and_english():
    zh = "工具加快了聚类 (Wolf et al., 2018)；Squidpy 统一了空间数据 [2]。零膨胀仍有争论 [3]！"
    parts = [p for p in citecheck.SENT_SPLIT.split(zh) if p and p.strip()]
    assert len(parts) == 3 and parts[1].startswith("Squidpy")
    en = "Clustering is fast [1]. Squidpy unifies data [2]. e.g. this stays."
    assert len([p for p in citecheck.SENT_SPLIT.split(en) if p and p.strip()]) == 2  # no split before "e.g."
