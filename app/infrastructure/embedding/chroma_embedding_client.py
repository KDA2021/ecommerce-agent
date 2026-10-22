# -*- coding: utf-8 -*-
"""Chroma 默认本地 embedding（all-MiniLM-L6-v2，384 维）。"""
from __future__ import annotations

from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from app.domain.catalog.ports.retrieval_ports import EmbeddingClient


class ChromaLocalEmbeddingClient(EmbeddingClient):
    """Chroma 默认本地 embedding（all-MiniLM-L6-v2）。"""

    def __init__(self) -> None:
        """初始化默认 embedding function。"""
        self._ef = DefaultEmbeddingFunction()

    async def embed(self, text: str) -> list[float]:
        """向量化单条文本。

        Args:
            text: 待向量化文本。

        Returns:
            向量。
        """
        return [float(x) for x in self._ef([text])[0]]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量向量化。

        Args:
            texts: 文本列表。

        Returns:
            向量列表。
        """
        if not texts:
            return []
        return [[float(x) for x in vec] for vec in self._ef(texts)]
