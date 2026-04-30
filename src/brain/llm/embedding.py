from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any, Protocol

import tiktoken

from brain.config import EmbeddingConfig
from brain.exceptions import EmbeddingError


class EmbeddingClient(Protocol):
    """Protocol for text embedding clients."""

    @property
    def dimension(self) -> int:
        """Expected embedding vector dimension."""

    @property
    def last_call_tokens(self) -> int:
        """Estimated tokens used by the most recent embed call."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed texts in input order."""


class OpenAICompatibleEmbeddingClient:
    """Embedding client backed by OpenAI-compatible embeddings APIs."""

    def __init__(
        self,
        config: EmbeddingConfig,
        *,
        client: Any | None = None,
        api_key: str | None = None,
    ) -> None:
        self._config = config
        self._last_call_tokens = 0
        self._client = client if client is not None else self._build_client(api_key)

    @property
    def dimension(self) -> int:
        """Expected embedding vector dimension."""
        return self._config.dimension

    @property
    def last_call_tokens(self) -> int:
        """Estimated tokens used by the most recent embed call."""
        return self._last_call_tokens

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed texts in batches and return vectors in input order."""
        text_list = list(texts)
        self._last_call_tokens = self._count_tokens(text_list)
        if not text_list:
            return []

        vectors: list[list[float]] = []
        for start in range(0, len(text_list), self._config.batch_size):
            batch = text_list[start : start + self._config.batch_size]
            vectors.extend(self._embed_batch(batch))
        return vectors

    def _build_client(self, api_key: str | None) -> Any:
        from openai import OpenAI

        client_kwargs: dict[str, str] = {}
        resolved_api_key = api_key if api_key is not None else os.environ.get(self._config.api_key_env)
        if resolved_api_key:
            client_kwargs["api_key"] = resolved_api_key
        if self._config.base_url:
            client_kwargs["base_url"] = self._config.base_url
        return OpenAI(**client_kwargs)

    def _embed_batch(self, batch: Sequence[str]) -> list[list[float]]:
        response = self._create_with_retry(batch)
        data = getattr(response, "data", None)
        if data is None and isinstance(response, dict):
            data = response.get("data")
        if not isinstance(data, list):
            raise EmbeddingError("Embedding response did not contain data")
        if len(data) != len(batch):
            raise EmbeddingError("Embedding response count did not match request count")

        vectors = [self._extract_vector(item) for item in data]
        for vector in vectors:
            if len(vector) != self._config.dimension:
                raise EmbeddingError(
                    f"Embedding dimension mismatch: expected {self._config.dimension}, "
                    f"got {len(vector)}"
                )
        return vectors

    def _create_with_retry(self, batch: Sequence[str]) -> Any:
        last_exc: Exception | None = None
        for _ in range(2):
            try:
                return self._client.embeddings.create(model=self._config.model, input=list(batch))
            except Exception as exc:
                last_exc = exc

        raise EmbeddingError("Embedding API call failed") from last_exc

    def _count_tokens(self, texts: Sequence[str]) -> int:
        try:
            encoding = tiktoken.encoding_for_model(self._config.model)
        except Exception:
            encoding = tiktoken.get_encoding("cl100k_base")
        return sum(len(encoding.encode(text)) for text in texts)

    @staticmethod
    def _extract_vector(item: Any) -> list[float]:
        embedding = getattr(item, "embedding", None)
        if embedding is None and isinstance(item, dict):
            embedding = item.get("embedding")
        if not isinstance(embedding, list):
            raise EmbeddingError("Embedding response item did not contain a vector")
        return [float(value) for value in embedding]
