import filelock
import pytest

from mnogobase.registry import STAGES, Registry, ingest_lock


@pytest.fixture
def reg(tmp_path):
    r = Registry(tmp_path / "state.db")
    yield r
    r.close()


def test_stage_lifecycle_and_attempts(reg):
    assert reg.pending_stages("d1") == list(STAGES)
    reg.set_stage("d1", "parse", "running")
    reg.set_stage("d1", "parse", "failed", error="boom")
    reg.set_stage("d1", "parse", "running")
    reg.set_stage("d1", "parse", "done")
    assert reg.stage_status("d1", "parse") == "done"
    assert reg.pending_stages("d1") == list(STAGES[1:])
    row = reg._db.execute("SELECT attempts FROM stages WHERE doc_id='d1'").fetchone()
    assert row[0] == 2


def test_reset_running_and_errors(reg):
    reg.set_stage("d1", "chunk", "running")
    reg.set_stage("d2", "embed", "failed", error="ConnectError: down")
    assert reg.reset_running() == 1
    assert reg.stage_status("d1", "chunk") == "pending"
    assert reg.stage_errors() == [("d2", "embed", "ConnectError: down")]
    assert reg.stage_summary()["embed"] == {"failed": 1}


def test_files_and_paths(reg):
    reg.upsert_file("/a.md", "d1", 10, 1.0, "done")
    reg.upsert_file("/b.md", "d1", 10, 1.0, "done")
    reg.upsert_file("/c.md", "d2", 5, 2.0, "failed")
    assert reg.get_file("/a.md").doc_id == "d1"
    assert sorted(reg.paths_for_doc("d1")) == ["/a.md", "/b.md"]
    assert reg.doc_ids() == ["d1"]
    assert reg.failed_paths() == ["/c.md"]
    reg.set_file_status("/c.md", "done")
    assert reg.failed_paths() == []


def test_clear_doc_removes_stage_and_chunk_rows(reg):
    reg.set_stage("d1", "parse", "done")
    reg.set_chunk_extract("d1:00000", "failed", "x")
    reg.set_chunk_extract("d2:00000", "done")
    reg.put_extraction("d1:00000", "v1", "m", "{}")
    reg.clear_doc("d1")
    assert reg.pending_stages("d1") == list(STAGES)
    assert reg.chunk_extract_counts("d1") == {}
    assert reg.chunk_extract_counts("d2") == {"done": 1}
    assert reg.get_extraction("d1:00000", "v1", "m") is None


def test_extraction_cache_dirty_meta(reg):
    reg.put_extraction("c1", "v1", "m", '{"a": 1}')
    reg.put_extraction("c1", "v1", "m", '{"a": 2}')
    assert reg.get_extraction("c1", "v1", "m") == '{"a": 2}'
    assert reg.get_extraction("c1", "v2", "m") is None
    reg.mark_dirty(["e1", "e2", "e1"])
    assert sorted(reg.dirty()) == ["e1", "e2"]
    reg.clear_dirty(["e1"])
    assert reg.dirty() == ["e2"]
    reg.set_meta("embedder", "ollama:x:768")
    assert reg.get_meta("embedder") == "ollama:x:768"
    reg.wipe()
    assert reg.dirty() == [] and reg.get_meta("embedder") is None


def test_ingest_lock_is_exclusive(tmp_path):
    with ingest_lock(tmp_path), pytest.raises(filelock.Timeout):
        ingest_lock(tmp_path).acquire()


def test_latest_extraction_ignores_model_and_prompt_version(reg):
    assert reg.get_latest_extraction("c1") is None
    reg.put_extraction("c1", "v1", "model-a", '{"a": 1}')
    reg.put_extraction("c1", "v2", "model-b", '{"b": 2}')
    reg.put_extraction("c2", "v1", "model-a", '{"c": 3}')
    assert reg.get_latest_extraction("c1") == '{"b": 2}'
    reg.put_extraction("c1", "v1", "model-a", '{"a": 4}')  # rewritten: now the latest
    assert reg.get_latest_extraction("c1") == '{"a": 4}'


def test_pending_removal_journal(reg):
    assert reg.pending_removals() == [] and reg.get_removal("d1") is None
    reg.put_removal("d2", '{"x": 2}')
    reg.put_removal("d1", '{"x": 1}')
    assert reg.get_removal("d1") == '{"x": 1}'
    assert reg.pending_removals() == [("d1", '{"x": 1}'), ("d2", '{"x": 2}')]
    reg.drop_removal("d1")
    assert reg.pending_removals() == [("d2", '{"x": 2}')]
    reg.wipe()
    assert reg.pending_removals() == []
