import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import access


class DummyPermissions:
    def __init__(self, administrator=False):
        self.administrator = administrator


class DummyRole:
    def __init__(self, role_id):
        self.id = role_id


class DummyMember:
    def __init__(self, user_id, *, role_ids=(), administrator=False):
        self.id = user_id
        self.roles = [DummyRole(role_id) for role_id in role_ids]
        self.guild_permissions = DummyPermissions(administrator)


class AccessTests(unittest.TestCase):
    def test_owner_is_read_from_environment(self):
        with patch("utils.access.get_csv_ids_setting", return_value=[123]):
            self.assertTrue(access.is_configured_owner(DummyMember(123)))
            self.assertFalse(access.is_configured_owner(DummyMember(456)))

    def test_staff_includes_configured_roles_and_administrators(self):
        values = {
            "BOT_OWNER_USER_IDS": [],
            "OWNER_ROLE_IDS": [10],
            "ADMIN_ROLE_IDS": [20],
            "MODERATOR_ROLE_IDS": [30],
            "STAFF_ROLE_IDS": [40],
        }
        with patch(
            "utils.access.get_csv_ids_setting",
            side_effect=lambda key: values.get(key, []),
        ):
            self.assertTrue(access.is_configured_staff(DummyMember(1, role_ids=[30])))
            self.assertTrue(access.is_configured_staff(DummyMember(2, administrator=True)))
            self.assertFalse(access.is_configured_staff(DummyMember(3, role_ids=[99])))

    def test_repository_config_is_safe_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"owner_user_ids": ["987"]}))
            with patch("utils.access.CONFIG_PATH", path), patch(
                "utils.access.get_csv_ids_setting",
                return_value=[],
            ), patch(
                "utils.access.get_json_ids_setting",
                return_value=[],
            ):
                self.assertTrue(access.is_configured_owner(DummyMember(987)))

    def test_role_sources_are_merged_instead_of_replacing_each_other(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"staff_role_ids": ["300"]}))
            with patch("utils.access.CONFIG_PATH", path), patch(
                "utils.access.get_csv_ids_setting",
                return_value=[100],
            ), patch(
                "utils.access.get_json_ids_setting",
                return_value=[200],
            ):
                self.assertEqual(
                    access._configured_ids("STAFF_ROLE_IDS", "staff_role_ids"),
                    {100, 200, 300},
                )
