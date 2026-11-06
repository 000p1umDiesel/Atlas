import importlib.util
import os
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "deploy/shared-host/update.py"


def module():
    spec = importlib.util.spec_from_file_location("server_update", SCRIPT)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def git(path, *args):
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", "-C", str(path), *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repositories(tmp_path):
    origin, writer, app = (tmp_path / name for name in ("origin", "writer", "app"))
    origin.mkdir()
    git(origin, "init", "--bare", "--initial-branch=main")
    git(tmp_path, "clone", str(origin), str(writer))
    (writer / "state.txt").write_text("old")
    git(writer, "add", ".")
    git(writer, "commit", "-m", "old")
    git(writer, "push", "origin", "main")
    git(tmp_path, "clone", str(origin), str(app))
    return writer, app


def publish(writer):
    (writer / "state.txt").write_text("new")
    git(writer, "commit", "-am", "new")
    git(writer, "push", "origin", "main")
    return git(writer, "rev-parse", "HEAD")


class Operations:
    def __init__(self, update, app, *, running=True, fail=None):
        self.update, self.app, self.was_running = update, app, running
        self.running, self.fail, self.calls = running, fail, []
        self.needs_repair = False

    def prepare(self, previous, latest):
        self.calls.append("prepare")

    @contextmanager
    def stopped(self):
        self.calls.append("stop")
        self.running = False
        keep = False
        try:
            yield
        except self.update.backup.KeepStoppedError:
            keep = True
            raise
        finally:
            if self.was_running and not keep:
                self.calls.append("start")
                self.running = True
                if self.fail == "start":
                    raise RuntimeError("startup failure")

    def snapshot(self, revision):
        assert not self.running
        assert (self.app / "state.txt").read_text() == "old"
        self.calls.append("backup")
        if self.fail == "backup":
            raise RuntimeError("archive failure")
        return self.app.parent / "private-backup"

    def install(self):
        assert not self.running
        self.calls.append("install")
        if self.fail == "install":
            raise RuntimeError("installation failure")

    def health(self):
        assert self.running
        self.calls.append("health")
        if self.fail == "health":
            raise RuntimeError("health failure")

    def halt(self):
        self.calls.append("halt")
        self.running = False

    def complete(self):
        pass


def test_update_fetches_fast_forward_and_backs_up_before_changing_code(repositories):
    update = module()
    writer, app = repositories
    latest = publish(writer)
    ops = Operations(update, app)
    update.perform(update.Repository(app), ops)
    assert git(app, "rev-parse", "HEAD") == latest
    assert (app / "state.txt").read_text() == "new"
    assert ops.calls == ["prepare", "stop", "backup", "install", "start", "health"]
    assert ops.running


@pytest.mark.parametrize("state", ["dirty", "diverged"])
def test_update_refuses_local_changes_before_downtime(repositories, state):
    update = module()
    writer, app = repositories
    (app / "state.txt").write_text("local work")
    if state == "diverged":
        git(app, "commit", "-am", "local divergence")
    before = git(app, "rev-parse", "HEAD")
    publish(writer)
    ops = Operations(update, app)
    with pytest.raises(RuntimeError):
        update.perform(update.Repository(app), ops)
    assert git(app, "rev-parse", "HEAD") == before
    assert (app / "state.txt").read_text() == "local work"
    assert not ops.calls
    assert ops.running


def test_current_revision_does_not_stop_or_reinstall(repositories):
    update = module()
    _, app = repositories
    ops = Operations(update, app)
    update.perform(update.Repository(app), ops)
    assert not ops.calls
    assert ops.running


def test_install_failure_keeps_stack_stopped_with_backup(repositories, capsys):
    update = module()
    writer, app = repositories
    latest = publish(writer)
    ops = Operations(update, app, fail="install")
    with pytest.raises(update.backup.KeepStoppedError, match="private-backup"):
        update.perform(update.Repository(app), ops)
    assert git(app, "rev-parse", "HEAD") == latest
    assert not ops.running
    assert "backup" in ops.calls and "start" not in ops.calls


def test_health_failure_stops_new_stack_without_reverting_possibly_migrated_data(repositories):
    update = module()
    writer, app = repositories
    latest = publish(writer)
    ops = Operations(update, app, fail="health")
    with pytest.raises(RuntimeError, match="private-backup"):
        update.perform(update.Repository(app), ops)
    assert git(app, "rev-parse", "HEAD") == latest
    assert not ops.running
    assert ops.calls[-2:] == ["health", "halt"]
    assert ops.calls.count("install") == 1


def test_stopped_mode_is_preserved_during_update(repositories):
    update = module()
    writer, app = repositories
    publish(writer)
    ops = Operations(update, app, running=False)
    update.perform(update.Repository(app), ops)
    assert not ops.running
    assert "install" in ops.calls
    assert "start" not in ops.calls and "health" not in ops.calls


