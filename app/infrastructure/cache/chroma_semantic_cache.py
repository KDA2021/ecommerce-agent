# -*- coding: utf-8 -*-
"""Chroma 语义缓存：问句向量相似命中直接复用回复。

P9 在 Chroma 之前加了一层**可选 Redis 精确 key 热路径**：

- Redis 命中归一化问句的精确 key 时直接返回，不调用 embedder、不查 Chroma；
- Chroma 语义命中后回填 Redis，下次同样问句走精确命中；
- ``remember`` 在写 Chroma 的同时写 Redis（TTL 24h）；
- Redis 未注入 / disabled / 任何异常 → 完全退回原 Chroma 路径（P8 行为不变）。
"""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import chromadb

from app.domain.catalog.ports.retrieval_ports import EmbeddingClient
from app.infrastructure.settings import Settings

# Redis 精确缓存 TTL：24 小时；只做热路径加速，过期后回落到 Chroma 语义命中。
_REDIS_TTL_SECONDS = 24 * 60 * 60

# 命中这些模式的问句不缓存：写操作、指代词、订单号都可能随上下文变化。
_UNSAFE_RE = re.compile(
    "|".join(
        [
            r"下单", r"买了", r"购买", r"付款", r"支付", r"取消", r"退单", r"退款", r"改地址",
            r"刚才", r"刚刚", r"上面", r"前面", r"那个", r"这个", r"它",
            r"我的订单", r"订单号", r"GBX-",
        ]
    )
)


def is_cacheable_query(query: str) -> bool:
    """判断 query 是否可缓存；含下单动作、指代词或订单号的问句不入缓存。

    Args:
        query: 用户问句。

    Returns:
        是否可缓存。
    """
    return not _UNSAFE_RE.search(query)


@dataclass(frozen=True)
class SemanticHit:
    """语义缓存命中：回复 + 相似度 + 命中的历史问句。"""

    reply: str           # 命中的缓存回复
    similarity: float    # 相似度（1 - cosine 距离）
    matched_query: str   # 命中的历史问句


class DisabledSemanticCache:
    """``SEMANTIC_CACHE_ENABLED=0`` 时的空实现：lookup 永远 miss，remember no-op。

    与 ``SemanticCache`` 保持同样接口，server / 工具无需分支判断；同时避免初始化
    Chroma 客户端和本地 embedding 模型，让对照组的成本统计更干净。
    """

    async def lookup(self, buyer_id, query, has_history, scope=""):
        """永远未命中。

        Args:
            buyer_id: 买家 id（忽略）。
            query: 用户问句（忽略）。
            has_history: 是否有历史（忽略）。
            scope: 作用域指纹（忽略）。

        Returns:
            None。
        """
        return None

    async def remember(self, buyer_id, query, reply, has_history, scope=""):
        """空操作：不写缓存。

        Args:
            buyer_id: 买家 id（忽略）。
            query: 用户问句（忽略）。
            reply: 回复（忽略）。
            has_history: 是否有历史（忽略）。
            scope: 作用域指纹（忽略）。
        """
        return None


