import itertools
from datetime import datetime, timedelta, timezone

import pytest

from automations.evaluator import Event, User, evaluate, substitute
from automations.models import DryRun, Rule


NOW = datetime(2026, 10, 15, tzinfo=timezone.utc).timestamp()


def rule(tree=None, *, trigger=None, allow_bots=False):
    return Rule.model_validate({
        "name": "test rule",
        "trigger": trigger or {"type": "MESSAGE"},
        "allow_bots": allow_bots,
        "conditions": tree,
        "actions": [{"type": "SEND_MESSAGE", "template": "hi", "channel_id": "333"}],
    })


def group(*nodes, operator="AND"):
    return {"operator": operator, "rules": list(nodes)}


def condition(kind, value, **fields):
    return {"type": kind, "value": value, **fields}


def test_absent_subject_and_missing_role_metadata_fail_closed_through_not():
    subject_role = condition("HAS_ROLE", "456", target="subject")
    event = Event("MESSAGE", actor=User("33"))
    assert not evaluate(rule(group(subject_role)), event, now=NOW)[0]
    matched, _, trace = evaluate(rule(group(subject_role, operator="NOT")), event, now=NOW, trace=True)
    assert not matched
    assert trace.children[2].result is None
    event.subject = User("44")
    assert not evaluate(rule(group(subject_role, operator="NOT")), event, now=NOW)[0]
    event.subject.roles = set()
    assert evaluate(rule(group(subject_role, operator="NOT")), event, now=NOW)[0]


def test_other_bots_opt_in_and_self_always_blocked():
    event = Event("MESSAGE", actor=User("33", bot=True))
    assert not evaluate(rule(), event, now=NOW)[0]
    assert evaluate(rule(allow_bots=True), event, now=NOW, bot_id="44")[0]
    assert not evaluate(rule(allow_bots=True), event, now=NOW, bot_id="33")[0]


@pytest.mark.parametrize("first,second", list(itertools.product([True, False], repeat=2)))
def test_nested_not_or_truth_table_and_complete_trace(first, second):
    tree = group(group(condition("HAS_ATTACHMENT", True), condition("HAS_EMBED", True), operator="OR"), operator="NOT")
    event = Event("MESSAGE", actor=User("33"), has_attachment=first, has_embed=second)
    matched, _, trace = evaluate(rule(tree), event, now=NOW, trace=True)
    assert matched == (not (first or second))
    children = trace.children[2].children[0].children
    assert [step.result for step in children] == [first, second]
    assert evaluate(rule(tree), event, now=NOW)[0] == matched


def test_short_circuit_skips_remaining_leaves_outside_trace(monkeypatch):
    from automations import evaluator
    original = evaluator._condition
    calls = []

    def record(node, *others):
        calls.append(node.type)
        return original(node, *others)

    monkeypatch.setattr(evaluator, "_condition", record)
    tree = group(condition("EXACT_MATCH", "hello"), condition("KEYWORD_MATCH", "hi"), operator="OR")
    event = Event("MESSAGE", actor=User("33"), message_content="hello")
    assert evaluate(rule(tree), event, now=NOW)[0]
    assert calls == ["EXACT_MATCH"]
    evaluate(rule(tree), event, now=NOW, trace=True)
    assert calls[-2:] == ["EXACT_MATCH", "KEYWORD_MATCH"]


@pytest.mark.parametrize("with_timezone", [True, False])
def test_account_age_handles_aware_and_naive_boundaries(with_timezone):
    boundary = datetime.fromtimestamp(NOW, timezone.utc) - timedelta(days=30)
    if not with_timezone:
        boundary = boundary.replace(tzinfo=None)
    event = Event("MESSAGE", actor=User("123", created_at=boundary))
    tree = group(condition("ACCOUNT_AGE_DAYS_GTE", 30))
    assert evaluate(rule(tree), event, now=NOW)[0]
    event.actor.created_at += timedelta(seconds=1)
    assert not evaluate(rule(tree), event, now=NOW)[0]


def test_cooldown_boundary_and_or_does_not_claim_unselected_branches():
    cooldown = condition("COOLDOWN_READY", {"scope": "user", "seconds": 30, "target": "subject"})
    event = Event("MESSAGE", actor=User("33"), subject=User("44"), message_content="hello")
    key = ("user", "44", 30)
    assert not evaluate(rule(group(cooldown)), event, now=NOW, timestamps={key: NOW - 29})[0]
    assert evaluate(rule(group(cooldown)), event, now=NOW, timestamps={key: NOW - 30})[:2] == (True, {key})
    assert evaluate(rule(group(condition("EXACT_MATCH", "hello"), cooldown, operator="OR")),
                    event, now=NOW, trace=True)[:2] == (True, set())


def test_reaction_target_with_emoji_id_and_simulated_roles():
    sample = DryRun.model_validate({
        "actor_id": "100", "subject_id": "200", "channel_id": "300",
        "users": [{"id": "200", "name": "Moss", "roles": ["999"]}],
        "emoji": "<a:leaf:888>", "reaction_count": 3, "message_content": "<@123> <@&777>",
    })
    event = Event.simulated("REACTION_ADD", sample, NOW)
    tree = group(
        condition("HAS_ROLE", "999", target="subject"),
        condition("USER_ID_IN", ["200"], target="200"),
        condition("EMOJI_IS", "888"),
        condition("REACTION_COUNT_GTE", 2),
        condition("MENTIONS_ROLE_ID", "777"),
    )
    assert evaluate(rule(tree, trigger={"type": "REACTION_ADD", "emoji": "888"}), event, now=NOW)[0]
    assert substitute("{actor.mention} met {subject.name}", event) == "<@100> met Moss"


def test_voice_and_member_update_filters_include_previous_channel_and_role_direction():
    member_event = Event("MEMBER_UPDATE", actor=User("100"), member_changes={"role_added"},
                         added_roles={"200"}, removed_roles={"300"})
    assert evaluate(rule(trigger={"type": "MEMBER_UPDATE", "member_change": "role_added", "role_id": "200"}), member_event, now=NOW)[0]
    assert not evaluate(rule(trigger={"type": "MEMBER_UPDATE", "member_change": "role_removed", "role_id": "300"}), member_event, now=NOW)[0]
    voice_event = Event("VOICE_STATE_UPDATE", actor=User("100"), channel_id="300", previous_channel_id="200", voice_change="move")
    assert evaluate(rule(trigger={"type": "VOICE_STATE_UPDATE", "channel_ids": ["200"], "voice_change": "move"}), voice_event, now=NOW)[0]
    assert not evaluate(rule(trigger={"type": "VOICE_STATE_UPDATE", "channel_ids": ["200"], "voice_change": "leave"}), voice_event, now=NOW)[0]
