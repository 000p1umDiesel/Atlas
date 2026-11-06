"""Fast-forward Git update with frozen CPU dependencies and a cold recovery copy."""

from __future__ import annotations

import argparse
import fcntl
import grp
import importlib.util
import json
import os
import pwd
import signal
import stat
import subprocess
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "shared_host_backup", Path(__file__).with_name("backup.py")
)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)

ROOT = backup.ROOT
APP = ROOT / "app"
ORIGIN = "https://github.com/000p1umDiesel/Atlas.git"
SYSTEMD = Path("/etc/systemd/system")


class Repository:
    def __init__(self, path: Path, *, owner: str | None = None, expected_origin: str | None = None):
        self.path, self.owner, self.expected_origin = path, owner, expected_origin

    def call(self, *args: str) -> str:
        command = ["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.path), *args]
        return backup.run(
            command, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, umask=0o027, user=self.owner
        )

    def plan(self) -> tuple[str, str]:
        if Path(self.call("rev-parse", "--show-toplevel")).resolve() != self.path.resolve():
            raise RuntimeError("Application directory must be the repository root")
        if self.call("branch", "--show-current") != "main":
            raise RuntimeError("Server updates require the main branch")
        if (
            self.call("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
            != "origin/main"
        ):
            raise RuntimeError("main must track origin/main")
        if self.expected_origin and self.call("remote", "get-url", "origin").rstrip(
            "/"
        ).removesuffix(".git") != self.expected_origin.removesuffix(".git"):
            raise RuntimeError(
                "origin must match the configured public project URL without credentials"
            )
        if self.call("status", "--porcelain"):
            raise RuntimeError(
                "Server checkout must be clean; preserve local changes before updating"
            )
        self.call("fetch", "--prune", "origin")
        previous, latest = self.call("rev-parse", "HEAD"), self.call("rev-parse", "origin/main")
        try:
            self.call("merge-base", "--is-ancestor", previous, latest)
        except RuntimeError:
            raise RuntimeError("Server branch diverged; refusing a force update") from None
        return previous, latest


def perform(repository: Repository, operations, *, sync: bool = False) -> None:
    previous, latest = repository.plan()
    if operations.needs_repair and not sync:
        raise RuntimeError("Previous update is incomplete; inspect it and use --sync to repair")
    if sync:
        latest = previous
    if previous == latest and not sync:
        print("Code is current; no installation or restart performed. Use --sync to reapply setup.")
        return
    operations.prepare(previous, latest)
    snapshot = None
    print(f"Deploying {previous[:12]} -> {latest[:12]}", flush=True)
    try:
        with operations.stopped():
            snapshot = operations.snapshot(previous)
            print("Before-update backup: " + str(snapshot), flush=True)
            try:
                repository.call("merge", "--ff-only", latest)
                if repository.call("rev-parse", "HEAD") != latest or repository.call(
                    "status", "--porcelain"
                ):
                    raise RuntimeError(
                        "Checkout changed during fast-forward; refusing installation"
                    )
                operations.install()
            except BaseException as error:
                raise backup.KeepStoppedError(
                    f"Update interrupted; stack kept stopped. Backup: {snapshot}; previous revision: {previous}. "
                    "Inspect the failure, then use --sync to reapply setup. No automatic data rollback."
                ) from error
        if operations.was_running:
            operations.health()
        operations.complete()
    except backup.KeepStoppedError:
        raise
    except BaseException as error:
        if snapshot is not None:
            operations.halt()
            raise backup.KeepStoppedError(
                f"Startup/check failed; stack stopped. Backup: {snapshot}; previous revision: {previous}. "
                "New revision retained; inspect logs before retrying."
            ) from error
        raise
    print(
        "Update installed."
        if operations.was_running
        else "Update installed; stack remains stopped.",
        flush=True,
    )


def unit_baseline(sources: list[Path], directory: Path, previous: dict | None = None) -> dict:
    hashes = {}
    for source in sources:
        installed = directory / source.name
        if installed.is_symlink() or not installed.is_file():
            raise RuntimeError("Installed systemd unit is missing or overridden by a symlink")
        digest = backup.digest(installed)
        if digest != backup.digest(source) and digest != (previous or {}).get(source.name):
            raise RuntimeError(
                "Installed units have an unrelated local override; review it before update"
            )
        hashes[source.name] = digest
    return hashes


def save_record(path: Path, record: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".update-state-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def runtime_permissions(root: Path, uid: int, gid: int) -> None:
    if root.is_symlink():
        raise RuntimeError("Runtime tree must not be a symlink")
    for directory, dirs, files in os.walk(root, followlinks=False):
        for path in [Path(directory), *(Path(directory) / name for name in [*dirs, *files])]:
            os.chown(path, uid, gid, follow_symlinks=False)
            if path.is_symlink():
                continue
            mode = path.stat().st_mode
            path.chmod(0o2750 if stat.S_ISDIR(mode) else 0o750 if mode & 0o111 else 0o640)


class Operations:
    def __init__(self):
        self.was_running = False
        self.destination = None
        self.owner = pwd.getpwuid(APP.stat().st_uid).pw_name
        self.uid, self.gid = APP.stat().st_uid, grp.getgrnam("mnogobase").gr_gid
        self.state_path = ROOT / "config/update-state.json"
        self.needs_repair = self.state_path.exists()
        self.record = None
        self.env = {
            **os.environ,
            "UV_PROJECT_ENVIRONMENT": str(APP / ".venv"),
            "UV_CACHE_DIR": str(ROOT / "cache/uv"),
            "UV_PYTHON_INSTALL_DIR": str(ROOT / "toolchains/python"),
        }
        self.env.pop("VIRTUAL_ENV", None)

    def units(self):
        return [APP / "deploy/shared-host/systemd" / name for name in backup.UNITS] + [
            APP / "deploy/shared-host/mnogobase-app.slice"
        ]

    def prepare(self, previous, latest):
        if self.owner != "admin":
            raise RuntimeError("This server profile requires application sources owned by admin")
        if self.needs_repair:
            if self.state_path.is_symlink():
                raise RuntimeError("Recovery record must not be a symlink")
            self.record = json.loads(self.state_path.read_text())
            if self.record.get("format") != 1 or previous not in {
                self.record.get("before"),
                self.record.get("target"),
            }:
                raise RuntimeError(
                    "Recovery record belongs to another revision; inspect it manually"
                )
        self.before, self.target = previous, latest
        self.baseline = unit_baseline(
            self.units(), SYSTEMD, self.record.get("unit_hashes") if self.record else None
        )
        if not (APP / "deploy/shared-host/python/uv.lock").is_file():
            raise RuntimeError("Missing frozen CPU profile")
        self.destination = backup.new_destination(ROOT, backup.BACKUPS)
        os.chown(APP, self.uid, self.gid)
        APP.chmod(0o2750)
        backup.run(
            [
                "install",
                "-d",
                "-o",
                self.owner,
                "-g",
                "mnogobase",
                "-m",
                "2750",
                str(ROOT / "cache/uv"),
            ]
        )

    def stopped(self):
        self.was_running = backup.states() == ["active"] * len(backup.UNITS)
        return backup.quiesced()

    def snapshot(self, revision):
        images = backup.image_ids()
        references = {
            name: json.loads(
                backup.run(["docker", "image", "inspect", "--format={{json .RepoDigests}}", image])
            )
            for name, image in images.items()
        }
        result = backup.archive(
            ROOT, self.destination, images, backup.HOST_PATHS, pull_references=references
        )
        with (result / "git-revision.txt").open("x") as stream:
            stream.write(revision + "\n")
        if self.record is None:
            self.record = {
                "format": 1,
                "before": self.before,
                "target": self.target,
                "backup": str(result),
                "unit_hashes": self.baseline,
            }
        save_record(self.state_path, self.record)
        return result

    def install(self):
        print("Syncing frozen CPU dependencies...", flush=True)
        # RapidOCR's checkpoint cache is owned by the service account; release it
        # while stopped so uv can replace an old package, without following links.
        runtime_permissions(APP / ".venv", self.uid, self.gid)
        backup.run(
            [
                str(ROOT / "toolchains/uv/uv"),
                "sync",
                "--project",
                str(APP / "deploy/shared-host/python"),
                "--locked",
                "--no-dev",
                "--python",
                str(APP / ".venv/bin/python"),
            ],
            env=self.env,
            umask=0o027,
            user=self.owner,
        )
        for tree in (APP / ".venv", APP / "src", APP / "deploy"):
            runtime_permissions(tree, self.uid, self.gid)
        backup.run(
            [
                str(APP / ".venv/bin/python"),
                "-c",
                "import torch, torchvision; from importlib.metadata import distributions; "
                "assert torch.version.cuda is None, 'CPU Torch required'; "
                "assert not any(d.metadata.get('Name','').lower().startswith('nvidia-') for d in distributions()), 'NVIDIA packages found'",
            ]
        )
        backup.run([str(APP / ".venv/bin/python"), str(APP / "deploy/shared-host/prepare_app.py")])
        sources = [str(path) for path in self.units()]
        backup.run(["systemd-analyze", "verify", *sources])
        backup.run(
            ["install", "-o", "root", "-g", "root", "-m", "644", *sources, "/etc/systemd/system/"]
        )
        backup.run(["systemctl", "daemon-reload"])

    def health(self):
        backup.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "10",
                "http://127.0.0.1:8080/health",
            ]
        )

    def halt(self):
        backup.run(["systemctl", "stop", *backup.UNITS])

    def complete(self):
        self.state_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sync", action="store_true", help="Reapply current revision's dependencies and units"
    )
    args = parser.parse_args()
    if os.geteuid() != 0 or ROOT.is_symlink() or not os.path.ismount(ROOT):
        raise RuntimeError("Run through sudo on the mounted production server")
    os.umask(0o077)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    with Path("/run/lock/mnogobase-backup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        operations = Operations()
        repository = Repository(APP, owner=operations.owner, expected_origin=ORIGIN)
        perform(repository, operations, sync=args.sync)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Update interrupted; inspect service states before continuing.") from None
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        raise SystemExit(
            str(error)
            if isinstance(error, RuntimeError)
            else "Update failed; private output suppressed."
        ) from None
