import httpx
from fastapi.testclient import TestClient

from papermap import citations as ci
from papermap.metadata import MetadataClient
from papermap.web.app import create_app

from .test_newfields import add_paper
from .test_pipeline import make_pipeline
from .test_web import _token


def openalex(req: httpx.Request):
    url = str(req.url)
    if "/works/doi:10.1/p0" in url:
        return httpx.Response(200, json={"id": "https://openalex.org/W100", "cited_by_count": 3,
                                         "referenced_works": ["https://openalex.org/W101", "https://openalex.org/W900"]})
    if "/works/doi:10.1/p1" in url:
        return httpx.Response(200, json={"id": "https://openalex.org/W101", "cited_by_count": 1,
                                         "referenced_works": ["https://openalex.org/W900", "https://openalex.org/W901"]})
    if "filter=openalex_id" in url:
        return httpx.Response(200, json={"results": [
            {"id": "https://openalex.org/W900", "doi": "https://doi.org/10.9/key", "title": "Key method paper",
             "publication_year": 2017, "cited_by_count": 9000, "authorships": [{"author": {"display_name": "Grace Zheng"}}]},
            {"id": "https://openalex.org/W101", "doi": "https://doi.org/10.1/p1", "title": "P1", "publication_year": 2021}]})
    if "filter=cites" in url:
        return httpx.Response(200, json={"results": [{"id": "https://openalex.org/W555", "title": "New citing work",
                                                      "publication_year": 2026, "doi": "https://doi.org/10.5/new"}]})
    return httpx.Response(404)


def test_network_missing_and_citing(config, registry):
    a, b = add_paper(registry, 0), add_paper(registry, 1)
    client = MetadataClient(registry, transport=httpx.MockTransport(openalex), min_interval_s=0)
    assert ci.fetch_missing(registry, client) == 2
    assert ci.library_edges(registry) == [(a, b)]          # p0 cites p1 (both in library)
    miss = ci.missing_key_papers(registry, 2)
    assert [m["ref_id"] for m in miss] == ["W900"] and miss[0]["first_author"] == "Zheng" and sorted(miss[0]["citing"]) == [a, b]
    works = ci.citing_works(registry, client, a)
    assert works[0]["title"] == "New citing work" and works[0]["in_library"] is None
    registry.execute("INSERT INTO dismissed_works VALUES ('W900', 'x')")
    assert ci.missing_key_papers(registry, 2) == []


def test_citations_page_and_graph(config, registry):
    pipe, _ = make_pipeline(config, registry)
    a, b = add_paper(registry, 0), add_paper(registry, 1)
    client = MetadataClient(registry, transport=httpx.MockTransport(openalex), min_interval_s=0)
    ci.fetch_missing(registry, client)
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    page = c.get("/citations").text
    assert "Key method paper" in page and "引用了" in page
    assert c.post("/api/citations/dismiss", json={"csrf": _token(c), "openalex_id": "W900"}).json()["ok"]
    assert "Key method paper" not in c.get("/citations").text
    assert "cite_edges" in c.get("/api/graph").json()
