"""Allowlisted file and audit helpers for the local Knowledge Manager."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.knowledge import reload_knowledge
from utils.privacy import redact_sensitive_text
from utils.settings import settings_database_path
from utils.sqlite import configure_sync_connection


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_DOCUMENT_BYTES = 1024 * 1024
ALLOWED_EXTENSIONS = {".md", ".txt"}


@dataclass(frozen=True)
class KnowledgeDocument:
    doc_key: str
    display_name: str
    relative_path: str
    category: str
    editable: bool
    visibility: str
    description: str
    reindex_supported: bool = False


DOCUMENTS = (
    KnowledgeDocument(
        "message-context",
        "Message Context Guide",
        "docs/message-context.md",
        "Message Context",
        True,
        "internal",
        "Internal operating guide for the private message-context system.",
    ),
    KnowledgeDocument(
        "staff-context",
        "Staff Context Guide",
        "docs/staff-context.md",
        "Staff Knowledge",
        True,
        "staff",
        "Private operating guide for staff context and staff AI.",
    ),
    KnowledgeDocument(
        "checklists",
        "Checklist Guide",
        "docs/checklists.md",
        "Bot Docs",
        False,
        "internal",
        "Reference documentation for the Discord checklist workflow.",
    ),
    KnowledgeDocument(
        "historical-imports",
        "Historical Imports Guide",
        "docs/historical-imports.md",
        "Import Docs",
        False,
        "internal",
        "Reference documentation for terminal-only historical imports.",
    ),
    KnowledgeDocument(
        "vc-log-imports",
        "VC Log Imports Guide",
        "docs/vc-log-imports.md",
        "Import Docs",
        False,
        "internal",
        "Reference documentation for terminal-only VC log imports.",
    ),
    KnowledgeDocument(
        "codebase-map",
        "Codebase Map",
        "docs/codebase-map.md",
        "Bot Docs",
        False,
        "internal",
        "Internal architecture map maintained with source changes.",
    ),
)
DOCUMENT_BY_KEY = {document.doc_key: document for document in DOCUMENTS}


def _connect() -> sqlite3.Connection:
    path = settings_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    return configure_sync_connection(connection)


def initialize_knowledge_schema() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                requested_by TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                processed_at TEXT,
                result_message TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_key TEXT NOT NULL,
                action TEXT NOT NULL,
                path TEXT NOT NULL,
                changed_by TEXT,
                old_size INTEGER,
                new_size INTEGER,
                backup_path TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_knowledge_audit_created
            ON knowledge_audit (created_at DESC, id DESC)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_dashboard_actions_pending
            ON dashboard_actions (status, action_type, id)
            """
        )
        connection.commit()


def get_document_definition(doc_key: str) -> KnowledgeDocument:
    document = DOCUMENT_BY_KEY.get(str(doc_key))
    if document is None:
        raise KeyError("Knowledge document was not found.")
    return document


def _document_path(document: KnowledgeDocument) -> Path:
    root = PROJECT_ROOT.resolve()
    candidate = PROJECT_ROOT / document.relative_path
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("Knowledge document path leaves the project directory.") from exc
    if candidate.is_symlink() and resolved != candidate.absolute():
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("Knowledge document symlink leaves the project directory.") from exc
    return resolved


def _read_text(path: Path) -> tuple[str, str | None]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return "", None
    except OSError as exc:
        return "", str(exc)
    if b"\x00" in raw:
        return "", "Binary content cannot be previewed."
    try:
        return redact_sensitive_text(raw.decode("utf-8")), None
    except UnicodeDecodeError:
        return "", "The document is not valid UTF-8 text."


def document_details(doc_key: str) -> dict[str, Any]:
    document = get_document_definition(doc_key)
    path = _document_path(document)
    base = asdict(document)
    base["path"] = path
    base["content"] = ""
    base["error"] = None
    if not path.is_file():
        base.update(
            status="missing",
            size_bytes=0,
            word_count=0,
            modified_at=None,
        )
        return base
    try:
        stat = path.stat()
    except OSError as exc:
        base.update(
            status="unreadable",
            size_bytes=0,
            word_count=0,
            modified_at=None,
            error=str(exc),
        )
        return base
    content, error = _read_text(path)
    status = "unreadable" if error else ("empty" if not content.strip() else "found")
    base.update(
        status=status,
        size_bytes=stat.st_size,
        word_count=len(content.split()) if not error else 0,
        modified_at=datetime.fromtimestamp(
            stat.st_mtime,
            tz=timezone.utc,
        ).astimezone(),
        content=content,
        error=error,
    )
    return base


def list_documents() -> list[dict[str, Any]]:
    return [document_details(document.doc_key) for document in DOCUMENTS]


