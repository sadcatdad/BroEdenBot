import json
import re
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from dashboard.app import app
from dashboard.rbac import initialize_rbac_schema, set_user_permission_override
from dashboard.users import hash_password, initialize_dashboard_users
from utils.settings import initialize_settings_from_env


@pytest.fixture
def garden(tmp_path, monkeypatch):
    path = tmp_path / "garden.db"
    for key, value in {
        "DATABASE_PATH": str(path), "DASHBOARD_ENABLED": "true", "DASHBOARD_USERNAME": "owner",
        "DASHBOARD_PASSWORD": "automation-password", "DASHBOARD_SECRET_KEY": "automation-test-session",
        "DASHBOARD_AUTH_MODE": "password", "GUILD_ID": "999999999999999999", "ENABLED_MODULES": "automations",
        "DASHBOARD_PUBLIC_URL": "",
    }.items():
        monkeypatch.setenv(key, value)
    initialize_settings_from_env()
    initialize_dashboard_users()
    initialize_rbac_schema()
    with TestClient(app) as client:
        yield client, path


def login(client, user="owner"):
    page = client.get("/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    response = client.post("/login", data={"username": user, "password": "automation-password", "csrf": csrf})
    assert response.status_code == 200
    csrf = re.search(r'name="csrf" value="([^"]+)"', response.text)[1]
    return {"X-CSRF-Token": csrf}


def definition():
    return {
        "name": "Match hello", "enabled": True, "trigger": {"type": "MESSAGE"},
        "conditions": {"operator": "AND", "rules": [{"type": "EXACT_MATCH", "value": "hello"}]},
        "actions": [{"type": "REPLY_MESSAGE", "template": "Hi {actor.mention}"}],
    }


def test_garden_create_validate_toggle_test_edit_conflict_archive_and_history(garden):
    client, path = garden
    headers = login(client)
    page = client.get("/automations")
    assert page.status_code == 200 and "automations.js?v=" in page.text
    assert page.headers["cache-control"] == "private, no-store"
    assert client.post("/api/automations", json=definition()).status_code == 400
    invalid = {**definition(), "actions": [{"type": "ADD_ROLE", "target": "actor"}]}
    response = client.post("/api/automations/validate", headers=headers, json=invalid)
    assert response.status_code == 422
    assert "role_id" in response.json()["detail"][0]["message"]
    assert client.post("/api/automations/validate", headers=headers, json=definition()).json()["valid"]
    created = client.post("/api/automations", headers=headers, json=definition())
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["rule"]["enabled"] is False
    endpoint = f"/api/automations/{row['id']}"
    toggle = client.post(endpoint + "/toggle", headers=headers, json={"revision": 1, "enabled": True})
    assert toggle.status_code == 200
    assert toggle.json()["rule"]["enabled"]
    sample = {"actor_id": "123", "message_content": "hello"}
    preview = client.post(endpoint + "/test", headers=headers, json=sample)
    assert preview.status_code == 200
    assert preview.json()["matched"]
    assert preview.json()["actions"][0]["template"] == "Hi <@123>"
    assert not client.post(endpoint + "/test", headers=headers, json={**sample, "message_content": "bye"}).json()["matched"]
    update = {"revision": 2, "rule": {**definition(), "name": "New name", "enabled": False}}
    assert client.put(endpoint, headers=headers, json=update).json()["rule"]["enabled"]
    assert client.put(endpoint, headers=headers, json=update).status_code == 409
    archived = client.request("DELETE", endpoint, headers=headers, json={"revision": 3})
    assert archived.json()["archived"] and not archived.json()["rule"]["enabled"]
    assert client.get("/api/automations").json()["rules"] == []
    assert len(client.get("/api/automations?archived=true").json()["rules"]) == 1
    history = client.get(endpoint + "/history").json()
    assert history["runs"] == []
    assert [row["operation"] for row in history["revisions"]] == ["archive", "edit", "enable", "create"]
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM automation_cooldowns").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM dashboard_audit_log WHERE action='automations.create'").fetchone()[0] == 1


