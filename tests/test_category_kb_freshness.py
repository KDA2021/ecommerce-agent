# -*- coding: utf-8 -*-
"""P13：品类知识库 freshness——Markdown 内容变更必须刷新 Chroma。

覆盖：
- 内容哈希归一化（排版差异不算变化）；
- 重复 bootstrap 幂等（不重复 embed / upsert）；
- 同一标题正文变更后刷新 vector / document / metadata，并能检索到新内容；
- 删除文件 / 小节后清理过期 Chroma 文档；
- 旧库中缺少 content_hash 的文档也能被迁移刷新。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from app.domain.catalog.ports.retrieval_ports import EmbeddingClient
from app.infrastructure.rag.category_knowledge import CategoryKnowledgeBase, chunk_markdown
from app.infrastructure.settings import Settings


class _DeterministicEmbedder(EmbeddingClient):
    """确定性离线 embedding（sha256 前 16 字节），避免测试依赖模型下载。"""

    def __init__(self) -> None:
        self.embedded_texts: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.embedded_texts.append(text)
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [byte / 255.0 for byte in digest[:16]]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(text) for text in texts]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        llm_base_url="", llm_api_key="", llm_model="", database_url="",
        data_dir=tmp_path, chroma_dir=tmp_path / "chroma",
        context_trigger_tokens=6000, context_keep_recent=8,
        semantic_cache_threshold=0.05, semantic_cache_bucket_limit=30,
        preference_top_k=5,
        reranker_enabled=True, reranker_model="BAAI/bge-reranker-base",
    )


def _two_section_md(position: str = "轻量耐用。") -> str:
    return f"# 标题\n\n## 定位\n\n{position}\n\n## 价格\n\n80-300元。\n"


def _ids_by_heading(text: str, source: str) -> dict[str, str]:
    return {chunk.heading: chunk.doc_id for chunk in chunk_markdown(text, source)}


def test_chunk_markdown_adds_hash_and_normalizes_layout() -> None:
    clean = chunk_markdown("## 定位\n\n轻量\n耐用。\n", "a.md")[0]
    messy = chunk_markdown("## 定位\n\n轻量  \n\n\n耐用。\r\n", "a.md")[0]

    assert clean.content_hash, "KnowledgeChunk 必须带 content_hash"
    assert clean.content_hash == messy.content_hash, "纯排版差异不应改变哈希"

    edited = chunk_markdown("## 定位\n\n轻量\n更耐用。\n", "a.md")[0]
    assert edited.content_hash != clean.content_hash, "正文变化必须改变哈希"


async def test_bootstrap_twice_unchanged_does_not_readd(tmp_path: Path) -> None:
    (tmp_path / "travel-gear.md").write_text(_two_section_md(), encoding="utf-8")
    embedder = _DeterministicEmbedder()
    kb = CategoryKnowledgeBase(_settings(tmp_path), embedder)

    assert await kb.bootstrap(tmp_path) == 2
    assert len(embedder.embedded_texts) == 2

    assert await kb.bootstrap(tmp_path) == 0
    assert len(embedder.embedded_texts) == 2, "内容未变不应重复 embed"
    assert kb._collection.count() == 2


async def test_bootstrap_refreshes_changed_section_and_search(tmp_path: Path) -> None:
    md_path = tmp_path / "travel-gear.md"
    old_text = _two_section_md("轻量耐用。")
    md_path.write_text(old_text, encoding="utf-8")
    embedder = _DeterministicEmbedder()
    kb = CategoryKnowledgeBase(_settings(tmp_path), embedder)
    assert await kb.bootstrap(tmp_path) == 2

    doc_id = _ids_by_heading(old_text, md_path.name)["定位"]
    before = kb._collection.get(
        ids=[doc_id], include=["documents", "embeddings", "metadatas"],
    )
    old_embedding = [float(x) for x in before["embeddings"][0]]
    old_hash = before["metadatas"][0]["content_hash"]

    new_text = _two_section_md("超轻耐用，仅 900 克。")
    md_path.write_text(new_text, encoding="utf-8")
    # 只有"定位"一节变化，"价格"节保持原样。
    assert await kb.bootstrap(tmp_path) == 1

    after = kb._collection.get(
        ids=[doc_id], include=["documents", "embeddings", "metadatas"],
    )
    new_chunk = next(c for c in chunk_markdown(new_text, md_path.name) if c.heading == "定位")
    assert after["documents"][0] == new_chunk.content
    assert [float(x) for x in after["embeddings"][0]] != old_embedding, "embedding 必须更新"
    assert after["metadatas"][0]["content_hash"] == new_chunk.content_hash
    assert after["metadatas"][0]["content_hash"] != old_hash

    hits = await kb.search(new_chunk.content, top_k=2)
    assert hits and hits[0]["content"] == new_chunk.content
    assert hits[0]["heading"] == "定位"


async def test_bootstrap_prunes_removed_file_and_section(tmp_path: Path) -> None:
    file_a = tmp_path / "a.md"
    file_b = tmp_path / "b.md"
    text_a = "# A\n\n## 定位\n\n甲。\n\n## 价格\n\n乙。\n"
    text_b = "# B\n\n## 材质\n\n丙。\n"
    file_a.write_text(text_a, encoding="utf-8")
    file_b.write_text(text_b, encoding="utf-8")
    kb = CategoryKnowledgeBase(_settings(tmp_path), _DeterministicEmbedder())
    assert await kb.bootstrap(tmp_path) == 3

    removed_section_id = _ids_by_heading(text_a, file_a.name)["价格"]
    removed_file_id = _ids_by_heading(text_b, file_b.name)["材质"]

    # 删除整份 b.md，并从 a.md 中删掉"价格"一节。
    file_b.unlink()
    file_a.write_text("# A\n\n## 定位\n\n甲。\n", encoding="utf-8")

    assert await kb.bootstrap(tmp_path) == 0
    assert kb._collection.get(ids=[removed_file_id])["ids"] == []
    assert kb._collection.get(ids=[removed_section_id])["ids"] == []
    assert kb._collection.count() == 1


async def test_bootstrap_migrates_legacy_doc_without_hash(tmp_path: Path) -> None:
    md_path = tmp_path / "legacy.md"
    text = "# A\n\n## 定位\n\n甲。\n"
    md_path.write_text(text, encoding="utf-8")
    embedder = _DeterministicEmbedder()
    kb = CategoryKnowledgeBase(_settings(tmp_path), embedder)
    chunk = chunk_markdown(text, md_path.name)[0]

    # 模拟旧版本已灌库的文档：有 source/heading，但没有 content_hash。
    kb._collection.upsert(
        ids=[chunk.doc_id],
        embeddings=[await embedder.embed("旧向量")],
        documents=["## 定位\n旧内容"],
        metadatas=[{"source": md_path.name, "heading": "定位"}],
    )

    assert await kb.bootstrap(tmp_path) == 1
    stored = kb._collection.get(ids=[chunk.doc_id], include=["documents", "metadatas"])
    assert stored["documents"][0] == chunk.content
    assert stored["metadatas"][0]["content_hash"] == chunk.content_hash
