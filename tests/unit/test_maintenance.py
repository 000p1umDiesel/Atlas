from types import SimpleNamespace

import pytest

from mnogobase.config import Settings, WikiSettings
from mnogobase.maintenance import ReindexError, reindex, reset


def make_app(tmp_path, calls: list[str]):
    settings = Settings().model_copy(
        update={"data_dir": tmp_path / ".mb", "wiki": WikiSettings(dir=tmp_path / "wiki")}
    )
    return SimpleNamespace(
        settings=settings,
        vectors=SimpleNamespace(drop_collections=lambda: calls.append("vectors")),
        graph=SimpleNamespace(wipe=lambda: calls.append("graph")),
        registry=SimpleNamespace(wipe=lambda: calls.append("registry")),
    )


def write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_reset_deletes_only_the_wiki_files_mnogobase_writes(tmp_path):
    wiki = tmp_path / "wiki"
    for rel in ("entities/attention.md", "index.md", "log.md", "notes.md", "drafts/idea.md"):
        write(wiki / rel)
    write(tmp_path / ".mb" / "cache" / "abc.chunks.json")
    calls: list[str] = []

    reset(make_app(tmp_path, calls))

    assert calls == ["vectors", "graph", "registry"]
    assert not (tmp_path / ".mb" / "cache").exists()
    assert sorted(p.relative_to(wiki).as_posix() for p in wiki.rglob("*")) == [
        "drafts",
        "drafts/idea.md",
        "notes.md",
    ]


def test_reset_removes_the_wiki_dir_once_it_is_empty(tmp_path):
    for rel in ("entities/attention.md", "index.md", "log.md"):
        write(tmp_path / "wiki" / rel)
    reset(make_app(tmp_path, []))
    assert not (tmp_path / "wiki").exists()
    reset(make_app(tmp_path, []))  # удалять уже нечего: всё равно без ошибок


class _Embedder:
    model_id = "fake"
    dim = 4

    def __init__(self, reply):
        self.reply = reply

    def embed_query(self, query):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        ([1.0, 0.0], "returns 2 dims, config expects 4"),  # провайдер, который не обрезает
        (ValueError("model returned 2 dims, config expects 4"), "model returned 2 dims"),
    ],
)
def test_reindex_checks_the_embedder_dimension_before_touching_the_index(tmp_path, reply, message):
    calls: list[str] = []
    app = make_app(tmp_path, calls)
    app.embedder = _Embedder(reply)
    app.vectors.drop_collections = lambda: calls.append("drop")
    app.registry.set_meta = lambda key, value: calls.append(f"meta {value}")

    with pytest.raises(ReindexError, match=message) as info:
        reindex(app)
    assert "nothing was changed" in str(info.value)
    assert calls == []  # старым индексом ещё можно пользоваться
