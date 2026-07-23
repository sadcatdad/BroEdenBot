"""Durable Discord storage jobs for Visual Content Studio assets."""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from .repository import _connect, initialize_visual_studio_schema, utcnow


MAX_STORAGE_ATTEMPTS = 3


def queue_asset_upload(
    asset_id: int,
    requested_by: str,
    storage_thread_id: int | str | None = None,
) -> int:
    """Queue the current normalized revision of an Asset Library item."""
    initialize_visual_studio_schema()
    with _connect() as connection:
        asset = connection.execute(
            "SELECT id, storage_key FROM visual_assets WHERE id = ?",
            (int(asset_id),),
        ).fetchone()
        if asset is None:
            raise ValueError("Visual asset was not found.")
        connection.execute(
            """
            UPDATE visual_asset_storage_jobs
            SET status = 'superseded', processed_at = ?,
                result_message = 'Replaced by a newer Asset Library upload.'
            WHERE asset_id = ? AND action = 'upload' AND status = 'pending'
            """,
            (utcnow(), int(asset_id)),
        )
        key = "upload:{}:{}:{}".format(
            int(asset_id),
            asset["storage_key"],
            str(storage_thread_id or "configured"),
        )
        existing = connection.execute(
            "SELECT id, status FROM visual_asset_storage_jobs WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if existing is not None:
            if existing["status"] in {"failed", "superseded"}:
                connection.execute(
                    """
                    UPDATE visual_asset_storage_jobs
                    SET status = 'pending', attempt_count = 0, processed_at = NULL,
                        failure_reason = NULL, result_message = NULL
                    WHERE id = ?
                    """,
                    (int(existing["id"]),),
                )
            connection.commit()
            return int(existing["id"])
        cursor = connection.execute(
            """
            INSERT INTO visual_asset_storage_jobs(
                asset_id, action, idempotency_key, requested_by,
                status, requested_at
            ) VALUES (?, 'upload', ?, ?, 'pending', ?)
            """,
            (int(asset_id), key, str(requested_by or "")[:120], utcnow()),
        )
        connection.commit()
        return int(cursor.lastrowid)


def queue_missing_asset_uploads(
    requested_by: str = "visual-storage-backfill",
    storage_thread_id: int | str | None = None,
) -> int:
    """Queue active legacy assets that do not yet have a Discord source."""
    initialize_visual_studio_schema()
    queued = 0
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT a.id, a.storage_key
            FROM visual_assets a
            LEFT JOIN visual_asset_discord_storage d ON d.asset_id = a.id
            WHERE a.archived_at IS NULL
              AND (
                d.asset_id IS NULL
                OR (? <> '' AND d.storage_thread_id <> ?)
              )
              AND NOT EXISTS (
                SELECT 1 FROM visual_asset_storage_jobs j
                WHERE j.asset_id = a.id AND j.action = 'upload'
                  AND j.status IN ('pending', 'processing')
              )
            ORDER BY a.id
            """
            ,
            (str(storage_thread_id or ""), str(storage_thread_id or "")),
        ).fetchall()
        for row in rows:
            key = "upload:{}:{}:{}".format(
                int(row["id"]),
                row["storage_key"],
                str(storage_thread_id or "configured"),
            )
            existing = connection.execute(
                "SELECT id, status FROM visual_asset_storage_jobs WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                if existing["status"] in {"failed", "superseded"}:
                    connection.execute(
                        """
                        UPDATE visual_asset_storage_jobs
                        SET status = 'pending', attempt_count = 0, processed_at = NULL,
                            failure_reason = NULL, result_message = NULL
                        WHERE id = ?
                        """,
                        (int(existing["id"]),),
                    )
                    queued += 1
                continue
            cursor = connection.execute(
                """
                INSERT INTO visual_asset_storage_jobs(
                    asset_id, action, idempotency_key, requested_by,
                    status, requested_at
                ) VALUES (?, 'upload', ?, ?, 'pending', ?)
                """,
                (int(row["id"]), key, str(requested_by)[:120], utcnow()),
            )
            queued += int(bool(cursor.rowcount))
        connection.commit()
    return queued


def prepare_asset_deletion(connection: sqlite3.Connection, asset_id: int, requested_by: str) -> bool:
    """Copy Discord message references into a deletion job before local removal."""
    processing = connection.execute(
        """
        SELECT 1 FROM visual_asset_storage_jobs
        WHERE asset_id = ? AND action = 'upload' AND status = 'processing'
        LIMIT 1
        """,
        (int(asset_id),),
    ).fetchone()
    if processing is not None:
        raise ValueError("Wait for the Discord storage upload to finish before permanently deleting this asset.")
    connection.execute(
        """
        UPDATE visual_asset_storage_jobs
        SET status = 'superseded', processed_at = ?,
            result_message = 'Asset was deleted before Discord upload.'
        WHERE asset_id = ? AND action = 'upload' AND status = 'pending'
        """,
        (utcnow(), int(asset_id)),
    )
    stored = connection.execute(
        "SELECT * FROM visual_asset_discord_storage WHERE asset_id = ?",
        (int(asset_id),),
    ).fetchone()
    if stored is None:
        return False
    key = "delete:{}:{}".format(int(asset_id), stored["message_id"])
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO visual_asset_storage_jobs(
            asset_id, action, idempotency_key, requested_by, status,
            storage_thread_id, message_id, attachment_url, requested_at
        ) VALUES (?, 'delete', ?, ?, 'pending', ?, ?, ?, ?)
        """,
        (
            int(asset_id), key, str(requested_by or "")[:120],
            str(stored["storage_thread_id"]), str(stored["message_id"]),
            str(stored["attachment_url"]), utcnow(),
        ),
    )
    return bool(cursor.rowcount)


