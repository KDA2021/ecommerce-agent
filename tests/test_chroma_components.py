# -*- coding: utf-8 -*-
from app.infrastructure.embedding.chroma_embedding_client import ChromaLocalEmbeddingClient
from app.infrastructure.vector.chroma_product_index import ChromaProductIndex


async def test_local_embedding_dimension():
    client = ChromaLocalEmbeddingClient()
    vec = await client.embed("旅行三件套")
    assert len(vec) == 384


async def test_chroma_product_index_upsert_and_search(tmp_path, monkeypatch):
    from app.infrastructure.persistence.seed_products import build_seed_products
    from app.infrastructure.settings import Settings

    settings = Settings(
        llm_base_url="https://api.deepseek.com/v1", llm_api_key="", llm_model="deepseek-v4-flash",
        database_url="mysql+asyncmy://ecommerce:ecommerce@localhost:3307/ecommerce",
        data_dir=tmp_path, chroma_dir=tmp_path / "chroma",
        context_trigger_tokens=6000, context_keep_recent=8,
        semantic_cache_threshold=0.05, semantic_cache_bucket_limit=30,
        preference_top_k=5,
        reranker_enabled=True, reranker_model="BAAI/bge-reranker-base",
    )
    index = ChromaProductIndex(settings)
    embedder = ChromaLocalEmbeddingClient()
    products = build_seed_products()[:2]
    embeddings = await embedder.embed_batch([p.searchable_text() for p in products])
    await index.ensure_ready(384)
    await index.upsert_products(products, embeddings)
    query = await embedder.embed("旅行三件套 抗造 轻便")
    hits = await index.search(query, top_n=2)
    assert hits, "应能召回商品"
