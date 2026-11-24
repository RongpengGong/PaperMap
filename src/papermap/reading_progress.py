"""Durable reader bookmarks. Page numbers are zero-based; offsets are page fractions."""
from __future__ import annotations

import math
import time

from .registry import Registry


def recent(registry: Registry) -> list[dict]:
    rows = registry.query(
        """SELECT id, title, reading_page AS page FROM papers
           WHERE reading_at IS NOT NULL AND read_status='reading' AND status!='ignored'
           ORDER BY reading_at DESC, id DESC LIMIT 3"""
    )
    return [dict(row) for row in rows]


def save(registry: Registry, paper_id: int, body: dict, page_count: int, supplement_id: int | None = None) -> bool:
    page, offset, at = (body.get(key) for key in ("page", "offset", "at"))
    if type(page) is not int or not 0 <= page < page_count:
        raise ValueError("页码超出当前 PDF 的范围。")
    if type(offset) not in (float, int) or not 0 <= offset <= 1 or not math.isfinite(offset):
        raise ValueError("页内位置必须在 0 和 1 之间。")
    # Timestamp the original interaction, not a delayed debounce or pagehide request.
    # This also orders updates from an embedded preview and a separate reader window.
    if type(at) is not int or not 0 < at <= time.time_ns() // 1000 + 60_000_000:
        raise ValueError("阅读时间无效。")
    if supplement_id is not None:
        return bool(registry.execute(
            "UPDATE paper_supplements SET reading_page=?,reading_offset=?,reading_at=? "
            "WHERE id=? AND paper_id=? AND (reading_at IS NULL OR reading_at < ?)",
            (page, offset, at, supplement_id, paper_id, at),
        ).rowcount)
    return bool(registry.execute(
        """UPDATE papers SET reading_page=?, reading_offset=?, reading_at=?,
                 read_status=CASE read_status WHEN 'unread' THEN 'reading' ELSE read_status END
           WHERE id=? AND (reading_at IS NULL OR reading_at < ?)""",
        (page, offset, at, paper_id, at),
    ).rowcount)
