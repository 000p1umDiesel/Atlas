import pytest

from mnogobase.config import Settings
from mnogobase.database import database_target
from mnogobase.registry import ingest_lock
from mnogobase.service.context import team_settings
from mnogobase.service.errors import ServiceError
from mnogobase.service.runtime import TeamService

pytestmark = pytest.mark.integration


@pytest.fixture
def service(tmp_path):
    settings = Settings(device="cpu", service={"root": tmp_path / "service"})
    service = TeamService(settings)
    user = service.store.create_user("alice", "correct horse battery staple")
    team = service.store.create_team("A")
    service.store.add_member(team["id"], user["id"], "admin")
    ctx = service.store.context(user["id"], team["id"])
    return service, ctx


def test_upload_is_durable_and_deduplicates_same_content(service):
    svc, ctx = service
    first = svc.upload(ctx, "notes.md", b"Transformer uses attention.")
    again = svc.upload(ctx, "notes.md", b"Transformer uses attention.")
    assert first["id"] == again["id"]
    assert len(svc.jobs(ctx)) == 1
    documents = TeamService(svc.settings).documents(ctx)
    assert len(documents) == 1
    assert documents[0]["name"] == "notes.md"
    assert documents[0]["status"] == "pending"
    assert svc.source(ctx, documents[0]["id"]).read_bytes() == b"Transformer uses attention."


@pytest.mark.parametrize("filename", ["../outside.md", "sub/file.md", "sub\\file.md", "x.exe", ""])
def test_upload_rejects_paths_and_unsupported_extensions(service, filename):
    svc, ctx = service
    with pytest.raises(ServiceError) as error:
        svc.upload(ctx, filename, b"private")
    assert error.value.status == 422
    assert svc.documents(ctx) == []


def test_upload_checks_actual_bytes_and_pending_limit(service):
    svc, ctx = service
    svc.settings.service.max_upload_mb = 1
    with pytest.raises(ServiceError) as error:
        svc.upload(ctx, "big.md", b"x" * (1024 * 1024 + 1))
    assert error.value.status == 413
    svc.settings.service.max_pending_jobs = 1
    svc.upload(ctx, "one.md", b"one")
    with pytest.raises(ServiceError) as error:
        svc.upload(ctx, "two.md", b"two")
    assert error.value.status == 429
    assert len(svc.documents(ctx)) == 1


def test_context_is_rechecked_after_membership_revocation(service):
    svc, ctx = service
    svc.store.remove_member(ctx.team_id, ctx.user_id)
    with pytest.raises(ServiceError) as error:
        svc.documents(ctx)
    assert error.value.status in (403, 404)


def test_busy_corpus_returns_conflict(service):
    svc, ctx = service
    settings = team_settings(svc.settings, ctx.team_id)
    with (
        ingest_lock(settings.data_dir, target=database_target(settings)),
        pytest.raises(ServiceError) as error,
    ):
        svc.upload(ctx, "one.md", b"one")
    assert error.value.status == 409


def test_source_cannot_follow_symlink_outside_team(service, tmp_path):
    svc, ctx = service
    svc.upload(ctx, "one.md", b"one")
    doc = svc.documents(ctx)[0]
    source = svc.source(ctx, doc["id"])
    outside = tmp_path / "private.md"
    outside.write_text("other team secret")
    source.unlink()
    source.symlink_to(outside)
    with pytest.raises(ServiceError) as error:
        svc.source(ctx, doc["id"])
    assert error.value.status == 404


def test_sources_and_jobs_are_scoped_to_team(service):
    svc, ctx = service
    svc.upload(ctx, "one.md", b"one")
    doc = svc.documents(ctx)[0]
    team = svc.store.create_team("B")
    svc.store.add_member(team["id"], ctx.user_id)
    other = svc.store.context(ctx.user_id, team["id"])
    assert svc.jobs(other) == []
    assert svc.documents(other) == []
    with pytest.raises(ServiceError) as error:
        svc.source(other, doc["id"])
    assert error.value.status == 404


