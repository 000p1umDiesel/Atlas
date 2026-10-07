You extract a knowledge graph from one fragment of a document.

Rules:
- Extract meaningful, reusable entities: people, organizations, methods, technologies, datasets, works, events, locations and concepts. Skip generic words such as "system", "approach", "paper", "model" (unless it is a named model).
- `name`: the canonical English name with its standard spelling (e.g. "Transformer", "Andrej Karpathy", "Retrieval-Augmented Generation"). If the fragment is not in English, translate the name into English and put the original spelling into `aliases`.
- `type`: exactly one of: $entity_types.
- `description`: one or two English sentences about the entity, based only on this fragment.
- `aliases`: other names, abbreviations or original-language spellings that appear in the fragment (may be empty).
- `relations` connect two entities from your `entities` list; use exactly the same `name` values. `predicate` is a short snake_case English verb phrase ("uses", "part_of", "proposed_by", "improves_on"). `description` is one English sentence. `strength` is an integer from 1 to 10: how explicit and important the relation is in the fragment.
- Use only information present in the fragment. If nothing is worth extracting, return empty lists.

Document: $title
Section: $headings

Fragment:
"""
$text
"""
