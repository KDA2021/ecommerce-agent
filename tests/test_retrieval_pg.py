# -*- coding: utf-8 -*-
"""检索降级与过滤逻辑（不依赖数据库，直接测 use case）。"""
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.ports.retrieval_ports import VectorHit
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository


class _FakeEmbedder:
    async def embed(self, text: str) -> list[float]:
        return [0.0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]


class _FakeVectorIndex:
    def __init__(self, hits: list[VectorHit]) -> None:
        self._hits = hits

    async def ensure_ready(self, vector_dim: int) -> None:
        return None

    async def upsert_products(self, products, embeddings) -> None:
        return None

    async def search(self, embedding, top_n: int) -> list[VectorHit]:
        return self._hits[:top_n]


class _RecordingReranker:
    def __init__(self, scores: list[float], fail: bool = False) -> None:
        self._scores = scores
        self._fail = fail
        self.calls: list = []

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        self.calls.append((query, list(documents)))
        if self._fail:
            raise RuntimeError("reranker unavailable")
        return list(self._scores)


async def test_keyword_fallback_when_no_vector_index():
    usecase = CatalogSearchUseCase(product_repo=InMemoryProductRepository())
    result = await usecase.execute(ProductSearchSpec(normalized_query="旅行三件套 抗造"))
    assert result["recall_strategy"] == "keyword_2gram"
    assert result["hits"][0]["product_id"] == "P1001"


async def test_reranker_reorders_hits_and_reports_embedding_rerank():
    # 向量召回顺序 P1002 → P1001；精排分数反转，验证最终排序吃的是 rerank 分数
    hits = [
        VectorHit(product_id="P1002", score=0.9),
        VectorHit(product_id="P1001", score=0.1),
    ]
    reranker = _RecordingReranker([0.2, 5.0])
    usecase = CatalogSearchUseCase(
        product_repo=InMemoryProductRepository(),
        embedder=_FakeEmbedder(),
        vector_index=_FakeVectorIndex(hits),
        reranker=reranker,
    )
    result = await usecase.execute(ProductSearchSpec(normalized_query="旅行三件套 抗造"))

    assert result["recall_strategy"] == "embedding_rerank"
    assert result["rerank_applied"] is True
    assert result["hits"][0]["product_id"] == "P1001"
    assert result["hits"][0]["score"] == 5.0
    assert len(reranker.calls) == 1
    assert reranker.calls[0][0] == "旅行三件套 抗造"
    assert len(reranker.calls[0][1]) == 2


async def test_reranker_failure_degrades_to_embedding_only():
    hits = [
        VectorHit(product_id="P1002", score=0.9),
        VectorHit(product_id="P1001", score=0.1),
    ]
    usecase = CatalogSearchUseCase(
        product_repo=InMemoryProductRepository(),
        embedder=_FakeEmbedder(),
        vector_index=_FakeVectorIndex(hits),
        reranker=_RecordingReranker([], fail=True),
    )
    result = await usecase.execute(ProductSearchSpec(normalized_query="旅行三件套 抗造"))

    assert result["recall_strategy"] == "embedding_only"
    assert result["rerank_applied"] is False
    assert result["hits"][0]["product_id"] == "P1002"
    assert result["hits"][0]["score"] == 0.9


async def test_price_cap_filter_is_reported():
    usecase = CatalogSearchUseCase(product_repo=InMemoryProductRepository())
    result = await usecase.execute(
        ProductSearchSpec(normalized_query="旅行三件套", price_max_major=100.0),
    )
    assert result["hits"], "预算内候选应返回"
    assert all(hit["price_major"] <= 100.0 for hit in result["hits"])
    # 超预算的 P1001 必须出现在 filtered_out 且带 reason
    assert any(
        item["product_id"] == "P1001" and item["reason"] == "over_price_cap"
        for item in result.get("filtered_out", [])
    )
