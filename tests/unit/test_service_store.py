from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import FrozenInstanceError

import pytest
from argon2 import PasswordHasher

from mnogobase.config import Settings
from mnogobase.service.context import TeamContext, team_settings, validate_team_id
from mnogobase.service.errors import ServiceError
from mnogobase.service.store import ServiceStore


@pytest.fixture
def store(tmp_path):
    return ServiceStore(tmp_path / "service.db")


@pytest.fixture
def member(store):
    user = store.create_user("alice", "correct horse")
    team = store.create_team("Research")
    store.add_member(team["id"], user["id"])
    return user, team, store.context(user["id"], team["id"])


def test_password_and_session_are_hashed_and_survive_restart(store):
    user = store.create_user("alice", "correct horse")
    token = store.login("alice", "correct horse")
    assert ServiceStore(store.path).authenticate(token) == user
    with sqlite3.connect(store.path) as conn:
        password_hash = conn.execute("SELECT password_hash FROM users").fetchone()[0]
        token_hash = conn.execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert PasswordHasher().verify(password_hash, "correct horse")
    assert token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert "correct horse" not in password_hash
    assert token_hash != token
    assert set(user) == {"id", "username"}


@pytest.mark.parametrize("username,password", [("alice", "wrong"), ("missing", "wrong")])
def test_bad_credentials_are_generic(store, username, password):
    store.create_user("alice", "correct horse")
    with pytest.raises(ServiceError) as exc:
        store.login(username, password)
    assert (exc.value.status, exc.value.detail) == (401, "Invalid username or password")


def test_logout_and_expired_sessions_are_rejected(store):
    store.create_user("alice", "correct horse")
    token = store.login("alice", "correct horse")
    store.logout(token)
    store.logout(token)
    with pytest.raises(ServiceError, match="Invalid or expired session"):
        store.authenticate(token)
    expired = store.login("alice", "correct horse")
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE sessions SET expires_at=0")
    with pytest.raises(ServiceError) as exc:
        store.authenticate(expired)
    assert exc.value.status == 401


def test_disabled_users_lose_sessions_and_existing_contexts(store, member):
    user, _, ctx = member
    token = store.login("alice", "correct horse")
    store.disable_user(user["id"])
    for operation in [lambda: store.authenticate(token), lambda: store.conversations(ctx)]:
        with pytest.raises(ServiceError) as exc:
            operation()
        assert exc.value.status in {401, 403}


def test_memberships_are_unique_and_roles_are_current(store, member):
    user, team, ctx = member
    assert store.teams(user["id"]) == [team]
    assert store.teams() == [team]
    assert store.get_team(team["id"]) == team
    assert store.members(team["id"]) == [
        {"user_id": user["id"], "username": "alice", "role": "member"}
    ]
    with pytest.raises(ServiceError) as exc:
        store.context(user["id"], team["id"], admin=True)
    assert exc.value.status == 403
    store.add_member(team["id"], user["id"], "admin")
    assert store.context(user["id"], team["id"], admin=True).role == "admin"
    store.add_member(team["id"], user["id"], "member")
    assert len(store.teams(user["id"])) == 1
    forged = TeamContext(ctx.team_id, ctx.user_id, "admin")
    assert store.create_conversation(forged)["user_id"] == ctx.user_id
    assert store.context(forged.user_id, forged.team_id).role == "member"


def test_membership_revocation_is_rechecked_for_every_chat_operation(store, member):
    user, team, ctx = member
    chat = store.create_conversation(ctx)
    store.append_message(ctx, chat["id"], "user", "Question")
    store.remove_member(team["id"], user["id"])
    operations = [
        lambda: store.context(user["id"], team["id"]),
        lambda: store.create_conversation(ctx),
        lambda: store.conversations(ctx),
        lambda: store.conversation(ctx, chat["id"]),
        lambda: store.messages(ctx, chat["id"]),
        lambda: store.recent_messages(ctx, chat["id"]),
        lambda: store.append_message(ctx, chat["id"], "assistant", "Answer"),
    ]
    for operation in operations:
        with pytest.raises(ServiceError) as exc:
            operation()
        assert exc.value.status == 403
    assert store.teams(user["id"]) == []


def test_conversations_are_private_even_from_team_admins(store, member):
    _, team, ctx = member
    chat = store.create_conversation(ctx, "Private question")
    other = store.create_user("bob", "another password")
    store.add_member(team["id"], other["id"], "admin")
    other_ctx = store.context(other["id"], team["id"])
    second_team = store.create_team("Other team")
    store.add_member(second_team["id"], ctx.user_id)
    second_ctx = store.context(ctx.user_id, second_team["id"])
    for forbidden_ctx in [other_ctx, second_ctx]:
        assert store.conversations(forbidden_ctx) == []
        for operation in [
            lambda ctx=forbidden_ctx: store.conversation(ctx, chat["id"]),
            lambda ctx=forbidden_ctx: store.messages(ctx, chat["id"]),
            lambda ctx=forbidden_ctx: store.recent_messages(ctx, chat["id"]),
            lambda ctx=forbidden_ctx: store.append_message(ctx, chat["id"], "user", "Attack"),
        ]:
            with pytest.raises(ServiceError) as exc:
                operation()
            assert exc.value.status == 404


