import json
import stat

import httpx
from typer.testing import CliRunner

from mnogobase import cli

runner = CliRunner()


def mock_api(monkeypatch, answer=None):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"token": "tok", "token_type": "bearer"})
        if request.url.path == "/api/ask":
            return httpx.Response(200, json=answer)
        if request.url.path == "/api/jobs":
            return httpx.Response(503, json={"detail": "Service temporarily unavailable"})
        return httpx.Response(200, json={"status": "ok"})

    real = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    return seen


def client(tmp_path, *args):
    base = ["client", "--url", "http://kb.test", "--credentials", str(tmp_path / "cred.json")]
    return runner.invoke(cli.app, [*base, *args])


def test_login_stores_private_token_and_uses_unscoped_paths(tmp_path, monkeypatch):
    answer = {
        "text": "Transformers use attention [1].",
        "sources": [{"path": "paper.pdf", "page": 2, "url": "/api/documents/abc/content"}],
    }
    seen = mock_api(monkeypatch, answer)
    result = client(tmp_path, "login", "owner", "--password", "long-enough-password")
    assert result.exit_code == 0, result.output
    credentials = tmp_path / "cred.json"
    assert json.loads(credentials.read_text()) == {"http://kb.test": "tok"}
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600

    result = client(tmp_path, "ask", "What is attention?")
    assert result.exit_code == 0, result.output
    assert "Transformers use attention [1]." in result.output
    assert "[1] paper.pdf, p.2  /api/documents/abc/content" in result.output
    assert seen[-1] == ("POST", "/api/ask", "Bearer tok")

    result = client(tmp_path, "ask", "What is attention?", "--json")
    assert json.loads(result.output) == answer

    for args, path in [
        (["status"], "/api/status"),
        (["documents", "list"], "/api/documents"),
        (["wiki", "show", "page id"], "/api/wiki/page%20id"),
        (["graph", "search", "x"], "/api/graph/search"),
    ]:
        assert client(tmp_path, *args).exit_code == 0
        assert seen[-1][1] == path.replace("%20", " ")


def test_errors_show_only_the_safe_detail(tmp_path, monkeypatch):
    mock_api(monkeypatch)
    client(tmp_path, "login", "owner", "--password", "long-enough-password")
    result = client(tmp_path, "jobs", "list")
    assert result.exit_code == 2
    assert "API 503: Service temporarily unavailable" in result.output


def test_requires_login_and_valid_url(tmp_path, monkeypatch):
    mock_api(monkeypatch)
    assert "client login" in client(tmp_path, "status").output
    result = runner.invoke(cli.app, ["client", "--url", "ftp://x", "status"])
    assert result.exit_code == 2
