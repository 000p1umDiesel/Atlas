import pytest

from mnogobase.llm.templates import render

CASES = {
    "extract": dict(
        entity_types="VAL_TYPES", title="VAL_TITLE", headings="VAL_HEADINGS", text="VAL_TEXT"
    ),
    "resolve_same": dict(
        a_name="VAL_A_NAME",
        a_type="VAL_A_TYPE",
        a_description="VAL_A_DESCRIPTION",
        b_name="VAL_B_NAME",
        b_type="VAL_B_TYPE",
        b_description="VAL_B_DESCRIPTION",
    ),
    "resolve_summarize": dict(name="VAL_NAME", type="VAL_TYPE", descriptions="VAL_DESCRIPTIONS"),
    "wiki_page": dict(
        language="VAL_LANGUAGE",
        name="VAL_NAME",
        type="VAL_TYPE",
        description="VAL_DESCRIPTION",
        aliases="VAL_ALIASES",
        relations="VAL_RELATIONS",
        evidence="VAL_EVIDENCE",
        example_id="VAL_EXAMPLE_ID",
        existing="VAL_EXISTING",
    ),
    "answer": dict(question="VAL_QUESTION", context="VAL_CONTEXT", language="VAL_LANGUAGE"),
    "translate_query": dict(question="VAL_QUESTION"),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_placeholder_is_filled(name):
    text = render(name, **CASES[name])
    assert "$" not in text
    for value in CASES[name].values():
        assert value in text
