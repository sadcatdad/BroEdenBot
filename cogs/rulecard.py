"""AI-assisted rule reminder drafts for staff review."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from cogs.mod_ai import (
    RULE_REMINDER_FOOTER,
    RULE_REMINDER_HEADER,
    _canonical_user_mentions,
    format_public_rule_card_embed,
)
from utils.ai_kb import format_kb_context, search_kb
from utils.ai_service import (
    AI_BUDGET_MESSAGE,
    check_ai_cooldown,
    generate_ai_response,
    set_ai_cooldown,
)
from utils.settings import get_csv_ids_setting
from utils.ui import branded_embed


logger = logging.getLogger(__name__)


def _has_rulecard_access(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    allowed_role_ids = set(get_csv_ids_setting("MODAI_ALLOWED_ROLE_IDS"))
    return any(role.id in allowed_role_ids for role in interaction.user.roles)


def _safe_json_object(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^\s*```(?:json)?|```\s*$", "", str(text or "").strip())
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return {
            "publicCardText": cleaned,
            "privateStaffNote": "Gemini did not return structured JSON.",
            "matchedSources": [],
        }
    return parsed if isinstance(parsed, dict) else {}


def _rulecard_embed_from_text(text: str) -> discord.Embed:
    return format_public_rule_card_embed({"rule_reminder": text})


class RulecardDraftView(discord.ui.View):
    def __init__(
        self,
        *,
        creator_id: int,
        channel: Any,
        embed: discord.Embed,
        mention_content: str,
    ) -> None:
        super().__init__(timeout=900)
        self.creator_id = creator_id
        self.channel = channel
        self.embed = embed
        self.mention_content = mention_content
        self._posted = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.creator_id:
            return True
        await interaction.response.send_message(
            "Only the staff member who generated this draft can use these buttons.",
            ephemeral=True,
        )
        return False

    async def _post(self, interaction: discord.Interaction, *, with_mentions: bool) -> None:
        if self._posted:
            await interaction.response.send_message("This draft was already posted.", ephemeral=True)
            return
        content = self.mention_content if with_mentions and self.mention_content else None
        try:
            await self.channel.send(
                content=content,
                embed=self.embed,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    roles=False,
                    users=bool(content),
                    replied_user=False,
                ),
            )
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            logger.exception("Could not post AI rulecard draft")
            await interaction.response.send_message(
                "I could not post that rule reminder. Check my channel permissions.",
                ephemeral=True,
            )
            return
        self._posted = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="Rule reminder posted.",
            view=self,
        )

    @discord.ui.button(label="Post without mention", style=discord.ButtonStyle.primary)
    async def post_without_mention(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self._post(interaction, with_mentions=False)

    @discord.ui.button(label="Post with mention(s)", style=discord.ButtonStyle.secondary)
    async def post_with_mentions(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self._post(interaction, with_mentions=True)

    @discord.ui.button(label="Discard", style=discord.ButtonStyle.danger)
    async def discard(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="Rule reminder draft discarded.",
            view=self,
        )
        self.stop()


class Rulecard(commands.Cog):
    rulecard = app_commands.Group(
        name="rulecard",
        description="Staff-reviewed rule reminder drafts",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.context_menu = app_commands.ContextMenu(
            name="Draft Rule Reminder",
            callback=self.draft_from_message,
        )

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self.context_menu, override=True)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(
            self.context_menu.name,
            type=discord.AppCommandType.message,
        )

    async def _deny(self, interaction: discord.Interaction) -> bool:
        if _has_rulecard_access(interaction):
            return False
        await interaction.response.send_message(
            "This rulecard tool is limited to administrators and approved staff roles.",
            ephemeral=True,
        )
        return True

    async def _cooldown(self, interaction: discord.Interaction) -> bool:
        ok, retry_after = await check_ai_cooldown(interaction.user.id, "staff")
        if ok:
            return False
        await interaction.response.send_message(
            f"Please wait {max(1, int(retry_after))} seconds before using another staff AI tool.",
            ephemeral=True,
        )
        return True

    async def _draft(
        self,
        interaction: discord.Interaction,
        *,
        topic: str,
        tone: str,
        target_channel: Any,
        mention_content: str = "",
        selected_message: Optional[discord.Message] = None,
        source_message_link: str = "",
    ) -> None:
        chunks = search_kb(
            query=topic,
            visibility="staff",
            limit=6,
            source_types=("rule", "guide", "staff_note", "faq"),
        )
        kb_context = format_kb_context(chunks, max_chars=14_000)
        selected_context = ""
        if selected_message is not None:
            selected_context = (
                f"Selected message author: {selected_message.author} ({selected_message.author.id})\n"
                f"Selected message link: {selected_message.jump_url}\n"
                f"Selected message content excerpt:\n{selected_message.content[:1200]}"
            )
        prompt = f"""
