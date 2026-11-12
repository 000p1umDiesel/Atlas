from mnogobase.files import rotate_if_large, write_atomic


def test_write_atomic_replaces_whole_file_and_leaves_no_temporary(tmp_path):
    path = tmp_path / "sub" / "cache.json"
    write_atomic(path, "first")
    write_atomic(path, "second")
    assert path.read_text(encoding="utf-8") == "second"
    assert [p.name for p in path.parent.iterdir()] == ["cache.json"]


def test_rotate_if_large_keeps_one_previous_copy(tmp_path):
    log = tmp_path / "log.md"
    rotate_if_large(log, 10)  # файла ещё нет
    log.write_text("short", encoding="utf-8")
    rotate_if_large(log, 10)
    assert log.read_text(encoding="utf-8") == "short"
    log.write_text("x" * 11, encoding="utf-8")
    rotate_if_large(log, 10)
    assert not log.exists() and (tmp_path / "log.1.md").read_text(encoding="utf-8") == "x" * 11
