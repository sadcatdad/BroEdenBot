import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from utils.audit_log import publish_audit


class AuditLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_audit_destination_suppresses_mentions(self):
        channel = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            send=AsyncMock(),
        )
        bot = SimpleNamespace(
            get_channel=lambda _channel_id: channel,
            fetch_channel=AsyncMock(),
        )
        guild = SimpleNamespace(id=1)

        with patch("utils.audit_log.get_setting", return_value="123"):
            sent = await publish_audit(bot, guild, "Audit", "Changed by <@42>")

        self.assertTrue(sent)
        kwargs = channel.send.await_args.kwargs
        self.assertFalse(kwargs["allowed_mentions"].users)
        self.assertFalse(kwargs["allowed_mentions"].roles)
        self.assertFalse(kwargs["allowed_mentions"].everyone)

    async def test_missing_audit_destination_is_a_safe_noop(self):
        bot = SimpleNamespace(get_channel=AsyncMock(), fetch_channel=AsyncMock())
        with patch("utils.audit_log.get_setting", return_value=""):
            sent = await publish_audit(bot, SimpleNamespace(id=1), "Audit", "Test")
        self.assertFalse(sent)
        bot.get_channel.assert_not_awaited()
