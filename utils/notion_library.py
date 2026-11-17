"""Private, explicitly allowlisted Notion publishing for Garden readers.

Only the API worker sees Notion credentials. Never crawl child pages implicitly:
each page needs its own audience, even when its parent is shared.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from uuid import UUID

import requests

from utils.settings import settings_database_path
from utils.sqlite import AutoClosingSQLiteConnection, configure_sync_connection

AUDIENCES = {"member", "scout", "captain", "ranger", "admin", "owner"}
DEFAULT_MANIFEST = Path(__file__).resolve().parent.parent / "config/notion-library.toml"
SYNC_LOCK = threading.Lock()


def sources():
    import tomllib
    path = Path(os.getenv("NOTION_LIBRARY_MANIFEST", str(DEFAULT_MANIFEST)))
    items = tomllib.loads(path.read_text(encoding="utf-8")).get("articles", [])
    seen = set()
    for item in items:
        item["page_id"] = str(UUID(item["page_id"]))
        if item["page_id"] in seen or item["audience"] not in AUDIENCES:
            raise ValueError("Invalid or duplicate library source")
        seen.add(item["page_id"])
    return items


def connect():
    return configure_sync_connection(sqlite3.connect(
        settings_database_path(), timeout=30, factory=AutoClosingSQLiteConnection
    ))


def initialize():
    with connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS notion_library_articles (
                page_id TEXT PRIMARY KEY, title TEXT NOT NULL,
                content TEXT NOT NULL, search_text TEXT NOT NULL,
                synced_at REAL NOT NULL, source_edited_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notion_library_status (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                attempted_at REAL, succeeded_at REAL, error TEXT
            );
        """)


def allowed(source, permissions):
    return "library." + source["audience"] in permissions


def articles(permissions, query=""):
    initialize()
    # The manifest is authoritative at read time: removals and audience changes
    # apply immediately, including while the sync worker is offline.
    visible = {s["page_id"]: s for s in sources() if allowed(s, permissions)}
    with connect() as db:
        rows = db.execute("SELECT * FROM notion_library_articles ORDER BY title COLLATE NOCASE").fetchall()
    result = []
    for row in rows:
        if row["page_id"] not in visible:
            continue
        if query and query.casefold() not in (row["title"] + " " + row["search_text"]).casefold():
            continue
        result.append(dict(row, audience=visible[row["page_id"]]["audience"]))
    return result


def sync_status():
    initialize()
    with connect() as db:
        row = db.execute("SELECT * FROM notion_library_status WHERE singleton=1").fetchone()
    return dict(row) if row else {}


class NotionUnavailable(Exception):
    pass


class NotionClient:
    def __init__(self, token):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": "Bearer " + token,
                                     "Notion-Version": "2025-09-03"})

    def get(self, path, params=None):
        for attempt in range(3):
            response = self.session.get("https://api.notion.com/v1/" + path,
                                        params=params, timeout=30, allow_redirects=False)
            if response.status_code == 429 and attempt < 2:
                try:
                    delay = min(30, max(1, float(response.headers.get("Retry-After", "1"))))
                except ValueError:
                    delay = 1
                time.sleep(delay)
                continue
            if response.status_code != 200:
                # No response bodies, request headers, or signed URLs in logs/UI.
                raise NotionUnavailable(f"Notion returned HTTP {response.status_code}")
            return response.json()
        raise NotionUnavailable("Notion rate limit")

    def blocks(self, page_id, budget, depth=0):
        if depth > 16:
            raise NotionUnavailable("Notion nesting limit exceeded")
        result, cursor = [], None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = self.get(f"blocks/{page_id}/children", params)
            for block in data["results"]:
                budget[0] -= 1
                if budget[0] < 0:
                    raise NotionUnavailable("Notion page size limit exceeded")
                kind = block["type"]
                # Child pages, databases, synced blocks and file URLs never
                # inherit this page's audience or become publicly accessible.
                if kind in {"child_page", "child_database", "synced_block", "link_to_page"}:
                    continue
                payload = block.get(kind, {})
                node = {"type": kind, "text": "".join(
                    r.get("plain_text", r.get("text", {}).get("content", ""))
                    for r in payload.get("rich_text", [])
                )}
                if kind == "table_row":
                    node["cells"] = ["".join(r.get("plain_text", r.get("text", {}).get("content", "")) for r in cell)
                                     for cell in payload.get("cells", [])]
                if kind in {"image", "file", "pdf", "video", "audio", "embed", "bookmark"}:
                    node["text"] = "Media is available to authorised editors in Notion."
                if kind == "to_do":
                    node["text"] = ("☑ " if payload.get("checked") else "☐ ") + node["text"]
                if block.get("has_children"):
                    node["children"] = self.blocks(block["id"], budget, depth + 1)
                result.append(node)
            if not data.get("has_more"):
                return result
            cursor = data.get("next_cursor")
            if not cursor:
                raise NotionUnavailable("Incomplete Notion pagination")


def plain_text(nodes):
    return "\n".join(n.get("text", "") + " " + " ".join(n.get("cells", []))
                     + " " + plain_text(n.get("children", [])) for n in nodes)


def sync_library(client=None):
    """Atomic per-page replacement; inaccessible sources are withdrawn.

    Transient failures retain the last complete copy. A 403/404/archive removes
    that page's copy. Every failure is reported without private response text.
    """
    if not SYNC_LOCK.acquire(blocking=False):
        return {"busy": True}
    try:
        initialize()
        selected = sources()
        token = os.getenv("NOTION_LIBRARY_TOKEN", "").strip()
        if client is None:
            if not token:
                raise NotionUnavailable("NOTION_LIBRARY_TOKEN is not configured")
            client = NotionClient(token)
        errors, count = [], 0
        for source in selected:
            page_id = source["page_id"]
            try:
                page = client.get("pages/" + page_id)
                if page.get("archived") or page.get("in_trash"):
                    raise NotionUnavailable("Notion returned HTTP 404")
                title = "".join(r.get("plain_text", r.get("text", {}).get("content", ""))
                                for prop in page.get("properties", {}).values()
                                if prop.get("type") == "title" for r in prop.get("title", []))
                nodes = client.blocks(page_id, [3000])
                with connect() as db:
                    db.execute("INSERT OR REPLACE INTO notion_library_articles VALUES (?,?,?,?,?,?)",
                               (page_id, title or "Untitled", json.dumps(nodes), plain_text(nodes),
                                time.time(), page.get("last_edited_time", "")))
                count += 1
            except (NotionUnavailable, requests.RequestException, ValueError, KeyError) as exc:
                reason = str(exc) if isinstance(exc, NotionUnavailable) else "Notion sync failed"
                errors.append(reason)
                if reason in {"Notion returned HTTP 403", "Notion returned HTTP 404"}:
                    with connect() as db:
                        db.execute("DELETE FROM notion_library_articles WHERE page_id=?", (page_id,))
        with connect() as db:
            ids = {s["page_id"] for s in selected}
            for row in db.execute("SELECT page_id FROM notion_library_articles").fetchall():
                if row[0] not in ids:
                    db.execute("DELETE FROM notion_library_articles WHERE page_id=?", (row[0],))
            db.execute("INSERT INTO notion_library_status VALUES(1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET attempted_at=excluded.attempted_at, succeeded_at=COALESCE(excluded.succeeded_at,succeeded_at),error=excluded.error",
                       (time.time(), None if errors else time.time(), "; ".join(errors) or None))
        return {"synced": count, "failed": len(errors)}
    finally:
        SYNC_LOCK.release()
