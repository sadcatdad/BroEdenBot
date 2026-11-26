"""Persistent routing for buttons posted by Automations."""

from __future__ import annotations

import re

import discord


class AutomationButton(discord.ui.DynamicItem[discord.ui.Button], template=r"auto_btn:(?P<rule_id>[1-9][0-9]{0,18}):(?P<button_id>[A-Za-z0-9_-]{1,32})"):
    def __init__(self, rule_id, button_id, *, label=None, style=discord.ButtonStyle.secondary, item=None):
        super().__init__(item if item is not None else discord.ui.Button(
            label=label, style=style, custom_id=f"auto_btn:{rule_id}:{button_id}"
        ))
        self.rule_id = rule_id
        self.button_id = button_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["rule_id"]), match["button_id"], item=item)

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("AutomationCog")
        if cog is None:
            await interaction.response.send_message("Automations are currently unavailable.", ephemeral=True)
            return
        await cog.handle_button(interaction, self.rule_id, self.button_id)


def button_view(rule_id, buttons):
    view = discord.ui.View(timeout=None)
    for button in buttons:
        view.add_item(AutomationButton(
            rule_id, button.id, label=button.label, style=getattr(discord.ButtonStyle, button.style),
        ))
    return view
