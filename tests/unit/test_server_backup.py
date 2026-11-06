import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "deploy/shared-host/backup.py"
IMAGES = {name: "sha256:" + "a" * 64 for name in ("postgres", "qdrant", "neo4j")}


def test_cancelled_command_cannot_leave_a_child_mutating_after_return(tmp_path):
    backup = module()
    marker = tmp_path / "late-write"
    launcher = tmp_path / "launcher.py"
    launcher.write_text(
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable,'-c',"
        "'import time,sys; from pathlib import Path; time.sleep(.5); Path(sys.argv[1]).write_text(\"late\")',"
        "sys.argv[1]],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "time.sleep(30)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        backup.run([sys.executable, str(launcher), str(marker)], timeout=0.2)
    time.sleep(0.7)
    assert not marker.exists()


def module():
    spec = importlib.util.spec_from_file_location("server_backup", SCRIPT)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def project(root):
    for name in ("config", "data", "app", "volumes/postgres", "volumes/qdrant", "volumes/neo4j"):
        (root / name).mkdir(parents=True, exist_ok=True)
    for name in ("storage.env", "config.yaml", "api.env"):
        (root / "config" / name).write_text("private value")
    (root / "config/storage.env").chmod(0o640)
    for name in ("pyproject.toml", "uv.lock"):
        (root / "app" / name).write_text("server CPU dependency pin")
    (root / "data/source.md").write_text("team knowledge")
    (root / "volumes/postgres/PG_VERSION").write_text("18")
    for name in ("app/.venv", "app/.git", "app/.env", "volumes/models", "cache", "toolchains"):
        p = root / name
        p.mkdir(parents=True) if ".env" not in name else p.write_text("do not copy")
        if p.is_dir():
            (p / "large").write_text("do not copy")
    (root / "app/src/mnogobase/wiki").mkdir(parents=True)
    (root / "app/src/mnogobase/wiki/build.py").write_text("keep source wiki module")


def test_cold_archive_roundtrip_private_complete_and_without_downloads(tmp_path):
    backup = module()
    root = tmp_path / "live"
    project(root)
    snapshot = tmp_path / "backup"
    backup.archive(root, snapshot, IMAGES)
    assert snapshot.stat().st_mode & 0o777 == 0o700
    assert (snapshot / "backup.tar.gz").stat().st_mode & 0o777 == 0o600
    restored = tmp_path / "restored"
    manifest = backup.extract(snapshot, restored)
    assert manifest["images"] == IMAGES
    assert (restored / "project/config/storage.env").read_text() == "private value"
    assert (restored / "project/config/storage.env").stat().st_mode & 0o777 == 0o640
    assert (restored / "project/app/uv.lock").read_text() == "server CPU dependency pin"
    assert (restored / "project/app/src/mnogobase/wiki/build.py").is_file()
    assert (restored / "project/volumes/postgres/PG_VERSION").read_text() == "18"
    for name in ("app/.venv", "app/.git", "app/.env", "volumes/models", "cache", "toolchains"):
        assert not (restored / "project" / name).exists()
    assert (root / "data/source.md").read_text() == "team knowledge"
    with pytest.raises(FileExistsError):
        backup.extract(snapshot, restored)


def test_corruption_is_rejected_before_creating_destination(tmp_path):
    backup = module()
    root = tmp_path / "live"
    project(root)
    snapshot = tmp_path / "backup"
    backup.archive(root, snapshot, IMAGES)
    with (snapshot / "backup.tar.gz").open("ab") as stream:
        stream.write(b"corrupt")
    dest = tmp_path / "recovery"
    with pytest.raises(RuntimeError, match="checksum"):
        backup.extract(snapshot, dest)
    assert not dest.exists()


@pytest.mark.parametrize("name,link", [("../../outside", None), ("project/link", "/etc")])
def test_unsafe_tar_is_rejected_before_extraction(tmp_path, name, link):
    backup = module()
    snapshot = tmp_path / "backup"
    snapshot.mkdir()
    with tarfile.open(snapshot / "backup.tar.gz", "w:gz") as tar:
        member = tarfile.TarInfo(name)
        if link is not None:
            member.type, member.linkname = tarfile.SYMTYPE, link
        tar.addfile(member)
    (snapshot / "manifest.json").write_text(
        json.dumps(
            {
                "format": 1,
                "sha256": hashlib.sha256((snapshot / "backup.tar.gz").read_bytes()).hexdigest(),
                "images": IMAGES,
            }
        )
    )
    dest = tmp_path / "recovery"
    with pytest.raises((RuntimeError, tarfile.FilterError)):
        backup.extract(snapshot, dest)
    assert not dest.exists()


@pytest.mark.parametrize("initial", ["active", "inactive"])
def test_quiesce_restores_prior_state_on_failure(monkeypatch, initial):
    backup = module()
    state = [initial] * len(backup.UNITS)
    actions = []

    def run(args, **kwargs):
        if args[:2] == ["systemctl", "show"]:
            return "\n".join(state)
        actions.append(args)
        if args[:2] == ["systemctl", "stop"]:
            assert args[2:] == list(backup.UNITS)
            state[:] = ["inactive"] * len(state)
        elif args[:2] == ["systemctl", "start"]:
            state[:] = ["active"] * len(state)
        return ""

    monkeypatch.setattr(backup, "run", run)
    with pytest.raises(ValueError, match="archive failed"), backup.quiesced():
        assert set(state) == {"inactive"}
        raise ValueError("archive failed")
    assert state == [initial] * len(backup.UNITS)
    assert any(a[:2] == ["systemctl", "start"] for a in actions) == (initial == "active")


def test_quiesce_refuses_partial_stack_before_mutation(monkeypatch):
    backup = module()
    actions = []

    def run(args, **kwargs):
        actions.append(args)
        return "active\ninactive\nactive\nactive\nactive\nactive"

    monkeypatch.setattr(backup, "run", run)
    with pytest.raises(RuntimeError, match="partially"), backup.quiesced():
        pytest.fail("partial stack must not be copied")
    assert all(a[:2] == ["systemctl", "show"] for a in actions)


def test_archive_refuses_destination_inside_source(tmp_path):
    backup = module()
    root = tmp_path / "live"
    project(root)
    with pytest.raises(RuntimeError, match="outside"):
        backup.archive(root, root / "data/backup", IMAGES)
    assert not (root / "data/backup").exists()


def test_host_absolute_symlinks_stay_inside_restored_tree(tmp_path):
    backup = module()
    root = tmp_path / "live"
    project(root)
    host = tmp_path / "nginx"
    host.mkdir()
    (host / "site").write_text("proxy configuration")
    (host / "enabled").symlink_to(host / "site")
    snapshot = tmp_path / "backup"
    backup.archive(root, snapshot, IMAGES, host_paths=[host])
    restored = tmp_path / "restored"
    backup.extract(snapshot, restored)
    link = restored / "host" / str(host).lstrip("/") / "enabled"
    assert link.read_text() == "proxy configuration"
    assert link.resolve().is_relative_to(restored)


def test_cleanup_failure_keeps_production_stopped(monkeypatch):
    backup = module()
    state = ["active"] * len(backup.UNITS)

    def run(args, **kwargs):
        if args[:2] == ["systemctl", "show"]:
            return "\n".join(state)
        if args[:2] == ["systemctl", "stop"]:
            state[:] = ["inactive"] * len(state)
        if args[:2] == ["systemctl", "start"]:
            pytest.fail("must not resume with possibly active recovery databases")
        return ""

    monkeypatch.setattr(backup, "run", run)
    with pytest.raises(backup.CleanupError), backup.quiesced():
        raise backup.CleanupError("temporary containers survived")
    assert set(state) == {"inactive"}


@pytest.mark.parametrize("contents", [b"hello", b"corrupt"])
def test_recovered_sources_match_upload_metadata(tmp_path, contents):
    backup = module()
    name = "data/service/teams/team/documents/2cf24dba5fb0a30e.md"
    source = tmp_path / name
    source.parent.mkdir(parents=True)
    source.write_bytes(contents)
    rows = [{"doc_id": "2cf24dba5fb0a30e", "path": "/srv/mnogobase/" + name}]
    if contents == b"hello":
        assert backup.check_sources(tmp_path, rows) == 1
    else:
        with pytest.raises(RuntimeError, match="source"):
            backup.check_sources(tmp_path, rows)


def test_source_check_rejects_metadata_outside_project(tmp_path):
    backup = module()
    with pytest.raises(RuntimeError, match="outside"):
        backup.check_sources(tmp_path, [{"doc_id": "2cf24dba5fb0a30e", "path": "/etc/passwd"}])


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_failed_recovery_cleans_anonymous_volumes_or_retains_bind_files(
    tmp_path, monkeypatch, cleanup_fails
):
    backup = module()
    root = tmp_path / "live"
    project(root)
    (root / "config/storage.env").write_text("POSTGRES_PASSWORD=private\nNEO4J_PASSWORD=private\n")
    snapshot = tmp_path / "backup"
    backup.archive(root, snapshot, IMAGES)
    monkeypatch.setattr(backup, "ROOT", root)
    teardown = []

    def run(args, **kwargs):
        if "up" in args:
            raise RuntimeError("database startup failed")
        if "down" in args:
            teardown.append(args)
            assert "--volumes" in args, "anonymous database log volumes must be removed"
            if cleanup_fails:
                raise RuntimeError("teardown failed")
        return ""

    monkeypatch.setattr(backup, "run", run)
    with pytest.raises(backup.CleanupError if cleanup_fails else RuntimeError):
        backup.recovery_check(snapshot)
    assert len(teardown) == 1
    assert bool(list(root.glob(".restore-check-*"))) == cleanup_fails
    assert (root / "data/source.md").read_text() == "team knowledge"


def test_recovery_parses_upload_rows_without_psql_generator_output(tmp_path, monkeypatch):
    backup = module()
    root = tmp_path / "live"
    project(root)
    (root / "data/source.md").write_text("hello")
    (root / "config/storage.env").write_text("POSTGRES_PASSWORD=private\nNEO4J_PASSWORD=private\n")
    snapshot = tmp_path / "backup"
    backup.archive(root, snapshot, IMAGES)
    monkeypatch.setattr(backup, "ROOT", root)

    def run(args, **kwargs):
        if "psql" in args:
            script = kwargs["input"]
            if "json_build_object" in script:
                rows = json.dumps(
                    {"doc_id": "2cf24dba5fb0a30e", "path": str(root / "data/source.md")}
                )
                # psql prints a normally terminated generator query's results before
                # a subsequent empty-buffer gexec reexecutes it (PostgreSQL18 docs).
                if script.split("\\gexec")[0].rstrip().endswith(";"):
                    return "SELECT json_build_object(...) FROM uploads;\n" + rows
                return rows
            return "mnogobase_service.users|2"
        if "cypher-shell" in args:
            return "label,nodes\nmb_team_test_Document,1"
        if "ps" in args:
            return "c" * 64
        if args[:2] == ["docker", "inspect"]:
            project = next(root.glob(".restore-check-*")).name.removeprefix(".restore-check-")
            return json.dumps(
                {
                    "Ports": {"6333/tcp": None},
                    "Networks": {
                        f"mnogobase-restore-{project}_default": {
                            "IPAddress": "172.30.0.2",
                            "NetworkID": "d" * 64,
                        }
                    },
                }
            )
        if args[:3] == ["docker", "network", "inspect"]:
            project = next(root.glob(".restore-check-*")).name.removeprefix(".restore-check-")
            return "true mnogobase-restore-" + project
        return ""

    class Opener:
        def open(self, url, timeout):
            assert url.startswith("http://172.30.0.2:6333/")
            return io.StringIO('{"result":{"collections":[]}}')

    monkeypatch.setattr(backup, "run", run)
    monkeypatch.setattr(backup, "build_opener", lambda *_: Opener())
    backup.recovery_check(snapshot)
    assert not list(root.glob(".restore-check-*"))
    assert (root / "data/source.md").read_text() == "hello"


@pytest.mark.parametrize(
    "failure", [None, "public_port", "foreign_network", "host_ip", "public_ip", "external_network"]
)
def test_recovery_qdrant_uses_only_its_private_network_without_published_ports(
    monkeypatch, failure
):
    backup = module()
    project_name = "mnogobase-restore-abc123abc123"
    settings = {
        "Bridge": "",
        "SandboxID": "e" * 64,
        "SandboxKey": "/var/run/docker/netns/example",
        "Ports": {"6333/tcp": None, "6334/tcp": None},
        "Networks": {
            project_name + "_default": {
                "IPAMConfig": None,
                "Links": None,
                "Aliases": ["qdrant"],
                "NetworkID": "d" * 64,
                "EndpointID": "b" * 64,
                "Gateway": "172.30.0.1",
                "IPAddress": "172.30.0.2",
                "IPPrefixLen": 16,
                "IPv6Gateway": "",
                "GlobalIPv6Address": "",
                "GlobalIPv6PrefixLen": 0,
                "MacAddress": "02:42:ac:1e:00:02",
                "DNSNames": ["qdrant"],
            }
        },
    }
    if failure == "public_port":
        settings["Ports"]["6333/tcp"] = [{"HostIp": "0.0.0.0", "HostPort": "32768"}]
    if failure == "foreign_network":
        settings["Networks"]["mnogobase-server_default"] = settings["Networks"].pop(
            project_name + "_default"
        )
    if failure == "host_ip":
        settings["Networks"][project_name + "_default"]["IPAddress"] = "127.0.0.1"
    if failure == "public_ip":
        settings["Networks"][project_name + "_default"]["IPAddress"] = "8.8.8.8"

    def run(args, **kwargs):
        if args[:2] == ["docker", "inspect"]:
            return json.dumps(settings)
        if args[:3] == ["docker", "network", "inspect"]:
            return ("false" if failure == "external_network" else "true") + " " + project_name
        if "port" in args:
            pytest.fail("Docker29 internal-only network has no usable published port")
        assert args[-3:] == ["ps", "-q", "qdrant"]
        return "c" * 64

    monkeypatch.setattr(backup, "run", run)
    compose = ["docker", "compose", "--project-name", project_name]
    if failure:
        with pytest.raises(RuntimeError):
            backup.qdrant_address(compose, {}, project_name)
    else:
        assert backup.qdrant_address(compose, {}, project_name) == "172.30.0.2:6333"
