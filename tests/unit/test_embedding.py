from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from brain.config import EmbeddingConfig
from brain.exceptions import EmbeddingError
from brain.llm.embedding import OpenAICompatibleEmbeddingClient


@dataclass
class FakeEmbedding:
    embedding: list[float]


@dataclass
class FakeEmbeddingResponse:
    data: list[FakeEmbedding]


class FakeEmbeddingsAPI:
    def __init__(self, vectors: list[list[float]] | None = None, fail_first: bool = False) -> None:
        self.vectors = vectors or [[0.1, 0.2, 0.3]]
        self.fail_first = fail_first
        self.calls: list[dict[str, Any]] = []

    def create(self, *, model: str, input: list[str]) -> FakeEmbeddingResponse:
        self.calls.append({"model": model, "input": input})
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("transient failure")
        vectors = [self.vectors[index % len(self.vectors)] for index, _ in enumerate(input)]
        return FakeEmbeddingResponse([FakeEmbedding(vector) for vector in vectors])


class FakeOpenAIClient:
    def __init__(self, vectors: list[list[float]] | None = None, fail_first: bool = False) -> None:
        self.embeddings = FakeEmbeddingsAPI(vectors=vectors, fail_first=fail_first)


def test_embed_returns_vectors_from_sdk_response() -> None:
    config = EmbeddingConfig(model="text-embedding-3-small", dimension=3)
    client = FakeOpenAIClient(vectors=[[0.1, 0.2, 0.3]])
    embedding = OpenAICompatibleEmbeddingClient(config, client=client)

    vectors = embedding.embed(["alpha", "beta"])

    assert vectors == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]
    assert embedding.dimension == 3
    assert embedding.last_call_tokens > 0
    assert client.embeddings.calls == [
        {"model": "text-embedding-3-small", "input": ["alpha", "beta"]}
    ]


def test_base_url_passed_to_openai_client(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_kwargs: dict[str, str] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs: str) -> None:
            seen_kwargs.update(kwargs)
            self.embeddings = FakeEmbeddingsAPI(vectors=[[1.0, 2.0]])

    import brain.llm.embedding as embedding_module

    monkeypatch.setattr(embedding_module, "OpenAI", FakeOpenAI, raising=False)
    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    config = EmbeddingConfig(
        api_key_env="EMBEDDING_API_KEY",
        base_url="https://example.test/v1",
        dimension=2,
    )
    monkeypatch.setenv("EMBEDDING_API_KEY", "test-key")

    client = OpenAICompatibleEmbeddingClient(config)
    assert client.embed(["hello"]) == [[1.0, 2.0]]

    assert seen_kwargs == {"api_key": "test-key", "base_url": "https://example.test/v1"}


def test_failed_call_retries_once_and_succeeds() -> None:
    config = EmbeddingConfig(dimension=3)
    client = FakeOpenAIClient(vectors=[[0.1, 0.2, 0.3]], fail_first=True)
    embedding = OpenAICompatibleEmbeddingClient(config, client=client)

    assert embedding.embed(["alpha"]) == [[0.1, 0.2, 0.3]]
    assert len(client.embeddings.calls) == 2


def test_batch_size_splits_250_texts_into_three_api_calls() -> None:
    config = EmbeddingConfig(dimension=3, batch_size=100)
    client = FakeOpenAIClient(vectors=[[0.1, 0.2, 0.3]])
    embedding = OpenAICompatibleEmbeddingClient(config, client=client)

    vectors = embedding.embed([f"text {index}" for index in range(250)])

    assert len(vectors) == 250
    assert [len(call["input"]) for call in client.embeddings.calls] == [100, 100, 50]


def test_dimension_mismatch_raises_embedding_error() -> None:
    config = EmbeddingConfig(dimension=3)
    client = FakeOpenAIClient(vectors=[[0.1, 0.2]])
    embedding = OpenAICompatibleEmbeddingClient(config, client=client)

    with pytest.raises(EmbeddingError, match="dimension mismatch"):
        embedding.embed(["alpha"])
