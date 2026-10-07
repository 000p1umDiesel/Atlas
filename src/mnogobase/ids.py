from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from pathlib import Path

NAMESPACE = uuid.UUID("5b0c9a8e-3f61-4d2a-9b7e-0c1d2e3f4a5b")
# keep + and # so that "C++" / "C#" do not collapse into "c"
_SEPARATORS = re.compile(r"[^\w+#]+|_+", re.UNICODE)
# keep "<slug>.md" well under the common 255-byte filename limit
_SLUG_MAX_CHARS = 80
_SLUG_MAX_BYTES = 200


def file_doc_id(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:16]


def chunk_id(doc_id: str, idx: int) -> str:
    return f"{doc_id}:{idx:05d}"


def normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKC", name).casefold()
    return " ".join(_SEPARATORS.sub(" ", text).split())


def entity_id(entity_type: str, name: str) -> str:
    key = f"{entity_type.casefold()}|{normalize_name(name)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def slugify(name: str) -> str:
    # "#" starts a URL fragment, so it must never reach a wiki filename or link
    slug = normalize_name(name).replace("#", "sharp").replace(" ", "-")
    if not slug:
        return "untitled"
    if len(slug) <= _SLUG_MAX_CHARS and len(slug.encode("utf-8")) <= _SLUG_MAX_BYTES:
        return slug
    # truncated: append a hash of the full slug so distinct long names stay distinct
    suffix = "-" + hashlib.sha1(slug.encode("utf-8")).hexdigest()[:8]
    head = slug[: _SLUG_MAX_CHARS - len(suffix)]
    while len(head.encode("utf-8")) > _SLUG_MAX_BYTES - len(suffix):
        head = head[:-1]
    return head.rstrip("-") + suffix


def point_id(key: str) -> str:
    return str(uuid.uuid5(NAMESPACE, key))
