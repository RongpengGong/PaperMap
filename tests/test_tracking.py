"""New-literature tracking (stage 7, task 6): one candidate per work, catch-up from the last success."""
from datetime import date, timedelta

from papermap import evidence, gaps, tracking
from .conftest import OWN_DOI
from .test_reader import client
from .test_web import _token


def rec(doi, title, refs=(), abstract="", oa=(), **kw):
    return {"doi": doi, "openalex_id": kw.get("oa_id"), "pmid": kw.get("pmid"), "title": title, "abstract": abstract,
            "pub_date": date.today().isoformat(), "year": date.today().year, "journal": "J", "authors": ["A B"],
            "is_preprint": False, "oa_urls": list(oa), "landing": f"https://doi.org/{doi}", "references": list(refs)}


class StubSource:
    def __init__(self, name, records):
        self.name, self.records, self.calls = name, records, []

    def supports(self, kind):
        return kind in ("field", "cites", "gap")

    def fetch(self, registry, watch, since, per_page):
        self.calls.append((watch["kind"], since))
        return self.records.get(watch["kind"], [])


def setup(config, registry, pdfs, monkeypatch):
    c, pipe, comp = client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    registry.update_paper(pid, openalex_id="W100")
    qid = evidence.create_question(registry, "成纤维细胞小生境？")
    gaps.add_gap(registry, qid, "空间证据", ["Visium", "fibroblast niche"])
    new = rec("10.1/new", "Fibroblast niche mapped with Visium in liver cancer", refs=["W100"],
              abstract="We used Visium to map the fibroblast niche.", oa=["https://x/a.pdf"], oa_id="W200")
    same_from_b = rec("10.1/NEW", "Fibroblast niche mapped with Visium in liver cancer", pmid="999")
    own = rec(OWN_DOI, "already in the library")
    a = StubSource("openalex", {"cites": [new, own], "gap": [new]})
    b = StubSource("pubmed", {"gap": [same_from_b, rec("10.1/other", "An unrelated topic entirely")]})
    monkeypatch.setattr(tracking, "sources", lambda pipeline: {"openalex": a, "pubmed": b})
    return c, pipe, comp, pid, qid, a, b


def test_one_candidate_with_reasons_and_catch_up(config, registry, pdfs, monkeypatch):
    c, pipe, comp, pid, qid, a, b = setup(config, registry, pdfs, monkeypatch)
    res = tracking.run(pipe)
    assert res["ok"] and res["new"] == 2 and not res["errors"]
    cands = tracking.candidates(registry)
    assert len(cands) == 2  # the same DOI from two sources is one candidate; the library paper is none
    top = cands[0]
    assert top["doi"] == "10.1/new" and top["pmid"] == "999" and set(top["sources"]) == {"OpenAlex", "PubMed"}
    kinds = {r["kind"] for r in top["reasons"]}
    assert {"cites", "gapmatch"} <= kinds and top["reasons"][0]["papers"][0]["id"] == pid
    assert top["score"] > cands[1]["score"]
    # same day: nothing is queried again; after three days off, each source resumes a week before its last success
    n = len(a.calls)
    tracking.run(pipe)
    assert len(a.calls) == n
    three_days_ago = (date.today() - timedelta(days=3)).isoformat()
    registry.execute("UPDATE track_cursors SET last_success=?", (three_days_ago,))
    tracking.run(pipe)
    assert a.calls[-1][1] == date.today() - timedelta(days=3 + tracking.OVERLAP_DAYS)
    assert len(tracking.candidates(registry)) == 2  # found again, still one candidate each
    page = c.get("/tracking").text
    assert "Fibroblast niche mapped" in page and "引用了你库里的" in page and "OpenAlex + PubMed" in page


def test_take_in_and_decisions(config, registry, pdfs, monkeypatch):
    c, pipe, comp, pid, qid, a, b = setup(config, registry, pdfs, monkeypatch)
    tracking.run(pipe)
    tok = _token(c)
    cid = tracking.candidates(registry)[0]["id"]
    other = tracking.candidates(registry)[1]["id"]
    got = []

    def fake_download(url, folder, name, timeout=90):
        got.append(url)
        dest = folder / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(pdfs["paper"].read_bytes())  # stands in for the downloaded open-access PDF
        return dest

    monkeypatch.setattr(tracking, "_download", fake_download)
    monkeypatch.setattr(tracking, "_import_then_sync", lambda pipeline, path: tracking.sync_library(pipeline.registry))
    r = c.post(f"/api/tracking/candidates/{cid}", json={"csrf": tok, "action": "take"}).json()
    assert r["ok"] and r["downloaded"] and got == ["https://x/a.pdf"]
    # once the paper with that DOI is in the library, the candidate is marked as taken in
    registry.execute("INSERT INTO paper_dois(paper_id, doi, kind) VALUES (?, '10.1/new', 'published')", (pid,))
    tracking.sync_library(registry)
    assert registry.one("SELECT status, paper_id FROM track_candidates WHERE id=?", (cid,))["paper_id"] == pid
    # no open-access copy: kept as "to download", with the publisher page
    r = c.post(f"/api/tracking/candidates/{other}", json={"csrf": tok, "action": "take"}).json()
    assert r["ok"] and not r["downloaded"] and r["landing"] == "https://doi.org/10.1/other"
    assert registry.one("SELECT status FROM track_candidates WHERE id=?", (other,))["status"] == "wanted"
    assert c.post(f"/api/tracking/candidates/{other}", json={"csrf": tok, "action": "dismissed"}).json()["ok"]
    assert not c.post(f"/api/tracking/candidates/{other}", json={"csrf": tok, "action": "x"}).json()["ok"]
    assert c.post(f"/api/tracking/candidates/{other}", json={"csrf": "x", "action": "new"}).status_code == 403


