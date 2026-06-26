from __future__ import annotations

import ast
import datetime
import json
import logging
import re
from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.ui import (
    INFO_COLOR,
    branded_embed,
    error_embed,
    progress_bar,
    success_embed,
    warning_embed,
)


logger = logging.getLogger(__name__)
ALLOWED_MENTIONS = discord.AllowedMentions.none()
EMOJIS = [
    "🇦", "🇧", "🇨", "🇩", "🇪", "🇫", "🇬", "🇭", "🇮", "🇯",
    "🇰", "🇱", "🇲", "🇳", "🇴", "🇵", "🇶", "🇷", "🇸", "🇹",
    "🇺", "🇻", "🇼", "🇽", "🇾",
]
MAX_POLL_OPTIONS = 25
MAX_OPTION_LENGTH = 80
MAX_POLL_SECONDS = 365 * 24 * 60 * 60
TOKEN_PATTERN = re.compile(
    r"(?P<value>\d+)\s*(?P<unit>s|sec|second|m|min|minute|h|hour|d|day|week|month|year)s?",
    re.IGNORECASE,
)
UNIT_SECONDS = {
    "s": 1,
    "sec": 1,
    "second": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "h": 3600,
    "hour": 3600,
    "d": 86400,
    "day": 86400,
    "week": 604800,
    "month": 2_592_000,
    "year": 31_536_000,
}


def parse_duration(value: str) -> int:
    text = value.strip()
    if not text:
        return 0
    matches = list(TOKEN_PATTERN.finditer(text))
    if not matches:
        return 0
    unmatched = TOKEN_PATTERN.sub("", text)
    if unmatched.strip(" ,+"):
        return 0
    seconds = sum(
        int(match.group("value"))
        * UNIT_SECONDS[match.group("unit").casefold()]
        for match in matches
    )
    return seconds if 0 < seconds <= MAX_POLL_SECONDS else 0


def parse_poll_options(raw_options: str) -> list[str]:
    options = [option.strip() for option in raw_options.split(",")]
    if not 2 <= len(options) <= MAX_POLL_OPTIONS:
        raise ValueError("Polls need between 2 and 25 options.")
    if any(not option for option in options):
        raise ValueError("Poll options cannot be blank.")
    if any(len(option) > MAX_OPTION_LENGTH for option in options):
        raise ValueError("Each poll option must be 80 characters or fewer.")
    normalized = [option.casefold() for option in options]
    if len(set(normalized)) != len(normalized):
        raise ValueError("Poll options must be unique.")
    return options


def serialize_options(options: Iterable[str]) -> str:
    return json.dumps(list(options), ensure_ascii=False)


def deserialize_options(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        parsed = ast.literal_eval(value)
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) for item in parsed
    ):
        raise ValueError("Stored poll options are invalid.")
    return parsed


