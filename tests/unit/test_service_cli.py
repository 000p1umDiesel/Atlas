from __future__ import annotations

import importlib.util

import pytest

from mnogobase.config import Settings
from mnogobase.database import database_target

pytestmark = pytest.mark.integration


def test_cli_registration_exists():
    assert importlib.util.find_spec("mnogobase.service.cli") is not None


def test_credentials_private_round_trip(tmp_path):
    import stat

    from mnogobase.service.client import Credentials

    path = tmp_path / "credentials.json"
    creds = Credentials(path)
    creds.save("http://localhost:8080", "secret-token")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert creds.token("http://localhost:8080") == "secret-token"
    assert creds.token("http://other:8080") is None
    creds.remove("http://localhost:8080")
    assert creds.token("http://localhost:8080") is None


def test_operator_cli_creates_team_and_user(tmp_path, monkeypatch):
    import typer
    from typer.testing import CliRunner

    from mnogobase.service.cli import register_cli
    from mnogobase.service.store import ServiceStore

    monkeypatch.setattr(
        "mnogobase.service.cli._settings", lambda: Settings(service={"root": tmp_path})
    )
    app = typer.Typer()
    register_cli(app)
    runner = CliRunner()
    result = runner.invoke(app, ["service", "init"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["user", "create", "alice", "--password", "long-enough-password"])
    assert result.exit_code == 0, result.output
    assert "long-enough-password" not in result.output
    result = runner.invoke(app, ["team", "create", "Research"])
    assert result.exit_code == 0, result.output
    store = ServiceStore(database_target(Settings(), "service"))
    team = store.teams()[0]
    user = store.authenticate(store.login("alice", "long-enough-password"))
    result = runner.invoke(app, ["team", "add-member", team["id"], user["id"], "--role", "admin"])
    assert result.exit_code == 0, result.output
    assert store.context(user["id"], team["id"]).role == "admin"


def test_http_cli_login_commands_chat_and_logout(tmp_path, monkeypatch):
    import json

    import httpx
    import typer
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    from mnogobase.api.app import create_api
    from mnogobase.service.cli import register_cli
    from mnogobase.service.store import ServiceStore

    settings = Settings(service={"root": tmp_path})
    store = ServiceStore(database_target(Settings(), "service"))
    user = store.create_user("alice", "long-enough-password")
    team = store.create_team("Team")
    store.add_member(team["id"], user["id"], "admin")

    class Runtime:
        def __init__(self):
            self.store = store

        def documents(self, ctx, limit=50, offset=0):
            return [{"id": "doc", "filename": "evidence.txt"}]

        def history(self, ctx, conversation_id, limit=100, offset=0):
            return store.messages(ctx, conversation_id, limit=limit, offset=offset)

        async def send_message(self, ctx, conversation_id, question, mode="rag"):
            store.append_message(ctx, conversation_id, "user", question)
            return store.append_message(
                ctx, conversation_id, "assistant", "Evidence answer", sources=[{"doc_id": "doc"}]
            )

    api = TestClient(create_api(settings, service=Runtime()))
    real_client = httpx.Client

    def route(request):
        response = api.request(
            request.method, str(request.url), content=request.content, headers=dict(request.headers)
        )
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content
        )

    monkeypatch.setattr(
        "mnogobase.service.client.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(route), **kwargs),
    )
    app = typer.Typer()
    register_cli(app)
    runner = CliRunner()
    prefix = [
        "client",
        "--url",
        "http://testserver",
        "--team",
        team["id"],
        "--credentials",
        str(tmp_path / "credentials.json"),
    ]
    result = runner.invoke(app, prefix + ["login", "alice", "--password", "long-enough-password"])
    assert result.exit_code == 0, result.output
    assert "long-enough-password" not in result.output
    result = runner.invoke(app, prefix + ["teams"])
    assert result.exit_code == 0 and team["id"] in result.output
    result = runner.invoke(app, prefix + ["documents", "list"])
    assert result.exit_code == 0 and "evidence.txt" in result.output
    result = runner.invoke(app, prefix + ["chat", "new", "--title", "Persistent"])
    assert result.exit_code == 0, result.output
    cid = json.loads(result.output)["id"]
    result = runner.invoke(app, prefix + ["chat", "send", cid, "Question"])
    assert (
        result.exit_code == 0 and "Evidence answer" in result.output and "doc_id" in result.output
    )
    result = runner.invoke(app, prefix + ["chat", "open", cid], input="Follow-up\n/exit\n")
    assert result.exit_code == 0, result.output
    assert "Evidence answer" in result.output
    result = runner.invoke(app, prefix + ["chat", "history", cid])
    assert result.exit_code == 0 and "Follow-up" in result.output
    assert len(store.messages(store.context(user["id"], team["id"]), cid)) == 4
    result = runner.invoke(app, prefix + ["logout"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, prefix + ["teams"])
    assert result.exit_code == 2 and "login" in result.output


def test_client_uses_configured_api_url_when_flag_omitted(tmp_path, monkeypatch):
    import httpx
    import typer
    from typer.testing import CliRunner

    from mnogobase.service.cli import register_cli

    seen = []

    def respond(request):
        seen.append(request.url.host)
        return httpx.Response(200, json={"token": "local-test-token"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "mnogobase.service.client._default_api_url",
        lambda: "http://configured-api:8080",
        raising=False,
    )
    monkeypatch.setattr(
        "mnogobase.service.client.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    app = typer.Typer()
    register_cli(app)
    result = CliRunner().invoke(
        app,
        [
            "client",
            "--credentials",
            str(tmp_path / "credentials.json"),
            "login",
            "alice",
            "--password",
            "long-enough-password",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == ["configured-api"]


def test_client_rejects_malformed_urls_cleanly(tmp_path):
    import typer
    from typer.testing import CliRunner

    from mnogobase.service.cli import register_cli

    app = typer.Typer()
    register_cli(app)
    for url in (
        "http://[bad",
        "http://localhost:bad",
        "file:///etc/passwd",
        "https://user:password@example.com",
    ):
        result = CliRunner().invoke(
            app,
            ["client", "--url", url, "--credentials", str(tmp_path / "credentials.json"), "teams"],
        )
        assert result.exit_code == 2, (url, result.output, result.exception)
        assert "password@example.com" not in result.output