def test_watches_and_model_rank(config, registry, pdfs, monkeypatch):
    c, pipe, comp, pid, qid, a, b = setup(config, registry, pdfs, monkeypatch)
    monkeypatch.setattr(type(config), "model_ready", lambda self: True)
    res = tracking.run(pipe)
    assert res["ranked"] == 2
    top = tracking.candidates(registry)[0]
    assert top["model_score"] == 8 and top["model_reason"] == "和你的缺口直接相关"
    assert registry.one("SELECT purpose FROM llm_calls ORDER BY id DESC")["purpose"] == "tracking_rank"
    ws = {w["kind"]: w for w in tracking.watches(registry)}
    assert {"cites", "gap"} <= set(ws)
    tok = _token(c)
    gw = ws["gap"]["id"]
    assert c.post(f"/api/tracking/watches/{gw}", json={"csrf": tok, "enabled": False}).json()["ok"]
    tracking.sync_watches(registry)  # a watch the user switched off stays off
    assert registry.one("SELECT enabled FROM track_watches WHERE id=?", (gw,))["enabled"] == 0
    assert c.post(f"/api/tracking/watches/{gw}", json={"csrf": tok, "query": "Visium OR CosMx"}).json()["ok"]
    assert not c.post(f"/api/tracking/watches/{gw}", json={"csrf": tok, "delete": True}).json()["ok"]
    aw = tracking.follow_author(registry, "https://openalex.org/A123", "Jane Doe")
    assert c.post(f"/api/tracking/watches/{aw}", json={"csrf": tok, "delete": True}).json()["ok"]


def test_openalex_parse_and_pmc_links():
    w = {"id": "https://openalex.org/W9", "doi": "https://doi.org/10.5555/ABC", "display_name": "T",
         "publication_date": "2026-11-01", "publication_year": 2026, "type": "preprint",
         "ids": {"pmcid": "https://www.ncbi.nlm.nih.gov/pmc/articles/123"},
         "abstract_inverted_index": {"world": [1], "hello": [0]},
         "best_oa_location": {"is_oa": True, "pdf_url": "https://e/x.pdf"},
         "referenced_works": ["https://openalex.org/W1"], "authorships": [{"author": {"display_name": "X Y"}}],
         "primary_location": {"source": {"display_name": "bioRxiv", "type": "repository"}}}
    r = tracking.OpenAlexSource.parse(w)
    assert r["doi"] == "10.5555/abc" and r["abstract"] == "hello world" and r["is_preprint"]
    assert r["oa_urls"] == ["pmc:PMC123", "https://e/x.pdf"] and r["references"] == ["W1"]


def test_query_parser_matches_like_the_search_engines():
    q = tracking.parse_query('("single-cell RNA-seq" OR scRNA-seq) AND (HCC OR "hepatocellular carcinoma")')
    assert tracking.query_matches(q, "an scrna-seq atlas of hcc")
    assert tracking.query_matches(q, "single-cell rna-seq of hepatocellular carcinoma")
    assert not tracking.query_matches(q, "an scrna-seq atlas of colon")
    assert not tracking.query_matches(tracking.parse_query("HCC"), "hcc1 cell line only")  # whole words
    assert tracking.parse_query("") is None


PUBMED_XML = """<?xml version="1.0"?><PubmedArticleSet>
<PubmedArticle><MedlineCitation><PMID>41000001</PMID><Article>
<Journal><Title>Hepatology</Title><JournalIssue><PubDate><Year>2026</Year><Month>Nov</Month></PubDate></JournalIssue></Journal>
<ArticleTitle>Spatial atlas of <i>fibroblast</i> niches in HCC.</ArticleTitle>
<Abstract><AbstractText Label="BACKGROUND">Fibroblasts matter.</AbstractText><AbstractText Label="RESULTS">Visium shows niches.</AbstractText></Abstract>
<AuthorList><Author><LastName>Li</LastName><ForeName>Wei</ForeName></Author></AuthorList>
<PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
<ELocationID EIdType="doi">10.1002/HEP.99999</ELocationID></Article></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="pubmed">41000001</ArticleId><ArticleId IdType="pmc">PMC1234567</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle>
<PubmedArticle><MedlineCitation><PMID>41000002</PMID><Article><Journal><Title>X</Title></Journal>
<ArticleTitle>Erratum: something</ArticleTitle><PublicationTypeList><PublicationType>Published Erratum</PublicationType></PublicationTypeList>
</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"""


