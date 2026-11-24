"""Exercise PaperMap's read-only endpoint with the official MCP client."""
import asyncio
import threading
from contextlib import asynccontextmanager

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from papermap import evidence, search
from papermap.web import mcp_api, plugin_api
from papermap.web.app import create_app

from .test_pipeline import make_pipeline

BASE = "http://127.0.0.1:9876"


@asynccontextmanager
async def local_http(config, registry, peer="127.0.0.1"):
    pipe, _ = make_pipeline(config, registry)
    app = create_app(config, registry, pipe)
    async with app.router.lifespan_context(app) as state:
        # ASGITransport does not inject lifespan state like an ASGI server does.
        async def asgi(scope, receive, send):
            await app({**scope, "state": dict(state)}, receive, send)

        transport = httpx.ASGITransport(app=asgi, client=(peer, 50000))
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as http:
            yield http


@asynccontextmanager
async def connected(config, registry):
    token = mcp_api.rotate_token(registry)
    async with local_http(config, registry) as http:
        http.headers["Authorization"] = "Bearer " + token
        async with streamable_http_client(BASE + "/mcp", http_client=http) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                info = await session.initialize()
                assert info.serverInfo.name == "PaperMap"
                yield session, http


async def call(session, name, **args):
    result = await session.call_tool(name, args)
    assert not result.isError, result.content
    return result.structuredContent


def test_mcp_read_tools(config, registry):
    first = registry.create_paper(title="First paper", status="done", doi="10.1/first",
                                  card_json={"methods": ["Imaging"]}, card_override="# Personal edit")
    second = registry.create_paper(title="Second paper", status="done", card_json={})
    removed = registry.create_paper(title="Removed", status="ignored", card_json={})
    qid = evidence.create_question(registry, "Is the pattern replicated?")
    evidence.create_question(registry, "Other question")
    search.ensure_schema(registry)
    for pid, stance in [(first, "supports"), (second, "opposes"), (removed, "opposes")]:
        registry.execute("INSERT INTO search_index(paper_id,kind,page,label,body) VALUES (?,?,?,?,?)",
                         (pid, "page", 3, "p.4", "Xenium in patients"))
        evidence.add_entry(registry, qid, "", pid, 3, "a" * 64, [[10, 20, 40, 35]],
                           "A passage from paper " + str(pid), "native", stance, "Checked")

    async def check():
        async with connected(config, registry) as (session, _):
            tools = (await session.list_tools()).tools
            assert {t.name for t in tools} == {"search_library", "get_paper_card", "list_research_questions",
                                               "get_question_evidence"}
            assert all(t.annotations.readOnlyHint for t in tools)
            assert all(str(registry.db_path) not in str(t) for t in tools)

            hit = await call(session, "search_library", query="Xenium", limit=1)
            assert hit["items"][0]["url"] == f"{BASE}/reader/{first}?page=4"
            rest = await call(session, "search_library", query="Xenium", offset=hit["next_offset"])
            assert [r["paper_id"] for r in rest["items"]] == [second] and rest["next_offset"] is None
            card = await call(session, "get_paper_card", paper_id=first)
            assert card["paper"]["title"] == "First paper" and card["generated_card"] == {"methods": ["Imaging"]}
            assert card["current_card_source"] == "edited_markdown" and card["edited_markdown"] == "# Personal edit"
            original = await call(session, "get_paper_card", paper_id=second)
            assert original["current_card_source"] == "generated_card" and original["edited_markdown"] is None
            assert (await session.call_tool("get_paper_card", {"paper_id": removed})).isError
            qs = await call(session, "list_research_questions", title_contains="replicated")
            assert qs["items"] == [{"id": qid, "title": "Is the pattern replicated?",
                                    "url": f"{BASE}/evidence?question={qid}"}]
            qs = await call(session, "list_research_questions", limit=1)
            assert qs["next_cursor"] == qid
            qs = await call(session, "list_research_questions", after=qid)
            assert qs["next_cursor"] is None and qs["items"][0]["title"] == "Other question"
            page1 = await call(session, "get_question_evidence", question_id=qid, limit=1)
            page2 = await call(session, "get_question_evidence", question_id=qid, after=page1["next_cursor"])
            assert page2["next_cursor"] is None and len(page2["papers"]) == 1
            opposing = await call(session, "get_question_evidence", question_id=qid, stance="opposes")
            assert opposing["papers"] == page2["papers"]
            item, = opposing["papers"][0]["evidence"]
            assert item["quote"].endswith(str(second)) and item["revision"] == "a" * 64
            assert item["page"] == 4 and item["url"] == f"{BASE}/reader/{second}?evidence={item['id']}&page=4"
            assert (await session.call_tool("import_pdf", {})).isError
            assert (await session.call_tool("get_question_evidence", {"question_id": qid, "limit": 0})).isError

    asyncio.run(check())