def test_sync_flag_reapplies_current_revision_without_resetting_source(repositories):
    update = module()
    _, app = repositories
    before = git(app, "rev-parse", "HEAD")
    ops = Operations(update, app)
    update.perform(update.Repository(app), ops, sync=True)
    assert git(app, "rev-parse", "HEAD") == before
    assert "backup" in ops.calls and "install" in ops.calls and ops.running


def test_incomplete_update_requires_explicit_repair_and_does_not_advance_to_another_commit(
    repositories,
):
    update = module()
    writer, app = repositories
    before = git(app, "rev-parse", "HEAD")
    publish(writer)
    ops = Operations(update, app)
    ops.needs_repair = True
    with pytest.raises(RuntimeError, match="incomplete"):
        update.perform(update.Repository(app), ops)
    assert not ops.calls
    update.perform(update.Repository(app), ops, sync=True)
    assert git(app, "rev-parse", "HEAD") == before
    assert "install" in ops.calls


def test_archive_failure_resumes_old_code_without_checkout(repositories):
    update = module()
    writer, app = repositories
    before = git(app, "rev-parse", "HEAD")
    publish(writer)
    ops = Operations(update, app, fail="backup")
    with pytest.raises(RuntimeError, match="archive"):
        update.perform(update.Repository(app), ops)
    assert git(app, "rev-parse", "HEAD") == before
    assert ops.running and "install" not in ops.calls


def test_partial_start_failure_halts_all_services_for_operator_recovery(repositories):
    update = module()
    writer, app = repositories
    latest = publish(writer)
    ops = Operations(update, app, fail="start")
    with pytest.raises(RuntimeError, match="private-backup"):
        update.perform(update.Repository(app), ops)
    assert git(app, "rev-parse", "HEAD") == latest
    assert not ops.running and ops.calls[-1] == "halt"


def test_deployment_disables_post_merge_hooks(repositories):
    update = module()
    writer, app = repositories
    publish(writer)
    hook = app / ".git/hooks/post-merge"
    hook.write_text("#!/bin/sh\nprintf 'hook mutation' > state.txt\n")
    hook.chmod(0o700)
    update.perform(update.Repository(app), Operations(update, app))
    assert (app / "state.txt").read_text() == "new"
    assert git(app, "status", "--porcelain") == ""


def test_source_is_revalidated_after_fast_forward(repositories):
    update = module()
    writer, app = repositories
    publish(writer)

    class ChangedAfterMerge(update.Repository):
        def call(self, *args):
            value = super().call(*args)
            if args[0] == "merge":
                (self.path / "state.txt").write_text("concurrent edit")
            return value

    ops = Operations(update, app)
    with pytest.raises(update.backup.KeepStoppedError):
        update.perform(ChangedAfterMerge(app), ops)
    assert "install" not in ops.calls and not ops.running
    assert (app / "state.txt").read_text() == "concurrent edit"


def test_known_previous_units_can_be_repaired_without_accepting_unrelated_overrides(tmp_path):
    update = module()
    source = tmp_path / "source/mnogobase-api.service"
    source.parent.mkdir()
    source.write_text("new unit")
    installed = tmp_path / "installed"
    installed.mkdir()
    unit = installed / source.name
    unit.write_text("old unit")
    known = {source.name: update.backup.digest(unit)}
    assert update.unit_baseline([source], installed, known) == known
    unit.write_text("new unit")
    assert update.unit_baseline([source], installed, known)[source.name] == update.backup.digest(
        source
    )
    unit.write_text("operator override")
    with pytest.raises(RuntimeError, match="override"):
        update.unit_baseline([source], installed, known)


def test_runtime_permissions_are_readable_not_writable_by_service_group(tmp_path):
    update = module()
    tree = tmp_path / "venv"
    (tree / "lib/models").mkdir(parents=True)
    source = tree / "lib/module.py"
    source.write_text("source")
    source.chmod(0o600)
    executable = tree / "entrypoint"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o700)
    outside = tmp_path / "private"
    outside.write_text("keep private")
    outside.chmod(0o600)
    (tree / "python").symlink_to(outside)
    update.runtime_permissions(tree, os.getuid(), os.getgid())
    # The unprivileged macOS sandbox strips setgid; production runs as Linux root.
    assert stat.S_IMODE((tree / "lib").stat().st_mode) & 0o777 == 0o750
    assert stat.S_IMODE(source.stat().st_mode) == 0o640
    assert stat.S_IMODE(executable.stat().st_mode) == 0o750
    assert stat.S_IMODE(outside.stat().st_mode) == 0o600
    assert outside.read_text() == "keep private"
