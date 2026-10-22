# -*- coding: utf-8 -*-
"""品类知识库：Markdown -> 按标题切块 -> Chroma category_kb。

灌库采用「内容哈希」判活：doc_id 只由来源文件与标题决定（UUID5），
因此标题不变时 doc_id 稳定；正文变化通过 ``content_hash`` 识别并刷新，
删除的文件 / 小节也会从 Chroma 清理，避免知识库出现过期内容。
"""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import chromadb

from app.domain.catalog.ports.retrieval_ports import EmbeddingClient
from app.infrastructure.settings import Settings

# 匹配 Markdown 二级标题，用于识别知识小节。
_HEADING_RE = re.compile(r"^##\s+(.+)$", flags=re.M)


def _normalize_content(content: str) -> str:
    """归一化知识块正文，用于计算稳定的内容哈希。

    只抹掉纯排版差异：统一换行符，并把任意连续空白（空格 / 换行 / 制表符）
    压成单个空格。这样仅重排、重换行或调整空行的编辑不会触发重灌；
    正文文字本身的任何改动都会保留并改变哈希。

    Args:
        content: 含标题的正文。

    Returns:
        归一化后的正文。
    """
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\s+", " ", text).strip()


def _content_hash(content: str) -> str:
    """计算归一化正文的 sha256。

    Args:
        content: 含标题的正文。

    Returns:
        十六进制哈希字符串。
    """
    return hashlib.sha256(_normalize_content(content).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class KnowledgeChunk:
    """知识切块：来源文件 + 小节标题 + 正文 + 确定性 doc_id + 内容哈希。"""

    source: str    # 来源 Markdown 文件名
    heading: str   # 二级标题（小节名）
    content: str   # 含标题的正文
    doc_id: str    # UUID5 确定性 id，保证重复灌库幂等
    content_hash: str = ""  # 归一化正文的 sha256；为兼容旧调用方保留默认值


def chunk_markdown(text: str, source: str) -> list[KnowledgeChunk]:
    """按 ``##`` 二级标题切块，doc_id 用 UUID5 保证稳定，正文附内容哈希。

    Args:
        text: Markdown 正文。
        source: 来源文件名。

    Returns:
        KnowledgeChunk 列表。
    """
    parts = re.split(r"^##\s+", text, flags=re.M)
    if not parts:
        return []
    chunks: list[KnowledgeChunk] = []
    for part in parts[1:]:
        lines = part.splitlines()
        heading = lines[0].strip()
        body = "\n".join(lines[1:]).strip()
        content = f"## {heading}\n{body}"
        chunks.append(KnowledgeChunk(
            source=source,
            heading=heading,
            content=content,
            doc_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"globex/kb/{source}/{heading}")),
            content_hash=_content_hash(content),
        ))
    return chunks


