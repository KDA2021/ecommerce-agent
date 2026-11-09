# -*- coding: utf-8 -*-
"""Chroma 实现 ProductVectorIndex（cosine）。"""
from __future__ import annotations

import asyncio
import math
import threading
from collections import deque

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
        self._timeout = float(getattr(settings, "chroma_timeout_seconds", 5.0))
        if not math.isfinite(self._timeout) or self._timeout <= 0:
            raise ValueError("chroma_timeout_seconds must be finite and positive")
        self._max_concurrency = getattr(settings, "chroma_max_concurrency", 2)
        self._max_queue = getattr(settings, "chroma_max_queue", 8)
        self._queue_timeout = float(getattr(settings, "chroma_queue_timeout_seconds", 0.2))
        if type(self._max_concurrency) is not int or self._max_concurrency < 1:
            raise ValueError("chroma_max_concurrency must be a positive integer")
        if type(self._max_queue) is not int or self._max_queue < 0:
            raise ValueError("chroma_max_queue must be a nonnegative integer")
        if not math.isfinite(self._queue_timeout) or self._queue_timeout <= 0:
            raise ValueError("chroma_queue_timeout_seconds must be finite and positive")
        self._pending: set[asyncio.Task] = set()
        self._waiters: deque[tuple[asyncio.Future, bool]] = deque()
        self._running = 0
        self._exclusive = False
        self._ensure_lock = threading.Lock()

    def _can_start(self, exclusive: bool) -> bool:
        return not self._exclusive and (
            self._running == 0 if exclusive else self._running < self._max_concurrency
        )

    def _reserve(self, exclusive: bool) -> None:
        self._running += 1
        self._exclusive = exclusive

    def _grant_waiters(self) -> None:
        # FIFO also prevents a waiting writer being starved by fresh queries.
        while self._waiters:
            future, exclusive = self._waiters[0]
            if future.cancelled():
                self._waiters.popleft()
                continue
            if not self._can_start(exclusive):
                break
            self._waiters.popleft()
            self._reserve(exclusive)
            future.set_result(None)

    def _release(self, exclusive: bool) -> None:
        self._running -= 1
        if exclusive:
            self._exclusive = False
        self._grant_waiters()

    async def _acquire(self, exclusive: bool) -> None:
        # All admission bookkeeping runs on the owning event loop, without an
        # await between the capacity check and reservation.
        if not self._waiters and self._can_start(exclusive):
            self._reserve(exclusive)
            return
        if len(self._waiters) >= self._max_queue:
            raise RuntimeError("Chroma operations in progress; queue is full")
        future = asyncio.get_running_loop().create_future()
        waiter = (future, exclusive)
        self._waiters.append(waiter)
        try:
            await asyncio.wait_for(asyncio.shield(future), self._queue_timeout)
        except BaseException:
            if future.done() and not future.cancelled():
                # Capacity was granted just as the caller timed out/cancelled.
                # No worker exists yet, so return this reservation immediately.
                self._release(exclusive)
            else:
                self._waiters.remove(waiter)
                future.cancel()
                self._grant_waiters()
            raise

    def _finished(self, task: asyncio.Task, exclusive: bool) -> None:
        self._pending.discard(task)
        self._release(exclusive)
        # Consume late exceptions after the original request stopped waiting.
        if not task.cancelled():
            task.exception()

    async def _run(self, operation, *, exclusive: bool = True):
        """Bound queued requests separately from native operations in flight.

        A queued timeout/cancellation removes the request without starting work.
        Once started, shield keeps the task tracked until the thread finishes.
        """
        await self._acquire(exclusive)
        task = asyncio.create_task(asyncio.to_thread(operation))
        self._pending.add(task)
        task.add_done_callback(lambda done: self._finished(done, exclusive))
        return await asyncio.wait_for(asyncio.shield(task), timeout=self._timeout)

    def _ensure(self) -> None:
        """懒初始化 PersistentClient 与 collection。"""
        with self._ensure_lock:
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
        await self._run(self._ensure)

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
        def upsert():
            self._ensure()
            self._collection.upsert(
                ids=[product_chroma_id(p.product_id) for p in products],
                embeddings=embeddings,
                metadatas=[{"product_id": p.product_id} for p in products],
            )

        await self._run(upsert)

    async def search(self, embedding: list[float], top_n: int) -> list[VectorHit]:
        """向量近邻检索。

        Args:
            embedding: 查询向量。
            top_n: 候选条数。

        Returns:
            VectorHit 列表（score = 1 - 距离）。
        """
        def query():
            self._ensure()
            return self._collection.query(query_embeddings=[embedding], n_results=top_n)

        result = await self._run(query, exclusive=False)
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
