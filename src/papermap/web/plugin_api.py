"""Versioned loopback API for external integrations with a user-issued bearer token."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import secrets
import time

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .. import search
from ..config import Config
from ..extension_events import event_page
from ..pdf_import import publish_pdf, staged_file
from ..pipeline import Pipeline
from ..registry import Registry

MAX_UPLOAD = 256 * 1024 * 1024
MAX_ID = (1 << 63) - 1
PAPER_FIELDS = ("id", "doi", "title", "journal", "year", "first_author", "status", "updated_at")


def rotate_token(registry: Registry) -> str:
    """Issue a token once. The registry only stores its SHA-256 digest."""
    token = "pm_" + secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    registry.execute("INSERT OR REPLACE INTO extension_api_credentials(id,digest) VALUES (1,?)", (digest,))
    return token


def revoke_token(registry: Registry) -> None:
    registry.execute("DELETE FROM extension_api_credentials WHERE id=1")


def token_enabled(registry: Registry) -> bool:
    return registry.one("SELECT id FROM extension_api_credentials WHERE id=1") is not None


def _matches(registry: Registry, token: str) -> bool:
    row = registry.one("SELECT digest FROM extension_api_credentials WHERE id=1")
    digest = hashlib.sha256(token.encode()).hexdigest()
    return bool(row and secrets.compare_digest(digest, row["digest"]))


def mount_api(app: FastAPI, config: Config, registry: Registry, pipeline: Pipeline, background) -> None:
    def authorize(request: Request) -> str:
        try:
            peer = ipaddress.ip_address(request.client.host) if request.client else None
        except ValueError:
            peer = None
        if peer is None or not peer.is_loopback or request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise HTTPException(403, "loopback_required")
        origin = request.headers.get("origin")
        if origin is not None and origin != f"{request.url.scheme}://{request.headers.get('host', '')}":
            raise HTTPException(403, "cross_origin")
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token or not _matches(registry, token):
            raise HTTPException(401, "invalid_token", headers={"WWW-Authenticate": "Bearer"})
        return token

    router = APIRouter(prefix="/api/v1", dependencies=[Depends(authorize)])

    @router.get("/papers")
    def papers(after: int = Query(0, ge=0, le=MAX_ID), limit: int = Query(50, ge=1, le=100)):
        rows = registry.query("SELECT " + ",".join(PAPER_FIELDS) +
                              " FROM papers WHERE id>? AND status!='ignored' ORDER BY id LIMIT ?", (after, limit))
        return {"items": [dict(row) for row in rows], "next_cursor": rows[-1]["id"] if rows else after}

    def find_paper(paper_id: int):
        row = registry.get_paper(paper_id) if 0 < paper_id <= MAX_ID else None
        if row is None or row["status"] == "ignored":
            raise HTTPException(404, "paper_not_found")
        return row

    @router.get("/papers/{paper_id}")
    def paper(paper_id: int):
        row = find_paper(paper_id)
        return {key: row[key] for key in PAPER_FIELDS}

    @router.get("/papers/{paper_id}/card")
    def card(paper_id: int):
        row = find_paper(paper_id)
        if not row["card_json"]:
            raise HTTPException(404, "card_not_ready")
        return {"paper_id": paper_id, "card": json.loads(row["card_json"])}

    @router.get("/search")
    def fulltext(q: str = Query(min_length=1, max_length=256), limit: int = Query(50, ge=1, le=100)):
        hits = search.search(registry, q, limit=limit)
        return {"items": [{"paper_id": hit["paper_id"], "kind": hit["kind"], "label": hit["label"],
                           "page": hit["page"] + 1 if hit["page"] is not None else None,
                           "snippet": hit["snippet"]} for hit in hits]}

    @router.get("/events")
    def events(after: int = Query(0, ge=0, le=MAX_ID), limit: int = Query(50, ge=1, le=100)):
        items = event_page(registry, after, limit)
        return {"items": items, "next_cursor": items[-1]["id"] if items else after}

    @router.get("/events/stream")
    async def stream(request: Request, token: str = Depends(authorize),
                     after: int = Query(0, ge=0, le=MAX_ID), timeout: int = Query(30, ge=1, le=60)):
        cursor = after
        if "last-event-id" in request.headers:
            header = request.headers["last-event-id"]
            if not header.isascii() or not header.isdecimal() or len(header) > 19 or int(header) > MAX_ID:
                raise HTTPException(422, "invalid_last_event_id")
            cursor = max(after, int(header))

        async def feed():
            nonlocal cursor
            deadline = time.monotonic() + timeout
            yield ": PaperMap v1\nretry: 1000\n\n"
            while time.monotonic() < deadline and not await request.is_disconnected() and _matches(registry, token):
                items = event_page(registry, cursor, 100)
                for item in items:
                    cursor = item["id"]
                    yield f"id: {cursor}\nevent: {item['type']}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                if len(items) < 100:
                    await asyncio.sleep(0.25)

        return StreamingResponse(feed(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @router.post("/import", status_code=202)
    async def import_pdf(request: Request, filename: str = Query("paper.pdf", min_length=1, max_length=240)):
        if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/pdf":
            raise HTTPException(415, "expected_application_pdf")
        if not filename.lower().endswith(".pdf"):
            raise HTTPException(422, "invalid_filename")
        length = request.headers.get("content-length")
        if length and (not length.isdecimal() or len(length) > 10 or int(length) > MAX_UPLOAD):
            raise HTTPException(413, "pdf_too_large")
        total = 0
        with staged_file(config.imported_dir) as tmp:
            with tmp.open("w+b") as output:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > MAX_UPLOAD:
                        raise HTTPException(413, "pdf_too_large")
                    output.write(chunk)
                output.seek(0)
                if b"%PDF-" not in output.read(1024):
                    raise HTTPException(422, "invalid_pdf")
            dest = publish_pdf(tmp, filename)
        registry.log("info", "Imported a PDF via the local API")
        background("api-import-" + secrets.token_hex(6), pipeline.handle_path, dest)
        return {"accepted": True, "filename": dest.name}

    app.include_router(router)
