import json

import pytest
from typer.testing import CliRunner

from mnogobase import cli

pytestmark = pytest.mark.integration
runner = CliRunner()


@pytest.fixture(autouse=True)
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "configure_logging", lambda *a: None)


def test_user_lifecycle_from_cli():
    password = "long-enough-password\nlong-enough-password\n"
    created = runner.invoke(cli.app, ["user", "create", "owner"], input=password)
    assert created.exit_code == 0, created.output
    assert "long-enough-password" not in created.output
    short = runner.invoke(cli.app, ["user", "create", "x"], input="short\nshort\n")
    assert short.exit_code == 2 and "12" in short.output
    duplicate = runner.invoke(cli.app, ["user", "create", "owner"], input=password)
    assert duplicate.exit_code == 2
    changed = runner.invoke(
        cli.app, ["user", "passwd", "owner"], input="another-long-password\n" * 2
    )
    assert changed.exit_code == 0, changed.output
    assert runner.invoke(cli.app, ["user", "disable", "owner"]).exit_code == 0
    listed = runner.invoke(cli.app, ["user", "list"])
    users = json.loads(listed.output)
    assert [(u["username"], u["active"]) for u in users] == [("owner", 0)]
    assert runner.invoke(cli.app, ["user", "enable", "owner"]).exit_code == 0
    assert runner.invoke(cli.app, ["user", "disable", "nobody"]).exit_code == 2
