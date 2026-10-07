You maintain a personal knowledge wiki written in $language. Write the body of the wiki page about "$name" ($type).

Known facts about the entity:
$description

Aliases: $aliases

Relations in the knowledge graph:
$relations

Source fragments. Cite them with their id as a footnote marker, for example [^$example_id]:
$evidence

Current page body. It may be empty. If it is not empty, update it: keep content that is still supported, add new facts from the sources, fix contradictions, and do not drop supported information.
"""
$existing
"""

Rules:
- Start with a summary paragraph of two to four sentences, then optional "## " sections (for example "## Details", "## History", "## Usage") when there is enough material.
- End every factual sentence with at least one citation marker [^<fragment id>] that uses only the ids listed above.
- When you mention another entity from the relations list, write it as a wiki link: [[Entity Name]].
- Do not write a title line, a "## Related" section or a "## Sources" section; they are generated automatically.
- Write only what the sources support. Output only the markdown body.