def test_team_directory_symlink_cannot_point_to_sibling_team(service):
    import shutil

    svc, ctx = service
    svc.upload(ctx, "one.md", b"one")
    doc = svc.documents(ctx)[0]
    own = svc.source(ctx, doc["id"]).parent
    other_team = svc.store.create_team("B")
    svc.store.add_member(other_team["id"], ctx.user_id)
    other = svc.store.context(ctx.user_id, other_team["id"])
    svc.upload(other, "one.md", b"one")
    sibling = svc.source(other, doc["id"]).parent
    shutil.rmtree(own)
    own.symlink_to(sibling, target_is_directory=True)
    with pytest.raises(ServiceError) as error:
        svc.source(ctx, doc["id"])
    assert error.value.status in (404, 409)


def test_source_read_is_a_snapshot_and_uses_original_filename(service):
    svc, ctx = service
    svc.upload(ctx, "заметки.md", b"one")
    doc = svc.documents(ctx)[0]
    data, filename = svc.read_source(ctx, doc["id"])
    svc.source(ctx, doc["id"]).unlink()
    assert data == b"one" and filename == "заметки.md"


def test_history_marks_removed_wiki_citation_unavailable(service, monkeypatch):
    from types import SimpleNamespace

    svc, ctx = service
    chat = svc.store.create_conversation(ctx)
    svc.store.append_message(
        ctx,
        chat["id"],
        "assistant",
        "Summary [1]",
        sources=[{"kind": "wiki", "ref": "a" * 16 + ":0", "url": "/wiki/a"}],
    )
    monkeypatch.setattr(
        svc,
        "_graph_factory",
        lambda _: SimpleNamespace(wiki_page=lambda _: None, close=lambda: None),
        raising=False,
    )
    assert svc.history(ctx, chat["id"])[0]["sources"][0]["available"] is False


def test_wiki_section_citation_has_working_page_url(service, monkeypatch):
    from types import SimpleNamespace

    svc, ctx = service
    page_id = "a" * 16
    source = svc._item(ctx, {"kind": "wiki", "ref": page_id + "#2"})
    assert source["url"] == f"/api/teams/{ctx.team_id}/wiki/{page_id}"
    settings = team_settings(svc.settings, ctx.team_id)
    settings.wiki.dir.mkdir(parents=True)
    (settings.wiki.dir / "page.md").write_text("Wiki page")
    monkeypatch.setattr(
        svc,
        "_graph_factory",
        lambda _: SimpleNamespace(
            wiki_page=lambda value: SimpleNamespace(path="page.md") if value == page_id else None,
            close=lambda: None,
        ),
    )
    chat = svc.store.create_conversation(ctx)
    svc.store.append_message(ctx, chat["id"], "assistant", "Summary", sources=[source])
    assert svc.history(ctx, chat["id"])[0]["sources"][0]["available"] is True


@pytest.mark.parametrize("fail", [False, True])
def test_upload_connections_close_on_success_and_failure(service, monkeypatch, fail):
    from sqlalchemy.exc import ResourceClosedError

    from mnogobase.service import runtime

    svc, ctx = service
    connections = []
    original_connect = runtime.connect

    def connect(*args, **kwargs):
        db = original_connect(*args, **kwargs)
        connections.append(db)
        return db

    monkeypatch.setattr(runtime, "connect", connect)
    settings = team_settings(svc.settings, ctx.team_id)
    try:
        with svc._uploads(settings) as db:
            db.execute("SELECT 1")
            if fail:
                raise ValueError("interrupted")
    except ValueError:
        assert fail
    assert connections
    for connection in connections:
        with pytest.raises(ResourceClosedError):
            connection.execute("SELECT 1")