class Poll(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS poll (
                title TEXT,
                options TEXT,
                endtime TEXT,
                channel INTEGER,
                msg INTEGER
            )
            """
        )
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS poll_votes (
                id INTEGER,
                user_id INTEGER,
                vote TEXT,
                PRIMARY KEY (id, user_id)
            )
            """
        )
        await self.bot.db.execute(
            """
            DELETE FROM poll
            WHERE rowid NOT IN (
                SELECT MIN(rowid)
                FROM poll
                GROUP BY msg
            )
            """
        )
        await self.bot.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_poll_message ON poll(msg)"
        )
        await self.bot.db.commit()

    async def cog_unload(self) -> None:
        self.poll_manager.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.poll_manager.is_running():
            self.poll_manager.start()

    @app_commands.command(name="poll", description="Create a polished community poll")
    @app_commands.describe(
        question="Poll question",
        options="Comma-separated options (2-25, maximum 80 characters each)",
        time="Duration such as 30m, 2h, 1d, or 1h 30m",
    )
    @app_commands.guild_only()
    async def poll(
        self,
        interaction: discord.Interaction,
        question: app_commands.Range[str, 1, 200],
        options: app_commands.Range[str, 3, 2_000],
        time: app_commands.Range[str, 1, 100],
    ) -> None:
        seconds = parse_duration(str(time))
        if not seconds:
            await interaction.response.send_message(
                embed=error_embed(
                    "Invalid duration",
                    "Use a duration like `30m`, `2h`, `1d`, or `1h 30m` "
                    "(maximum one year).",
                ),
                ephemeral=True,
            )
            return
        try:
            parsed_options = parse_poll_options(str(options))
        except ValueError as exc:
            await interaction.response.send_message(
                embed=error_embed("Invalid poll options", str(exc)),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        end = discord.utils.utcnow() + datetime.timedelta(seconds=seconds)
        provisional_data = (
            str(question).strip(),
            serialize_options(parsed_options),
            end.isoformat(),
            interaction.channel.id,
            0,
        )
        embed, file, view = await self.create_poll(provisional_data)
        try:
            poll_message = await interaction.channel.send(
                embed=embed,
                file=file,
                view=view,
                allowed_mentions=ALLOWED_MENTIONS,
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                embed=error_embed(
                    "Poll not posted",
                    "I could not post the poll in this channel. Please check "
                    "my channel permissions.",
                ),
                ephemeral=True,
            )
            return
        poll_data = (
            *provisional_data[:4],
            poll_message.id,
        )
        try:
            await self.bot.db.execute(
                """
                INSERT INTO poll (title, options, endtime, channel, msg)
                VALUES (?, ?, ?, ?, ?)
                """,
                poll_data,
            )
            await self.bot.db.commit()
        except Exception:
            await self.bot.db.rollback()
            try:
                await poll_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            raise
        await interaction.followup.send(
            embed=success_embed(
                "Poll launched",
                f"[Open the poll]({poll_message.jump_url}) • "
                f"Voting ends <t:{int(end.timestamp())}:R>.",
            ),
            ephemeral=True,
        )

    @tasks.loop(seconds=30)
    async def poll_manager(self) -> None:
        try:
            now = discord.utils.utcnow().isoformat()
            cursor = await self.bot.db.execute(
                """
                SELECT title, options, endtime, channel, msg
                FROM poll
                WHERE endtime <= ?
                ORDER BY endtime ASC
                """,
                (now,),
            )
            due_polls = await cursor.fetchall()
            await cursor.close()
            for poll_data in due_polls:
                try:
                    await self._finish_poll(poll_data)
                except Exception:
                    logger.exception(
                        "Unexpected poll finalization failure message_id=%s",
                        poll_data[4],
                    )
        except Exception:
            logger.exception("Poll manager cycle failed")

    @poll_manager.before_loop
    async def before_poll_manager(self) -> None:
        await self.bot.wait_until_ready()

    async def _finish_poll(self, poll_data: tuple) -> None:
        _, _, _, channel_id, message_id = poll_data
        channel = self.bot.get_channel(channel_id)
        try:
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            embed, file, view = await self.create_poll(poll_data)
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(
                    embed=embed,
                    attachments=[file],
                    view=view,
                    allowed_mentions=ALLOWED_MENTIONS,
                )
            except discord.NotFound:
                await channel.send(
                    embed=embed,
                    file=file,
                    view=view,
                    allowed_mentions=ALLOWED_MENTIONS,
                )
        except ValueError:
            logger.exception("Discarding malformed poll message_id=%s", message_id)
            await self._delete_poll_data(message_id)
            return
        except discord.Forbidden:
            logger.exception("Poll result cannot be posted message_id=%s", message_id)
            await self._delete_poll_data(message_id)
            return
        except discord.HTTPException:
            logger.exception("Could not finalize poll message_id=%s", message_id)
            return

        await self._delete_poll_data(message_id)

    async def _delete_poll_data(self, message_id: int) -> None:
        await self.bot.db.execute("DELETE FROM poll WHERE msg = ?", (message_id,))
        await self.bot.db.execute(
            "DELETE FROM poll_votes WHERE id = ?",
            (message_id,),
        )
        await self.bot.db.commit()

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        data = interaction.data if isinstance(interaction.data, dict) else {}
        custom_id = str(data.get("custom_id", ""))
        if not custom_id.startswith("poll|"):
            return

        if interaction.message is None:
            await interaction.response.send_message(
                "That poll button is no longer valid.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        cursor = await self.bot.db.execute(
            "SELECT title, options, endtime, channel, msg FROM poll WHERE msg = ?",
            (interaction.message.id,),
        )
        poll_data = await cursor.fetchone()
        await cursor.close()
        if poll_data is None:
            await interaction.followup.send(
                embed=warning_embed("Poll closed", "Voting has already ended."),
                ephemeral=True,
            )
            return
        try:
            options = deserialize_options(poll_data[1])
        except (SyntaxError, ValueError):
            logger.exception(
                "Discarding malformed poll options message_id=%s",
                interaction.message.id,
            )
            await self._delete_poll_data(interaction.message.id)
            await interaction.followup.send(
                "That poll is no longer valid and has been closed.",
                ephemeral=True,
            )
            return
        if custom_id.startswith("poll|||"):
            legacy_option = custom_id.split("|||", 1)[1]
            try:
                option_index = options.index(legacy_option)
            except ValueError:
                option_index = -1
        else:
            parts = custom_id.split("|", 1)
            option_index = (
                int(parts[1])
                if len(parts) == 2 and parts[1].isdigit()
                else -1
            )
        if option_index < 0 or option_index >= len(options):
            await interaction.followup.send(
                "That poll option is no longer valid.",
                ephemeral=True,
            )
            return

        option = options[option_index]
        await self.bot.db.execute(
            """
            INSERT OR REPLACE INTO poll_votes (id, user_id, vote)
            VALUES (?, ?, ?)
            """,
            (interaction.message.id, interaction.user.id, option),
        )
        await self.bot.db.commit()
        await interaction.followup.send(
            embed=success_embed(
                "Vote recorded",
                f"You voted for **{discord.utils.escape_markdown(option)}**. "
                "You can change your vote until the poll closes.",
            ),
            ephemeral=True,
        )

    async def create_poll(
        self,
        poll_data: tuple,
    ) -> tuple[discord.Embed, discord.File, discord.ui.View]:
        title, raw_options, endtime, _, message_id = poll_data
        options = deserialize_options(raw_options)[:MAX_POLL_OPTIONS]
        end = datetime.datetime.fromisoformat(str(endtime))
        if end.tzinfo is None:
            end = end.replace(tzinfo=datetime.timezone.utc)
        active = end > discord.utils.utcnow()

        cursor = await self.bot.db.execute(
            """
            SELECT vote, COUNT(*)
            FROM poll_votes
            WHERE id = ?
            GROUP BY vote
            """,
            (message_id,),
        )
        counts = dict(await cursor.fetchall())
        await cursor.close()
        total = sum(counts.values())

        if active:
            embed = branded_embed(
                f"📊 {title}",
                f"Choose one option below.\nVoting ends "
                f"<t:{int(end.timestamp())}:R>.",
                color=INFO_COLOR,
                footer=f"Bro Eden Poll • {total:,} vote(s) cast",
            )
            file = discord.File("assets/votenow.png", filename="poll.png")
        else:
            embed = branded_embed(
                f"🏁 {title}",
                f"Voting closed <t:{int(end.timestamp())}:R>.\n"
                f"**{total:,} total vote(s)**",
                color=INFO_COLOR,
                footer="Bro Eden Poll • Final results",
            )
            file = discord.File("assets/results.png", filename="poll.png")
        embed.set_image(url="attachment://poll.png")

        view = discord.ui.View(timeout=None)
        for index, option in enumerate(options):
            safe_option = discord.utils.escape_markdown(option)
            if active:
                embed.add_field(
                    name=f"{EMOJIS[index]} {safe_option}"[:256],
                    value="Tap the matching button to vote.",
                    inline=False,
                )
            else:
                count = int(counts.get(option, 0))
                percent = count / max(total, 1) * 100
                embed.add_field(
                    name=f"{EMOJIS[index]} {safe_option}"[:256],
                    value=(
                        f"`{progress_bar(count, total, width=12)}` "
                        f"**{count:,}** vote(s) • {percent:.1f}%"
                    ),
                    inline=False,
                )
            view.add_item(
                discord.ui.Button(
                    label=option,
                    emoji=EMOJIS[index],
                    custom_id=f"poll|{index}",
                    style=discord.ButtonStyle.primary,
                    disabled=not active,
                )
            )
        return embed, file, view


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Poll(bot))
