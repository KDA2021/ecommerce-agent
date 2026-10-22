# -*- coding: utf-8 -*-
"""本地 BGE Reranker：CrossEncoder 精排，CPU 推理，失败由调用方降级。"""
from __future__ import annotations

import asyncio
from typing import Optional

from sentence_transformers import CrossEncoder

from app.domain.catalog.ports.retrieval_ports import Reranker


class BgeReranker(Reranker):
    """本地 BGE CrossEncoder 精排器（启动预热，CPU 推理）。"""

    def __init__(self, model_name: str = "BAAI/bge-reranker-base") -> None:
        """记录模型名，不在此加载权重。

        Args:
            model_name: CrossEncoder 模型名或本地路径。
        """
        self._model_name = model_name
        self._model: Optional[CrossEncoder] = None

    def _ensure_model(self) -> CrossEncoder:
        """首次调用时加载 CrossEncoder。

        Returns:
            CrossEncoder 实例。
        """
        if self._model is None:
            self._model = CrossEncoder(self._model_name, max_length=512)
        return self._model

    async def warmup(self) -> CrossEncoder:
        """预热模型：在工作线程中加载 CrossEncoder 权重。

        1.1GB 权重的首次加载是同步阻塞 I/O；若直接在事件循环里执行，
        会卡死整个进程（并行检索子任务因此集体超时）。放到
        ``asyncio.to_thread`` 后事件循环始终可调度。

        Returns:
            已完成加载的 CrossEncoder 实例。
        """
        return await asyncio.to_thread(self._ensure_model)

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        """在线程池里做 (query, document) 打分，避免阻塞事件循环。

        Args:
            query: 查询词。
            documents: 候选文档列表。

        Returns:
            与 documents 等长的分数列表。
        """
        if not documents:
            return []
        # 先经 warmup 确保权重已加载；它内部走线程池，不会阻塞事件循环。
        model = await self.warmup()
        pairs = [[query, document] for document in documents]
        scores = await asyncio.to_thread(model.predict, pairs)
        return [float(score) for score in scores]
