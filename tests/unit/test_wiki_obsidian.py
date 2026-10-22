import json

from mnogobase.wiki.obsidian import GRAPH_FILTER, OTHER_COLOR, write_graph_settings


def read_graph(wiki_dir):
    return json.loads((wiki_dir / ".obsidian" / "graph.json").read_text(encoding="utf-8"))


def test_fresh_vault_gets_filter_and_one_color_per_type(tmp_path):
    assert write_graph_settings(tmp_path, ["Person", "Concept", "Other"])
    graph = read_graph(tmp_path)
    assert graph["search"] == GRAPH_FILTER
    queries = [g["query"] for g in graph["colorGroups"]]
    assert queries == ["[type:Person]", "[type:Concept]", "[type:Other]"]
    colors = [g["color"]["rgb"] for g in graph["colorGroups"]]
    assert colors[2] == OTHER_COLOR
    assert len(set(colors)) == 3
    assert all(g["color"]["a"] == 1 for g in graph["colorGroups"])


def test_same_types_get_same_colors_in_another_vault(tmp_path):
    write_graph_settings(tmp_path / "a", ["Person", "Concept"])
    write_graph_settings(tmp_path / "b", ["Person", "Concept"])
    assert read_graph(tmp_path / "a") == read_graph(tmp_path / "b")


def test_user_settings_are_kept(tmp_path):
    path = tmp_path / ".obsidian" / "graph.json"
    path.parent.mkdir()
    mine = {"query": "path:notes", "color": {"a": 1, "rgb": 1}}
    recolored = {"query": "[type:Person]", "color": {"a": 1, "rgb": 2}}
    path.write_text(
        json.dumps({"search": "tag:#draft", "showTags": True, "colorGroups": [mine, recolored]}),
        encoding="utf-8",
    )
    write_graph_settings(tmp_path, ["Person", "Concept"])
    graph = read_graph(tmp_path)
    assert graph["search"] == f"{GRAPH_FILTER} tag:#draft"
    assert graph["showTags"] is True
    assert graph["colorGroups"][:2] == [mine, recolored]
    assert [g["query"] for g in graph["colorGroups"][2:]] == ["[type:Concept]"]


def test_second_run_changes_nothing(tmp_path):
    write_graph_settings(tmp_path, ["Person", "Concept"])
    before = read_graph(tmp_path)
    assert not write_graph_settings(tmp_path, ["Person", "Concept"])
    assert read_graph(tmp_path) == before


def test_new_type_gets_a_color_not_used_yet(tmp_path):
    write_graph_settings(tmp_path, ["Person", "Concept"])
    write_graph_settings(tmp_path, ["Person", "Concept", "Dataset"])
    colors = [g["color"]["rgb"] for g in read_graph(tmp_path)["colorGroups"]]
    assert len(colors) == 3 and len(set(colors)) == 3


def test_many_types_still_get_colors(tmp_path):
    types = [f"Type{i}" for i in range(40)]
    write_graph_settings(tmp_path, types)
    assert len(read_graph(tmp_path)["colorGroups"]) == 40


def test_type_with_space_is_quoted(tmp_path):
    write_graph_settings(tmp_path, ["Research Area"])
    assert read_graph(tmp_path)["colorGroups"][0]["query"] == '[type:"Research Area"]'


def test_broken_graph_json_is_left_alone(tmp_path):
    path = tmp_path / ".obsidian" / "graph.json"
    path.parent.mkdir()
    path.write_text("{not json", encoding="utf-8")
    assert not write_graph_settings(tmp_path, ["Person"])
    assert path.read_text(encoding="utf-8") == "{not json"
