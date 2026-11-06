import importlib.util
from pathlib import Path

import pytest
import yaml

from mnogobase.config import Settings

ROOT = Path(__file__).parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"deploy/shared-host/{name}.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_server_profile_preserves_model_identity_and_enables_vision(tmp_path):
    source = yaml.safe_load((ROOT / "config.yaml").read_text())
    before = Settings(**source)
    configured = module("prepare_app").server_config(source, tmp_path)
    settings = Settings(**configured)
    assert (
        settings.serving.compose_override == tmp_path / "app/deploy/shared-host/models.override.yml"
    )
    assert settings.device == "cpu" and settings.vision.enabled
    assert settings.embedder.model_id == before.embedder.model_id
    assert settings.embedder.doc_template == before.embedder.doc_template
    assert settings.embedder.query_template == before.embedder.query_template
    assert settings.rerank.model == before.rerank.model
    assert settings.vision.model == before.vision.model
    assert source["llm"]["concurrency"] == before.llm.concurrency
    assert settings.service.root.is_relative_to(tmp_path)
    assert settings.service.max_concurrent_operations == 1
    assert settings.embedder.batch_size == 4
    assert settings.embedder.server.ctx == 4096


def test_only_ocr_cache_becomes_writable_and_symlinks_are_rejected(tmp_path):
    import os

    setup = module("prepare_app")
    root = tmp_path / "root"
    package = root / "app/.venv/lib/python3.12/site-packages/rapidocr"
    package.mkdir(parents=True)
    origin = package / "__init__.py"
    origin.write_text("")
    before = package.stat().st_mode & 0o777
    setup.prepare_ocr_cache(root, os.getuid(), os.getgid(), origin=origin)
    assert (package / "models").stat().st_mode & 0o777 == 0o750
    assert package.stat().st_mode & 0o777 == before
    (package / "models").rmdir()
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (package / "models").symlink_to(outside)
    with pytest.raises(RuntimeError, match="symlink"):
        setup.prepare_ocr_cache(root, os.getuid(), os.getgid(), origin=origin)


def test_existing_config_without_server_limits_is_rejected_without_overwrite(tmp_path, monkeypatch):
    import os

    setup = module("prepare_app")
    root = tmp_path / "root"
    config = root / "config/config.yaml"
    config.parent.mkdir(parents=True)
    original = "device: cpu\nvision:\n  enabled: true\n"
    config.write_text(original)
    monkeypatch.setattr(setup.os.path, "ismount", lambda _: True)
    monkeypatch.setattr(setup, "prepare_ocr_cache", lambda *a, **kw: None)
    with pytest.raises(RuntimeError, match="compose_override"):
        setup.prepare(root, os.getuid(), os.getgid())
    assert config.read_text() == original
