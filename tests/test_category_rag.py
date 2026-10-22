# -*- coding: utf-8 -*-

from app.infrastructure.rag.category_knowledge import chunk_markdown


def test_chunk_markdown_splits_headings(tmp_path):
    md = tmp_path / "travel-gear.md"
    md.write_text("# 标题\n\n## 定位\n\n轻量耐用。\n\n## 价格\n\n80-300元。\n", encoding="utf-8")
    chunks = chunk_markdown(md.read_text(encoding="utf-8"), md.name)
    assert [c.heading for c in chunks] == ["定位", "价格"]
    assert all(c.source == "travel-gear.md" for c in chunks)


async def test_knowledge_base_bootstrap_and_search(tmp_path):
    from app.infrastructure.embedding.chroma_embedding_client import ChromaLocalEmbeddingClient
    from app.infrastructure.rag.category_knowledge import CategoryKnowledgeBase
    from app.infrastructure.settings import Settings

    settings = Settings(
        llm_base_url="", llm_api_key="", llm_model="", database_url="",
        data_dir=tmp_path, chroma_dir=tmp_path / "chroma",
        context_trigger_tokens=6000, context_keep_recent=8,
        semantic_cache_threshold=0.05, semantic_cache_bucket_limit=30,
        preference_top_k=5,
        reranker_enabled=True, reranker_model="BAAI/bge-reranker-base",
    )
    (tmp_path / "travel-gear.md").write_text(
        "# 标题\n\n## 定位\n\n轻量耐用。\n\n## 价格\n\n80-300元。\n", encoding="utf-8",
    )
    kb = CategoryKnowledgeBase(settings, ChromaLocalEmbeddingClient())
    inserted = await kb.bootstrap(tmp_path)
    assert inserted >= 1
    hits = await kb.search("旅行装备怎么挑材质", top_k=2)
    assert hits and all("content" in h for h in hits)