def test_mcp_local_read_authorization(config, registry):
    regular_token = plugin_api.rotate_token(registry)
    assert not mcp_api.token_enabled(registry)
    read_token = mcp_api.rotate_token(registry)
    digest, = registry.one("SELECT digest FROM mcp_read_credentials")
    assert len(digest) == 64 and read_token not in digest

    async def check():
        async with local_http(config, registry) as http:
            assert (await http.post("/mcp", json={})).status_code == 401
            http.headers["Authorization"] = "Bearer " + regular_token
            assert (await http.post("/mcp", json={})).status_code == 401
            http.headers["Authorization"] = "Bearer " + read_token
            assert (await http.get("/api/v1/papers")).status_code == 401
            assert (await http.post("/api/v1/import", content=b"%PDF-")).status_code == 401
            assert (await http.post("/mcp", headers={"Origin": "https://foreign.example"}, json={})).status_code == 403
            assert (await http.post("/mcp", headers={"Host": "foreign.example"}, json={})).status_code == 403
            replacement = mcp_api.rotate_token(registry)
            assert (await http.post("/mcp", json={})).status_code == 401
            mcp_api.revoke_token(registry)
            http.headers["Authorization"] = "Bearer " + replacement
            assert (await http.post("/mcp", json={})).status_code == 401
            assert not mcp_api.token_enabled(registry)
            http.headers["Authorization"] = "Bearer " + regular_token
            assert (await http.get("/api/v1/papers")).status_code == 200

        async with local_http(config, registry, peer="192.0.2.1") as http:
            assert (await http.post("/mcp", json={})).status_code == 403

    asyncio.run(check())


def test_unindexed_search_and_readonly_connection(config, registry, monkeypatch):
    def try_to_write(library, *args, **kwargs):
        library.execute("CREATE TABLE unexpected_write(id INTEGER)")

    def internal_error(*args, **kwargs):
        raise OSError(f"Private data at {registry.db_path}")

    async def check():
        async with connected(config, registry) as (session, _):
            has_index = "SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_index'"
            assert registry.one(has_index) is None
            assert await call(session, "search_library", query="anything") == {"items": [], "next_offset": None}
            assert registry.one(has_index) is None
            monkeypatch.setattr(search, "search", try_to_write)
            result = await session.call_tool("search_library", {"query": "anything"})
            assert result.isError and registry.one("SELECT 1 FROM sqlite_master WHERE name='unexpected_write'") is None
            monkeypatch.setattr(search, "search", internal_error)
            error = await session.call_tool("search_library", {"query": "anything"})
            assert error.isError and "Library data could not be read" in error.content[0].text
            assert str(registry.db_path) not in error.content[0].text
            assert (await call(session, "list_research_questions"))["items"] == []

    asyncio.run(check())


def test_concurrent_tools_keep_papermap_responsive(config, registry, monkeypatch):
    qid = evidence.create_question(registry, "Concurrent reads")
    searching, comparing, release = threading.Event(), threading.Event(), threading.Event()
    original_search, original_comparison = search.search, evidence.question_comparison

    def held(fn, started):
        def read(*args, **kwargs):
            started.set()
            assert release.wait(5), "the web request must finish while these tools are still reading"
            return fn(*args, **kwargs)
        return read

    monkeypatch.setattr(search, "search", held(original_search, searching))
    monkeypatch.setattr(evidence, "question_comparison", held(original_comparison, comparing))

    async def check():
        async with connected(config, registry) as (session, http):
            tasks = [call(session, "search_library", query="test"), call(session, "get_question_evidence", question_id=qid)]
            # More MCP requests than the four worker slots, sharing a single client session.
            results = asyncio.gather(*tasks, *(call(session, "list_research_questions") for _ in range(6)))
            try:
                assert await asyncio.to_thread(searching.wait, 3)
                assert await asyncio.to_thread(comparing.wait, 3)
                page = await asyncio.wait_for(http.get("/"), timeout=3)
                assert page.status_code == 200
            finally:
                release.set()
                responses = await results
            assert responses[0]["items"] == [] and responses[1]["title"] == "Concurrent reads"
            assert all(r["items"][0]["id"] == qid for r in responses[2:])

    asyncio.run(check())