You are drafting a Discord server rule reminder card for staff review.
The AI only drafts. Staff will decide whether to post.

Tone: {tone}
Topic or issue: {topic}
Source message link: {source_message_link or "none"}

<selected_message_context>
{selected_context or "none"}
</selected_message_context>

<kb_context>
{kb_context or "No matching KB context found."}
</kb_context>

Rules:
- Do not accuse, shame, diagnose, or escalate.
- Do not invent policy. If the matching rule is unclear, say so in privateStaffNote.
- Do not include long quotes from the source message.
- publicCardText must be suitable to post publicly.
- Prefer this public format:
{RULE_REMINDER_HEADER}
Short reminder text.

Please keep this in mind going forward.

{RULE_REMINDER_FOOTER}

Return ONLY JSON with:
publicCardText, privateStaffNote, matchedSources.
""".strip()
        result = await generate_ai_response(
            task_type="rulecard_draft",
            prompt=prompt,
            requested_tier="default",
            max_output_tokens=900,
            temperature=0.3,
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
            channel_id=getattr(target_channel, "id", interaction.channel_id),
            source_command="/rulecard draft",
            metadata={"source_message_link": source_message_link},
            db=getattr(self.bot, "db", None),
        )
        if result.blocked_by_budget:
            await interaction.followup.send(AI_BUDGET_MESSAGE, ephemeral=True)
            return
        if not result.ok or not result.text:
            await interaction.followup.send(
                result.error or "The rule reminder draft could not be generated.",
                ephemeral=True,
            )
            return
        await set_ai_cooldown(interaction.user.id, "staff")
        parsed = _safe_json_object(result.text)
        public_text = str(parsed.get("publicCardText") or result.text).strip()
        private_note = str(parsed.get("privateStaffNote") or "No private note returned.").strip()
        matched_sources = parsed.get("matchedSources") or [
            f"{chunk['source_name']} / {chunk.get('section_title') or 'General'}"
            for chunk in chunks[:3]
        ]
        embed = _rulecard_embed_from_text(public_text)
        view = RulecardDraftView(
            creator_id=interaction.user.id,
            channel=target_channel,
            embed=embed,
            mention_content=mention_content,
        )
        note = (
            f"Target channel: {getattr(target_channel, 'mention', target_channel)}\n"
            f"Mention behavior: {'available' if mention_content else 'no mentions provided'}\n"
            f"Private staff note: {private_note}\n"
            f"Matched sources: {', '.join(str(item) for item in matched_sources) or 'none'}"
        )
        await interaction.followup.send(
            content=note[:1900],
            embed=embed,
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @rulecard.command(name="draft", description="Draft a rule reminder for staff review")
    @app_commands.describe(
        topic_or_issue="Rule topic or situation",
        mentioned_user="Optional member to mention if posted with mentions",
        tone="Draft tone",
        channel="Target channel; defaults to current channel",
        source_message_link="Optional source message link",
    )
    @app_commands.choices(
        tone=[
            app_commands.Choice(name="friendly", value="friendly"),
            app_commands.Choice(name="firm", value="firm"),
            app_commands.Choice(name="serious", value="serious"),
            app_commands.Choice(name="brief", value="brief"),
        ]
    )
    @app_commands.guild_only()
    async def draft(
        self,
        interaction: discord.Interaction,
        topic_or_issue: app_commands.Range[str, 1, 700],
        mentioned_user: Optional[discord.Member] = None,
        tone: Optional[app_commands.Choice[str]] = None,
        channel: Optional[
            Union[discord.TextChannel, discord.Thread, discord.ForumChannel]
        ] = None,
        source_message_link: Optional[str] = None,
    ) -> None:
        if await self._deny(interaction) or await self._cooldown(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        target_channel = channel or interaction.channel
        mention_content = mentioned_user.mention if mentioned_user else ""
        await self._draft(
            interaction,
            topic=topic_or_issue,
            tone=tone.value if tone else "friendly",
            target_channel=target_channel,
            mention_content=mention_content,
            source_message_link=source_message_link or "",
        )

    async def draft_from_message(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        if await self._deny(interaction) or await self._cooldown(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._draft(
            interaction,
            topic=message.content[:700] or "General server rule reminder",
            tone="friendly",
            target_channel=message.channel,
            mention_content=message.author.mention,
            selected_message=message,
            source_message_link=message.jump_url,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Rulecard(bot))