def pending_storage_jobs(limit: int = 10) -> List[Dict[str, Any]]:
    initialize_visual_studio_schema()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM visual_asset_storage_jobs
            WHERE status = 'pending'
            ORDER BY id LIMIT ?
            """,
            (max(1, min(50, int(limit))),),
        ).fetchall()
    return [dict(row) for row in rows]


def claim_storage_job(job_id: int) -> bool:
    initialize_visual_studio_schema()
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE visual_asset_storage_jobs
            SET status = 'processing', attempt_count = attempt_count + 1
            WHERE id = ? AND status = 'pending'
            """,
            (int(job_id),),
        )
        connection.commit()
        return bool(cursor.rowcount)


def recover_storage_jobs(timeout_seconds: int = 600) -> int:
    del timeout_seconds  # Recovery is attempt-bounded; wall-clock state is not trusted after restart.
    initialize_visual_studio_schema()
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE visual_asset_storage_jobs
            SET status = 'pending',
                failure_reason = 'Recovered after an interrupted storage worker.'
            WHERE status = 'processing' AND attempt_count < ?
            """,
            (MAX_STORAGE_ATTEMPTS,),
        )
        connection.execute(
            """
            UPDATE visual_asset_storage_jobs
            SET status = 'failed', processed_at = ?,
                failure_reason = 'Storage retry limit was exhausted before restart.'
            WHERE status = 'processing' AND attempt_count >= ?
            """,
            (utcnow(), MAX_STORAGE_ATTEMPTS),
        )
        connection.commit()
        return int(cursor.rowcount)


def record_storage_receipt(
    job_id: int,
    *,
    storage_thread_id: int | str,
    message_id: int | str,
    attachment_url: str,
) -> None:
    initialize_visual_studio_schema()
    with _connect() as connection:
        connection.execute(
            """
            UPDATE visual_asset_storage_jobs
            SET storage_thread_id = ?, message_id = ?, attachment_url = ?
            WHERE id = ?
            """,
            (
                str(storage_thread_id), str(message_id),
                str(attachment_url), int(job_id),
            ),
        )
        connection.commit()


def complete_upload_job(job_id: int, asset_id: int, message: str = "Stored in Discord.") -> None:
    initialize_visual_studio_schema()
    with _connect() as connection:
        job = connection.execute(
            "SELECT * FROM visual_asset_storage_jobs WHERE id = ?",
            (int(job_id),),
        ).fetchone()
        if job is None or not job["storage_thread_id"] or not job["message_id"] or not job["attachment_url"]:
            raise ValueError("Discord storage receipt is incomplete.")
        asset = connection.execute(
            "SELECT id FROM visual_assets WHERE id = ?",
            (int(asset_id),),
        ).fetchone()
        if asset is None:
            raise ValueError("Visual asset was deleted before storage completed.")
        connection.execute(
            """
            INSERT INTO visual_asset_discord_storage(
                asset_id, storage_thread_id, message_id, attachment_url,
                sync_status, last_error, updated_at
            ) VALUES (?, ?, ?, ?, 'ready', NULL, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                storage_thread_id = excluded.storage_thread_id,
                message_id = excluded.message_id,
                attachment_url = excluded.attachment_url,
                sync_status = 'ready',
                last_error = NULL,
                updated_at = excluded.updated_at
            """,
            (
                int(asset_id), str(job["storage_thread_id"]),
                str(job["message_id"]), str(job["attachment_url"]), utcnow(),
            ),
        )
        connection.execute(
            """
            UPDATE visual_asset_storage_jobs
            SET status = 'completed', processed_at = ?, result_message = ?,
                failure_reason = NULL
            WHERE id = ?
            """,
            (utcnow(), str(message)[:500], int(job_id)),
        )
        connection.commit()


def complete_delete_job(job_id: int, message: str = "Discord message deleted.") -> None:
    initialize_visual_studio_schema()
    with _connect() as connection:
        connection.execute(
            """
            UPDATE visual_asset_storage_jobs
            SET status = 'completed', processed_at = ?, result_message = ?,
                failure_reason = NULL
            WHERE id = ?
            """,
            (utcnow(), str(message)[:500], int(job_id)),
        )
        connection.commit()


def retry_or_fail_storage_job(job_id: int, message: str) -> bool:
    initialize_visual_studio_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT attempt_count FROM visual_asset_storage_jobs WHERE id = ?",
            (int(job_id),),
        ).fetchone()
        retry = bool(row and int(row["attempt_count"] or 0) < MAX_STORAGE_ATTEMPTS)
        connection.execute(
            """
            UPDATE visual_asset_storage_jobs
            SET status = ?, processed_at = ?, failure_reason = ?
            WHERE id = ?
            """,
            (
                "pending" if retry else "failed",
                None if retry else utcnow(),
                str(message)[:500],
                int(job_id),
            ),
        )
        connection.commit()
        return retry


def storage_references() -> List[Dict[str, Any]]:
    initialize_visual_studio_schema()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM visual_asset_discord_storage ORDER BY asset_id"
        ).fetchall()
    return [dict(row) for row in rows]


def update_storage_url(asset_id: int, attachment_url: str) -> None:
    initialize_visual_studio_schema()
    with _connect() as connection:
        connection.execute(
            """
            UPDATE visual_asset_discord_storage
            SET attachment_url = ?, sync_status = 'ready',
                last_error = NULL, updated_at = ?
            WHERE asset_id = ?
            """,
            (str(attachment_url), utcnow(), int(asset_id)),
        )
        connection.commit()


def storage_overview() -> Dict[str, Any]:
    initialize_visual_studio_schema()
    with _connect() as connection:
        total = int(connection.execute(
            "SELECT COUNT(*) FROM visual_assets WHERE archived_at IS NULL"
        ).fetchone()[0])
        stored = int(connection.execute(
            """
            SELECT COUNT(*) FROM visual_asset_discord_storage d
            JOIN visual_assets a ON a.id = d.asset_id
            WHERE a.archived_at IS NULL AND d.sync_status = 'ready'
            """
        ).fetchone()[0])
        pending = int(connection.execute(
            "SELECT COUNT(*) FROM visual_asset_storage_jobs WHERE status IN ('pending', 'processing')"
        ).fetchone()[0])
        failed = int(connection.execute(
            "SELECT COUNT(*) FROM visual_asset_storage_jobs WHERE status = 'failed'"
        ).fetchone()[0])
    return {
        "total_assets": total,
        "stored_assets": stored,
        "pending_jobs": pending,
        "failed_jobs": failed,
    }
