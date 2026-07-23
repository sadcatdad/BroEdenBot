"""Discord-backed storage worker for Visual Content Studio assets."""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any, Optional

import discord
from discord.ext import commands, tasks

from utils.settings import get_setting
from utils.visual_studio.discord_storage import (
    claim_storage_job,
    complete_delete_job,
    complete_upload_job,
    pending_storage_jobs,
    queue_missing_asset_uploads,
    record_storage_receipt,
    recover_storage_jobs,
    retry_or_fail_storage_job,
    storage_references,
    update_storage_url,
)
from utils.visual_studio.repository import initialize_visual_studio_schema
from utils.visual_studio.storage import asset_bytes, get_asset


logger = logging.getLogger(__name__)


class VisualAssetDiscordStorage(commands.Cog):
    """Move normalized Asset Library images into one configured forum post."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @staticmethod
    def _configured_thread_id() -> int:
        value = str(get_setting("VISUAL_ASSET_STORAGE_THREAD_ID", "") or "").strip()
        return int(value) if value.isdigit() else 0

    async def cog_load(self) -> None:
        await asyncio.to_thread(initialize_visual_studio_schema)
        await asyncio.to_thread(recover_storage_jobs)
        thread_id = self._configured_thread_id()
        if thread_id:
            queued = await asyncio.to_thread(
                queue_missing_asset_uploads,
                "visual-storage-backfill",
                thread_id,
            )
            if queued:
                logger.info("Queued %s existing Visual Studio asset(s) for Discord storage.", queued)

    def cog_unload(self) -> None:
        self.storage_worker.cancel()
        self.refresh_storage_links.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.storage_worker.is_running():
            self.storage_worker.start()
        if not self.refresh_storage_links.is_running():
            self.refresh_storage_links.start()

    async def _resolve_thread(self, thread_id: int) -> Optional[discord.Thread]:
        destination = self.bot.get_channel(thread_id)
        if destination is None:
            try:
                destination = await self.bot.fetch_channel(thread_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        return destination if isinstance(destination, discord.Thread) else None

    @staticmethod
    async def _open_thread(thread: discord.Thread) -> None:
        if not thread.archived:
            return
        try:
            await thread.edit(archived=False, reason="Visual Content Studio asset storage")
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _upload(self, job: dict[str, Any]) -> None:
        asset = await asyncio.to_thread(get_asset, int(job["asset_id"]))
        if asset is None:
            raise ValueError("Visual asset no longer exists.")
        thread_id = self._configured_thread_id()
        if not thread_id:
            raise ValueError("Asset Library Storage Forum Post is not configured.")
        thread = await self._resolve_thread(thread_id)
        if thread is None:
            raise ValueError("The configured Asset Library storage ID is not an available Discord forum post/thread.")
        await self._open_thread(thread)

        if job.get("attachment_url") and job.get("message_id"):
            await asyncio.to_thread(
                complete_upload_job,
                int(job["id"]),
                int(job["asset_id"]),
                "Recovered the existing Discord upload receipt.",
            )
            return

        data = await asyncio.to_thread(
            asset_bytes,
            str(asset["storage_key"]),
            asset.get("discord_attachment_url"),
        )
        filename = "bro-eden-visual-asset-{}.png".format(int(asset["id"]))
        content = "Visual Content Studio · Asset #{} · {}".format(
            int(asset["id"]), str(asset["name"])[:100]
        )
        allowed = discord.AllowedMentions.none()
        message = None
        previous_thread_id = int(asset.get("discord_storage_thread_id") or 0)
        previous_message_id = int(asset.get("discord_message_id") or 0)
        if (
            previous_message_id
            and previous_thread_id == thread_id
        ):
            try:
                existing = await thread.fetch_message(int(asset["discord_message_id"]))
                message = await existing.edit(
                    content=content,
                    attachments=[discord.File(io.BytesIO(data), filename=filename)],
                    allowed_mentions=allowed,
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
        if message is None:
            message = await thread.send(
                content=content,
                file=discord.File(io.BytesIO(data), filename=filename),
                allowed_mentions=allowed,
            )
        if not message.attachments:
            raise RuntimeError("Discord did not preserve the Visual Studio asset attachment.")
        attachment_url = str(message.attachments[0].url)
        await asyncio.to_thread(
            record_storage_receipt,
            int(job["id"]),
            storage_thread_id=thread_id,
            message_id=message.id,
            attachment_url=attachment_url,
        )
        await asyncio.to_thread(
            complete_upload_job,
            int(job["id"]),
            int(job["asset_id"]),
        )
        if (
            previous_thread_id
            and previous_message_id
            and (
                previous_thread_id != thread_id
                or previous_message_id != int(message.id)
            )
        ):
            previous_thread = await self._resolve_thread(previous_thread_id)
            if previous_thread is not None:
                try:
                    previous_message = await previous_thread.fetch_message(
                        previous_message_id
                    )
                    await previous_message.delete(
                        reason="Visual Content Studio asset moved to configured storage"
                    )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    logger.warning(
                        "Stored replacement for visual asset %s but could not remove old Discord message %s.",
                        asset["id"],
                        previous_message_id,
                    )

    async def _delete(self, job: dict[str, Any]) -> None:
        thread_id = int(job.get("storage_thread_id") or 0)
        message_id = int(job.get("message_id") or 0)
        if not thread_id or not message_id:
            await asyncio.to_thread(
                complete_delete_job,
                int(job["id"]),
                "No Discord message reference remained.",
            )
            return
        thread = await self._resolve_thread(thread_id)
        if thread is None:
            raise ValueError("The Asset Library storage forum post is unavailable.")
        await self._open_thread(thread)
        try:
            message = await thread.fetch_message(message_id)
            await message.delete(reason="Visual Content Studio asset permanently deleted")
        except discord.NotFound:
            pass
        await asyncio.to_thread(complete_delete_job, int(job["id"]))

    @tasks.loop(seconds=5)
    async def storage_worker(self) -> None:
        jobs = await asyncio.to_thread(pending_storage_jobs, 10)
        for job in jobs:
            if not await asyncio.to_thread(claim_storage_job, int(job["id"])):
                continue
            try:
                if job["action"] == "delete":
                    await self._delete(job)
                else:
                    await self._upload(job)
            except Exception as exc:
                logger.exception(
                    "Visual asset Discord storage job failed job_id=%s asset_id=%s",
                    job["id"],
                    job["asset_id"],
                )
                await asyncio.to_thread(
                    retry_or_fail_storage_job,
                    int(job["id"]),
                    "{}: {}".format(type(exc).__name__, str(exc))[:500],
                )

    @storage_worker.before_loop
    async def before_storage_worker(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=15)
    async def refresh_storage_links(self) -> None:
        if self._configured_thread_id():
            await asyncio.to_thread(
                queue_missing_asset_uploads,
                "visual-storage-backfill",
                self._configured_thread_id(),
            )
        references = await asyncio.to_thread(storage_references)
        for reference in references:
            thread = await self._resolve_thread(int(reference["storage_thread_id"]))
            if thread is None:
                continue
            try:
                message = await thread.fetch_message(int(reference["message_id"]))
                if message.attachments:
                    url = str(message.attachments[0].url)
                    if url != str(reference["attachment_url"]):
                        await asyncio.to_thread(
                            update_storage_url,
                            int(reference["asset_id"]),
                            url,
                        )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning(
                    "Could not refresh Visual Studio Discord source asset_id=%s",
                    reference["asset_id"],
                )

    @refresh_storage_links.before_loop
    async def before_refresh_storage_links(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VisualAssetDiscordStorage(bot))
