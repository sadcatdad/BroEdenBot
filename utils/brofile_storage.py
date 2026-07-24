"""Idempotent Discord storage jobs for member BROfile media."""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from utils.brofiles import _connect, _now, initialize_brofile_schema


MAX_STORAGE_ATTEMPTS = 3


def queue_media_upload(
    media_id: int,
    requested_by: str,
    storage_thread_id: int | str,
) -> int:
    """Queue the latest normalized revision of one BROfile media item."""
    initialize_brofile_schema()
    thread_id = str(storage_thread_id or "").strip()
    if not thread_id.isdigit():
        raise ValueError("Configure a valid BROfile storage forum-post/thread ID.")
    with _connect() as connection:
        media = connection.execute(
            "SELECT id, storage_key FROM brofile_media WHERE id = ?",
            (int(media_id),),
        ).fetchone()
        if media is None:
            raise ValueError("BROfile media was not found.")
        connection.execute(
            """
            UPDATE brofile_media_storage_jobs
            SET status = 'superseded', processed_at = ?,
                result_message = 'Replaced by a newer BROfile upload.'
            WHERE media_id = ? AND action = 'upload' AND status = 'pending'
            """,
            (_now(), int(media_id)),
        )
        key = "upload:{}:{}:{}".format(
            int(media_id),
            str(media["storage_key"]),
            thread_id,
        )
        existing = connection.execute(
            """
            SELECT id, status FROM brofile_media_storage_jobs
            WHERE idempotency_key = ?
            """,
            (key,),
        ).fetchone()
        if existing is not None:
            if str(existing["status"]) in {"failed", "superseded"}:
                connection.execute(
                    """
                    UPDATE brofile_media_storage_jobs
                    SET status = 'pending', attempt_count = 0,
                        processed_at = NULL, failure_reason = NULL,
                        result_message = NULL
                    WHERE id = ?
                    """,
                    (int(existing["id"]),),
                )
            connection.commit()
            return int(existing["id"])
        cursor = connection.execute(
            """
            INSERT INTO brofile_media_storage_jobs(
                media_id, action, idempotency_key, requested_by,
                status, requested_at
            ) VALUES (?, 'upload', ?, ?, 'pending', ?)
            """,
            (
                int(media_id),
                key,
                str(requested_by or "")[:120],
                _now(),
            ),
        )
        connection.execute(
            """
            UPDATE brofile_media
            SET discord_sync_status = 'pending', discord_last_error = NULL
            WHERE id = ?
            """,
            (int(media_id),),
        )
        connection.commit()
        return int(cursor.lastrowid)


