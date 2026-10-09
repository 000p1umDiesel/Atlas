import pytest

from mnogobase.embedding.sparse import BM25Encoder

pytestmark = pytest.mark.integration  # скачивает модель BM25 с Hugging Face


def test_bm25_query_overlaps_matching_document():
    enc = BM25Encoder()
    docs = enc.encode_documents(["the transformer uses attention", "cats are cute"])
    query = enc.encode_query("attention")
    assert docs[0].indices and docs[1].indices
    assert set(query.indices) & set(docs[0].indices)
    assert not set(query.indices) & set(docs[1].indices)
