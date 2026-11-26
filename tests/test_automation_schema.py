from copy import deepcopy

import pytest
from pydantic import ValidationError

from automations.models import MAX_DEPTH, Action, DryRun, Rule


def make_rule(**values):
    return {
        "name": "Welcome",
        "trigger": {"type": "MESSAGE"},
        "actions": [{"type": "SEND_MESSAGE", "template": "Hello {actor.mention}"}],
        **values,
    }


def test_recursive_schema_preserves_types_and_defaults():
    tree = {"operator": "AND", "rules": [
        {"type": "IS_BOT", "target": "subject", "value": False},
        {"operator": "OR", "rules": [
            {"type": "KEYWORD_MATCH", "value": "(?i)hello"},
            {"operator": "NOT", "rules": [{"type": "HAS_ROLE", "target": "321", "value": "654"}]},
        ]},
        {"type": "COOLDOWN_READY", "value": {"scope": "channel", "seconds": 5}},
    ]}
    rule = Rule.model_validate(make_rule(conditions=tree))
    assert rule.conditions.rules[0].value is False
    assert rule.conditions.rules[2].value.seconds == 5
    assert not rule.enabled and not rule.allow_bots
    assert Rule.model_validate_json(rule.model_dump_json(exclude_none=True)) == rule


@pytest.mark.parametrize("tree", [
    {"operator": "XOR", "rules": [{"type": "IS_BOT", "value": True}]},
    {"operator": "AND", "rules": [{"type": "UNKNOWN", "value": True}]},
    {"operator": "AND", "rules": [{"type": "KEYWORD_MATCH", "value": "([a-"}]},
    {"operator": "NOT", "rules": []},
    {"operator": "NOT", "rules": [{"type": "IS_BOT", "value": True}] * 2},
    {"operator": "AND", "rules": [{"type": "MESSAGE_LENGTH_GTE", "value": "30"}]},
    {"operator": "AND", "rules": [{"type": "IS_THREAD", "target": "actor", "value": True}]},
    {"operator": "AND", "rules": [{"type": "HAS_ANY_ROLE", "value": ["18446744073709551616"]}]},
    {"operator": "AND", "rules": [{"type": "MENTIONS_USER_ID", "value": 123}]},
    {"operator": "AND", "rules": [{"type": "COOLDOWN_READY", "value": {"scope": "server", "seconds": 5, "target": "subject"}}]},
])
def test_invalid_condition_tree_rejected(tree):
    with pytest.raises(ValidationError):
        Rule.model_validate(make_rule(conditions=tree))


def test_depth_and_total_tree_size_are_bounded():
    node = {"type": "IS_BOT", "value": False}
    tree = deepcopy(node)
    for _ in range(MAX_DEPTH):
        tree = {"operator": "AND", "rules": [tree]}
    with pytest.raises(ValidationError, match="condition nodes"):
        Rule.model_validate(make_rule(conditions=tree))
    with pytest.raises(ValidationError, match="condition nodes"):
        Rule.model_validate(make_rule(conditions={"operator": "AND", "rules": [
            {"operator": "OR", "rules": [deepcopy(node) for _ in range(16)]} for _ in range(4)
        ]}))


@pytest.mark.parametrize("action", [
    {"type": "ADD_ROLE", "target": "actor"},
    {"type": "DM_USER", "target": "all", "template": "Hi"},
    {"type": "DELETE_TRIGGER_MESSAGE", "role_id": "123"},
    {"type": "SEND_MESSAGE", "buttons": [{"id": "a", "label": "A"}] * 2},
    {"type": "SEND_MESSAGE", "buttons": [{"id": "space id", "label": "Invalid"}]},
    {"type": "SEND_MESSAGE", "embed": {"title": "A", "fields": [{"name": "N", "value": "V", "inline": "true"}]}},
    {"type": "TIMEOUT_USER", "duration_seconds": 2419201, "target": "123"},
    {"type": "EDIT_MESSAGE", "channel_id": "18446744073709551616", "message_id": "123", "template": "Hi"},
])
def test_malformed_actions_are_rejected(action):
    with pytest.raises(ValidationError):
        Action.model_validate(action)


def test_trigger_and_action_compatibility():
    with pytest.raises(ValidationError, match="button ID"):
        Rule.model_validate(make_rule(trigger={"type": "BUTTON_CLICK"}))
    with pytest.raises(ValidationError, match="need a button trigger"):
        Rule.model_validate(make_rule(actions=[{"type": "EPHEMERAL_REPLY", "template": "Hello"}]))
    with pytest.raises(ValidationError, match="destination channel"):
        Rule.model_validate(make_rule(trigger={"type": "MEMBER_JOIN"}))
    assert Rule.model_validate(make_rule(
        trigger={"type": "BUTTON_CLICK", "button_id": "welcome"},
        actions=[{"type": "EPHEMERAL_REPLY", "template": "Hello"}],
    ))
    assert Action.model_validate({"type": "EDIT_MESSAGE", "buttons": []})
    with pytest.raises(ValidationError):
        DryRun.model_validate({"actor_id": 999})


@pytest.mark.parametrize("command", [
    None, {"name": "!verify"}, {"name": "?verify @user"}, {"name": "verify"}, {"name": "?9verify"},
    {"name": "?verify", "access": "roles"}, {"name": "?verify", "role_ids": ["123"]},
])
def test_command_requires_a_valid_name_and_access_configuration(command):
    with pytest.raises(ValidationError):
        Rule.model_validate(make_rule(trigger={"type": "COMMAND", "command": command}))


def test_command_trigger_roundtrips_with_message_actions_and_normalizes_name():
    model = Rule.model_validate(make_rule(
        trigger={"type": "COMMAND", "command": {"name": "?Verify", "access": "roles", "role_ids": ["123"]}},
        actions=[{"type": "REPLY_MESSAGE", "template": "Verified {subject.mention}"}],
    ))
    assert model.trigger.command.name == "?verify"
    assert model.trigger.command.require_mention
    assert Rule.model_validate_json(model.model_dump_json(exclude_none=True)) == model