def test_pubmed_fetch_uses_entrez_date_and_parses(config, registry):
    import httpx

    from papermap.metadata import MetadataClient

    seen = []

    def handler(req):
        seen.append(req.url)
        if "esearch" in req.url.path:
            return httpx.Response(200, json={"esearchresult": {"idlist": ["41000001", "41000002"]}})
        return httpx.Response(200, text=PUBMED_XML)

    md = MetadataClient(registry, transport=httpx.MockTransport(handler))
    src = tracking.PubMedSource(md)
    src.MIN_INTERVAL = 0
    recs = src.fetch(registry, {"kind": "field", "query": '"fibroblast niche" OR Visium'}, date(2026, 11, 17), 50)
    term = seen[0].params["term"]
    assert '"2026/11/17"[EDAT]' in term and '"fibroblast niche" OR Visium' in term
    assert len(recs) == 1  # the erratum is skipped
    r = recs[0]
    assert r["doi"] == "10.1002/hep.99999" and r["pmid"] == "41000001" and r["oa_urls"] == ["pmc:PMC1234567"]
    assert r["title"] == "Spatial atlas of fibroblast niches in HCC" and "RESULTS: Visium" in r["abstract"]
    assert r["authors"] == ["Wei Li"] and r["pub_date"] == "2026-11-01"


def test_biorxiv_window_matched_locally_and_preprint_dedup(config, registry, pdfs, monkeypatch):
    import httpx

    from papermap.metadata import MetadataClient

    calls = []
    page = {"messages": [{"status": "ok", "total": 3}], "collection": [
        {"doi": "10.1101/2026.11.01.111", "title": "Fibroblast niche in liver cancer by Visium", "authors": "Li, W.; Chen, X.",
         "date": "2026-11-18", "version": "1", "abstract": "Spatial.", "published": "NA"},
        {"doi": "10.1101/2026.11.01.111", "title": "Fibroblast niche in liver cancer by Visium", "authors": "Li, W.; Chen, X.",
         "date": "2026-11-20", "version": "2", "abstract": "Spatial v2.", "published": "10.1038/s41586-026-00001-1"},
        {"doi": "10.1101/2026.11.02.222", "title": "Plant roots", "authors": "Z, Z.", "date": "2026-11-19", "version": "1",
         "abstract": "Roots.", "published": "NA"}]}

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json=page if "/biorxiv/" in req.url.path else {"messages": [{"total": 0}], "collection": []})

    src = tracking.BioRxivSource(MetadataClient(registry, transport=httpx.MockTransport(handler), min_interval_s=0))
    w = {"kind": "gap", "query": '"fibroblast niche" OR Visium'}
    recs = src.fetch(registry, w, date(2026, 11, 17), 50)
    assert [r["doi"] for r in recs] == ["10.1101/2026.11.01.111"] and recs[0]["version"] == 2
    assert recs[0]["related_dois"] == ["10.1038/s41586-026-00001-1"] and recs[0]["authors"] == ["W. Li", "X. Chen"]
    n = len(calls)
    src.fetch(registry, {"kind": "field", "query": "roots"}, date(2026, 11, 17), 50)
    assert len(calls) == n  # one download per run, shared by every watch

    # the published version found by another source and the preprint end up as ONE candidate
    c, pipe, *_ = client(config, registry, pdfs)
    tracking.ensure_schema(registry)
    lib = tracking._library_keys(registry)  # noqa: SLF001
    wid = registry.execute("INSERT INTO track_watches(kind, ref, label, query, created_at) VALUES ('gap','x','x','x','t')").lastrowid
    cid1, _ = tracking.upsert(registry, rec("10.1038/s41586-026-00001-1", "Fibroblast niche in liver cancer (journal)"), wid, "openalex", lib)
    cid2, new = tracking.upsert(registry, recs[0], wid, "biorxiv", lib)
    assert cid1 == cid2 and not new
    # and a preprint whose published version is already in the library is not proposed at all
    lib[0].add("10.1038/s41586-026-00002-2")
    assert tracking.upsert(registry, {**recs[0], "doi": "10.1101/x", "related_dois": ["10.1038/s41586-026-00002-2"]}, wid, "biorxiv", lib) == (None, False)