def queue_missing_media_uploads(
    requested_by: str,
    storage_thread_id: int | str,
) -> int:
    """Queue current media that is missing or stored in another thread."""
    initialize_brofile_schema()
    thread_id = str(storage_thread_id or "").strip()
    if not thread_id.isdigit():
        return 0
    queued = 0
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT id, storage_key FROM brofile_media media
            WHERE (
                media.discord_attachment_url IS NULL
                OR COALESCE(media.discord_storage_thread_id, '') <> ?
            )
              AND NOT EXISTS (
                SELECT 1 FROM brofile_media_storage_jobs jobs
                WHERE jobs.media_id = media.id
                  AND jobs.action = 'upload'
                  AND jobs.status IN ('pending', 'processing')
              )
            ORDER BY id
            """,
            (thread_id,),
        ).fetchall()
        for row in rows:
            key = "upload:{}:{}:{}".format(
                int(row["id"]),
                str(row["storage_key"]),
                thread_id,
            )
            existing = connection.execute(
                """
                SELECT id, status FROM brofile_media_storage_jobs
                WHERE idempotency_key = ?
                """,
                (key,),
            ).fetchone()
            if existing is not None:
                if str(existing["status"]) in {"failed", "superseded"}:
                    connection.execute(
                        """
                        UPDATE brofile_media_storage_jobs
                        SET status = 'pending', attempt_count = 0,
                            processed_at = NULL, failure_reason = NULL,
                            result_message = NULL
                        WHERE id = ?
                        """,
                        (int(existing["id"]),),
                    )
                    queued += 1
                continue
            connection.execute(
                """
                INSERT INTO brofile_media_storage_jobs(
                    media_id, action, idempotency_key, requested_by,
                    status, requested_at
                ) VALUES (?, 'upload', ?, ?, 'pending', ?)
                """,
                (
                    int(row["id"]),
                    key,
                    str(requested_by or "")[:120],
                    _now(),
                ),
            )
            connection.execute(
                """
                UPDATE brofile_media
                SET discord_sync_status = 'pending', discord_last_error = NULL
                WHERE id = ?
                """,
                (int(row["id"]),),
            )
            queued += 1
        connection.commit()
    return queued


def prepare_media_deletion(
    connection: sqlite3.Connection,
    media_id: int,
    requested_by: str,
) -> bool:
    """Preserve any Discord message reference before deleting local metadata."""
    processing = connection.execute(
        """
        SELECT 1 FROM brofile_media_storage_jobs
        WHERE media_id = ? AND action = 'upload' AND status = 'processing'
        LIMIT 1
        """,
        (int(media_id),),
    ).fetchone()
    if processing is not None:
        raise ValueError(
            "Wait for the BROfile image storage upload to finish before deleting it."
        )
    connection.execute(
        """
        UPDATE brofile_media_storage_jobs
        SET status = 'superseded', processed_at = ?,
            result_message = 'BROfile media was deleted before Discord upload.'
        WHERE media_id = ? AND action = 'upload' AND status = 'pending'
        """,
        (_now(), int(media_id)),
    )
    stored = connection.execute(
        """
        SELECT discord_storage_thread_id, discord_message_id,
               discord_attachment_url
        FROM brofile_media WHERE id = ?
        """,
        (int(media_id),),
    ).fetchone()
    if (
        stored is None
        or not stored["discord_storage_thread_id"]
        or not stored["discord_message_id"]
    ):
        return False
    key = "delete:{}:{}".format(int(media_id), stored["discord_message_id"])
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO brofile_media_storage_jobs(
            media_id, action, idempotency_key, requested_by, status,
            storage_thread_id, message_id, attachment_url, requested_at
        ) VALUES (?, 'delete', ?, ?, 'pending', ?, ?, ?, ?)
        """,
        (
            int(media_id),
            key,
            str(requested_by or "")[:120],
            str(stored["discord_storage_thread_id"]),
            str(stored["discord_message_id"]),
            str(stored["discord_attachment_url"] or ""),
            _now(),
        ),
    )
    return bool(cursor.rowcount)


def pending_storage_jobs(limit: int = 10) -> List[Dict[str, Any]]:
    initialize_brofile_schema()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM brofile_media_storage_jobs
            WHERE status = 'pending'
            ORDER BY id LIMIT ?
            """,
            (max(1, min(50, int(limit))),),
        ).fetchall()
    return [dict(row) for row in rows]


def claim_storage_job(job_id: int) -> bool:
    initialize_brofile_schema()
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE brofile_media_storage_jobs
            SET status = 'processing', attempt_count = attempt_count + 1
            WHERE id = ? AND status = 'pending'
            """,
            (int(job_id),),
        )
        connection.commit()
        return bool(cursor.rowcount)


