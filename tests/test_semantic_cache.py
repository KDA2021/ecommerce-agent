# -*- coding: utf-8 -*-
from app.infrastructure.cache.chroma_semantic_cache import SemanticCache, is_cacheable_query
from app.infrastructure.embedding.chroma_embedding_client import ChromaLocalEmbeddingClient


def _settings(tmp_path):
    from app.infrastructure.settings import Settings
    return Settings(
        llm_base_url="", llm_api_key="", llm_model="", database_url="",
        data_dir=tmp_path, chroma_dir=tmp_path / "chroma",
        context_trigger_tokens=6000, context_keep_recent=8,
        semantic_cache_threshold=0.05, semantic_cache_bucket_limit=30,
        preference_top_k=5,
        reranker_enabled=True, reranker_model="BAAI/bge-reranker-base",
    )


async def test_cache_roundtrip(tmp_path):
    cache = SemanticCache(_settings(tmp_path), ChromaLocalEmbeddingClient())
    await cache.remember("b1", "推荐旅行三件套", "这是缓存回复", has_history=False)
    hit = await cache.lookup("b1", "推荐旅行三件套", has_history=False)
    assert hit is not None and hit.reply == "这是缓存回复"


def test_write_operations_are_not_cacheable():
    assert not is_cacheable_query("帮我下单")
    assert is_cacheable_query("推荐旅行三件套")


async def test_has_history_skips_cache(tmp_path):
    cache = SemanticCache(_settings(tmp_path), ChromaLocalEmbeddingClient())
    await cache.remember("b1", "推荐旅行三件套", "这是缓存回复", has_history=False)
    assert await cache.lookup("b1", "推荐旅行三件套", has_history=True) is None


async def test_buyers_are_isolated(tmp_path):
    cache = SemanticCache(_settings(tmp_path), ChromaLocalEmbeddingClient())
    await cache.remember("b1", "推荐旅行三件套", "b1 的回复", has_history=False)
    assert await cache.lookup("b2", "推荐旅行三件套", has_history=False) is None
