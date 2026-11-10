"""Токенизатор для HybridChunker и описания рисунков через VLM."""

from __future__ import annotations

import base64
import io
from pathlib import Path

from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer
from pydantic import ConfigDict, PrivateAttr

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tiff"}

# Не пишите здесь, зачем нужно описание («for retrieval» и т. п.): модель переносит
# эти слова в описание. Так Transformer у нас однажды стал «model for document retrieval».
DESCRIBE_PROMPT = (
    "Describe this figure from a scientific paper. List the components, labels, numbers "
    "and arrows you can see and how they are connected. Do not guess anything that is not visible."
)
DESCRIPTION_PREFIX = "Image description: "


class ServerTokenizer(BaseTokenizer):
    """Считает токены через /tokenize у llama-server с моделью embeddings.

    Так лимит чанка измеряется тем же токенизатором, что и у модели, которая
    потом будет эти чанки кодировать. Отдельный HF-токенизатор для GGUF не нужен.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)
    api: object
    max_tokens: int = 512
    _cache: dict = PrivateAttr(default_factory=dict)

    def count_tokens(self, text: str) -> int:
        if text not in self._cache:
            root = str(self.api.http.base_url).rstrip("/").removesuffix("/v1")
            body = {"content": text, "add_special": False}
            self._cache[text] = len(
                self.api.request("POST", root + "/tokenize", json=body)["tokens"]
            )
        return self._cache[text]

    def get_max_tokens(self) -> int:
        return self.max_tokens

    def get_tokenizer(self):
        return self.count_tokens


def make_tokenizer(embedder, max_tokens: int = 512) -> ServerTokenizer:
    tokenizer = ServerTokenizer(api=embedder, max_tokens=max_tokens)
    tokenizer.count_tokens("probe")  # сразу проверяем, что /tokenize отвечает
    return tokenizer


def describe_image(image, vision) -> str:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    content = [
        {"type": "image_url", "image_url": {"url": url}},
        {"type": "text", "text": DESCRIBE_PROMPT},
    ]
    response = vision.chat([{"role": "user", "content": content}], max_tokens=400)
    text = (response["choices"][0]["message"]["content"] or "").strip()
    if not text:
        raise ValueError("VLM вернула пустое описание")
    return text


def add_image_descriptions(doc, path: Path, vision, *, min_area: float = 0.05):
    """Вставляет текстовое описание сразу после каждого рисунка в DoclingDocument.

    Описание получает ту же страницу и тот же раздел, что и рисунок, поэтому
    HybridChunker положит его в чанк рядом с подписью к рисунку.
    """
    from docling_core.types.doc import DocItemLabel

    if vision is None:
        return doc
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        from PIL import Image

        with Image.open(path) as image:
            doc.add_text(
                label=DocItemLabel.TEXT, text=DESCRIPTION_PREFIX + describe_image(image, vision)
            )
        return doc

    for picture in list(doc.pictures):
        if picture.prov:
            prov = picture.prov[0]
            page = doc.pages.get(prov.page_no)
            if page and prov.bbox.area() / (page.size.width * page.size.height) < min_area:
                continue
        image = picture.get_image(doc)
        if image is None:
            raise ValueError(f"{path.name}: у рисунка {picture.self_ref} нет пикселей")
        parent = picture.parent.resolve(doc) if picture.parent else doc.body
        doc.add_text(
            label=DocItemLabel.TEXT,
            text=DESCRIPTION_PREFIX + describe_image(image, vision),
            prov=picture.prov[0] if picture.prov else None,
            parent=parent,
        )
        # add_text добавляет элемент в конец раздела; переносим его сразу за рисунок.
        description = parent.children.pop()
        position = next(i for i, ref in enumerate(parent.children) if ref.cref == picture.self_ref)
        parent.children.insert(position + 1, description)
    return doc
