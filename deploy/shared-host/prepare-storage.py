"""Prepare mounted server storage and credentials; never print or rotate secrets."""

from __future__ import annotations

import grp
import os
import secrets
from pathlib import Path

ROOT = Path("/srv/mnogobase")


def prepare(root: Path, gid: int) -> None:
    if root.is_symlink() or not os.path.ismount(root):
        raise RuntimeError("Storage must be mounted before preparation")
    directories = {
        root / "config": 0o750,
        root / "volumes": 0o711,
        **{root / "volumes" / name: 0o700 for name in ("postgres", "qdrant", "neo4j")},
    }
    for directory, mode in directories.items():
        if directory.is_symlink() or directory.resolve() != directory:
            raise RuntimeError("Storage directory must not be a symlink")
        existed = directory.exists()
        directory.mkdir(mode=mode, parents=True, exist_ok=True)
        # Existing database directories belong to the container's own UID.
        if not existed or directory == root / "config":
            os.chown(directory, -1, gid)
            directory.chmod(mode)

    path = root / "config/storage.env"
    if path.is_symlink():
        raise RuntimeError("Credential file must not be a symlink")
    if path.exists():
        keys = {line.partition("=")[0] for line in path.read_text().splitlines()}
        if not {"POSTGRES_PASSWORD", "NEO4J_PASSWORD", "DATABASE_URL"} <= keys:
            raise RuntimeError("Existing credentials are incomplete; no changes made")
        if path.stat().st_mode & 0o007:
            raise RuntimeError("Existing credentials must not be accessible to other users")
        print("Existing storage credentials preserved.")
        return

    postgres, neo4j = secrets.token_hex(24), secrets.token_hex(24)
    content = (
        f"POSTGRES_PASSWORD={postgres}\nNEO4J_PASSWORD={neo4j}\n"
        f"DATABASE_URL=postgresql://mnogobase:{postgres}@127.0.0.1:5432/mnogobase\n"
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchown(stream.fileno(), -1, gid)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
        os.fchmod(stream.fileno(), 0o640)
    print("Storage directories and credentials created. Secret values are not displayed.")


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit("Run this preparation command through sudo.")
    try:
        prepare(ROOT, grp.getgrnam("mnogobase").gr_gid)
    except (OSError, RuntimeError, KeyError) as exc:
        raise SystemExit(str(exc)) from None