def _next_backup_path(document: KnowledgeDocument) -> Path:
    backup_dir = PROJECT_ROOT / "backups" / "knowledge"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = backup_dir / f"{document.doc_key}-{stamp}{Path(document.relative_path).suffix}"
    counter = 2
    while candidate.exists():
        candidate = backup_dir / (
            f"{document.doc_key}-{stamp}-{counter}"
            f"{Path(document.relative_path).suffix}"
        )
        counter += 1
    return candidate


def _record_audit(
    *,
    doc_key: str,
    action: str,
    path: str,
    changed_by: str,
    old_size: int | None = None,
    new_size: int | None = None,
    backup_path: str | None = None,
) -> None:
    initialize_knowledge_schema()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO knowledge_audit (
                doc_key, action, path, changed_by, old_size, new_size, backup_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc_key,
                action,
                path,
                changed_by,
                old_size,
                new_size,
                backup_path,
            ),
        )
        connection.commit()


def save_document(doc_key: str, content: str, changed_by: str) -> Path | None:
    document = get_document_definition(doc_key)
    if not document.editable:
        raise ValueError("This knowledge document is read-only.")
    path = _document_path(document)
    if path.suffix.casefold() not in ALLOWED_EXTENSIONS:
        raise ValueError("Only allowlisted Markdown and text files can be edited.")
    if "\x00" in content:
        raise ValueError("Binary content is not allowed.")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_DOCUMENT_BYTES:
        raise ValueError("Document content must be 1 MB or smaller.")
    if redact_sensitive_text(content) != content:
        raise ValueError("Document content appears to contain a credential or secret.")

    path.parent.mkdir(parents=True, exist_ok=True)
    old_size = path.stat().st_size if path.is_file() else 0
    old_mode = path.stat().st_mode & 0o777 if path.is_file() else 0o644
    backup_path = None
    if path.is_file():
        backup_path = _next_backup_path(document)
        backup_path.write_bytes(path.read_bytes())

    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.chmod(temporary_name, old_mode)
        os.replace(temporary_name, path)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()

    relative_backup = (
        str(backup_path.relative_to(PROJECT_ROOT)) if backup_path else None
    )
    _record_audit(
        doc_key=document.doc_key,
        action="edit",
        path=document.relative_path,
        changed_by=changed_by,
        old_size=old_size,
        new_size=len(encoded),
        backup_path=relative_backup,
    )
    return backup_path


def recent_knowledge_audit(limit: int = 30) -> list[dict[str, Any]]:
    initialize_knowledge_schema()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT doc_key, action, path, changed_by, old_size, new_size,
                   backup_path, created_at
            FROM knowledge_audit
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def queue_knowledge_reindex(doc_key: str | None, requested_by: str) -> int:
    initialize_knowledge_schema()
    if doc_key is None:
        payload = {"scope": "all"}
        audit_doc_key = "all"
        audit_path = "*"
        action = "reindex_all_requested"
    else:
        document = get_document_definition(doc_key)
        if not document.reindex_supported:
            raise ValueError("This document is not used by a cached knowledge loader.")
        _document_path(document)
        payload = {"doc_key": document.doc_key}
        audit_doc_key = document.doc_key
        audit_path = document.relative_path
        action = "reindex_requested"

    payload_json = json.dumps(payload, separators=(",", ":"))
    with _connect() as connection:
        existing = connection.execute(
            """
            SELECT id FROM dashboard_actions
            WHERE action_type = 'reindex_knowledge'
              AND status IN ('pending', 'processing')
              AND payload_json = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (payload_json,),
        ).fetchone()
        if existing:
            action_id = int(existing["id"])
        else:
            cursor = connection.execute(
                """
                INSERT INTO dashboard_actions (
                    action_type, payload_json, status, requested_by
                ) VALUES ('reindex_knowledge', ?, 'pending', ?)
                """,
                (payload_json, requested_by),
            )
            action_id = int(cursor.lastrowid)
        connection.commit()
    _record_audit(
        doc_key=audit_doc_key,
        action=action,
        path=audit_path,
        changed_by=requested_by,
    )
    return action_id


def process_knowledge_reindex(payload: dict[str, Any]) -> tuple[bool, str]:
    if payload == {"scope": "all"}:
        label = "all knowledge documents"
    elif set(payload) == {"doc_key"}:
        document = get_document_definition(str(payload["doc_key"]))
        if not document.reindex_supported:
            raise ValueError("Document does not support reindexing.")
        _document_path(document)
        label = document.display_name
    else:
        raise ValueError("Invalid knowledge reindex payload.")
    counts = reload_knowledge()
    return (
        True,
        f"Reloaded {label}: {counts['public_sources']} public and "
        f"{counts['staff_sources']} combined staff sources available.",
    )
