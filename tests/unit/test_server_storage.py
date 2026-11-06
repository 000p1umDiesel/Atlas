import importlib.util
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "deploy/shared-host/prepare-storage.py"


def module():
    spec = importlib.util.spec_from_file_location("server_storage", SCRIPT)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_prepare_requires_mounted_storage(tmp_path, monkeypatch):
    setup = module()
    root = tmp_path / "storage"
    root.mkdir()
    monkeypatch.setattr(setup.os.path, "ismount", lambda _: False)
    with pytest.raises(RuntimeError, match="mounted"):
        setup.prepare(root, os.getgid())
    assert list(root.iterdir()) == []


def test_prepare_preserves_credentials_and_rejects_symlinks(tmp_path, monkeypatch):
    setup = module()
    root = tmp_path / "storage"
    root.mkdir()
    monkeypatch.setattr(setup.os.path, "ismount", lambda _: True)
    setup.prepare(root, os.getgid())
    env = root / "config/storage.env"
    original = env.read_bytes()
    assert env.stat().st_mode & 0o777 == 0o640
    assert b"DATABASE_URL=postgresql://" in original
    setup.prepare(root, os.getgid())
    assert env.read_bytes() == original
    for name in ("postgres", "qdrant", "neo4j"):
        assert (root / "volumes" / name).is_dir()
    env.unlink()
    outside = tmp_path / "unrelated"
    outside.write_text("unchanged")
    env.symlink_to(outside)
    with pytest.raises(RuntimeError, match="symlink"):
        setup.prepare(root, os.getgid())
    assert outside.read_text() == "unchanged"
