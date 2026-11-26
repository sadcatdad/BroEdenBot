import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from automations.cog import AutomationCog
from automations.evaluator import Event, User
from automations.executor import Executor, source_audience_allows
from automations.models import Rule
from automations.storage import MissingRule, RevisionConflict, Store
from automations.views import AutomationButton, button_view


def definition(actions=None, conditions=None):
    return Rule.model_validate({
        "name": "An example", "trigger": {"type": "MESSAGE"}, "conditions": conditions,
        "actions": actions or [{"type": "SEND_MESSAGE", "template": "Hello {actor.mention}"}],
    })


def server_channel(guild, channel_id, *, public=True, overwrites=None):
    channel = Mock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.guild = guild
    channel.is_nsfw.return_value = False
    channel.overwrites = overwrites or {}
    channel.permissions_for.return_value = discord.Permissions(view_channel=public, read_message_history=public)
    return channel


def test_storage_cooldowns_concurrent_claim_and_revision_guards(tmp_path):
    async def exercise():
        path = tmp_path / "rules.db"
        async with Store.open(path) as first, Store.open(path) as second:
            await first.initialize()
            model = definition(conditions={"operator": "AND", "rules": [{
                "type": "COOLDOWN_READY", "value": {"scope": "user", "seconds": 10},
            }]})
            rule = await first.create("123", model, "editor")
            with pytest.raises(MissingRule):
                await second.get("999", rule.id)
            rule = await first.update("123", rule.id, rule.revision, "editor", enabled=True)
            event = Event("MESSAGE", actor=User("456"))
            claims = await asyncio.gather(*(
                store.claim(rule, event, f"event-{i}", bot_id="999", now=100)
                for i, store in enumerate((first, second))
            ))
            assert sum(run_id is not None for run_id in claims) == 1
            run_id = next(x for x in claims if x is not None)
            await first.finish(run_id, "succeeded", [{"type": "SEND_MESSAGE", "status": "succeeded"}])
            assert await first.claim(rule, event, "first", bot_id="999", now=109.9) is None
            assert await first.claim(rule, event, "first", bot_id="999", now=110)
            assert await second.claim(rule, event, "first", bot_id="999", now=121) is None
            new = await second.update("123", rule.id, rule.revision, "editor2", enabled=False)
            assert not new.rule.enabled
            assert await first.claim(rule, event, "late", bot_id="999", now=130) is None
            with pytest.raises(RevisionConflict):
                await first.update("123", rule.id, rule.revision, "editor", enabled=True)
            history = await first.history("123", rule.id)
            assert [version["operation"] for version in history["revisions"]] == ["disable", "enable", "create"]
            assert history["runs"][-1]["status"] == "succeeded"

    asyncio.run(exercise())


def test_executor_isolates_error_paces_order_and_preserves_mentions(tmp_path, monkeypatch):
    async def exercise():
        async with Store.open(tmp_path / "rules.db") as store:
            await store.initialize()
            row = await store.create("1", definition([
                {"type": "DM_USER", "target": "subject", "template": "Unavailable"},
                {"type": "SEND_MESSAGE", "template": "Hi {actor.mention}"},
            ]), "owner")
            guild = SimpleNamespace(id=1)
            channel = server_channel(guild, 10)
            sleep = AsyncMock()
            monkeypatch.setattr("automations.executor.asyncio.sleep", sleep)
            runner = Executor(guild, Event("MESSAGE", actor=User("22")), channel=channel, bot_id=30, stored=row)
            results = await runner.run(AsyncMock(return_value=True))
            assert [result["status"] for result in results] == ["failed", "succeeded"]
            sleep.assert_awaited_once_with(0.25)
            assert channel.send.await_args.kwargs["content"] == "Hi <@22>"
            assert channel.send.await_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}

    asyncio.run(exercise())


def test_restricted_trigger_channel_cannot_send_to_a_wider_audience():
    guild = SimpleNamespace(id=1, default_role=object())
    role = discord.Object(id=1)
    private = server_channel(guild, 101, public=False, overwrites={role: discord.PermissionOverwrite(view_channel=False)})
    equally_private = server_channel(guild, 102, public=False, overwrites={role: discord.PermissionOverwrite(view_channel=False)})
    public = server_channel(guild, 103)
    assert not source_audience_allows(private, public)
    assert source_audience_allows(private, equally_private)
    assert source_audience_allows(public, private)
    private_thread = Mock(spec=discord.Thread, id=104, guild=guild)
    private_thread.is_private.return_value = True
    assert not source_audience_allows(private_thread, public)


