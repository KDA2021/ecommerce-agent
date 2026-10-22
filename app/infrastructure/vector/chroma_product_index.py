# -*- coding: utf-8 -*-
"""Chroma 实现 ProductVectorIndex（cosine）。"""
from __future__ import annotations

import chromadb

from app.domain.catalog.ports.retrieval_ports import ProductVectorIndex, VectorHit
from app.domain.catalog.product import Product
from app.infrastructure.persistence.mysql_repositories import product_chroma_id
from app.infrastructure.settings import Settings


class ChromaProductIndex(ProductVectorIndex):
    """Chroma 商品向量索引（collection: products，cosine）。"""

    def __init__(self, settings: Settings) -> None:
        """记录 settings，collection 延迟到首次使用时创建。

        Args:
            settings: 运行时配置。
        """
        self._settings = settings
        self._collection = None

    def _ensure(self) -> None:
        """懒初始化 PersistentClient 与 collection。"""
        if self._collection is None:
            self._client = chromadb.PersistentClient(path=str(self._settings.chroma_dir))
            self._collection = self._client.get_or_create_collection(
                name="products", metadata={"hnsw:space": "cosine"},
            )

    async def ensure_ready(self, vector_dim: int) -> None:
        """确保 collection 存在（幂等）。

        Args:
            vector_dim: 向量维度（占位，Chroma 自动推断）。
        """
        self._ensure()

    async def upsert_products(self, products: list[Product], embeddings: list[list[float]]) -> None:
        """按 product_chroma_id 幂等写入商品向量。

        Args:
            products: 商品列表。
            embeddings: 与商品一一对应的向量。
        """
        if len(products) != len(embeddings):
            raise ValueError("products 与 embeddings 数量不一致")
        if not products:
            return
        self._ensure()
        self._collection.upsert(
            ids=[product_chroma_id(p.product_id) for p in products],
            embeddings=embeddings,
            metadatas=[{"product_id": p.product_id} for p in products],
        )

    async def search(self, embedding: list[float], top_n: int) -> list[VectorHit]:
        """向量近邻检索。

        Args:
            embedding: 查询向量。
            top_n: 候选条数。

        Returns:
            VectorHit 列表（score = 1 - 距离）。
        """
        self._ensure()
        result = self._collection.query(query_embeddings=[embedding], n_results=top_n)
        # Chroma 返回结果按 query 维度嵌套；这里只有一条查询，取 [0]。
        ids = result.get("ids", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[1.0]])[0]
        hits = []
        for idx, chroma_id in enumerate(ids):
            meta = metadatas[idx] or {}
            hits.append(
                VectorHit(
                    product_id=meta.get("product_id", chroma_id),
                    score=1.0 - float(distances[idx]),
                )
            )
        return hits