class SemanticCache:
    """Chroma 语义缓存 + 可选 Redis 精确 key 热路径，按 buyer + 偏好指纹分桶。"""

    def __init__(
        self,
        settings: Settings,
        embedder: EmbeddingClient,
        redis_cache: Any | None = None,
    ) -> None:
        """初始化 Chroma collection（cosine）与可选 Redis 热路径。

        Args:
            settings: 运行时配置。
            embedder: embedding 客户端。
            redis_cache: 可选 ``RedisCache``（或任何提供
                ``get_json`` / ``set_json`` 的对象）；默认 ``None`` = 只用 Chroma。
        """
        self._settings = settings
        self._embedder = embedder
        self._redis = redis_cache
        self._client = chromadb.PersistentClient(path=str(settings.chroma_dir))
        self._collection = self._client.get_or_create_collection(
            name="semantic_cache", metadata={"hnsw:space": "cosine"},
        )

    async def lookup(self, buyer_id, query, has_history, scope=""):
        """按 Redis 精确 key → Chroma 相似度顺序查找缓存回复。

        Args:
            buyer_id: 买家 id。
            query: 本轮问句。
            has_history: 是否已有会话历史。
            scope: 偏好作用域指纹。

        Returns:
            SemanticHit；有历史 / 不可缓存 / embedding 失败时返回 None。
        """
        # 写操作、指代词等上下文相关问句直接不查缓存：Redis 与 Chroma 一视同仁。
        if has_history or not is_cacheable_query(query):
            return None
        normalized = _normalize(query)

        # P9 热路径：精确 key 命中就返回，完全不碰 embedder / Chroma。
        if self._redis is not None:
            cached = await _redis_safe_get(self._redis, _redis_key(buyer_id, scope, normalized))
            cached_hit = _hit_from_redis_payload(cached, normalized)
            if cached_hit is not None:
                return cached_hit

        try:
            vector = await self._embedder.embed(normalized)
        except Exception:
            return None
        try:
            result = self._collection.query(
                query_embeddings=[vector],
                n_results=self._settings.semantic_cache_bucket_limit,
                # Chroma 多字段过滤必须用 $and 包裹，否则条件不会同时生效。
                where={"$and": [{"buyer_id": buyer_id}, {"scope": scope}]},
                include=["metadatas", "distances"],
            )
        except Exception:
            return None
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]
        best = None
        # 距离越小越相似；阈值内的候选里取相似度最高的一条。
        for meta, distance in zip(metadatas, distances):
            if distance <= self._settings.semantic_cache_threshold:
                hit = SemanticHit(
                    reply=meta.get("reply", ""),
                    similarity=1.0 - float(distance),
                    matched_query=meta.get("query", ""),
                )
                if hit.reply and (best is None or hit.similarity > best.similarity):
                    best = hit
        # P9：Chroma 语义命中后回填 Redis 精确 key，下次同样问句零 embedding 命中。
        if best is not None and self._redis is not None:
            await _redis_safe_set(
                self._redis,
                _redis_key(buyer_id, scope, normalized),
                _redis_payload(normalized, best.reply),
            )
        return best

    async def remember(self, buyer_id, query, reply, has_history, scope=""):
        """同时写入 Chroma 与 Redis 并裁剪同桶旧条目。

        Args:
            buyer_id: 买家 id。
            query: 本轮问句。
            reply: 待缓存回复。
            has_history: 是否已有会话历史。
            scope: 偏好作用域指纹。
        """
        # 不缓存错误回复，避免把失败答案固化后反复命中。
        if has_history or not is_cacheable_query(query) or not reply or reply.startswith("[error]"):
            return
        normalized = _normalize(query)
        try:
            vector = await self._embedder.embed(normalized)
        except Exception:
            return
        doc_id = str(uuid.uuid4())
        self._collection.upsert(
            ids=[doc_id],
            embeddings=[vector],
            metadatas=[{
                "buyer_id": buyer_id, "scope": scope, "query": normalized,
                "reply": reply, "created_at": time.time(),
            }],
        )
        self._prune(buyer_id, scope)
        # P9：Chroma 成功后同步写 Redis 精确 key（TTL 24h）；失败静默跳过，不影响主链路。
        if self._redis is not None:
            await _redis_safe_set(
                self._redis,
                _redis_key(buyer_id, scope, normalized),
                _redis_payload(normalized, reply),
            )

    def _prune(self, buyer_id, scope):
        """同桶条目超过上限时按创建时间淘汰最旧的。

        Args:
            buyer_id: 买家 id。
            scope: 偏好作用域指纹。
        """
        result = self._collection.get(where={"$and": [{"buyer_id": buyer_id}, {"scope": scope}]})
        metas = result.get("metadatas", [])
        if len(metas) <= self._settings.semantic_cache_bucket_limit:
            return
        # 按创建时间升序排列，淘汰最旧的若干条。
        ordered = sorted(
            zip(result.get("ids", []), metas),
            key=lambda pair: pair[1].get("created_at", 0.0),
        )
        remove_ids = [doc_id for doc_id, _ in ordered[: -self._settings.semantic_cache_bucket_limit]]
        if remove_ids:
            self._collection.delete(ids=remove_ids)