def recover_storage_jobs() -> int:
    initialize_brofile_schema()
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE brofile_media_storage_jobs
            SET status = 'pending',
                failure_reason = 'Recovered after an interrupted storage worker.'
            WHERE status = 'processing' AND attempt_count < ?
            """,
            (MAX_STORAGE_ATTEMPTS,),
        )
        connection.execute(
            """
            UPDATE brofile_media_storage_jobs
            SET status = 'failed', processed_at = ?,
                failure_reason = 'Storage retry limit was exhausted before restart.'
            WHERE status = 'processing' AND attempt_count >= ?
            """,
            (_now(), MAX_STORAGE_ATTEMPTS),
        )
        connection.commit()
        return int(cursor.rowcount)


def get_media_for_storage(media_id: int) -> Optional[Dict[str, Any]]:
    initialize_brofile_schema()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT media.*, profiles.display_name, profiles.username
            FROM brofile_media media
            JOIN brofiles profiles
              ON profiles.guild_id = media.guild_id
             AND profiles.user_id = media.user_id
            WHERE media.id = ?
            """,
            (int(media_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def record_storage_receipt(
    job_id: int,
    *,
    storage_thread_id: int | str,
    message_id: int | str,
    attachment_url: str,
) -> None:
    initialize_brofile_schema()
    with _connect() as connection:
        connection.execute(
            """
            UPDATE brofile_media_storage_jobs
            SET storage_thread_id = ?, message_id = ?, attachment_url = ?
            WHERE id = ?
            """,
            (
                str(storage_thread_id),
                str(message_id),
                str(attachment_url),
                int(job_id),
            ),
        )
        connection.commit()


def complete_upload_job(
    job_id: int,
    media_id: int,
    message: str = "Stored in Discord.",
) -> None:
    initialize_brofile_schema()
    with _connect() as connection:
        job = connection.execute(
            "SELECT * FROM brofile_media_storage_jobs WHERE id = ?",
            (int(job_id),),
        ).fetchone()
        if (
            job is None
            or not job["storage_thread_id"]
            or not job["message_id"]
            or not job["attachment_url"]
        ):
            raise ValueError("BROfile Discord storage receipt is incomplete.")
        cursor = connection.execute(
            """
            UPDATE brofile_media
            SET discord_storage_thread_id = ?, discord_message_id = ?,
                discord_attachment_url = ?, discord_sync_status = 'ready',
                discord_last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (
                str(job["storage_thread_id"]),
                str(job["message_id"]),
                str(job["attachment_url"]),
                _now(),
                int(media_id),
            ),
        )
        if not cursor.rowcount:
            raise ValueError("BROfile media was deleted before storage completed.")
        connection.execute(
            """
            UPDATE brofile_media_storage_jobs
            SET status = 'completed', processed_at = ?, result_message = ?,
                failure_reason = NULL
            WHERE id = ?
            """,
            (_now(), str(message)[:500], int(job_id)),
        )
        connection.commit()


def complete_delete_job(
    job_id: int,
    message: str = "Discord message deleted.",
) -> None:
    initialize_brofile_schema()
    with _connect() as connection:
        connection.execute(
            """
            UPDATE brofile_media_storage_jobs
            SET status = 'completed', processed_at = ?, result_message = ?,
                failure_reason = NULL
            WHERE id = ?
            """,
            (_now(), str(message)[:500], int(job_id)),
        )
        connection.commit()


def retry_or_fail_storage_job(job_id: int, message: str) -> bool:
    initialize_brofile_schema()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT media_id, attempt_count FROM brofile_media_storage_jobs
            WHERE id = ?
            """,
            (int(job_id),),
        ).fetchone()
        retry = bool(
            row and int(row["attempt_count"] or 0) < MAX_STORAGE_ATTEMPTS
        )
        connection.execute(
            """
            UPDATE brofile_media_storage_jobs
            SET status = ?, processed_at = ?, failure_reason = ?
            WHERE id = ?
            """,
            (
                "pending" if retry else "failed",
                None if retry else _now(),
                str(message)[:500],
                int(job_id),
            ),
        )
        if row is not None:
            connection.execute(
                """
                UPDATE brofile_media
                SET discord_sync_status = ?,
                    discord_last_error = ?
                WHERE id = ?
                """,
                (
                    "pending" if retry else "failed",
                    str(message)[:500],
                    int(row["media_id"]),
                ),
            )
        connection.commit()
        return retry


def storage_references() -> List[Dict[str, Any]]:
    initialize_brofile_schema()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT id AS media_id, discord_storage_thread_id AS storage_thread_id,
                   discord_message_id AS message_id,
                   discord_attachment_url AS attachment_url
            FROM brofile_media
            WHERE discord_storage_thread_id IS NOT NULL
              AND discord_message_id IS NOT NULL
            ORDER BY id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def update_storage_url(media_id: int, attachment_url: str) -> None:
    initialize_brofile_schema()
    with _connect() as connection:
        connection.execute(
            """
            UPDATE brofile_media
            SET discord_attachment_url = ?, discord_sync_status = 'ready',
                discord_last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (str(attachment_url), _now(), int(media_id)),
        )
        connection.commit()


def storage_overview() -> Dict[str, int]:
    initialize_brofile_schema()
    with _connect() as connection:
        total = int(
            connection.execute("SELECT COUNT(*) FROM brofile_media").fetchone()[0]
        )
        stored = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM brofile_media
                WHERE discord_sync_status = 'ready'
                  AND discord_attachment_url IS NOT NULL
                """
            ).fetchone()[0]
        )
        pending = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM brofile_media_storage_jobs
                WHERE status IN ('pending', 'processing')
                """
            ).fetchone()[0]
        )
        failed = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM brofile_media_storage_jobs
                WHERE status = 'failed'
                """
            ).fetchone()[0]
        )
    return {
        "total_assets": total,
        "stored_assets": stored,
        "pending_jobs": pending,
        "failed_jobs": failed,
    }
