"""Read-only MCP tools for a running PaperMap library, served over loopback HTTP."""
from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager, closing, contextmanager
from functools import partial, wraps
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
from fastapi import HTTPException, Request
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .. import evidence, search
from ..registry import Registry
from .plugin_api import LocalToken, MAX_ID, PAPER_FIELDS, check_local_request

_tokens = LocalToken("mcp_read_credentials", "pm_read_")
rotate_token = _tokens.rotate
revoke_token = _tokens.revoke
token_enabled = _tokens.enabled

PageSize = Annotated[int, Field(ge=1, le=50)]
After = Annotated[int, Field(ge=0, le=MAX_ID)]
Identifier = Annotated[int, Field(ge=1, le=MAX_ID)]
Query = Annotated[str, Field(min_length=1, max_length=256)]
Stance = Literal["all", "supports", "opposes", "pending"]


def _base(ctx: Context) -> str:
    # The transport accepts only a loopback host and peer. Never put the bearer
    # token or other request headers in a link returned to a model.
    return str(ctx.request_context.request.base_url).rstrip("/")


class _LibraryReader(sqlite3.Connection):
    """The two read operations used by search and research question comparisons."""

    def one(self, sql, params=()):
        return self.execute(sql, params).fetchone()

    def query(self, sql, params=()):
        return self.execute(sql, params).fetchall()