@pytest.mark.parametrize("metadata_removed", [False, True])
async def test_removal_recovers_after_source_was_unlinked(service, monkeypatch, metadata_removed):
    from types import SimpleNamespace

    from mnogobase.background.queue import JobQueue

    svc, ctx = service
    upload = svc.upload(ctx, "one.md", b"one")
    doc = svc.documents(ctx)[0]
    path = svc.source(ctx, doc["id"])
    settings = team_settings(svc.settings, ctx.team_id)
    queue = JobQueue(database_target(settings, "jobs"))
    queue.claim()
    queue.finish(upload["id"])
    queue.close()
    await svc.remove_document(ctx, doc["id"])
    # Прерывание после удаления файла, но до завершения durable задания.
    path.unlink()
    if metadata_removed:
        with svc._uploads(settings) as db:
            db.execute("DELETE FROM uploads WHERE doc_id=?", (doc["id"],))
    removed = []

    async def remove(paths, **kwargs):
        removed.extend(paths)
        return SimpleNamespace(wiki=None)

    monkeypatch.setattr(
        svc,
        "_factory",
        lambda _: SimpleNamespace(pipeline=SimpleNamespace(remove=remove), close=lambda: None),
    )
    assert await svc.work_once() == 1
    assert removed == [path]
    assert svc.documents(ctx) == []
    assert svc.jobs(ctx)[0]["status"] == "done"


async def test_model_operation_limit_is_shared_and_released_after_error(service, monkeypatch):
    from types import SimpleNamespace

    svc, ctx = service
    svc.settings.service.max_concurrent_operations = 1
    closed = []
    monkeypatch.setattr(
        svc, "_factory", lambda _: SimpleNamespace(close=lambda: closed.append(True))
    )
    other = TeamService(svc.settings, app_factory=svc._factory)
    settings = team_settings(svc.settings, ctx.team_id)
    with pytest.raises(ValueError, match="operation failed"):
        async with svc._app(settings):
            with pytest.raises(ServiceError) as error:
                async with other._app(settings):
                    pytest.fail("A second core operation must not exceed capacity")
            assert error.value.status == 429
            raise ValueError("operation failed")
    async with other._app(settings):
        pass
    assert closed == [True, True]


async def test_failed_processing_blocks_knowledge_reads_until_successful_retry(service):
    from mnogobase.background.queue import JobQueue

    svc, ctx = service
    queued = svc.upload(ctx, "one.md", b"one")
    settings = team_settings(svc.settings, ctx.team_id)
    queue = JobQueue(database_target(settings, "jobs"))
    try:
        queue.claim()
        queue.fail(queued["id"], "InterruptedWrite", max_attempts=1, delay=0)
        with pytest.raises(ServiceError) as error:
            await svc.search(ctx, "one")
        assert error.value.status == 409
        assert svc.documents(ctx) and svc.jobs(ctx)[0]["status"] == "failed"
        queue.retry(queued["id"])
        with pytest.raises(ServiceError) as error:
            await svc.search(ctx, "one")
        assert error.value.status == 409
        queue.claim()
        queue.finish(queued["id"])
    finally:
        queue.close()
    assert await svc.search(ctx, "one") == []


async def test_removing_failed_ingest_reconciles_corpus_and_old_job(service, monkeypatch):
    from types import SimpleNamespace

    from mnogobase.background.queue import JobQueue

    svc, ctx = service
    queued = svc.upload(ctx, "one.md", b"one")
    doc = svc.documents(ctx)[0]
    settings = team_settings(svc.settings, ctx.team_id)
    queue = JobQueue(database_target(settings, "jobs"))
    try:
        queue.claim()
        queue.fail(queued["id"], "PartialWrite", max_attempts=1, delay=0)
    finally:
        queue.close()

    async def remove(paths, **kwargs):
        return SimpleNamespace(wiki=None)

    monkeypatch.setattr(
        svc,
        "_factory",
        lambda _: SimpleNamespace(pipeline=SimpleNamespace(remove=remove), close=lambda: None),
    )
    await svc.remove_document(ctx, doc["id"])
    assert await svc.work_once() == 1
    assert svc.documents(ctx) == []
    assert await svc.search(ctx, "one") == []
    assert [job["status"] for job in svc.jobs(ctx)] == ["done", "superseded"]
