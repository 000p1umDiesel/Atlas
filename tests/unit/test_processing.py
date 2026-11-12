import asyncio
from types import SimpleNamespace

import pytest

from mnogobase.config import Settings
from mnogobase.database import database_target
from mnogobase.files import read_jsonl
from mnogobase.llm.client import Usage
from mnogobase.registry import Registry

pytestmark = pytest.mark.integration


async def test_measured_ingest_records_stages_usage_and_wiki_failure(tmp_path):
    from mnogobase.processing import measured_ingest

    settings = Settings(data_dir=tmp_path / "state", wiki={"dir": tmp_path / "wiki"})
    usage = Usage()

    async def ingest(paths, progress, **kwargs):
        progress.files_found(1)
        progress.file_started("a.md")
        progress.step("extract", 2)
        usage.tokens_in += 10
        usage.tokens_out += 4
        progress.advance(2)
        progress.file_done("processed")
        progress.step("wiki draft", 1)
        usage.tokens_in += 5
        progress.advance(failed=True)
        return SimpleNamespace(
            processed=["a.md"], skipped=[], failed={}, wiki=SimpleNamespace(failed=["page"])
        )

    app = SimpleNamespace(
        settings=settings, llm=SimpleNamespace(usage=usage), pipeline=SimpleNamespace(ingest=ingest)
    )
    await measured_ingest(app, [], phase="build", output=tmp_path / "processing.jsonl")
    row = read_jsonl(tmp_path / "processing.jsonl")[0]
    assert row["status"] == "error"
    assert row["tokens_in"] == 15 and row["tokens_out"] == 4
    assert row["stages"][0]["tokens_in"] == 10
    assert row["stages"][1]["failed"] == 1
    assert row["wiki_failed"] == 1 and row["processed"] == 1
    assert "shared" in row["scope"]


async def test_interruption_is_persisted_without_hiding_exception(tmp_path):
    from mnogobase.processing import measured_ingest

    async def ingest(*args, progress, **kwargs):
        progress.step("parse")
        raise asyncio.CancelledError

    settings = Settings(data_dir=tmp_path / "state", wiki={"dir": tmp_path / "wiki"})
    app = SimpleNamespace(settings=settings, pipeline=SimpleNamespace(ingest=ingest))
    with pytest.raises(asyncio.CancelledError):
        await measured_ingest(app, [], output=tmp_path / "cost.jsonl")
    row = read_jsonl(tmp_path / "cost.jsonl")[0]
    assert row["status"] == "interrupted"
    assert row["tokens_in"] is None


def test_corpus_binding_detects_changed_wiki_and_indexed_sources(tmp_path):
    from mnogobase.processing import corpus_snapshot

    settings = Settings(data_dir=tmp_path / "state", wiki={"dir": tmp_path / "wiki"})
    before = corpus_snapshot(settings)
    assert not settings.data_dir.exists()
    registry = Registry(database_target(settings))
    registry.upsert_file("a.md", "id1", 1, 1, "done")
    indexed = corpus_snapshot(settings)
    assert before != indexed
    settings.wiki.dir.mkdir()
    (settings.wiki.dir / "a.md").write_text("wiki")
    wiki = corpus_snapshot(settings)
    assert indexed != wiki
    assert wiki == corpus_snapshot(settings)
    registry.close()


def test_changed_corpus_blocks_resume(tmp_path):
    from mnogobase.evaluation.core import prepare_run

    dataset = tmp_path / "questions.jsonl"
    dataset.write_text('{"id":"q","question":"Q?"}\n')
    prepare_run(dataset, tmp_path / "run", Settings(), corpus={"fingerprint": "a"})
    with pytest.raises(ValueError, match="configuration changed"):
        prepare_run(dataset, tmp_path / "run", Settings(), corpus={"fingerprint": "b"})


def test_partial_graph_retry_changes_corpus_even_if_file_still_failed(tmp_path):
    from mnogobase.processing import corpus_snapshot

    settings = Settings(data_dir=tmp_path / "state", wiki={"dir": tmp_path / "wiki"})
    registry = Registry(database_target(settings))
    registry.upsert_file("a.md", "id", 1, 1, "failed")
    registry.set_stage("id", "graph", "running")
    registry.set_stage("id", "graph", "failed")
    before = corpus_snapshot(settings)
    registry.set_stage("id", "graph", "running")
    registry.set_stage("id", "graph", "failed")
    assert before != corpus_snapshot(settings)
    registry.close()