def _server(db_path: Path) -> FastMCP:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    workers = anyio.CapacityLimiter(4)

    @contextmanager
    def open_library():
        # Each call gets its own read-only connection and consistent snapshot.
        with closing(sqlite3.connect(uri, uri=True, factory=_LibraryReader)) as reader:
            reader.row_factory = sqlite3.Row
            reader.execute("PRAGMA query_only=ON")
            reader.execute("BEGIN")
            yield reader

    server = FastMCP("PaperMap", stateless_http=True, json_response=True,
                     instructions="Read the user's local research library. Cite evidence by its quote and reader URL. "
                                  "Card summaries and search snippets are context, not verified quotations.")
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    def read_tool(fn):
        @wraps(fn)
        async def run(*args, **kwargs):
            try:
                return await anyio.to_thread.run_sync(partial(fn, *args, **kwargs), limiter=workers)
            except ToolError:
                raise
            except Exception:
                # Database/OS diagnostics may contain private paths or data.
                raise ToolError("Library data could not be read") from None

        return server.tool(annotations=read_only)(run)

    @read_tool
    def search_library(query: Query, ctx: Context, offset: After = 0, limit: PageSize = 15) -> dict[str, Any]:
        """Search indexed PDF pages, cards, notes and translations. Pages are one-based; follow next_offset."""
        with open_library() as registry:
            hits = search.search(registry, query, limit=limit + 1, offset=offset, visible_only=True)
            base = _base(ctx)
            page_hits = hits[:limit]
            paper_ids = sorted({hit["paper_id"] for hit in page_hits})
            papers = {}
            if paper_ids:
                placeholders = ",".join("?" for _ in paper_ids)
                rows = registry.query(f"SELECT id,title,doi FROM papers WHERE id IN ({placeholders}) "
                                      "AND status != 'ignored'", tuple(paper_ids))
                papers = {row["id"]: row for row in rows}
            items = []
            for hit in page_hits:
                pid = hit["paper_id"]
                if pid not in papers:
                    continue
                page = hit["page"] + 1 if hit["page"] is not None else None
                path = f"/reader/{pid}?page={page}" if page else f"/paper/{pid}"
                items.append({"paper_id": pid, "title": papers[pid]["title"], "doi": papers[pid]["doi"],
                              "kind": hit["kind"], "page": page, "snippet": hit["snippet"], "url": base + path})
            return {"items": items, "next_offset": offset + limit if len(hits) > limit else None}

    @read_tool
    def get_paper_card(paper_id: Identifier, ctx: Context) -> dict[str, Any]:
        """Read a paper's current card. edited_markdown takes precedence over the original generated_card."""
        with open_library() as registry:
            row = registry.one("SELECT " + ",".join(PAPER_FIELDS) + ",card_json,card_override "
                               "FROM papers WHERE id=? AND status!='ignored'", (paper_id,))
            if row is None:
                raise ToolError("Paper not found")
            edited = row["card_override"]
            generated = json.loads(row["card_json"]) if row["card_json"] else None
            source = "edited_markdown" if edited else ("generated_card" if generated is not None else None)
            return {"paper": {k: row[k] for k in PAPER_FIELDS}, "url": f"{_base(ctx)}/paper/{paper_id}",
                    "current_card_source": source, "edited_markdown": edited, "generated_card": generated}

    @read_tool
    def list_research_questions(ctx: Context, title_contains: Annotated[str, Field(max_length=256)] = "",
                                after: After = 0, limit: PageSize = 25) -> dict[str, Any]:
        """Find confirmed research questions by title. Pass next_cursor as after to get another page."""
        with open_library() as registry:
            rows = registry.query("SELECT id,title FROM evidence_questions WHERE status='active' AND id>? "
                                  "AND instr(lower(title), lower(?))>0 ORDER BY id LIMIT ?",
                                  (after, title_contains.strip(), limit + 1))
            base = _base(ctx)
            items = [{"id": row["id"], "title": row["title"], "url": f"{base}/evidence?question={row['id']}"}
                     for row in rows[:limit]]
            return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}

    @read_tool
    def get_question_evidence(question_id: Identifier, ctx: Context, stance: Stance = "all",
                              after: After = 0, limit: PageSize = 15) -> dict[str, Any]:
        """Read saved source excerpts and the user's judgment. Filter for opposes to find counterevidence.
        Each excerpt has a one-based PDF page and a revision-aware URL. Card context is unverified.
        Pass next_cursor as after for the next batch of excerpts."""
        with open_library() as registry:
            data = evidence.question_comparison(registry, question_id)
            if data is None:
                raise ToolError("Research question not found")
            candidate = [(entry, paper) for paper in data["papers"] for entry in paper["evidence"]
                         if entry["id"] > after and (stance == "all" or entry["stance"] == stance)]
            candidate.sort(key=lambda pair: pair[0]["id"])
            base = _base(ctx)
            groups = {}
            for entry, paper in candidate[:limit]:
                pid = paper["paper_id"]
                if pid not in groups:
                    groups[pid] = {k: paper[k] for k in ("paper_id", "title", "doi", "first_author", "year", "card_context")}
                    groups[pid]["evidence"] = []
                groups[pid]["evidence"].append({**entry, "url": base + entry["url"]})
            return {"id": data["id"], "title": data["title"], "judgment": data["judgment"], "gaps": data["gaps"],
                    "url": f"{base}/evidence?question={question_id}", "papers": list(groups.values()),
                    "next_cursor": candidate[limit-1][0]["id"] if len(candidate) > limit else None}

    return server


@asynccontextmanager
async def lifespan(registry: Registry):
    """A fresh SDK session manager for each ASGI lifespan."""
    server = _server(registry.db_path)
    transport = server.streamable_http_app()
    async with server.session_manager.run():
        yield transport


class ReadOnlyEndpoint:
    """Authenticate each HTTP request, including requests after token rotation."""

    def __init__(self, registry: Registry):
        self.registry = registry

    async def __call__(self, scope, receive, send):
        request = Request(scope)
        check_local_request(request)
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if (scheme.lower() != "bearer" or not token
                or not await anyio.to_thread.run_sync(_tokens.matches, self.registry, token)):
            raise HTTPException(401, "invalid_mcp_token", headers={"WWW-Authenticate": 'Bearer realm="PaperMap MCP"'})
        await request.state.papermap_mcp(scope, receive, send)