def test_gateway_path_refreshes_rules_from_second_writer_and_dispatches_once(tmp_path, monkeypatch):
    async def exercise():
        path = tmp_path / "rules.db"
        guild = SimpleNamespace(id=100, name="Test guild")
        user = discord.Object(id=300)
        user.display_name, user.bot = "Fern", False
        channel = server_channel(guild, 200)
        bot = SimpleNamespace(user=discord.Object(id=500), intents=discord.Intents.none())
        monkeypatch.setattr("automations.cog.configured_guild", lambda: "100")
        cog = AutomationCog(bot)
        message = SimpleNamespace(
            id=999, guild=guild, webhook_id=None, channel=channel, author=user, reference=None,
            mentions=[], role_mentions=[], attachments=[], embeds=[], content="sample",
            jump_url="https://discord.test/channels/100/200/999",
        )
        async with Store.open(path) as engine, Store.open(path) as dashboard:
            cog.store = engine
            await engine.initialize()
            row = await dashboard.create("100", definition(), "dashboard")
            await cog.refresh()
            assert not cog.rules_by_trigger
            await dashboard.update("100", row.id, row.revision, "dashboard", enabled=True)
            await cog.refresh()
            await cog.on_message(message)
            event = cog.queue.get_nowait()
            await cog.process(*event[:2], *event[2])
            await cog.process(*event[:2], *event[2])
            channel.send.assert_awaited_once()
            rule = await dashboard.get("100", row.id)
            assert rule.last_triggered_at
            await dashboard.update("100", row.id, rule.revision, "dashboard", enabled=False)
            message.id = 998
            await cog.process("MESSAGE", guild, message)
            channel.send.assert_awaited_once()

    asyncio.run(exercise())


def test_persistent_button_custom_id_can_be_reconstructed_after_restart():
    async def exercise():
        action = definition([{"type": "SEND_MESSAGE", "buttons": [{"id": "join", "label": "Join"}]}]).actions[0]
        view = button_view(12, action.buttons)
        item = view.children[0]
        assert view.is_persistent()
        rule_id, button_id = 12, "join"
        custom_id = f"auto_btn:{rule_id}:{button_id}"
        assert item.custom_id == custom_id
        match = AutomationButton.__discord_ui_compiled_template__.fullmatch(custom_id)
        route = Mock()
        route.handle_button = AsyncMock()
        interaction = SimpleNamespace(client=SimpleNamespace(get_cog=Mock(return_value=route)))
        restored = await AutomationButton.from_custom_id(interaction, item.item, match)
        await restored.callback(interaction)
        route.handle_button.assert_awaited_once_with(interaction, rule_id, button_id)

    asyncio.run(exercise())


def test_voice_move_cannot_publish_activity_from_private_previous_channel(tmp_path):
    async def exercise():
        async with Store.open(tmp_path / "rules.db") as store:
            await store.initialize()
            row = await store.create("1", definition(), "owner")
        guild = SimpleNamespace(id=1, default_role=object())
        role = discord.Object(id=1)
        private = server_channel(guild, 101, public=False, overwrites={role: discord.PermissionOverwrite(view_channel=False)})
        public = server_channel(guild, 103)
        runner = Executor(guild, Event("VOICE_STATE_UPDATE", actor=User("22")), channel=public,
                          previous_channel=private, bot_id=30, stored=row)
        results = await runner.run(AsyncMock(return_value=True))
        assert results[0]["status"] == "failed"
        assert "restricted channel" in results[0]["detail"]
        public.send.assert_not_awaited()

    asyncio.run(exercise())


def test_real_extension_lifecycle_registers_and_removes_commands_and_views(tmp_path, monkeypatch):
    from discord.ext import commands

    async def exercise():
        monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "extension.db"))
        monkeypatch.setattr("automations.cog.configured_guild", lambda: "100")
        async with commands.Bot(command_prefix="!", intents=discord.Intents.none()) as bot:
            await bot.load_extension("cogs.automations")
            cog = bot.get_cog("AutomationCog")
            rows = await cog.store.list("100")
            assert len(rows) == 1 and not rows[0].rule.enabled
            assert bot.tree.get_command("automation").get_command("test")
            pattern = AutomationButton.__discord_ui_compiled_template__
            assert pattern in bot._connection._view_store._dynamic_items
            await bot.reload_extension("cogs.automations")
            assert bot.get_cog("AutomationCog") is not cog
            assert all(task.cancelled() for task in cog.tasks)
            await bot.unload_extension("cogs.automations")
            assert bot.tree.get_command("automation") is None
            assert pattern not in bot._connection._view_store._dynamic_items

    asyncio.run(exercise())
