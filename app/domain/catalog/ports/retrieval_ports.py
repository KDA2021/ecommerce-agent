# -*- coding: utf-8 -*-
"""检索基础设施端口：EmbeddingClient / ProductVectorIndex / Reranker

Domain 不关心实现：Infrastructure 当前提供 Chroma 本地 embedding、Chroma 向量索引与本地 BGE reranker。
UseCase 通过这三个端口完成"embed → 向量召回 → rerank"二阶段召回，
任一环节不可用时由 UseCase 负责降级（关键词召回 / 跳过精排）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.domain.catalog.product import Product


class EmbeddingClient(ABC):
    """文本向量化端口。"""

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """把单条文本向量化。

        Args:
            text: 待向量化文本。

        Returns:
            向量。
        """
        ...

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量向量化。

        Args:
            texts: 文本列表。

        Returns:
            与输入等长的向量列表。
        """
        ...


@dataclass(frozen=True)
class VectorHit:
    """向量召回命中：product_id + 相似度分。"""

    product_id: str
    score: float


class ProductVectorIndex(ABC):
    """商品向量索引端口。"""

    @abstractmethod
    async def ensure_ready(self, vector_dim: int) -> None:
        """确保 collection 存在（幂等）。

        Args:
            vector_dim: 向量维度。
        """

    @abstractmethod
    async def upsert_products(self, products: list[Product], embeddings: list[list[float]]) -> None:
        """把商品向量写入索引。

        Args:
            products: 商品列表。
            embeddings: 与商品一一对应的向量。
        """
        ...

    @abstractmethod
    async def search(self, embedding: list[float], top_n: int) -> list[VectorHit]:
        """向量近邻检索。

        Args:
            embedding: 查询向量。
            top_n: 候选条数。

        Returns:
            VectorHit 列表。
        """
        ...


class Reranker(ABC):
    """精排端口。"""

    @abstractmethod
    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        """返回与 documents 等长的精排分数；失败抛异常，由调用方降级。

        Args:
            query: 查询词。
            documents: 候选文档列表。
        """