def test_messages_preserve_snapshots_and_chronological_pagination(store, member):
    _, _, ctx = member
    chat = store.create_conversation(ctx, "Sources")
    sources = [{"doc_id": "abc", "title": "Paper"}]
    first = store.append_message(ctx, chat["id"], "user", "Question", query="Rephrased")
    second = store.append_message(
        ctx, chat["id"], "assistant", "Answer", mode="all", sources=sources, status="done"
    )
    sources[0]["title"] = "changed"
    assert first["query"] == "Rephrased"
    assert second["sources"] == [{"doc_id": "abc", "title": "Paper"}]
    assert second["team_id"] == ctx.team_id and second["user_id"] == ctx.user_id
    assert store.messages(ctx, chat["id"], limit=1, offset=0) == [first]
    assert store.messages(ctx, chat["id"], limit=1, offset=1) == [second]
    assert store.messages(ctx, chat["id"], offset=2) == []
    assert store.conversation(ctx, chat["id"])["title"] == "Sources"


def test_recent_messages_return_latest_turns_beyond_first_thousand(store, member):
    _, _, ctx = member
    chat = store.create_conversation(ctx)
    written = [
        store.append_message(ctx, chat["id"], "user", f"Question {index}") for index in range(1005)
    ]
    assert store.messages(ctx, chat["id"], limit=1000)[-1] == written[999]
    assert store.recent_messages(ctx, chat["id"]) == written[-12:]
    assert store.recent_messages(ctx, chat["id"], limit=2) == written[-2:]
    empty = store.create_conversation(ctx)
    assert store.recent_messages(ctx, empty["id"]) == []


@pytest.mark.parametrize("limit,offset", [(0, 0), (1001, 0), (1, -1)])
def test_pagination_is_bounded(store, member, limit, offset):
    _, _, ctx = member
    chat = store.create_conversation(ctx)
    for operation in [
        lambda: store.messages(ctx, chat["id"], limit=limit, offset=offset),
        lambda: store.conversations(ctx, limit=limit, offset=offset),
    ]:
        with pytest.raises(ServiceError) as exc:
            operation()
        assert exc.value.status == 400


@pytest.mark.parametrize("limit", [0, 1001, -1])
def test_recent_messages_limits_are_bounded(store, member, limit):
    _, _, ctx = member
    chat = store.create_conversation(ctx)
    with pytest.raises(ServiceError) as exc:
        store.recent_messages(ctx, chat["id"], limit=limit)
    assert exc.value.status == 400


def test_input_errors_do_not_leak_sqlite_details(store):
    user = store.create_user("alice", "password")
    team = store.create_team("Team")
    for operation in [
        lambda: store.create_user("alice", "other"),
        lambda: store.create_user("", "password"),
        lambda: store.create_user("other", ""),
        lambda: store.create_team(""),
        lambda: store.add_member(team["id"], user["id"], "owner"),
        lambda: store.add_member(team["id"], "missing"),
        lambda: store.login("alice", "password", ttl_hours=0),
    ]:
        with pytest.raises(ServiceError) as exc:
            operation()
        assert exc.value.status in {400, 404, 409}


def test_team_context_is_immutable_and_paths_are_isolated(tmp_path):
    base = Settings(service={"root": tmp_path / "data"})
    before = base.model_dump()
    team_id = "a" * 32
    scoped = team_settings(base, team_id)
    root = tmp_path / "data" / "teams" / team_id
    assert scoped.data_dir == root / "state"
    assert scoped.wiki.dir == root / "wiki"
    assert scoped.logs_dir == root / "logs"
    assert scoped.runs_dir == root / "runs"
    assert scoped.qdrant.prefix == f"mb_team_{team_id}_"
    assert scoped.neo4j.team_id == team_id
    scoped.llm.model = "changed"
    assert base.model_dump() == before
    assert team_settings(base, "b" * 32).data_dir != scoped.data_dir
    with pytest.raises(FrozenInstanceError):
        TeamContext(team_id, "user", "member").role = "admin"


@pytest.mark.parametrize("value", ["../x", "A" * 32, "a" * 31, "a" * 33, "", "a" * 32 + "\n"])
def test_unsafe_team_ids_are_rejected(value):
    with pytest.raises(ServiceError) as exc:
        validate_team_id(value)
    assert exc.value.status == 400
    assert validate_team_id("0123456789abcdef" * 2) == "0123456789abcdef" * 2
