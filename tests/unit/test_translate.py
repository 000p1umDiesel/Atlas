import pytest

from mnogobase.retrieval.translate import QueryTranslator, needs_translation
from tests.fakes import FakeLLM


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What is ReAct?", False),
        ("Как в ReAct комбинируют ReAct и CoT-SC?", True),
        ("Что такое LLaMA?", True),
        ("LLaMA-65B 1.4T?", False),
        ("123 ?", False),
    ],
)
def test_needs_translation(question, expected):
    assert needs_translation(question) is expected


async def test_english_question_skips_the_llm():
    llm = FakeLLM(lambda task, prompt: "unused")
    assert await QueryTranslator(llm).alt_queries("What is attention?") == []
    assert llm.calls == []


async def test_translation_is_cleaned_and_memoized():
    llm = FakeLLM(lambda task, prompt: '"What is attention?"\n')
    translator = QueryTranslator(llm)
    assert await translator.alt_queries("Что такое внимание?") == ["What is attention?"]
    assert await translator.alt_queries("Что такое внимание?") == ["What is attention?"]
    assert len(llm.calls_for("query")) == 1
    assert "Что такое внимание?" in llm.calls_for("query")[0]


async def test_failed_or_unchanged_translation_gives_no_alternative():
    def boom(task, prompt):
        raise RuntimeError("down")

    assert await QueryTranslator(FakeLLM(boom)).alt_queries("Что такое внимание?") == []
    same = FakeLLM(lambda task, prompt: "что такое внимание?")
    assert await QueryTranslator(same).alt_queries("Что такое внимание?") == []
