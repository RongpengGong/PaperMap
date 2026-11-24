"""Durable extension events emitted atomically with their source transitions."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import Registry

SCHEMA = """
CREATE TABLE IF NOT EXISTS extension_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    object_id INTEGER NOT NULL,
    slug TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS mcp_read_credentials (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS extension_api_credentials (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    digest TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS extension_paper_ready_insert
AFTER INSERT ON papers WHEN NEW.status = 'done' AND NEW.card_json IS NOT NULL
BEGIN
    INSERT INTO extension_events(type,object_id) VALUES ('paper.ready', NEW.id);
END;
CREATE TRIGGER IF NOT EXISTS extension_paper_ready_update
AFTER UPDATE OF status,card_json ON papers
WHEN NEW.status = 'done' AND NEW.card_json IS NOT NULL
    AND (OLD.status != 'done' OR OLD.card_json IS NULL)
BEGIN
    INSERT INTO extension_events(type,object_id) VALUES ('paper.ready', NEW.id);
END;
CREATE TRIGGER IF NOT EXISTS extension_field_proposed
AFTER UPDATE OF ready_notified ON pending_fields
WHEN NEW.ready_notified = 1 AND OLD.ready_notified = 0 AND NEW.status = 'open'
BEGIN
    INSERT INTO extension_events(type,object_id,slug) VALUES ('field.proposed', NEW.id, NEW.slug);
END;
"""


def latest_event_id(registry: Registry) -> int:
    return int(registry.one("SELECT COALESCE(MAX(id), 0) AS latest FROM extension_events")["latest"])


def event_page(registry: Registry, after: int, limit: int) -> list[dict]:
    rows = registry.query("SELECT * FROM extension_events WHERE id > ? ORDER BY id LIMIT ?", (after, limit))
    events = []
    for row in rows:
        data = ({"paper_id": row["object_id"]} if row["type"] == "paper.ready"
                else {"field_id": row["object_id"], "slug": row["slug"]})
        events.append({"id": row["id"], "type": row["type"], "created_at": row["created_at"], "data": data})
    return events
