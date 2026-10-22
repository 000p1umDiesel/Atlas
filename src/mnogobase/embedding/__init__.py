from __future__ import annotations

from mnogobase.config import EmbedderSettings
from mnogobase.embedding.base import Embedder


def build_embedder(settings: EmbedderSettings) -> Embedder:
    """Эмбеддер провайдера `embedder.provider`."""
    if settings.provider == "openai":
        from mnogobase.embedding.openai_compat import OpenAICompatEmbedder

        return OpenAICompatEmbedder(settings)
    from mnogobase.embedding.ollama import OllamaEmbedder

    return OllamaEmbedder(settings)
