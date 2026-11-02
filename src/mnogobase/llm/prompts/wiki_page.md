You maintain a personal knowledge wiki written in $language. Write the body of the wiki page about the entity supplied in the data.

Rules:
- Start with a summary paragraph of two to four sentences, then optional "## " sections (for example "## Details", "## History", "## Usage") when there is enough material.
- End every factual sentence with at least one citation marker [^<fragment id>] that uses only the ids supplied with the source fragments in the data.
- When you mention another entity from the relations list, write it as a wiki link: [[Entity Name]].
- Do not write a title line, a "## Related" section or a "## Sources" section; they are generated automatically.
- Write only what the sources support. Output only the markdown body.
- If the current page body is not empty, update it: keep supported content, add facts from the sources, fix contradictions, and do not drop supported information.

<!-- untrusted-data -->
Entity: $name ($type)

Known facts about the entity:
$description

Aliases: $aliases

Relations in the knowledge graph:
$relations

Source fragments (example citation marker: [^$example_id]):
$evidence

Current page body (may be empty):
"""
$existing
"""
