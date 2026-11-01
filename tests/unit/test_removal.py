from pathlib import Path

import pytest

from mnogobase.registry import Registry


@pytest.fixture
def registry(tmp_path):
    registry = Registry(tmp_path / "state.db")
    yield registry
    registry.close()


def register(registry, path, doc_id="d1"):
    registry.upsert_file(str(path.resolve()), doc_id, 1, 0, "done")


def test_select_removals_matches_registered_paths_under_missing_directory(registry, tmp_path):
    from mnogobase.removal import select_removals

    a, b = tmp_path / "gone" / "a.md", tmp_path / "gone" / "nested" / "b.md"
    outside = tmp_path / "gone-other" / "c.md"
    for path in (a, b, outside):
        register(registry, path)
    selected = select_removals(registry, [a, tmp_path / "gone", a])
    assert [r.path for r in selected] == [str(a), str(b)]
    assert len(registry.files()) == 3


def test_missing_selection_keeps_live_sources_and_honors_scope(registry, tmp_path):
    from mnogobase.removal import select_removals

    live, gone, outside = tmp_path / "a.md", tmp_path / "b.md", tmp_path / "other" / "c.md"
    live.write_text("live")
    for path in (live, gone, outside):
        register(registry, path)
    assert [r.path for r in select_removals(registry, [], missing=True)] == [
        str(gone),
        str(outside),
    ]
    assert select_removals(registry, [live], missing=True) == []
    assert [r.path for r in select_removals(registry, [gone], missing=True)] == [str(gone)]
    assert select_removals(registry, [tmp_path / "unknown"]) == []


def test_selection_requires_targets_unless_missing_is_explicit(registry):
    from mnogobase.removal import select_removals

    with pytest.raises(ValueError, match="paths|--missing"):
        select_removals(registry, [])


def test_missing_selection_handles_a_parent_directory_replaced_by_a_file(registry, tmp_path):
    from mnogobase.removal import select_removals

    folder = tmp_path / "docs"
    folder.mkdir()
    source = folder / "a.md"
    register(registry, source)
    folder.rmdir()
    folder.write_text("directory replaced by a regular file")
    assert [r.path for r in select_removals(registry, [], missing=True)] == [str(source)]


def test_missing_selection_does_not_treat_access_errors_as_deletions(
    registry, tmp_path, monkeypatch
):
    from mnogobase.removal import select_removals

    source = tmp_path / "restricted.md"
    register(registry, source)
    stat = Path.stat

    def denied(path, *args, **kwargs):
        if path == source:
            raise PermissionError("unreadable folder")
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)
    with pytest.raises(PermissionError):
        select_removals(registry, [], missing=True)
    assert len(registry.files()) == 1
