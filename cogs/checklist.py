"""Persistent staff checklists with synchronized Discord posts."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.settings import get_csv_ids_setting
from utils.ui import INFO_COLOR, MUTED_COLOR, branded_embed


logger = logging.getLogger(__name__)

UNAUTHORIZED_MESSAGE = "You do not have permission to use checklist commands."
MAX_ITEM_LENGTH = 500
MAX_NAME_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 1_000
MAX_RENDERED_ITEMS = 20
SELECT_LIMIT = 25
ALLOWED_MENTIONS = discord.AllowedMentions.none()
POST_SEND_PERMISSIONS = (
    ("view_channel", "View Channel"),
    ("send_messages", "Send Messages"),
    ("embed_links", "Embed Links"),
)
THREAD_SEND_PERMISSIONS = (
    ("view_channel", "View Channel"),
    ("send_messages_in_threads", "Send Messages in Threads"),
    ("embed_links", "Embed Links"),
)
POST_UPDATE_PERMISSIONS = (
    ("view_channel", "View Channel"),
    ("read_message_history", "Read Message History"),
    ("embed_links", "Embed Links"),
)


def parse_id_set(value: Optional[str]) -> set[int]:
    result: set[int] = set()
    for item in re.split(r"[\s,]+", value or ""):
        try:
            parsed = int(item)
        except ValueError:
            continue
        if parsed > 0:
            result.add(parsed)
    return result


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def discord_timestamp(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return f"<t:{int(parsed.timestamp())}:f>"
    except (TypeError, ValueError):
        return str(value or "unknown")


def truncate(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def checklist_reference(row: dict[str, Any]) -> str:
    return f"#{row['id']} • {truncate(row['name'], 80)}"


def human_join(values: list[str]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    return ", ".join(values[:-1]) + f", and {values[-1]}"


def channel_reference(channel: Any) -> str:
    return (
        getattr(channel, "mention", None)
        or f"channel {getattr(channel, 'id', 'unknown')}"
    )


class ChecklistModal(discord.ui.Modal):
    def __init__(
        self,
        cog: "ChecklistCog",
        checklist_id: int,
        action: str,
        *,
        current_name: str = "",
        current_description: str = "",
        refresh_panel: bool = True,
    ) -> None:
        title = "Add checklist item" if action == "add" else "Rename checklist"
        super().__init__(title=title, timeout=300)
        self.cog = cog
        self.checklist_id = checklist_id
        self.action = action
        self.refresh_panel = refresh_panel

        if action == "add":
            self.primary = discord.ui.TextInput(
                label="Item text",
                placeholder="What needs to be done?",
                max_length=MAX_ITEM_LENGTH,
            )
            self.secondary = discord.ui.TextInput(
                label="Position (optional)",
                placeholder="Leave blank to add to the bottom",
                required=False,
                max_length=4,
            )
        else:
            self.primary = discord.ui.TextInput(
                label="Checklist name",
                default=current_name[:MAX_NAME_LENGTH],
                max_length=MAX_NAME_LENGTH,
            )
            self.secondary = discord.ui.TextInput(
                label="Description (optional)",
                default=current_description[:MAX_DESCRIPTION_LENGTH],
                required=False,
                style=discord.TextStyle.paragraph,
                max_length=MAX_DESCRIPTION_LENGTH,
            )
        self.add_item(self.primary)
        self.add_item(self.secondary)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.cog.ensure_access(interaction):
            return
        if self.action == "add":
            position: Optional[int] = None
            raw_position = str(self.secondary.value or "").strip()
            if raw_position:
                if not raw_position.isdigit() or int(raw_position) < 1:
                    await interaction.response.send_message(
                        "Position must be a positive whole number.",
                        ephemeral=True,
                    )
                    return
                position = int(raw_position)
            await self.cog.add_item(
                interaction,
                self.checklist_id,
                str(self.primary.value),
                position,
                refresh_panel=self.refresh_panel,
            )
            return

        await self.cog.rename_from_ui(
            interaction,
            self.checklist_id,
            str(self.primary.value),
            str(self.secondary.value or ""),
            refresh_panel=self.refresh_panel,
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        await self.cog.handle_component_error(interaction, "checklist modal", error)


class ChecklistItemSelect(discord.ui.Select):
    def __init__(
        self,
        cog: "ChecklistCog",
        checklist_id: int,
        rows: list[dict[str, Any]],
        action: str,
    ) -> None:
        options = [
            discord.SelectOption(
                label=truncate(row["content"], 100),
                value=str(row["id"]),
                description=(
                    f"Item {row['position']} • "
                    f"{'complete' if row['status'] == 'complete' else 'open'}"
                ),
                emoji="✅" if row["status"] == "complete" else "⬜",
            )
            for row in rows[:SELECT_LIMIT]
        ]
        super().__init__(
            placeholder=(
                "Choose an item to toggle"
                if action == "toggle"
                else "Choose an item to delete"
            ),
            min_values=1,
            max_values=1,
            options=options,
        )
        self.cog = cog
        self.checklist_id = checklist_id
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.cog.ensure_access(interaction):
            return
        item_id = int(self.values[0])
        if self.action == "toggle":
            await self.cog.toggle_item(interaction, self.checklist_id, item_id)
        else:
            await self.cog.delete_item(interaction, self.checklist_id, item_id)


class ChecklistItemSelectView(discord.ui.View):
    def __init__(
        self,
        cog: "ChecklistCog",
        checklist_id: int,
        rows: list[dict[str, Any]],
        action: str,
    ) -> None:
        super().__init__(timeout=300)
        self.add_item(ChecklistItemSelect(cog, checklist_id, rows, action))

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        await self.children[0].cog.handle_component_error(
            interaction,
            f"checklist item selector ({type(item).__name__})",
            error,
        )


class ChecklistChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, cog: "ChecklistCog", checklist_id: int) -> None:
        super().__init__(
            placeholder="Choose a channel for the checklist",
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.news,
                discord.ChannelType.public_thread,
                discord.ChannelType.private_thread,
            ],
            min_values=1,
            max_values=1,
        )
        self.cog = cog
        self.checklist_id = checklist_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.cog.ensure_access(interaction):
            return
        channel = self.values[0]
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.cog.post_checklist(
            interaction,
            self.checklist_id,
            channel,
            update_existing=False,
        )
        await interaction.followup.send(result, ephemeral=True)


class ChecklistChannelSelectView(discord.ui.View):
    def __init__(self, cog: "ChecklistCog", checklist_id: int) -> None:
        super().__init__(timeout=300)
        self.add_item(ChecklistChannelSelect(cog, checklist_id))

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        await self.children[0].cog.handle_component_error(
            interaction,
            f"checklist channel selector ({type(item).__name__})",
            error,
        )


class ChecklistChoiceSelect(discord.ui.Select):
    def __init__(
        self,
        cog: "ChecklistCog",
        rows: list[dict[str, Any]],
        action: str,
    ) -> None:
        options = [
            discord.SelectOption(
                label=truncate(row["name"], 100),
                value=str(row["id"]),
                description=f"Checklist #{row['id']} • {row['status']}",
            )
            for row in rows[:SELECT_LIMIT]
        ]
        super().__init__(
            placeholder="Choose a checklist",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.cog = cog
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.cog.ensure_access(interaction):
            return
        checklist_id = int(self.values[0])
        if self.action == "view":
            await self.cog.defer_private(interaction)
            await self.cog.send_panel(interaction, checklist_id, edit=True)
            return
        await interaction.response.edit_message(
            content="Confirm deletion of this checklist and its active posts.",
            embed=None,
            view=ChecklistDeleteConfirmView(self.cog, checklist_id),
        )


class ChecklistChoiceView(discord.ui.View):
    def __init__(
        self,
        cog: "ChecklistCog",
        rows: list[dict[str, Any]],
        action: str,
    ) -> None:
        super().__init__(timeout=300)
        self.add_item(ChecklistChoiceSelect(cog, rows, action))

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        await self.children[0].cog.handle_component_error(
            interaction,
            f"checklist selector ({type(item).__name__})",
            error,
        )


class ChecklistDeleteConfirmView(discord.ui.View):
    def __init__(self, cog: "ChecklistCog", checklist_id: int) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.checklist_id = checklist_id

    @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if not await self.cog.ensure_access(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.cog.soft_delete_checklist(
            self.checklist_id,
            interaction.user.id,
        )
        await interaction.edit_original_response(
            content=result,
            embed=None,
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if not await self.cog.ensure_access(interaction):
            return
        await interaction.response.edit_message(
            content="Checklist deletion cancelled.",
            embed=None,
            view=None,
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        await self.cog.handle_component_error(
            interaction,
            f"checklist deletion control ({type(item).__name__})",
            error,
        )


class ChecklistPanelView(discord.ui.View):
    def __init__(self, cog: "ChecklistCog", checklist: dict[str, Any]) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.checklist_id = int(checklist["id"])
        archived = checklist["status"] == "archived"
        self.archive.label = "Restore" if archived else "Archive"
        self.archive.style = (
            discord.ButtonStyle.success if archived else discord.ButtonStyle.secondary
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.cog.ensure_access(interaction)

    @discord.ui.button(label="Add Item", style=discord.ButtonStyle.primary, row=0)
    async def add(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(
            ChecklistModal(self.cog, self.checklist_id, "add")
        )

    @discord.ui.button(label="Toggle Complete", style=discord.ButtonStyle.success, row=0)
    async def toggle(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog.open_item_selector(interaction, self.checklist_id, "toggle")

    @discord.ui.button(label="Delete Item", style=discord.ButtonStyle.danger, row=0)
    async def delete_item_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog.open_item_selector(interaction, self.checklist_id, "delete")

    @discord.ui.button(label="Rename", style=discord.ButtonStyle.secondary, row=1)
    async def rename(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        checklist = await self.cog.get_checklist(
            interaction.guild_id,
            self.checklist_id,
            statuses=("active", "archived"),
        )
        if not checklist:
            await interaction.response.send_message(
                "That checklist is no longer available.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            ChecklistModal(
                self.cog,
                self.checklist_id,
                "rename",
                current_name=checklist["name"],
                current_description=checklist["description"] or "",
            )
        )

    @discord.ui.button(label="Post", style=discord.ButtonStyle.secondary, row=1)
    async def post(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_message(
            "Choose where to post this checklist.",
            view=ChecklistChannelSelectView(self.cog, self.checklist_id),
            ephemeral=True,
        )

    @discord.ui.button(label="Archive", style=discord.ButtonStyle.secondary, row=1)
    async def archive(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog.toggle_archive(interaction, self.checklist_id)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog.send_panel(interaction, self.checklist_id, edit=True)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        await self.cog.handle_component_error(
            interaction,
            f"checklist management control ({type(item).__name__})",
            error,
        )


class PostedChecklistView(discord.ui.View):
    """Persistent controls attached to public checklist posts."""

    def __init__(self, cog: "ChecklistCog", checklist: dict[str, Any]) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.checklist_id = int(checklist["id"])
        archived = checklist["status"] == "archived"
        self.archive.label = "Restore" if archived else "Archive"
        self.archive.style = (
            discord.ButtonStyle.success if archived else discord.ButtonStyle.secondary
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.cog.ensure_access(interaction)

    @discord.ui.button(
        label="Add Item",
        style=discord.ButtonStyle.primary,
        row=0,
        custom_id="checklist:posted:add",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(
            ChecklistModal(
                self.cog,
                self.checklist_id,
                "add",
                refresh_panel=False,
            )
        )

    @discord.ui.button(
        label="Toggle Complete",
        style=discord.ButtonStyle.success,
        row=0,
        custom_id="checklist:posted:toggle",
    )
    async def toggle(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog.open_item_selector(interaction, self.checklist_id, "toggle")

    @discord.ui.button(
        label="Delete Item",
        style=discord.ButtonStyle.danger,
        row=0,
        custom_id="checklist:posted:delete-item",
    )
    async def delete_item_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog.open_item_selector(interaction, self.checklist_id, "delete")

    @discord.ui.button(
        label="Rename",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="checklist:posted:rename",
    )
    async def rename(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        checklist = await self.cog.get_checklist(
            interaction.guild_id,
            self.checklist_id,
            statuses=("active", "archived"),
        )
        if not checklist:
            await interaction.response.send_message(
                "That checklist is no longer available.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            ChecklistModal(
                self.cog,
                self.checklist_id,
                "rename",
                current_name=checklist["name"],
                current_description=checklist["description"] or "",
                refresh_panel=False,
            )
        )

    @discord.ui.button(
        label="Post",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="checklist:posted:post",
    )
    async def post(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_message(
            "Choose where to post this checklist.",
            view=ChecklistChannelSelectView(self.cog, self.checklist_id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Archive",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="checklist:posted:archive",
    )
    async def archive(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog.toggle_archive(
            interaction,
            self.checklist_id,
            refresh_panel=False,
        )

    @discord.ui.button(
        label="Refresh",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="checklist:posted:refresh",
    )
    async def refresh(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        summary = await self.cog.sync_checklist_posts(self.checklist_id)
        await interaction.followup.send(
            f"Checklist refreshed. Updated {summary['updated']} post(s); "
            f"missing {summary['missing']}; failed {summary['failed']}.",
            ephemeral=True,
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        await self.cog.handle_component_error(
            interaction,
            f"posted checklist control ({type(item).__name__})",
            error,
        )


class ChecklistCog(commands.Cog):
    checklist = app_commands.Group(
        name="checklist",
        description="Manage persistent internal staff checklists",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.allowed_role_ids = parse_id_set(
            os.getenv("CHECKLIST_ALLOWED_ROLE_IDS", "")
        )
        self._write_lock = asyncio.Lock()
        self._posts_upgraded = False

    async def cog_load(self) -> None:
        await self.bot.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS checklists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'archived', 'deleted')),
                created_by_user_id TEXT,
                created_by_name TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT,
                deleted_by_user_id TEXT
            );
            CREATE TABLE IF NOT EXISTS checklist_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                checklist_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'complete', 'deleted')),
                position INTEGER NOT NULL,
                created_by_user_id TEXT,
                created_by_name TEXT,
                created_at TEXT NOT NULL,
                completed_by_user_id TEXT,
                completed_by_name TEXT,
                completed_at TEXT,
                deleted_by_user_id TEXT,
                deleted_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (checklist_id) REFERENCES checklists(id)
            );
            CREATE TABLE IF NOT EXISTS checklist_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                checklist_id INTEGER NOT NULL,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                posted_by_user_id TEXT,
                posted_at TEXT NOT NULL,
                last_synced_at TEXT,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'missing', 'deleted')),
                FOREIGN KEY (checklist_id) REFERENCES checklists(id)
            );
            CREATE INDEX IF NOT EXISTS idx_checklists_guild_status_name
                ON checklists (guild_id, status, name);
            CREATE INDEX IF NOT EXISTS idx_checklist_items_checklist_status_position
                ON checklist_items (checklist_id, status, position);
            CREATE INDEX IF NOT EXISTS idx_checklist_posts_checklist_status
                ON checklist_posts (checklist_id, status);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_checklist_posts_channel_message
                ON checklist_posts (channel_id, message_id);
            """
        )
        await self.bot.db.commit()
        await self.register_persistent_post_views()

    async def register_persistent_post_views(self) -> None:
        """Restore component dispatch for posts after a bot restart."""
        if not hasattr(self.bot, "add_view"):
            return
        rows = await self.fetch_all(
            """
            SELECT cp.message_id, c.*
            FROM checklist_posts cp
            JOIN checklists c ON c.id = cp.checklist_id
            WHERE cp.status = 'active' AND c.status IN ('active', 'archived')
            """
        )
        for row in rows:
            self.bot.add_view(
                PostedChecklistView(self, row),
                message_id=int(row["message_id"]),
            )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Add controls to posts created before posted controls existed."""
        if self._posts_upgraded:
            return
        self._posts_upgraded = True
        rows = await self.fetch_all(
            """
            SELECT DISTINCT checklist_id
            FROM checklist_posts
            WHERE status = 'active'
            """
        )
        for row in rows:
            await self.sync_checklist_posts(int(row["checklist_id"]))

    def has_access(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        if interaction.user.id in get_csv_ids_setting("BOT_OWNER_USER_IDS"):
            return True
        return any(role.id in self.allowed_role_ids for role in interaction.user.roles)

    async def ensure_access(self, interaction: discord.Interaction) -> bool:
        if self.has_access(interaction):
            return True
        if interaction.response.is_done():
            await interaction.followup.send(UNAUTHORIZED_MESSAGE, ephemeral=True)
        else:
            await interaction.response.send_message(
                UNAUTHORIZED_MESSAGE,
                ephemeral=True,
            )
        return False

    async def defer_private(
        self,
        interaction: discord.Interaction,
        *,
        thinking: bool = False,
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=thinking)

    def missing_bot_permissions(
        self,
        interaction: discord.Interaction,
        channel: Any,
        *,
        action: str,
    ) -> list[str]:
        guild = getattr(channel, "guild", None) or interaction.guild
        if guild is None or interaction.guild_id is None:
            return []
        if guild.id != interaction.guild_id:
            return ["a channel in this server"]
        member = getattr(guild, "me", None)
        bot_user = getattr(self.bot, "user", None)
        if member is None and bot_user is not None:
            member = guild.get_member(bot_user.id)
        if member is None or not hasattr(channel, "permissions_for"):
            return []
        permissions = channel.permissions_for(member)
        if action == "send":
            required = (
                THREAD_SEND_PERMISSIONS
                if isinstance(channel, discord.Thread)
                else POST_SEND_PERMISSIONS
            )
        else:
            required = POST_UPDATE_PERMISSIONS
        return [
            label
            for name, label in required
            if not getattr(permissions, name, False)
        ]

    def permission_failure_message(
        self,
        interaction: discord.Interaction,
        channel: Any,
        *,
        action: str,
    ) -> Optional[str]:
        missing = self.missing_bot_permissions(interaction, channel, action=action)
        if not missing:
            return None
        if missing == ["a channel in this server"]:
            return "That checklist can only be posted to a channel in this server."
        verb = "update the checklist post" if action == "update" else "post the checklist"
        return (
            f"I need {human_join(missing)} in {channel_reference(channel)} "
            f"to {verb}."
        )

    async def handle_component_error(
        self,
        interaction: discord.Interaction,
        operation: str,
        error: Exception,
    ) -> None:
        logger.error(
            "Checklist component failed: operation=%s error_type=%s",
            operation,
            type(error).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )
        message = (
            "That checklist control could not be completed. "
            "Please try again or reopen it with `/checklist view`."
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            logger.exception("Could not deliver checklist component error")

    async def fetch_one(
        self,
        sql: str,
        parameters: Iterable[Any] = (),
    ) -> Optional[dict[str, Any]]:
        cursor = await self.bot.db.execute(sql, tuple(parameters))
        row = await cursor.fetchone()
        columns = [entry[0] for entry in cursor.description or ()]
        await cursor.close()
        return dict(zip(columns, row)) if row else None

    async def fetch_all(
        self,
        sql: str,
        parameters: Iterable[Any] = (),
    ) -> list[dict[str, Any]]:
        cursor = await self.bot.db.execute(sql, tuple(parameters))
        rows = await cursor.fetchall()
        columns = [entry[0] for entry in cursor.description or ()]
        await cursor.close()
        return [dict(zip(columns, row)) for row in rows]

    async def get_checklist(
        self,
        guild_id: Optional[int],
        checklist_id: int,
        *,
        statuses: tuple[str, ...] = ("active",),
    ) -> Optional[dict[str, Any]]:
        if guild_id is None:
            return None
        placeholders = ",".join("?" for _ in statuses)
        return await self.fetch_one(
            f"""
            SELECT *
            FROM checklists
            WHERE guild_id = ? AND id = ? AND status IN ({placeholders})
            """,
            (str(guild_id), checklist_id, *statuses),
        )

    async def resolve_checklist(
        self,
        guild_id: int,
        reference: str,
        statuses: tuple[str, ...],
    ) -> Optional[dict[str, Any]]:
        value = reference.strip()
        match = re.match(r"^#?(\d+)", value)
        placeholders = ",".join("?" for _ in statuses)
        if match:
            return await self.fetch_one(
                f"""
                SELECT * FROM checklists
                WHERE guild_id = ? AND id = ? AND status IN ({placeholders})
                """,
                (str(guild_id), int(match.group(1)), *statuses),
            )
        return await self.fetch_one(
            f"""
            SELECT * FROM checklists
            WHERE guild_id = ? AND name = ? COLLATE NOCASE
              AND status IN ({placeholders})
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (str(guild_id), value, *statuses),
        )

    async def active_items(self, checklist_id: int) -> list[dict[str, Any]]:
        return await self.fetch_all(
            """
            SELECT *
            FROM checklist_items
            WHERE checklist_id = ? AND status != 'deleted'
            ORDER BY position, id
            """,
            (checklist_id,),
        )

    async def render_checklist(
        self,
        checklist_id: int,
        *,
        management: bool = False,
    ) -> Optional[discord.Embed]:
        checklist = await self.fetch_one(
            "SELECT * FROM checklists WHERE id = ?",
            (checklist_id,),
        )
        if not checklist:
            return None
        items = await self.active_items(checklist_id)
        completed = sum(item["status"] == "complete" for item in items)
        status = checklist["status"]
        status_suffix = f" [{status.upper()}]" if status != "active" else ""
        embed = branded_embed(
            f"Checklist: {truncate(checklist['name'], 220)}{status_suffix}",
            truncate(checklist["description"], 1_500) or None,
            color=MUTED_COLOR if status != "active" else INFO_COLOR,
            footer="",
        )
        embed.add_field(
            name="Progress",
            value=f"{completed}/{len(items)} complete",
            inline=False,
        )
        rendered = []
        character_count = 0
        shown = 0
        for item in items[:MAX_RENDERED_ITEMS]:
            content = truncate(item["content"], 220)
            line = (
                f"☑ ~~{content.replace('~', '')}~~"
                if item["status"] == "complete"
                else f"☐ {content}"
            )
            # Discord embed field values are limited to 1,024 characters.
            # Leave room for the hidden-item note below.
            if character_count + len(line) + 1 > 850:
                break
            rendered.append(line)
            character_count += len(line)
            shown += 1
        item_text = "\n".join(rendered) if rendered else "No items yet."
        hidden = len(items) - shown
        if hidden:
            item_text += (
                f"\n\n+ {hidden} more item{'s' if hidden != 1 else ''}. "
                "Use `/checklist view` to manage."
            )
        embed.add_field(name="Items", value=item_text, inline=False)
        if management:
            active_posts = await self.fetch_one(
                """
                SELECT COUNT(*) AS count
                FROM checklist_posts
                WHERE checklist_id = ? AND status = 'active'
                """,
                (checklist_id,),
            )
            embed.add_field(
                name="Management",
                value=(
                    f"Status: **{status}**\n"
                    f"Active posts: **{active_posts['count']}**\n"
                    "Controls are private and permission-checked."
                ),
                inline=False,
            )
        embed.set_footer(
            text=(
                f"Checklist ID: {checklist_id} • "
                f"Last updated: {checklist['updated_at'][:16].replace('T', ' ')} UTC"
            )
        )
        return embed

    async def send_panel(
        self,
        interaction: discord.Interaction,
        checklist_id: int,
        *,
        edit: bool = False,
    ) -> None:
        checklist = await self.get_checklist(
            interaction.guild_id,
            checklist_id,
            statuses=("active", "archived"),
        )
        if not checklist:
            message = "That active or archived checklist could not be found."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return
        embed = await self.render_checklist(checklist_id, management=True)
        view = ChecklistPanelView(self, checklist)
        if edit:
            if interaction.response.is_done():
                await interaction.edit_original_response(
                    content=None,
                    embed=embed,
                    view=view,
                    allowed_mentions=ALLOWED_MENTIONS,
                )
            else:
                await interaction.response.edit_message(
                    content=None,
                    embed=embed,
                    view=view,
                    allowed_mentions=ALLOWED_MENTIONS,
                )
        else:
            if interaction.response.is_done():
                await interaction.followup.send(
                    embed=embed,
                    view=view,
                    ephemeral=True,
                    allowed_mentions=ALLOWED_MENTIONS,
                )
            else:
                await interaction.response.send_message(
                    embed=embed,
                    view=view,
                    ephemeral=True,
                    allowed_mentions=ALLOWED_MENTIONS,
                )

    async def sync_checklist_posts(self, checklist_id: int) -> dict[str, int]:
        summary = {"updated": 0, "missing": 0, "failed": 0}
        embed = await self.render_checklist(checklist_id)
        if not embed:
            return summary
        checklist = await self.fetch_one(
            "SELECT * FROM checklists WHERE id = ?",
            (checklist_id,),
        )
        if not checklist or checklist["status"] == "deleted":
            return summary
        posts = await self.fetch_all(
            """
            SELECT * FROM checklist_posts
            WHERE checklist_id = ? AND status = 'active'
            """,
            (checklist_id,),
        )
        for post in posts:
            channel = self.bot.get_channel(int(post["channel_id"]))
            try:
                if channel is None:
                    channel = await self.bot.fetch_channel(int(post["channel_id"]))
                message = await channel.fetch_message(int(post["message_id"]))
                await message.edit(
                    embed=embed,
                    view=PostedChecklistView(self, checklist),
                    allowed_mentions=ALLOWED_MENTIONS,
                )
            except (discord.NotFound, discord.Forbidden):
                await self.bot.db.execute(
                    "UPDATE checklist_posts SET status = 'missing' WHERE id = ?",
                    (post["id"],),
                )
                summary["missing"] += 1
            except discord.HTTPException:
                logger.warning(
                    "Checklist post sync failed: checklist=%s post=%s",
                    checklist_id,
                    post["id"],
                )
                summary["failed"] += 1
            else:
                await self.bot.db.execute(
                    """
                    UPDATE checklist_posts
                    SET last_synced_at = ?
                    WHERE id = ?
                    """,
                    (utc_now(), post["id"]),
                )
                summary["updated"] += 1
        await self.bot.db.commit()
        return summary

    async def post_checklist(
        self,
        interaction: discord.Interaction,
        checklist_id: int,
        channel: Any,
        *,
        update_existing: bool,
    ) -> str:
        checklist = await self.get_checklist(
            interaction.guild_id,
            checklist_id,
            statuses=("active", "archived"),
        )
        if not checklist:
            return "That checklist could not be found."
        embed = await self.render_checklist(checklist_id)
        if update_existing:
            existing = await self.fetch_one(
                """
                SELECT * FROM checklist_posts
                WHERE checklist_id = ? AND channel_id = ? AND status = 'active'
                ORDER BY id DESC LIMIT 1
                """,
                (checklist_id, str(channel.id)),
            )
            if existing:
                missing_permissions = self.permission_failure_message(
                    interaction,
                    channel,
                    action="update",
                )
                if missing_permissions:
                    return missing_permissions
                try:
                    message = await channel.fetch_message(int(existing["message_id"]))
                    await message.edit(
                        embed=embed,
                        view=PostedChecklistView(self, checklist),
                        allowed_mentions=ALLOWED_MENTIONS,
                    )
                except (discord.NotFound, discord.Forbidden):
                    await self.bot.db.execute(
                        "UPDATE checklist_posts SET status = 'missing' WHERE id = ?",
                        (existing["id"],),
                    )
                    await self.bot.db.commit()
                except discord.HTTPException:
                    return "The existing checklist post could not be updated."
                else:
                    await self.bot.db.execute(
                        """
                        UPDATE checklist_posts
                        SET last_synced_at = ?
                        WHERE id = ?
                        """,
                        (utc_now(), existing["id"]),
                    )
                    await self.bot.db.commit()
                    return (
                        "Updated the existing checklist post in "
                        f"{channel_reference(channel)}."
                    )
        missing_permissions = self.permission_failure_message(
            interaction,
            channel,
            action="send",
        )
        if missing_permissions:
            return missing_permissions
        try:
            message = await channel.send(
                embed=embed,
                view=PostedChecklistView(self, checklist),
                allowed_mentions=ALLOWED_MENTIONS,
            )
        except discord.Forbidden:
            return (
                f"I could not post the checklist in {channel_reference(channel)}. "
                "Please check the bot's channel permissions."
            )
        except discord.HTTPException:
            return "I could not post the checklist in that channel."
        now = utc_now()
        await self.bot.db.execute(
            """
            INSERT INTO checklist_posts (
                checklist_id, guild_id, channel_id, message_id,
                posted_by_user_id, posted_at, last_synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checklist_id,
                str(interaction.guild_id),
                str(channel.id),
                str(message.id),
                str(interaction.user.id),
                now,
                now,
            ),
        )
        await self.bot.db.commit()
        return f"Posted checklist #{checklist_id} in {channel_reference(channel)}."

    async def add_item(
        self,
        interaction: discord.Interaction,
        checklist_id: int,
        content: str,
        position: Optional[int],
        *,
        refresh_panel: bool = True,
    ) -> None:
        await self.defer_private(interaction)
        content = content.strip()
        if not content:
            await interaction.edit_original_response(
                content="Item text cannot be blank.",
                view=None,
            )
            return
        checklist = await self.get_checklist(
            interaction.guild_id,
            checklist_id,
            statuses=("active",),
        )
        if not checklist:
            await interaction.edit_original_response(
                content="Only active checklists can receive new items.",
                view=None,
            )
            return
        async with self._write_lock:
            items = await self.active_items(checklist_id)
            target = min(position or (len(items) + 1), len(items) + 1)
            now = utc_now()
            await self.bot.db.execute(
                """
                UPDATE checklist_items
                SET position = position + 1, updated_at = ?
                WHERE checklist_id = ? AND status != 'deleted' AND position >= ?
                """,
                (now, checklist_id, target),
            )
            await self.bot.db.execute(
                """
                INSERT INTO checklist_items (
                    checklist_id, content, position, created_by_user_id,
                    created_by_name, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checklist_id,
                    content.strip(),
                    target,
                    str(interaction.user.id),
                    interaction.user.display_name,
                    now,
                    now,
                ),
            )
            await self.bot.db.execute(
                "UPDATE checklists SET updated_at = ? WHERE id = ?",
                (now, checklist_id),
            )
            await self.bot.db.commit()
        await self.sync_checklist_posts(checklist_id)
        if refresh_panel:
            await self.send_panel(interaction, checklist_id, edit=True)
        else:
            await interaction.followup.send(
                "Item added and all posted checklists were updated.",
                ephemeral=True,
            )

    async def open_item_selector(
        self,
        interaction: discord.Interaction,
        checklist_id: int,
        action: str,
    ) -> None:
        await self.defer_private(interaction)
        items = await self.active_items(checklist_id)
        if not items:
            await interaction.followup.send(
                "This checklist has no active items.",
                ephemeral=True,
            )
            return
        note = (
            f" Showing the first {SELECT_LIMIT}; use the command panel after "
            "handling these items."
            if len(items) > SELECT_LIMIT
            else ""
        )
        await interaction.followup.send(
            (
                "Choose an item to toggle."
                if action == "toggle"
                else "Choose an item to soft-delete."
            )
            + note,
            view=ChecklistItemSelectView(self, checklist_id, items, action),
            ephemeral=True,
        )

    async def toggle_item(
        self,
        interaction: discord.Interaction,
        checklist_id: int,
        item_id: int,
    ) -> None:
        await self.defer_private(interaction)
        item = await self.fetch_one(
            """
            SELECT ci.*
            FROM checklist_items ci
            JOIN checklists c ON c.id = ci.checklist_id
            WHERE ci.id = ? AND ci.checklist_id = ? AND ci.status != 'deleted'
              AND c.guild_id = ? AND c.status = 'active'
            """,
            (item_id, checklist_id, str(interaction.guild_id)),
        )
        if not item:
            await interaction.edit_original_response(
                content="That active item could not be found.",
                view=None,
            )
            return
        completing = item["status"] == "open"
        now = utc_now()
        await self.bot.db.execute(
            """
            UPDATE checklist_items
            SET status = ?,
                completed_by_user_id = ?,
                completed_by_name = ?,
                completed_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                "complete" if completing else "open",
                str(interaction.user.id) if completing else None,
                interaction.user.display_name if completing else None,
                now if completing else None,
                now,
                item_id,
            ),
        )
        await self.bot.db.execute(
            "UPDATE checklists SET updated_at = ? WHERE id = ?",
            (now, checklist_id),
        )
        await self.bot.db.commit()
        await self.sync_checklist_posts(checklist_id)
        await interaction.edit_original_response(
            content=(
                f"Item marked {'complete' if completing else 'open'}."
            ),
            view=None,
        )

    async def delete_item(
        self,
        interaction: discord.Interaction,
        checklist_id: int,
        item_id: int,
    ) -> None:
        await self.defer_private(interaction)
        item = await self.fetch_one(
            """
            SELECT ci.*
            FROM checklist_items ci
            JOIN checklists c ON c.id = ci.checklist_id
            WHERE ci.id = ? AND ci.checklist_id = ? AND ci.status != 'deleted'
              AND c.guild_id = ? AND c.status = 'active'
            """,
            (item_id, checklist_id, str(interaction.guild_id)),
        )
        if not item:
            await interaction.edit_original_response(
                content="That active item could not be found.",
                view=None,
            )
            return
        now = utc_now()
        async with self._write_lock:
            await self.bot.db.execute(
                """
                UPDATE checklist_items
                SET status = 'deleted', deleted_by_user_id = ?,
                    deleted_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (str(interaction.user.id), now, now, item_id),
            )
            remaining = await self.active_items(checklist_id)
            await self.bot.db.executemany(
                "UPDATE checklist_items SET position = ?, updated_at = ? WHERE id = ?",
                [(index, now, row["id"]) for index, row in enumerate(remaining, 1)],
            )
            await self.bot.db.execute(
                "UPDATE checklists SET updated_at = ? WHERE id = ?",
                (now, checklist_id),
            )
            await self.bot.db.commit()
        await self.sync_checklist_posts(checklist_id)
        await interaction.edit_original_response(
            content="Item soft-deleted.",
            view=None,
        )

    async def rename_from_ui(
        self,
        interaction: discord.Interaction,
        checklist_id: int,
        name: str,
        description: str,
        *,
        refresh_panel: bool = True,
    ) -> None:
        await self.defer_private(interaction)
        name = name.strip()
        description = description.strip()
        if not name:
            await interaction.edit_original_response(
                content="Checklist name cannot be blank.",
                view=None,
            )
            return
        checklist = await self.get_checklist(
            interaction.guild_id,
            checklist_id,
            statuses=("active", "archived"),
        )
        if not checklist:
            await interaction.edit_original_response(
                content="That checklist could not be found.",
                view=None,
            )
            return
        now = utc_now()
        await self.bot.db.execute(
            """
            UPDATE checklists
            SET name = ?, description = ?, updated_at = ?
            WHERE id = ?
            """,
            (name, description or None, now, checklist_id),
        )
        await self.bot.db.commit()
        await self.sync_checklist_posts(checklist_id)
        if refresh_panel:
            await self.send_panel(interaction, checklist_id, edit=True)
        else:
            await interaction.followup.send(
                "Checklist renamed and all posted copies were updated.",
                ephemeral=True,
            )

    async def toggle_archive(
        self,
        interaction: discord.Interaction,
        checklist_id: int,
        *,
        refresh_panel: bool = True,
    ) -> None:
        await self.defer_private(interaction)
        checklist = await self.get_checklist(
            interaction.guild_id,
            checklist_id,
            statuses=("active", "archived"),
        )
        if not checklist:
            await interaction.edit_original_response(
                content="That checklist could not be found.",
                view=None,
            )
            return
        status = "active" if checklist["status"] == "archived" else "archived"
        await self.bot.db.execute(
            "UPDATE checklists SET status = ?, updated_at = ? WHERE id = ?",
            (status, utc_now(), checklist_id),
        )
        await self.bot.db.commit()
        await self.sync_checklist_posts(checklist_id)
        if refresh_panel:
            await self.send_panel(interaction, checklist_id, edit=True)
        else:
            await interaction.followup.send(
                f"Checklist {'restored' if status == 'active' else 'archived'} "
                "and posted copies were updated.",
                ephemeral=True,
            )

    async def delete_active_posts(self, checklist_id: int) -> dict[str, int]:
        summary = {"deleted": 0, "missing": 0, "failed": 0}
        posts = await self.fetch_all(
            """
            SELECT * FROM checklist_posts
            WHERE checklist_id = ? AND status = 'active'
            """,
            (checklist_id,),
        )
        for post in posts:
            channel = self.bot.get_channel(int(post["channel_id"]))
            try:
                if channel is None:
                    channel = await self.bot.fetch_channel(int(post["channel_id"]))
                message = await channel.fetch_message(int(post["message_id"]))
                await message.delete()
            except (discord.NotFound, discord.Forbidden):
                new_status = "missing"
                summary["missing"] += 1
            except discord.HTTPException:
                summary["failed"] += 1
                continue
            else:
                new_status = "deleted"
                summary["deleted"] += 1
            await self.bot.db.execute(
                "UPDATE checklist_posts SET status = ? WHERE id = ?",
                (new_status, post["id"]),
            )
        await self.bot.db.commit()
        return summary

    async def soft_delete_checklist(self, checklist_id: int, user_id: int) -> str:
        now = utc_now()
        async with self._write_lock:
            await self.bot.db.execute(
                """
                UPDATE checklists
                SET status = 'deleted', deleted_at = ?,
                    deleted_by_user_id = ?, updated_at = ?
                WHERE id = ? AND status != 'deleted'
                """,
                (now, str(user_id), now, checklist_id),
            )
            await self.bot.db.execute(
                """
                UPDATE checklist_items
                SET status = 'deleted', deleted_by_user_id = ?,
                    deleted_at = COALESCE(deleted_at, ?), updated_at = ?
                WHERE checklist_id = ? AND status != 'deleted'
                """,
                (str(user_id), now, now, checklist_id),
            )
            await self.bot.db.commit()
        posts = await self.delete_active_posts(checklist_id)
        return (
            f"Checklist #{checklist_id} was soft-deleted. "
            f"Posts deleted: {posts['deleted']}; missing: {posts['missing']}; "
            f"failed: {posts['failed']}."
        )

    async def autocomplete_for_statuses(
        self,
        interaction: discord.Interaction,
        current: str,
        statuses: tuple[str, ...],
    ) -> list[app_commands.Choice[str]]:
        if not self.has_access(interaction) or interaction.guild_id is None:
            return []
        placeholders = ",".join("?" for _ in statuses)
        term = current.strip()
        like = f"%{term}%"
        rows = await self.fetch_all(
            f"""
            SELECT id, name, status
            FROM checklists
            WHERE guild_id = ? AND status IN ({placeholders})
              AND (? = '' OR name LIKE ? OR CAST(id AS TEXT) LIKE ?)
            ORDER BY updated_at DESC
            LIMIT 25
            """,
            (str(interaction.guild_id), *statuses, term, like, like),
        )
        return [
            app_commands.Choice(
                name=truncate(f"#{row['id']} • {row['name']}", 100),
                value=str(row["id"]),
            )
            for row in rows
        ]

    @checklist.command(name="create", description="Create a persistent staff checklist")
    @app_commands.describe(
        name="Checklist name",
        description="Optional checklist description",
        post_channel="Optional channel to post the checklist in immediately",
    )
    @app_commands.guild_only()
    async def create(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, MAX_NAME_LENGTH],
        description: Optional[app_commands.Range[str, 1, MAX_DESCRIPTION_LENGTH]] = None,
        post_channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if not await self.ensure_access(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        now = utc_now()
        cursor = await self.bot.db.execute(
            """
            INSERT INTO checklists (
                guild_id, name, description, created_by_user_id,
                created_by_name, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(interaction.guild_id),
                name.strip(),
                description.strip() if description else None,
                str(interaction.user.id),
                interaction.user.display_name,
                now,
                now,
            ),
        )
        await self.bot.db.commit()
        checklist_id = int(cursor.lastrowid)
        await cursor.close()
        post_result = None
        if post_channel:
            post_result = await self.post_checklist(
                interaction,
                checklist_id,
                post_channel,
                update_existing=False,
            )
        await self.send_panel(interaction, checklist_id)
        if post_result:
            await interaction.followup.send(post_result, ephemeral=True)

    @checklist.command(name="view", description="Open a private checklist management panel")
    @app_commands.describe(checklist="Checklist name or ID; omit to choose from a menu")
    @app_commands.guild_only()
    async def view(
        self,
        interaction: discord.Interaction,
        checklist: Optional[str] = None,
    ) -> None:
        if not await self.ensure_access(interaction):
            return
        await self.defer_private(interaction, thinking=True)
        if checklist:
            row = await self.resolve_checklist(
                interaction.guild_id,
                checklist,
                ("active", "archived"),
            )
            if not row:
                await interaction.followup.send(
                    "No matching active or archived checklist was found.",
                    ephemeral=True,
                )
                return
            await self.send_panel(interaction, row["id"])
            return
        rows = await self.fetch_all(
            """
            SELECT id, name, status
            FROM checklists
            WHERE guild_id = ? AND status = 'active'
            ORDER BY updated_at DESC
            LIMIT 26
            """,
            (str(interaction.guild_id),),
        )
        if not rows:
            await interaction.followup.send(
                "There are no active checklists.",
                ephemeral=True,
            )
            return
        note = (
            " Showing the 25 most recently updated; use autocomplete to find others."
            if len(rows) > SELECT_LIMIT
            else ""
        )
        await interaction.followup.send(
            "Choose a checklist." + note,
            view=ChecklistChoiceView(self, rows, "view"),
            ephemeral=True,
        )

    @checklist.command(name="list", description="List checklists and their progress")
    @app_commands.describe(status="Checklist status to list")
    @app_commands.choices(
        status=[
            app_commands.Choice(name="Active", value="active"),
            app_commands.Choice(name="Archived", value="archived"),
            app_commands.Choice(name="Deleted", value="deleted"),
        ]
    )
    @app_commands.guild_only()
    async def list_checklists(
        self,
        interaction: discord.Interaction,
        status: str = "active",
    ) -> None:
        if not await self.ensure_access(interaction):
            return
        await self.defer_private(interaction, thinking=True)
        rows = await self.fetch_all(
            """
            SELECT c.id, c.name, c.created_by_name, c.created_by_user_id,
                   c.updated_at,
                   SUM(CASE WHEN ci.status = 'complete' THEN 1 ELSE 0 END) AS complete,
                   SUM(CASE WHEN ci.status != 'deleted' THEN 1 ELSE 0 END) AS total,
                   COUNT(DISTINCT CASE WHEN cp.status = 'active' THEN cp.id END) AS posts
            FROM checklists c
            LEFT JOIN checklist_items ci ON ci.checklist_id = c.id
            LEFT JOIN checklist_posts cp ON cp.checklist_id = c.id
            WHERE c.guild_id = ? AND c.status = ?
            GROUP BY c.id
            ORDER BY c.updated_at DESC
            LIMIT 50
            """,
            (str(interaction.guild_id), status),
        )
        if not rows:
            await interaction.followup.send(
                f"No {status} checklists were found.",
                ephemeral=True,
            )
            return
        lines = []
        for row in rows:
            creator = row["created_by_name"] or f"User {row['created_by_user_id']}"
            lines.append(
                f"**#{row['id']} • {truncate(row['name'], 70)}**\n"
                f"{row['complete'] or 0}/{row['total'] or 0} complete • "
                f"{row['posts']} active post(s) • by {truncate(creator, 40)} • "
                f"updated {discord_timestamp(row['updated_at'])}"
            )
        embed = branded_embed(
            f"{status.title()} checklists",
            "\n\n".join(lines)[:4_000],
            color=MUTED_COLOR if status != "active" else INFO_COLOR,
        )
        embed.set_footer(text="At most 50 checklists are shown.")
        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
            allowed_mentions=ALLOWED_MENTIONS,
        )

    @checklist.command(name="post", description="Post a checklist into a channel")
    @app_commands.describe(
        checklist="Active or archived checklist name or ID",
        channel="Channel that should receive the checklist",
        update_existing="Update an active post in this channel instead of making another",
    )
    @app_commands.guild_only()
    async def post_command(
        self,
        interaction: discord.Interaction,
        checklist: str,
        channel: discord.TextChannel,
        update_existing: bool = False,
    ) -> None:
        if not await self.ensure_access(interaction):
            return
        await self.defer_private(interaction, thinking=True)
        row = await self.resolve_checklist(
            interaction.guild_id,
            checklist,
            ("active", "archived"),
        )
        if not row:
            await interaction.followup.send(
                "No matching active or archived checklist was found.",
                ephemeral=True,
            )
            return
        result = await self.post_checklist(
            interaction,
            row["id"],
            channel,
            update_existing=update_existing,
        )
        await interaction.followup.send(result, ephemeral=True)

    @checklist.command(name="delete", description="Soft-delete a checklist")
    @app_commands.describe(checklist="Active checklist name or ID; omit to choose")
    @app_commands.guild_only()
    async def delete_command(
        self,
        interaction: discord.Interaction,
        checklist: Optional[str] = None,
    ) -> None:
        if not await self.ensure_access(interaction):
            return
        await self.defer_private(interaction, thinking=True)
        if checklist:
            row = await self.resolve_checklist(
                interaction.guild_id,
                checklist,
                ("active",),
            )
            if not row:
                await interaction.followup.send(
                    "No matching active checklist was found.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                f"Confirm deletion of {checklist_reference(row)} and its active posts.",
                view=ChecklistDeleteConfirmView(self, row["id"]),
                ephemeral=True,
            )
            return
        rows = await self.fetch_all(
            """
            SELECT id, name, status FROM checklists
            WHERE guild_id = ? AND status = 'active'
            ORDER BY updated_at DESC LIMIT 25
            """,
            (str(interaction.guild_id),),
        )
        if not rows:
            await interaction.followup.send(
                "There are no active checklists to delete.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "Choose a checklist to delete.",
            view=ChecklistChoiceView(self, rows, "delete"),
            ephemeral=True,
        )

    @checklist.command(name="rename", description="Rename or redescribe a checklist")
    @app_commands.describe(
        checklist="Active or archived checklist name or ID",
        new_name="New checklist name",
        description="New description; omit to keep the current description",
    )
    @app_commands.guild_only()
    async def rename_command(
        self,
        interaction: discord.Interaction,
        checklist: str,
        new_name: app_commands.Range[str, 1, MAX_NAME_LENGTH],
        description: Optional[app_commands.Range[str, 1, MAX_DESCRIPTION_LENGTH]] = None,
    ) -> None:
        if not await self.ensure_access(interaction):
            return
        await self.defer_private(interaction, thinking=True)
        row = await self.resolve_checklist(
            interaction.guild_id,
            checklist,
            ("active", "archived"),
        )
        if not row:
            await interaction.followup.send(
                "No matching active or archived checklist was found.",
                ephemeral=True,
            )
            return
        await self.rename_from_ui(
            interaction,
            row["id"],
            new_name,
            row["description"] if description is None else description,
        )

    @checklist.command(name="archive", description="Archive an active checklist")
    @app_commands.describe(
        checklist="Active checklist name or ID",
        delete_posts="Delete active Discord posts while archiving",
    )
    @app_commands.guild_only()
    async def archive_command(
        self,
        interaction: discord.Interaction,
        checklist: str,
        delete_posts: bool = False,
    ) -> None:
        if not await self.ensure_access(interaction):
            return
        await self.defer_private(interaction, thinking=True)
        row = await self.resolve_checklist(
            interaction.guild_id,
            checklist,
            ("active",),
        )
        if not row:
            await interaction.followup.send(
                "No matching active checklist was found.",
                ephemeral=True,
            )
            return
        await self.bot.db.execute(
            "UPDATE checklists SET status = 'archived', updated_at = ? WHERE id = ?",
            (utc_now(), row["id"]),
        )
        await self.bot.db.commit()
        if delete_posts:
            posts = await self.delete_active_posts(row["id"])
            detail = (
                f" Posts deleted: {posts['deleted']}; missing: {posts['missing']}; "
                f"failed: {posts['failed']}."
            )
        else:
            await self.sync_checklist_posts(row["id"])
            detail = " Existing posts were retained and updated."
        await interaction.followup.send(
            f"Archived {checklist_reference(row)}.{detail}",
            ephemeral=True,
        )

    @checklist.command(name="restore", description="Restore an archived checklist")
    @app_commands.describe(checklist="Archived checklist name or ID")
    @app_commands.guild_only()
    async def restore_command(
        self,
        interaction: discord.Interaction,
        checklist: str,
    ) -> None:
        if not await self.ensure_access(interaction):
            return
        await self.defer_private(interaction, thinking=True)
        row = await self.resolve_checklist(
            interaction.guild_id,
            checklist,
            ("archived",),
        )
        if not row:
            await interaction.followup.send(
                "No matching archived checklist was found.",
                ephemeral=True,
            )
            return
        await self.bot.db.execute(
            "UPDATE checklists SET status = 'active', updated_at = ? WHERE id = ?",
            (utc_now(), row["id"]),
        )
        await self.bot.db.commit()
        summary = await self.sync_checklist_posts(row["id"])
        await interaction.followup.send(
            f"Restored {checklist_reference(row)}. "
            f"Updated {summary['updated']} active post(s).",
            ephemeral=True,
        )

    @checklist.command(
        name="refresh",
        description="Refresh posted copies and reattach their controls",
    )
    @app_commands.describe(checklist="Active or archived checklist name or ID")
    @app_commands.guild_only()
    async def refresh_command(
        self,
        interaction: discord.Interaction,
        checklist: str,
    ) -> None:
        if not await self.ensure_access(interaction):
            return
        await self.defer_private(interaction, thinking=True)
        row = await self.resolve_checklist(
            interaction.guild_id,
            checklist,
            ("active", "archived"),
        )
        if not row:
            await interaction.followup.send(
                "No matching active or archived checklist was found.",
                ephemeral=True,
            )
            return
        summary = await self.sync_checklist_posts(row["id"])
        await interaction.followup.send(
            f"Refreshed {checklist_reference(row)} and reattached its controls. "
            f"Updated {summary['updated']} post(s); missing {summary['missing']}; "
            f"failed {summary['failed']}.",
            ephemeral=True,
        )

    @checklist.command(name="export", description="Export a checklist as CSV")
    @app_commands.describe(checklist="Checklist name or ID")
    @app_commands.guild_only()
    async def export_command(
        self,
        interaction: discord.Interaction,
        checklist: str,
    ) -> None:
        if not await self.ensure_access(interaction):
            return
        await self.defer_private(interaction, thinking=True)
        row = await self.resolve_checklist(
            interaction.guild_id,
            checklist,
            ("active", "archived", "deleted"),
        )
        if not row:
            await interaction.followup.send(
                "No matching checklist was found.",
                ephemeral=True,
            )
            return
        items = await self.fetch_all(
            """
            SELECT id, position, content, status, created_by_name, created_at,
                   completed_by_name, completed_at, deleted_at
            FROM checklist_items
            WHERE checklist_id = ?
            ORDER BY position, id
            """,
            (row["id"],),
        )
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "id",
                "position",
                "content",
                "status",
                "created_by_name",
                "created_at",
                "completed_by_name",
                "completed_at",
                "deleted_at",
            ],
        )
        writer.writeheader()
        writer.writerows(items)
        file = discord.File(
            io.BytesIO(output.getvalue().encode("utf-8")),
            filename=f"checklist-{row['id']}.csv",
        )
        await interaction.followup.send(
            content=f"Export for {checklist_reference(row)}.",
            file=file,
            ephemeral=True,
        )

    @view.autocomplete("checklist")
    @post_command.autocomplete("checklist")
    @rename_command.autocomplete("checklist")
    @refresh_command.autocomplete("checklist")
    async def general_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self.autocomplete_for_statuses(
            interaction,
            current,
            ("active", "archived"),
        )

    @export_command.autocomplete("checklist")
    async def export_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self.autocomplete_for_statuses(
            interaction,
            current,
            ("active", "archived", "deleted"),
        )

    @delete_command.autocomplete("checklist")
    @archive_command.autocomplete("checklist")
    async def active_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self.autocomplete_for_statuses(
            interaction,
            current,
            ("active",),
        )

    @restore_command.autocomplete("checklist")
    async def archived_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self.autocomplete_for_statuses(
            interaction,
            current,
            ("archived",),
        )

    @commands.Cog.listener()
    async def on_raw_message_delete(
        self,
        payload: discord.RawMessageDeleteEvent,
    ) -> None:
        await self.bot.db.execute(
            """
            UPDATE checklist_posts
            SET status = 'missing'
            WHERE channel_id = ? AND message_id = ? AND status = 'active'
            """,
            (str(payload.channel_id), str(payload.message_id)),
        )
        await self.bot.db.commit()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ChecklistCog(bot))