def test_view_permission_does_not_confer_mutation_or_unsaved_simulation(garden):
    client, path = garden
    assert client.get("/api/automations", follow_redirects=False).status_code == 303
    headers = login(client)
    row = client.post("/api/automations", headers=headers, json=definition()).json()
    with sqlite3.connect(path) as db:
        uid = db.execute(
            """INSERT INTO dashboard_users(username,password_hash,role,status,auth_provider)
               VALUES ('reader',?,'viewer','active','password')""",
            (hash_password("automation-password"),),
        ).lastrowid
    assert client.get("/api/automations").status_code == 200
    client.cookies.clear()
    headers = login(client, "reader")
    assert client.get("/api/automations").status_code == 403
    set_user_permission_override(uid, "automations.view", "allow", changed_by="test")
    assert client.get("/api/automations").status_code == 200
    assert 'id="automation-new"' not in client.get("/automations").text
    assert client.post("/api/automations", headers=headers, json=definition()).status_code == 403
    endpoint = f"/api/automations/{row['id']}"
    assert client.post(endpoint + "/test", headers=headers, json={}).status_code == 200
    assert client.post(endpoint + "/test", headers=headers, json={"rule": definition()}).status_code == 403


def test_gemini_chat_drafts_validate_and_are_never_saved(garden, monkeypatch):
    client, _ = garden
    headers = login(client)
    from dashboard import automations_routes as routes
    monkeypatch.setattr(routes, "get_ai_config", lambda: SimpleNamespace(available=True))
    monkeypatch.setattr(routes.ai_service, "check_ai_cooldown", AsyncMock(return_value=(True, 0)))
    monkeypatch.setattr(routes.ai_service, "set_ai_cooldown", AsyncMock())
    generate = AsyncMock(return_value=SimpleNamespace(
        ok=True, text=json.dumps({"message": "Here is a draft.", "rule_json": json.dumps(definition())}),
    ))
    monkeypatch.setattr(routes.ai_service, "generate_ai_response", generate)
    response = client.post("/api/automations/draft", headers=headers, json={
        "messages": [{"role": "user", "text": "Reply when someone says hello."}],
    })
    assert response.status_code == 200
    assert response.json()["rule"]["enabled"] is False
    assert generate.await_args.kwargs["sensitive_content"] is True
    assert client.get("/api/automations").json()["rules"] == []
    generate.return_value.text = json.dumps({"message": "Draft.", "rule_json": '{"enabled":true}'})
    assert client.post("/api/automations/draft", headers=headers, json={
        "messages": [{"role": "user", "text": "Try again."}],
    }).status_code == 422


def test_simulator_member_picker_uses_only_configured_guild(garden, monkeypatch):
    client, _ = garden
    headers = login(client)
    from dashboard import automations_routes as routes
    search = AsyncMock(return_value=[{
        "user": {"id": "123", "username": "fern"},
        "nick": "Forest Fern", "roles": ["456"], "joined_at": "2020-12-12T00:00:00+00:00",
    }])
    monkeypatch.setattr(routes, "search_members", search)
    assert client.post("/api/automations/members", headers=headers, json={"query": " "}).status_code == 422
    result = client.post("/api/automations/members", headers=headers, json={"query": "  Fern  "})
    assert result.status_code == 200
    search.assert_awaited_once_with("999999999999999999", "Fern")
    member = result.json()["members"][0]
    assert member["id"] == "123"
    assert member["name"] == "Forest Fern"
    assert member["join_age_days"] > 365
    assert member["roles"] == ["456"]


def test_oauth_roles_are_rechecked_on_every_automation_request(garden, monkeypatch):
    from datetime import datetime, timezone
    from dashboard import automations_routes as routes
    from dashboard.rbac import list_roles, replace_discord_role_mappings, sync_discord_role_assignments

    client, path = garden
    login(client)
    dashboard_role = next(role["id"] for role in list_roles() if role["role_key"] == "administrator")
    replace_discord_role_mappings(["123456789012345690"], dashboard_role, changed_by="test")
    with sqlite3.connect(path) as db:
        user_id = db.execute("SELECT id FROM dashboard_users WHERE username='owner'").fetchone()[0]
        db.execute(
            """UPDATE dashboard_users SET auth_provider='discord', discord_user_id='123456789012345691',
               discord_verification_status='verified', discord_verified_at=?, access_source='discord_role', role='viewer'
               WHERE id=?""", (datetime.now(timezone.utc).isoformat(), user_id),
        )
        db.execute("DELETE FROM dashboard_user_role_assignments WHERE user_id=?", (user_id,))
    sync_discord_role_assignments(user_id, ["123456789012345690"])
    fresh = AsyncMock(return_value={"roles": ["123456789012345690"]})
    monkeypatch.setattr(routes, "fetch_member", fresh)
    assert client.get("/api/automations").status_code == 200
    fresh.return_value = {"roles": []}
    response = client.get("/api/automations")
    assert response.status_code == 403
    assert "current Discord roles" in response.json()["detail"]
