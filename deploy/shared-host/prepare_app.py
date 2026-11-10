"""Create a CPU/Vision server config without changing the source config or secrets."""

from __future__ import annotations

import copy
import grp
import importlib.util
import os
import pwd
from pathlib import Path

import yaml

from mnogobase.config import Settings

ROOT = Path("/srv/mnogobase")


def server_config(base: dict, root: Path) -> dict:
    result = copy.deepcopy(base)
    result["device"] = "cpu"
    result["serving"] = {
        "compose_override": str(root / "app/deploy/shared-host/models.override.yml")
    }
    result["data_dir"] = str(root / "data/legacy")
    result["logs_dir"] = str(root / "data/logs")
    result["runs_dir"] = str(root / "data/runs")
    result.setdefault("wiki", {})["dir"] = str(root / "data/wiki")
    result.setdefault("service", {}).update(max_concurrent_asks=1)
    result.setdefault("llm", {})["concurrency"] = 2
    result.setdefault("embedder", {})["batch_size"] = 4
    result.setdefault("vision", {})["enabled"] = True
    for role in ("embedder", "rerank", "vision"):
        section = result.setdefault(role, {})
        if section.get("serve") != "llamacpp":
            raise RuntimeError(f"{role} must use llama.cpp in this CPU profile")
        # Retain the original picture options, model names, weights and templates.
        server = section.setdefault("server", {})
        server["ctx"] = 4096
        server["args"] = [
            *server.get("args", []),
            "--threads",
            "2",
            "--threads-batch",
            "2",
            "--parallel",
            "1",
            "--host",
            "0.0.0.0",
        ]
    if result["llm"].get("serve") != "api":
        raise RuntimeError("This profile requires the configured external LLM API")
    Settings(**result)
    return result


def prepare_ocr_cache(root: Path, uid: int, gid: int, *, origin: Path | None = None) -> None:
    if origin is None:
        spec = importlib.util.find_spec("rapidocr")
        if spec is None or spec.origin is None:
            raise RuntimeError("RapidOCR is not installed in the application environment")
        origin = Path(spec.origin)
    cache = origin.parent / "models"
    if cache.is_symlink() or not cache.parent.resolve().is_relative_to(
        (root / "app/.venv").resolve()
    ):
        raise RuntimeError("RapidOCR cache must not be a symlink or leave the application venv")
    cache.mkdir(mode=0o750, exist_ok=True)
    # This library manages its own downloads inside its model directory.
    # Only the checkpoint cache, not package code, is writable by the service.
    os.chown(cache, uid, gid)
    cache.chmod(0o750)


def prepare(root: Path, uid: int, gid: int) -> None:
    if root.is_symlink() or not os.path.ismount(root):
        raise RuntimeError("Storage must be mounted before preparation")
    paths = [
        root / p
        for p in (
            "config",
            "volumes",
            "volumes/models",
            "data",
            "cache",
            "cache/runtime",
            "cache/runtime/huggingface",
            "data/tmp",
            "data/legacy",
            "data/logs",
            "data/runs",
            "data/wiki",
            "data/service",
        )
    ]
    for path in paths:
        if path.is_symlink() or path.resolve() != path:
            raise RuntimeError("Server directory must not be a symlink")
        path.mkdir(parents=True, exist_ok=True)
        if (
            path == root / "data"
            or path.is_relative_to(root / "data")
            or path.is_relative_to(root / "cache/runtime")
        ):
            os.chown(path, uid, gid)
            path.chmod(0o750)
        elif path == root / "volumes/models":
            os.chown(path, -1, gid)
            path.chmod(0o750)

    prepare_ocr_cache(root, uid, gid)
    target = root / "config/config.yaml"
    if target.is_symlink():
        raise RuntimeError("Server config must not be a symlink")
    if target.exists():
        settings = Settings(**yaml.safe_load(target.read_text()))
        if settings.device != "cpu" or not settings.vision.enabled:
            raise RuntimeError("Existing config must enable CPU and Vision; no changes made")
        expected_override = root / "app/deploy/shared-host/models.override.yml"
        if settings.serving.compose_override != expected_override:
            raise RuntimeError(
                f"Existing config must set serving.compose_override to {expected_override}; no changes made"
            )
        print("Existing server config preserved.")
        return
    base = yaml.safe_load((root / "app/config.yaml").read_text())
    content = yaml.safe_dump(server_config(base, root), allow_unicode=True, sort_keys=False)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchown(stream.fileno(), -1, gid)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
        os.fchmod(stream.fileno(), 0o640)
    print("Server config created; Vision enabled; original config preserved.")


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit("Run preparation through sudo.")
    try:
        prepare(ROOT, pwd.getpwnam("mnogobase").pw_uid, grp.getgrnam("mnogobase").gr_gid)
    except (OSError, RuntimeError, KeyError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
