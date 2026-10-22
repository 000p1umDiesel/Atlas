import io

from rich.console import Console

from mnogobase.config import EmbedderSettings, Settings, VisionSettings
from mnogobase.model_view import UpReporter, up_table
from mnogobase.model_watch import ModelWatch, RoleStatus, WeightFile, downloaded, weight_files
from mnogobase.serving import Served, _parse_listing

GB = 1_000_000_000
VL_TREE = [
    WeightFile("Qwen3-VL-2B-Instruct-Q4_K_M.gguf", "aaa", 1 * GB),
    WeightFile("Qwen3-VL-2B-Instruct-UD-Q4_K_M.gguf", "bbb", 1 * GB),  # другой квант
    WeightFile("Qwen3-VL-2B-Instruct-Q8_0.gguf", "ccc", 2 * GB),
    WeightFile("mmproj-F32.gguf", "ddd", 2 * GB),
    WeightFile("mmproj-BF16.gguf", "eee", GB // 2),
]
VISION = Served("vision", VisionSettings(enabled=True))


def test_weight_files_pick_the_quant_and_one_projector():
    files = weight_files(VISION, lambda repo: VL_TREE)
    assert [f.path for f in files] == ["Qwen3-VL-2B-Instruct-Q4_K_M.gguf", "mmproj-BF16.gguf"]
    split = [
        WeightFile("Q4_K_M/model-Q4_K_M-00001-of-00002.gguf", "a", 1),
        WeightFile("Q4_K_M/model-Q4_K_M-00002-of-00002.gguf", "b", 1),
        WeightFile("model-Q4_K_S.gguf", "c", 1),
    ]
    embed = Served("embedder", EmbedderSettings())
    assert [f.sha256 for f in weight_files(embed, lambda repo: split)] == ["a", "b"]
    vllm = Served("embedder", EmbedderSettings(serve="vllm"))
    tree = [WeightFile("model-00001.safetensors", "s", 5), WeightFile("pytorch_model.bin", "p", 9)]
    assert [f.sha256 for f in weight_files(vllm, lambda repo: tree)] == ["s"]


def test_downloaded_counts_hub_blobs_partials_and_flat_files():
    files = [WeightFile("model-Q4_K_M.gguf", "aaa", 100), WeightFile("mmproj-F16.gguf", "bbb", 50)]
    listing = [
        (100, "/hub/models--org--repo/blobs/aaa"),  # готовый блоб
        (20, "/hub/models--org--repo/blobs/bbb.incomplete"),  # недокачанный
        (999, "/hub/models--org--repo/blobs/zzz"),  # чужой файл
    ]
    assert downloaded(files, "org/repo", listing) == 120
    flat = [(30, "/models/org_repo_mmproj-F16.gguf.downloadInProgress")]
    assert downloaded(files, "org/repo", flat) == 30
    assert downloaded(files, "org/repo", [(30, "/models/other_mmproj-F16.gguf")]) == 0


def test_find_output_is_parsed():
    assert _parse_listing("12 /models/a b.gguf\nfind: '/root/x': No such file\n") == [
        (12, "/models/a b.gguf")
    ]


class FakeRunner:
    def __init__(self):
        self.running: set[str] = set()
        self.files: list[tuple[int, str]] = []

    def alive(self, served):
        return served.role in self.running

    def cache_listing(self, served):
        return list(self.files)


def test_watch_follows_a_role_from_queue_to_ready():
    runner, ready = FakeRunner(), set()
    clock = iter(range(0, 100, 10))
    watch = ModelWatch(
        [VISION],
        runner,
        tree=lambda repo: VL_TREE,
        ready=lambda s: s.role in ready,
        clock=lambda: next(clock),
    )
    [status] = watch.poll()
    assert status.phase == "queued"
    runner.running.add("vision")
    runner.files = [(GB // 2, "/hub/blobs/eee")]
    [status] = watch.poll()
    assert (status.phase, status.done, status.total) == ("downloading", GB // 2, GB + GB // 2)
    runner.files.append((GB // 2, "/hub/blobs/aaa.incomplete"))
    [status] = watch.poll()
    assert status.rate == GB // 2 / 10 and status.eta == 10
    runner.files[1] = (GB, "/hub/blobs/aaa")
    [status] = watch.poll()
    assert status.phase == "loading" and not status.cached
    ready.add("vision")
    assert watch.poll()[0].phase == "ready"


def test_watch_marks_cached_weights_and_survives_no_network():
    runner = FakeRunner()
    runner.running.add("vision")
    runner.files = [(GB, "/hub/blobs/aaa"), (GB // 2, "/hub/blobs/eee")]
    watch = ModelWatch([VISION], runner, tree=lambda repo: VL_TREE, ready=lambda s: False)
    [status] = watch.poll()
    assert status.phase == "loading" and status.cached

    def offline(repo):
        raise OSError("no network")

    watch = ModelWatch([VISION], runner, tree=offline, ready=lambda s: False)
    assert watch.poll()[0].phase == "starting"


def _render(statuses, width):
    console = Console(width=width, file=io.StringIO(), color_system=None)
    console.print(up_table(statuses, 83, width))
    return console.file.getvalue()


def test_table_keeps_progress_details_on_a_narrow_terminal():
    embed = Served("embedder", Settings().embedder)
    statuses = [RoleStatus(embed, "downloading", 1_300_000_000, 2_500_000_000, 38e6)]
    narrow = _render(statuses, 80)
    assert "1.3/2.5 GB · 38 MB/s · 0:31 left" in narrow and "llamacpp" not in narrow
    assert "Qwen3-Embedding-4B:Q4_K_M" in _render(statuses, 120)


def test_without_a_terminal_progress_is_printed_as_occasional_lines():
    out = io.StringIO()
    clock = iter([0, 1, 1, 5, 5, 40, 40, 41, 41])  # старт, затем время и момент печати
    reporter = UpReporter(Console(file=out, width=200), clock=lambda: next(clock))
    status = RoleStatus(VISION, "downloading", GB // 2, GB, 10e6)
    with reporter.live():
        reporter.update([status])  # первая строка
        reporter.update([status])  # через 4 с: молчит
        reporter.update([status])  # через 39 с: снова строка
        status.phase = "ready"
        reporter.update([status])  # смена фазы печатается сразу
    lines = out.getvalue().splitlines()
    assert lines == [
        "[0:01] vision: downloading — 0.5/1.0 GB · 10 MB/s · 0:50 left",
        "[0:40] vision: downloading — 0.5/1.0 GB · 10 MB/s · 0:50 left",
        "[0:41] vision: ✓ ready — 1.0 GB downloaded",
    ]
