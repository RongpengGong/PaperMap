import httpx
import pytest

from papermap.metadata import MetadataClient, MetadataUnavailable

CR = {"message": {"DOI": "10.1/X", "title": ["A title"], "author": [{"given": "Ann", "family": "Lee"}],
                  "issued": {"date-parts": [[2023, 5]]}, "container-title": ["Hepatology"],
                  "type": "journal-article", "relation": {"has-preprint": [{"id-type": "doi", "id": "10.1101/2022.01.01.1"}]}}}
OA = {"id": "https://openalex.org/W123", "doi": "https://doi.org/10.1/x", "title": "A title",
      "publication_year": 2023, "topics": [{"display_name": "Liver Cancer"}],
      "abstract_inverted_index": {"Hello": [0], "world": [1]}}


def make_client(registry, handler):
    return MetadataClient(registry, transport=httpx.MockTransport(handler), min_interval_s=0)


def test_lookup_merges_and_caches(registry):
    calls = []

    def handler(req: httpx.Request):
        calls.append(str(req.url))
        if "crossref" in req.url.host:
            return httpx.Response(200, json=CR)
        return httpx.Response(200, json=OA)

    c = make_client(registry, handler)
    meta = c.lookup("10.1/x")
    assert meta["title"] == "A title" and meta["year"] == 2023 and meta["openalex_id"] == "W123"
    assert meta["relations"]["has-preprint"] == ["10.1101/2022.01.01.1"]
    assert meta["abstract"] == "Hello world"
    assert meta["authors"][0]["family"] == "Lee"
    n = len(calls)
    c.lookup("10.1/x")
    assert len(calls) == n  # served from SQLite cache


def test_404_returns_none(registry):
    c = make_client(registry, lambda req: httpx.Response(404))
    assert c.lookup("10.1/missing") is None


def test_retry_then_unavailable(registry, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    seen = {"n": 0}

    def handler(req):
        seen["n"] += 1
        return httpx.Response(503)

    c = make_client(registry, handler)
    with pytest.raises(MetadataUnavailable):
        c.crossref_work("10.1/x")
    assert seen["n"] == 4  # 1 + 3 retries


def test_retry_recovers(registry, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    seq = [httpx.Response(429), httpx.Response(200, json=CR)]
    c = make_client(registry, lambda req: seq.pop(0))
    assert c.crossref_work("10.1/x")["journal"] == "Hepatology"
