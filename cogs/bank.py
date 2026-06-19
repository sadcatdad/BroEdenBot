import datetime
import math
import os

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from config import COLOR


DATABASE_PATH = "brobank.db"
SUPPORTED_BY_BANK = (
    "Funds support Bro Eden events, giveaways, bots, server improvements, "
    "and new features."
)


def allowed_role_ids():
    role_ids = set()
    for value in os.getenv("BANK_ALLOWED_ROLE_IDS", "").split(","):
        value = value.strip()
        if value.isdigit():
            role_ids.add(int(value))
    return role_ids


async def has_bank_access(interaction: discord.Interaction):
    permissions = getattr(interaction.user, "guild_permissions", None)
    if permissions and permissions.administrator:
        return True

    permitted_roles = allowed_role_ids()
    return any(
        role.id in permitted_roles
        for role in getattr(interaction.user, "roles", [])
    )


class Bank(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = None

    async def cog_load(self):
        self.db = await aiosqlite.connect(DATABASE_PATH)
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS bank_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                discord_user_id INTEGER,
                display_name TEXT,
                amount REAL NOT NULL,
                note TEXT,
                is_public INTEGER DEFAULT 1,
                source TEXT DEFAULT 'manual',
                external_id TEXT,
                created_at TEXT NOT NULL,
                created_by INTEGER
            )
            """
        )
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS bank_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        await self.db.commit()

    async def cog_unload(self):
        if self.db:
            await self.db.close()

    bank = app_commands.Group(name="bank", description="Bro Eden Bank commands")

    @bank.command(name="add", description="Add a contribution to the Bro Eden Bank")
    @app_commands.describe(
        user="Discord member making the contribution",
        amount="Contribution amount",
        note="Short public-safe description",
    )
    @app_commands.check(has_bank_access)
    async def add(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: float,
        note: str,
    ):
        amount = self.valid_amount(amount)
        if amount is None:
            await interaction.response.send_message(
                "Amount must be a positive number.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        await self.add_transaction(
            transaction_type="contribution",
            amount=amount,
            note=note,
            created_by=interaction.user.id,
            discord_user_id=user.id,
            display_name=user.display_name,
            is_public=True,
        )

        await interaction.followup.send(
            f"Added {self.money(amount)} from {user.mention}.", ephemeral=True
        )
        await self.refresh_configured_embed()

    @bank.command(name="expense", description="Record a Bro Eden Bank expense")
    @app_commands.describe(amount="Expense amount", note="What the funds supported")
    @app_commands.check(has_bank_access)
    async def expense(
        self, interaction: discord.Interaction, amount: float, note: str
    ):
        amount = self.valid_amount(amount)
        if amount is None:
            await interaction.response.send_message(
                "Amount must be a positive number.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        await self.add_transaction(
            transaction_type="expense",
            amount=amount,
            note=note,
            created_by=interaction.user.id,
        )
        await interaction.followup.send(
            f"Recorded an expense of {self.money(amount)}.", ephemeral=True
        )
        await self.refresh_configured_embed()

    @bank.command(name="balance", description="Show the available bank balance")
    @app_commands.check(has_bank_access)
    async def balance(self, interaction: discord.Interaction):
        totals = await self.get_totals()
        await interaction.response.send_message(
            (
                f"**Available balance:** {self.money(totals['balance'])}\n"
                f"Contributions: {self.money(totals['contributions'])}\n"
                f"Expenses: {self.money(totals['expenses'])}"
            ),
            ephemeral=True,
        )

    @bank.command(
        name="leaderboard", description="Show totals for public contributors"
    )
    @app_commands.check(has_bank_access)
    async def leaderboard(self, interaction: discord.Interaction):
        contributors = await self.get_top_contributors(limit=10)
        if contributors:
            lines = [
                f"**{index}.** {self.public_donor_name(user_id, name)} — "
                f"{self.money(amount)}"
                for index, (user_id, name, amount) in enumerate(
                    contributors, start=1
                )
            ]
            description = "\n".join(lines)
        else:
            description = "No public contributions have been recorded yet."

        embed = discord.Embed(
            title="Bro Eden Bank Leaderboard",
            description=description,
            color=COLOR,
        )
        await interaction.response.send_message(embed=embed)

    @bank.command(
        name="refresh", description="Create or update the public bank summary here"
    )
    @app_commands.check(has_bank_access)
    async def refresh(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "Use this command in a server text channel.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        message = await self.publish_embed(interaction.channel)
        await self.set_setting("bank_channel_id", str(interaction.channel.id))
        await self.set_setting("bank_message_id", str(message.id))
        await interaction.followup.send(
            f"Bank summary refreshed in {interaction.channel.mention}.",
            ephemeral=True,
        )

    @bank.command(
        name="setchannel", description="Set this as the public bank channel"
    )
    @app_commands.check(has_bank_access)
    async def setchannel(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "Use this command in a server text channel.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        await self.set_setting("bank_channel_id", str(interaction.channel.id))
        await self.set_setting("bank_message_id", "")
        message = await self.publish_embed(interaction.channel)
        await self.set_setting("bank_message_id", str(message.id))
        await interaction.followup.send(
            f"{interaction.channel.mention} is now the public bank channel.",
            ephemeral=True,
        )

    @bank.command(name="clear", description="Clear all BroBank test data")
    @app_commands.check(has_bank_access)
    async def clear(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.db.execute("DELETE FROM bank_transactions")
        await self.db.execute(
            "DELETE FROM sqlite_sequence WHERE name = 'bank_transactions'"
        )
        await self.db.execute(
            "DELETE FROM bank_settings WHERE key = 'bank_message_id'"
        )
        await self.db.commit()
        await interaction.followup.send(
            "BroBank test data has been cleared. Run /bank refresh to create "
            "a fresh public embed.",
            ephemeral=True,
        )

    async def add_transaction(
        self,
        transaction_type: str,
        amount: float,
        note: str,
        created_by: int,
        discord_user_id: int = None,
        display_name: str = None,
        is_public: bool = True,
    ):
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        await self.db.execute(
            """
            INSERT INTO bank_transactions (
                type,
                discord_user_id,
                display_name,
                amount,
                note,
                is_public,
                source,
                external_id,
                created_at,
                created_by
            )
            VALUES (?, ?, ?, ?, ?, ?, 'manual', NULL, ?, ?)
            """,
            (
                transaction_type,
                discord_user_id,
                display_name,
                amount,
                note.strip(),
                int(is_public),
                created_at,
                created_by,
            ),
        )
        await self.db.commit()

    async def get_totals(self):
        cursor = await self.db.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN type = 'contribution' THEN amount END), 0),
                COALESCE(SUM(CASE WHEN type = 'expense' THEN amount END), 0),
                COALESCE(SUM(
                    CASE
                        WHEN type = 'contribution' THEN amount
                        WHEN type = 'expense' THEN -amount
                        WHEN type = 'adjustment' THEN amount
                        ELSE 0
                    END
                ), 0)
            FROM bank_transactions
            """
        )
        contributions, expenses, balance = await cursor.fetchone()

        cursor = await self.db.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT COALESCE(
                    CAST(discord_user_id AS TEXT),
                    NULLIF(display_name, ''),
                    'private-' || id
                )
                FROM bank_transactions
                WHERE type = 'contribution'
                GROUP BY 1
            )
            """
        )
        contributor_count = (await cursor.fetchone())[0]
        return {
            "contributions": contributions,
            "expenses": expenses,
            "balance": balance,
            "contributors": contributor_count,
        }

    async def get_top_contributors(self, limit=5):
        cursor = await self.db.execute(
            """
            SELECT
                discord_user_id,
                NULLIF(display_name, '') AS donor_name,
                SUM(amount) AS total
            FROM bank_transactions
            WHERE
                type = 'contribution'
                AND is_public = 1
                AND (discord_user_id IS NOT NULL OR NULLIF(display_name, '') IS NOT NULL)
            GROUP BY discord_user_id, donor_name
            ORDER BY total DESC
            LIMIT ?
            """,
            (limit,),
        )
        return await cursor.fetchall()

    async def get_recent_activity(self, limit=5):
        cursor = await self.db.execute(
            """
            SELECT
                type,
                discord_user_id,
                display_name,
                amount,
                note,
                is_public,
                created_at
            FROM bank_transactions
            WHERE
                type != 'contribution'
                OR (
                    is_public = 1
                    AND (
                        discord_user_id IS NOT NULL
                        OR NULLIF(display_name, '') IS NOT NULL
                    )
                )
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return await cursor.fetchall()

    async def build_public_embed(self):
        totals = await self.get_totals()
        contributors = await self.get_top_contributors()
        activity = await self.get_recent_activity()

        embed = discord.Embed(
            title="🏦 Bro Eden Bank",
            description=SUPPORTED_BY_BANK,
            color=COLOR,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(
            name="Available Balance",
            value=f"**{self.money(totals['balance'])}**",
            inline=False,
        )
        embed.add_field(
            name="Total Contributions",
            value=self.money(totals["contributions"]),
        )
        embed.add_field(
            name="Total Expenses",
            value=self.money(totals["expenses"]),
        )
        embed.add_field(
            name="Contributors",
            value=f"{totals['contributors']:,}",
        )

        if contributors:
            leaderboard = "\n".join(
                f"**{index}.** {self.public_donor_name(user_id, name)} — "
                f"{self.money(amount)}"
                for index, (user_id, name, amount) in enumerate(
                    contributors, start=1
                )
            )
        else:
            leaderboard = "No public contributions yet."
        embed.add_field(name="Top Contributors", value=leaderboard, inline=False)

        if activity:
            recent = "\n".join(
                self.format_activity(*transaction) for transaction in activity
            )
        else:
            recent = "No activity recorded yet."
        embed.add_field(name="Recent Activity", value=recent, inline=False)
        embed.set_footer(text="Bro Eden Bank • Updated")
        return embed

    async def publish_embed(self, channel: discord.TextChannel):
        embed = await self.build_public_embed()
        message_id = await self.get_setting("bank_message_id")

        if message_id:
            try:
                message = await channel.fetch_message(int(message_id))
                await message.edit(embed=embed)
                return message
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        return await channel.send(embed=embed)

    async def refresh_configured_embed(self):
        channel_id = await self.get_setting("bank_channel_id")
        if not channel_id:
            return

        channel = self.bot.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            return

        try:
            message = await self.publish_embed(channel)
            await self.set_setting("bank_message_id", str(message.id))
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def get_setting(self, key: str):
        cursor = await self.db.execute(
            "SELECT value FROM bank_settings WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set_setting(self, key: str, value: str):
        await self.db.execute(
            """
            INSERT INTO bank_settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await self.db.commit()

    @staticmethod
    def valid_amount(amount: float):
        if not math.isfinite(amount) or amount <= 0:
            return None
        return round(amount, 2)

    @staticmethod
    def money(amount: float):
        return f"${amount:,.2f} USD"

    @staticmethod
    def public_donor_name(user_id, display_name):
        if user_id:
            return f"<@{user_id}>"
        return discord.utils.escape_markdown(display_name)

    def format_activity(
        self,
        transaction_type,
        discord_user_id,
        display_name,
        amount,
        note,
        is_public,
        created_at,
    ):
        timestamp = int(datetime.datetime.fromisoformat(created_at).timestamp())
        safe_note = discord.utils.escape_markdown(note or "No note")

        if transaction_type == "contribution":
            donor = self.public_donor_name(discord_user_id, display_name)
            return (
                f"➕ **{self.money(amount)}** from {donor} — {safe_note} "
                f"• <t:{timestamp}:R>"
            )

        if transaction_type == "expense":
            return (
                f"➖ **{self.money(amount)}** expense — {safe_note} "
                f"• <t:{timestamp}:R>"
            )

        direction = "+" if amount >= 0 else "−"
        return (
            f"🔧 **{direction}{self.money(abs(amount))}** adjustment — "
            f"{safe_note} • <t:{timestamp}:R>"
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.CheckFailure):
            message = "You do not have permission to use BroBank commands."
        else:
            message = "The bank command could not be completed."

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Bank(bot))