def _normalize(query: str) -> str:
    """归一化问句：去空白与句末标点，提升相似命中率。

    Args:
        query: 原始问句。

    Returns:
        归一化后的问句。
    """
    collapsed = re.sub(r"\s+", "", query.strip())
    return collapsed.rstrip("？?。.!！~")


def _escape_key_part(value: str) -> str:
    """转义 Redis key 分段里的 ``%`` / ``:``，避免 buyer+scope 拼接产生歧义。

    Args:
        value: 原始分段（buyer_id / scope）。

    Returns:
        转义后的分段；映射是单射，因此不同 buyer/scope 组合不会撞 key。
    """
    return str(value).replace("%", "%25").replace(":", "%3A")


def _redis_key(buyer_id, scope, normalized_query: str) -> str:
    """构造 Redis 精确缓存 key：buyer + scope + 归一化问句哈希。

    buyer 与 scope 都进入 key，保证作用域隔离；问句用 sha256 定长，
    避免超长/含分隔符的问句破坏 key 结构。

    Args:
        buyer_id: 买家 id。
        scope: 偏好作用域指纹。
        normalized_query: 归一化后的问句。

    Returns:
        形如 ``sem:v1:{buyer}:{scope}:{sha256(query)[:32]}`` 的 key。
    """
    digest = hashlib.sha256(normalized_query.encode("utf-8")).hexdigest()[:32]
    return f"sem:v1:{_escape_key_part(buyer_id)}:{_escape_key_part(scope)}:{digest}"


def _redis_payload(normalized_query: str, reply: str) -> dict[str, str]:
    """构造 Redis value：回复 + 命中的归一化问句。

    Args:
        normalized_query: 归一化后的问句。
        reply: 缓存回复。

    Returns:
        可直接 JSON 序列化的 dict。
    """
    return {"query": normalized_query, "reply": reply}


def _hit_from_redis_payload(payload: Any, normalized_query: str) -> Optional[SemanticHit]:
    """把 Redis value 还原成 ``SemanticHit``；结构非法按未命中处理。

    Args:
        payload: ``get_json`` 返回的对象。
        normalized_query: 当前归一化问句（value 缺 query 字段时兜底）。

    Returns:
        SemanticHit（精确命中相似度恒为 1.0）；非法结构返回 None。
    """
    if not isinstance(payload, dict):
        return None
    reply = payload.get("reply")
    if not isinstance(reply, str) or not reply:
        return None
    matched_query = payload.get("query")
    if not isinstance(matched_query, str) or not matched_query:
        matched_query = normalized_query
    return SemanticHit(reply=reply, similarity=1.0, matched_query=matched_query)


async def _redis_safe_get(redis_cache, key: str) -> Any:
    """读 Redis；任何异常都按未命中（``None``），保证故障回退。

    Args:
        redis_cache: Redis 门面或测试替身。
        key: 缓存键。

    Returns:
        JSON 反序列化结果；未命中/异常为 None。
    """
    try:
        return await redis_cache.get_json(key)
    except Exception:
        return None


async def _redis_safe_set(redis_cache, key: str, value: Any) -> None:
    """写 Redis（TTL 24h）；任何异常都静默跳过，不冒泡到主链路。

    Args:
        redis_cache: Redis 门面或测试替身。
        key: 缓存键。
        value: 可 JSON 序列化的对象。
    """
    try:
        await redis_cache.set_json(key, value, ttl=_REDIS_TTL_SECONDS)
    except Exception:
        return