class CategoryKnowledgeBase:
    """Chroma 品类知识库（collection: category_kb）。"""

    def __init__(self, settings: Settings, embedder: EmbeddingClient) -> None:
        """初始化 Chroma collection 与 embedding 客户端。

        Args:
            settings: 运行时配置。
            embedder: embedding 客户端。
        """
        self._client = chromadb.PersistentClient(path=str(settings.chroma_dir))
        self._collection = self._client.get_or_create_collection(
            name="category_kb", metadata={"hnsw:space": "cosine"},
        )
        self._embedder = embedder

    async def bootstrap(self, knowledge_dir: Path) -> int:
        """把知识目录下的 Markdown 切块灌库，并让 Chroma 与源文件保持一致。

        刷新规则：

        1. doc_id 已存在且 ``content_hash`` 一致：跳过，不重复 embed / upsert；
        2. doc_id 不存在或哈希不一致：先删旧 id，再写入新正文、新向量与新哈希；
        3. 目录中已删除的文件、文件里已删除的小节：对应 Chroma 文档一并清理
           （``knowledge_dir`` 是该 collection 的权威来源）。

        目录不存在时不做任何清理，避免配置错误导致误删整库。

        Args:
            knowledge_dir: 知识文档目录。

        Returns:
            本次新增或刷新的块数（内容未变的块不计入）。
        """
        inserted = 0
        current_ids: set[str] = set()
        current_sources: set[str] = set()
        for md_file in sorted(knowledge_dir.glob("*.md")):
            current_sources.add(md_file.name)
            text = md_file.read_text(encoding="utf-8")
            for chunk in chunk_markdown(text, md_file.name):
                current_ids.add(chunk.doc_id)
                if self._is_fresh(chunk):
                    continue
                # 内容变化 / 首次入库：先删旧 id 再 upsert，避免旧向量与旧 metadata 残留。
                self._collection.delete(ids=[chunk.doc_id])
                vector = await self._embedder.embed(chunk.content)
                self._collection.upsert(
                    ids=[chunk.doc_id],
                    embeddings=[vector],
                    documents=[chunk.content],
                    metadatas=[{
                        "source": chunk.source,
                        "heading": chunk.heading,
                        "content_hash": chunk.content_hash,
                    }],
                )
                inserted += 1
        if knowledge_dir.is_dir():
            self._prune_stale(current_ids, current_sources)
        return inserted

    def _is_fresh(self, chunk: KnowledgeChunk) -> bool:
        """判断块是否已入库且内容哈希一致。

        Args:
            chunk: 待检查的知识块。

        Returns:
            已存在且哈希一致返回 True；否则返回 False。
        """
        metadata = self._existing_metadata(chunk.doc_id)
        if metadata is None:
            return False
        return metadata.get("content_hash") == chunk.content_hash

    def _existing_metadata(self, doc_id: str) -> dict | None:
        """读取单个 doc_id 的 metadata。

        Args:
            doc_id: 知识块 id。

        Returns:
            metadata 字典；不存在时返回 None。
        """
        result = self._collection.get(ids=[doc_id], include=["metadatas"])
        ids = result.get("ids") or []
        if not ids:
            return None
        metadatas = result.get("metadatas") or []
        metadata = metadatas[0] if metadatas else None
        return dict(metadata or {})

    def _prune_stale(self, current_ids: set[str], current_sources: set[str]) -> int:
        """删除本次目录中已不存在的知识块。

        既覆盖整份文件被删除（source 不在 current_sources），
        也覆盖文件内小节被删除（source 仍在但 doc_id 不在 current_ids）。

        Args:
            current_ids: 本次扫描到的全部 doc_id。
            current_sources: 本次扫描到的全部来源文件名。

        Returns:
            删除的块数。
        """
        result = self._collection.get(include=["metadatas"])
        ids = result.get("ids") or []
        metadatas = result.get("metadatas") or []
        stale: list[str] = []
        for index, doc_id in enumerate(ids):
            metadata = metadatas[index] if index < len(metadatas) else None
            source = (metadata or {}).get("source", "")
            if source not in current_sources or doc_id not in current_ids:
                stale.append(doc_id)
        if stale:
            self._collection.delete(ids=stale)
        return len(stale)

    async def _exists(self, doc_id: str) -> bool:
        """判断 doc_id 是否已入库（保留以兼容既有调用）。

        Args:
            doc_id: 知识块 id。

        Returns:
            是否已存在。
        """
        result = self._collection.get(ids=[doc_id])
        return bool(result.get("ids"))

    async def search(self, question: str, top_k: int = 3) -> list[dict]:
        """按向量检索知识片段。

        Args:
            question: 检索问题。
            top_k: 返回片段数。

        Returns:
            含 content / source / heading / score 的片段列表。
        """
        vector = await self._embedder.embed(question)
        result = self._collection.query(query_embeddings=[vector], n_results=top_k)
        # Chroma 返回结果按 query 维度嵌套；这里只有一条查询，取 [0]。
        docs = result.get("documents", [[]])[0]
        metas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]
        return [
            {
                "content": docs[i],
                "source": (metas[i] or {}).get("source", ""),
                "heading": (metas[i] or {}).get("heading", ""),
                "score": round(1.0 - float(distances[i]), 4),
            }
            for i in range(len(docs))
        ]
