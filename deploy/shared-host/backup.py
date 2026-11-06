"""Cold server backups and an isolated database recovery check. Run through sudo."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import tarfile
import time
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4

ROOT = Path("/srv/mnogobase")
BACKUPS = Path("/var/backups/mnogobase")
UNITS = (
    "mnogobase.target",
    "mnogobase-api.service",
    "mnogobase-monitor.service",
    "mnogobase-worker.service",
    "mnogobase-models.service",
    "mnogobase-storage.service",
)
STORES = ("postgres", "qdrant", "neo4j")
CONTENTS = ("config", "data", "app", *(f"volumes/{name}" for name in STORES))
HOST_PATHS = (
    Path("/etc/nginx"),
    Path("/etc/letsencrypt"),
    *(Path("/etc/systemd/system") / name for name in UNITS),
    Path("/etc/systemd/system/mnogobase-app.slice"),
    Path("/etc/fstab"),
)


class KeepStoppedError(RuntimeError):
    """The current state is unsafe to resume automatically."""


class CleanupError(KeepStoppedError):
    """Keep production stopped when temporary containers could still be running."""


def run(args: list[str], *, env=None, input=None, umask=-1, timeout=2100, user=None) -> str:
    credentials = {}
    if user is not None:
        account = pwd.getpwnam(user)
        credentials = {
            "user": account.pw_uid,
            "group": account.pw_gid,
            "extra_groups": os.getgrouplist(user, account.pw_gid),
        }
        env = {
            **(os.environ if env is None else env),
            "HOME": account.pw_dir,
            "USER": user,
            "LOGNAME": user,
        }
    process = subprocess.Popen(
        args,
        env=env,
        umask=umask,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        **credentials,
    )
    try:
        stdout, _ = process.communicate(input=input, timeout=timeout)
    except BaseException:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=2)
        except BaseException:
            pass
        finally:
            # The leader may have exited while a descendant with redirected I/O
            # remains alive. Terminate the entire installer group before returning.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        raise
    if process.returncode:
        # Never echo subprocess stderr, environment values or a full Docker inspect.
        raise RuntimeError(f"{args[0]} {args[1]} failed; private command output was suppressed")
    return stdout.strip()


def states() -> list[str]:
    return run(["systemctl", "show", "--property=ActiveState", "--value", *UNITS]).split()


def offline() -> None:
    if states() != ["inactive"] * len(UNITS):
        raise RuntimeError("All six Mnogobase units must be inactive")
    for project in ("mnogobase-server", "mnogobase-models"):
        if run(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={project}"]):
            raise RuntimeError("Mnogobase containers are still running; refusing a cold copy")


@contextmanager
def quiesced():
    initial = states()
    resume = initial == ["active"] * len(UNITS)
    if not resume and initial != ["inactive"] * len(UNITS):
        raise RuntimeError("Mnogobase is partially active or failed; inspect units before backup")
    try:
        if resume:
            print("Stopping all six Mnogobase units...", flush=True)
            run(["systemctl", "stop", *UNITS])
        offline()
        yield
    except KeepStoppedError:
        resume = False
        print(
            "Operation requires recovery: production remains stopped. Inspect the reported details."
        )
        raise
    finally:
        if resume:
            print("Starting Mnogobase again...", flush=True)
            run(["systemctl", "start", "mnogobase.target"])
            if states() != ["active"] * len(UNITS):
                raise RuntimeError("Mnogobase restart incomplete; inspect systemctl status")
            print("All six Mnogobase units are active.", flush=True)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def include(member: tarfile.TarInfo):
    parts = PurePosixPath(member.name).parts
    if (
        parts[:2] == ("project", "app")
        and len(parts) > 2
        and (
            parts[2]
            in {
                ".git",
                ".venv",
                ".planning",
                ".mnogobase",
                ".agents",
                ".codex",
                ".aws",
                ".ssh",
                "documents",
                "wiki",
                "logs",
                "runs",
                ".deployment-backup",
            }
            or parts[2].startswith(".env")
        )
    ):
        return None
    if "__pycache__" in parts or any(p in {".pytest_cache", ".ruff_cache"} for p in parts):
        return None
    if parts[:3] == ("project", "data", "tmp"):
        return None
    return member


def include_host(member: tarfile.TarInfo):
    # Rebase absolute nginx site/module links inside the archived host tree.
    if member.issym() and member.linkname.startswith("/"):
        member.linkname = os.path.relpath(
            "host/" + member.linkname.lstrip("/"), str(PurePosixPath(member.name).parent)
        )
        member.pax_headers.pop("linkpath", None)
    return member


def archive(
    root: Path, destination: Path, images: dict, host_paths=(), *, pull_references=None
) -> Path:
    if destination.resolve().is_relative_to(root.resolve()):
        raise RuntimeError("Backup destination must be outside project storage")
    for relative in CONTENTS:
        source = root / relative
        if source.is_symlink() or not source.is_dir():
            raise RuntimeError(f"Missing or symlinked backup source: {relative}")
    for relative in (
        "config/storage.env",
        "config/config.yaml",
        "config/api.env",
        "app/pyproject.toml",
        "app/uv.lock",
    ):
        if not (root / relative).is_file():
            raise RuntimeError(f"Missing required file: {relative}")
    destination.mkdir(mode=0o700)
    payload = destination / "backup.tar.gz"
    with payload.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        with tarfile.open(fileobj=stream, mode="w:gz", dereference=False) as tar:
            for relative in CONTENTS:
                tar.add(root / relative, arcname=f"project/{relative}", filter=include)
            for path in host_paths:
                if path.exists():
                    tar.add(path, arcname="host/" + str(path).lstrip("/"), filter=include_host)
        stream.flush()
        os.fsync(stream.fileno())
    manifest = {
        "format": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "images": images,
        "pull_references": pull_references or {},
        "sha256": digest(payload),
    }
    # A partial archive is not a completed backup: manifest is written last.
    with (destination / "manifest.json").open("x") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(manifest, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    return destination


def safe_member(member: tarfile.TarInfo, destination: str):
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts or path.parts[:1] not in {("project",), ("host",)}:
        raise RuntimeError("Unsafe archive path")
    filtered = tarfile.data_filter(member, destination)
    # Keep numeric owners and permissions needed by the three database images.
    return filtered.replace(uid=member.uid, gid=member.gid, mode=member.mode & 0o777)


def extract(snapshot: Path, destination: Path) -> dict:
    manifest = json.loads((snapshot / "manifest.json").read_text())
    payload = snapshot / "backup.tar.gz"
    if manifest.get("format") != 1 or digest(payload) != manifest.get("sha256"):
        raise RuntimeError("Backup checksum or format mismatch")
    images = manifest.get("images", {})
    if set(images) != set(STORES) or any(
        not re.fullmatch(r"sha256:[0-9a-f]{64}", v) for v in images.values()
    ):
        raise RuntimeError("Backup must identify exact database images")
    with tarfile.open(payload, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            safe_member(member, str(destination))
        needed = sum(member.size for member in members if member.isfile())
        if shutil.disk_usage(destination.parent).free < needed + 512 * 1024**2:
            raise RuntimeError("Not enough free space for an isolated restore")
        destination.mkdir(mode=0o700)
        tar.extractall(destination, members=members, numeric_owner=True, filter=safe_member)
    return manifest


def image_ids() -> dict:
    images = {}
    for service in STORES:
        ids = run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                "label=com.docker.compose.project=mnogobase-server",
                "--filter",
                f"label=com.docker.compose.service={service}",
            ]
        ).split()
        if len(ids) != 1:
            raise RuntimeError(f"Expected exactly one existing {service} container")
        image = run(["docker", "inspect", "--format={{.Image}}", ids[0]])
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise RuntimeError("Cannot identify database image")
        images[service] = image
    return images


def check_sources(project: Path, rows: list[dict]) -> int:
    for row in rows:
        original = Path(row["path"])
        if not original.is_relative_to(ROOT):
            raise RuntimeError("Upload metadata points outside project storage")
        restored = project / original.relative_to(ROOT)
        if not restored.resolve().is_relative_to(project.resolve()) or not restored.is_file():
            raise RuntimeError("Recovered source file is missing or outside the recovery tree")
        if digest(restored)[:16] != row["doc_id"]:
            raise RuntimeError("Recovered source does not match its document ID")
    return len(rows)


def qdrant_address(compose: list[str], env: dict, project: str) -> str:
    """The Linux host can access an internal bridge IP without publishing a port."""
    container = run([*compose, "ps", "-q", "qdrant"], env=env)
    if not re.fullmatch(r"[0-9a-f]{12,64}", container):
        raise RuntimeError("Expected exactly one recovery Qdrant container")
    settings = json.loads(
        run(
            [
                "docker",
                "inspect",
                "--format={{json .NetworkSettings}}",
                container,
            ],
            env=env,
        )
    )
    if any(settings.get("Ports", {}).values()):
        raise RuntimeError("Recovery databases must not publish host ports")
    networks = settings.get("Networks", {})
    if set(networks) != {project + "_default"}:
        raise RuntimeError("Recovery Qdrant is attached to an unexpected network")
    network = networks[project + "_default"]
    identity = run(
        [
            "docker",
            "network",
            "inspect",
            "--format",
            '{{.Internal}} {{index .Labels "com.docker.compose.project"}}',
            network["NetworkID"],
        ],
        env=env,
    )
    if identity != "true " + project:
        raise RuntimeError("Recovery network must be internal and belong to the recovery project")
    address = ipaddress.IPv4Address(network["IPAddress"])
    if (
        not address.is_private
        or address.is_loopback
        or address.is_unspecified
        or address.is_multicast
    ):
        raise RuntimeError("Recovery Qdrant must have a private container IP")
    return f"{address}:6333"


def recovery_check(snapshot: Path) -> None:
    work = ROOT / (".restore-check-" + uuid4().hex[:12])
    compose = None
    env = None
    started = False
    try:
        manifest = extract(snapshot, work)
        from dotenv import dotenv_values

        secrets = dotenv_values(work / "project/config/storage.env")
        env = dict(os.environ)
        for key in ("POSTGRES_PASSWORD", "NEO4J_PASSWORD"):
            if not secrets.get(key):
                raise RuntimeError("Backup is missing database credentials")
            env[key] = secrets[key]
        env["RESTORE_ROOT"] = str(work / "project")
        for service, image in manifest["images"].items():
            env[f"RESTORE_{service.upper()}_IMAGE"] = image
        project = "mnogobase-restore-" + work.name.removeprefix(".restore-check-")
        compose = [
            "docker",
            "compose",
            "--project-name",
            project,
            "--env-file",
            str(work / "project/config/storage.env"),
            "-f",
            str(Path(__file__).with_name("restore-test.yml")),
        ]
        print(f"Recovery project: {project}; copied files: {work}", flush=True)
        print("Starting isolated copies of the three databases...", flush=True)
        started = True
        run([*compose, "up", "-d", "--wait", "--wait-timeout", "240", "--pull", "never"], env=env)
        sql = """SELECT format(
          'SELECT %L AS object, count(*) AS rows FROM %I.%I;',
          table_schema || '.' || table_name, table_schema, table_name)
        FROM information_schema.tables
        WHERE table_type='BASE TABLE' AND table_schema NOT IN ('pg_catalog','information_schema')
        ORDER BY table_schema,table_name
        \\gexec
        """
        counts = run(
            [
                *compose,
                "exec",
                "-T",
                "postgres",
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "mnogobase",
                "-d",
                "mnogobase",
                "-At",
            ],
            env=env,
            input=sql,
        )
        if not counts:
            raise RuntimeError("Recovered PostgreSQL has no application tables")
        print("PostgreSQL recovered table counts:\n" + counts, flush=True)
        source_sql = """SELECT format(
          'SELECT json_build_object(''doc_id'',doc_id,''path'',path) FROM %I.uploads;',
          table_schema) FROM information_schema.tables
        WHERE table_name='uploads' AND table_schema NOT IN ('pg_catalog','information_schema')
        \\gexec
        """
        sources = run(
            [
                *compose,
                "exec",
                "-T",
                "postgres",
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "mnogobase",
                "-d",
                "mnogobase",
                "-At",
            ],
            env=env,
            input=source_sql,
        )
        verified = check_sources(
            work / "project", [json.loads(row) for row in sources.splitlines()]
        )
        print(f"Recovered upload files verified against document IDs: {verified}", flush=True)
        graph = run(
            [
                *compose,
                "exec",
                "-T",
                "-e",
                "NEO4J_USERNAME=neo4j",
                "-e",
                "NEO4J_PASSWORD",
                "neo4j",
                "cypher-shell",
                "--format",
                "plain",
                "MATCH (n) UNWIND labels(n) AS label RETURN label, count(*) AS nodes ORDER BY label",
            ],
            env=env,
        )
        print("Neo4j recovered label counts:\n" + graph, flush=True)
        address = qdrant_address(compose, env, project)
        opener = build_opener(ProxyHandler({}))

        def get(path):
            with opener.open(f"http://{address}/{path}", timeout=5) as response:
                return json.load(response)["result"]

        deadline = time.monotonic() + 60
        while True:
            try:
                collections = get("collections")["collections"]
                break
            except (URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Recovered Qdrant did not become ready") from None
                time.sleep(1)
        vectors = {
            c["name"]: get("collections/" + quote(c["name"], safe=""))["points_count"]
            for c in collections
        }
        print("Qdrant recovered point counts:\n" + json.dumps(vectors, indent=2), flush=True)
    finally:
        if started:
            try:
                run([*compose, "down", "--volumes", "--timeout", "30"], env=env)
            except Exception:
                raise CleanupError(f"Recovery cleanup failed; retained directory: {work}") from None
        if work.exists():
            shutil.rmtree(work)
    print("RECOVERY CHECK OK (files and database reads; no LLM calls).", flush=True)


def new_destination(root: Path, backups: Path) -> Path:
    if backups.resolve() != backups or backups.is_relative_to(root):
        raise RuntimeError("Backup directory must be outside project storage without symlinks")
    backups.mkdir(mode=0o700, parents=True, exist_ok=True)
    if backups.stat().st_uid != 0 or backups.stat().st_mode & 0o077:
        raise RuntimeError("Backup parent must be root-owned with mode0700")
    needed = sum(
        int(line.split()[0])
        for line in run(["du", "-sb", *(str(root / p) for p in CONTENTS)]).splitlines()
    )
    if shutil.disk_usage(backups).free < needed + 512 * 1024**2:
        raise RuntimeError("Not enough free space for a backup")
    return backups / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--check", action="store_true", help="Also boot an isolated recovery copy")
    check = commands.add_parser("check")
    check.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0 or ROOT.is_symlink() or not os.path.ismount(ROOT):
        raise RuntimeError("Run through sudo on the server with /srv/mnogobase mounted")
    os.umask(0o077)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    with Path("/run/lock/mnogobase-backup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "create":
            destination = new_destination(ROOT, BACKUPS)
            images = image_ids()
            references = {
                name: json.loads(
                    run(["docker", "image", "inspect", "--format={{json .RepoDigests}}", image])
                )
                for name, image in images.items()
            }
            with quiesced():
                archive(ROOT, destination, images, HOST_PATHS, pull_references=references)
                print("Backup: " + str(destination), flush=True)
                if args.check:
                    recovery_check(destination)
        else:
            with quiesced():
                recovery_check(args.snapshot.resolve(strict=True))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Interrupted; inspect Mnogobase unit states before continuing.") from None
    except (OSError, RuntimeError, ValueError, tarfile.TarError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(
            str(exc)
            if isinstance(exc, RuntimeError)
            else "Backup/check failed; no secret values displayed."
        ) from None
