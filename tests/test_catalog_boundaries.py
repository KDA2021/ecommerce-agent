"""Search failure isolation and landed-cost hard constraints, without model calls."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.money import Money
from app.domain.catalog.product import Product
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.domain.catalog.sku import Sku
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.vector.chroma_product_index import ChromaProductIndex


def product(price=129, currency="CNY", ships_to=None):
    return Product(product_id="P-BOUNDARY", title="旅行套装", brand="Test", category="旅行装备",
                   origin_country="CN", description="旅行套装", highlights=[],
                   ships_to=ships_to or ["CN", "US"],
                   skus=[Sku(sku_id="S1", spec="标准", price=Money.from_major_units(price, currency), stock=10)])


@pytest.mark.parametrize("price,currency,country,cap,total", [
    (129, "CNY", "CN", 150, 154),
    (6000, "CNY", "CN", 6100, 6115),
    (10, "USD", "US", 100, 136),
])
async def test_budget_includes_freight_tax_and_currency_conversion(price, currency, country, cap, total):
    search = CatalogSearchUseCase(InMemoryProductRepository([product(price, currency)]))
    result = await search.execute(ProductSearchSpec(normalized_query="旅行套装", ship_to=country,
                                                    price_max_major=cap))
    assert result["hits"] == []
    rejected = result["filtered_out"][0]
    assert rejected["reason"] == "over_price_cap"
    assert float(rejected["pricing"]["landed_total"]["amount"]) == total


async def test_exact_landed_cap_is_accepted_and_merchandise_cap_still_works_without_destination():
    search = CatalogSearchUseCase(InMemoryProductRepository([product()]))
    landed = await search.execute(ProductSearchSpec(normalized_query="旅行套装", ship_to="CN", price_max_major=154))
    assert landed["hits"][0]["landed_price"]["landed_total_major"] == 154
    assert "budget_match" in [r["code"] for r in landed["hits"][0]["reasons"]]
    merchandise = await search.execute(ProductSearchSpec(normalized_query="旅行套装", price_max_major=129))
    assert merchandise["hits"]


async def test_unquotable_destination_cannot_claim_budget_match():
    search = CatalogSearchUseCase(InMemoryProductRepository([product(ships_to=["AU"])]))
    result = await search.execute(ProductSearchSpec(normalized_query="旅行套装", ship_to="AU", price_max_major=200))
    assert result["hits"] == []
    assert result["filtered_out"][0]["reason"] == "landed_price_unavailable"


async def test_undeliverable_candidate_is_rejected():
    search = CatalogSearchUseCase(InMemoryProductRepository([product(ships_to=["CN"])]))
    result = await search.execute(ProductSearchSpec(normalized_query="旅行套装", ship_to="US", price_max_major=1000))
    assert result["hits"] == []
    assert result["filtered_out"][0]["reason"] == "ship_to_unavailable"


async def test_chroma_timeout_falls_back_without_blocking_loop_or_stacking_queries():
    started, release = threading.Event(), threading.Event()

    class Collection:
        calls = 0

        def query(self, **kwargs):
            self.calls += 1
            started.set()
            release.wait(2)
            return {"ids": [[]], "metadatas": [[]], "distances": [[]]}

    class Embedder:
        async def embed(self, text):
            return [0.0]

    collection = Collection()
    index = ChromaProductIndex(SimpleNamespace(chroma_timeout_seconds=0.03,
                                             chroma_max_concurrency=1, chroma_max_queue=0))
    index._collection = collection
    search = CatalogSearchUseCase(InMemoryProductRepository([product()]), Embedder(), index)
    try:
        # The watchdog releases the fake query even on the broken synchronous path.
        watchdog = threading.Timer(0.4, release.set)
        watchdog.start()
        task = asyncio.create_task(search.execute(ProductSearchSpec(normalized_query="旅行套装")))
        await asyncio.sleep(0.01)
        assert not release.is_set(), "Chroma blocked the event loop"
        result = await asyncio.wait_for(task, 0.2)
        assert started.is_set()
        assert result["recall_strategy"] == "keyword_2gram"
        assert result["hits"]
        again = await search.execute(ProductSearchSpec(normalized_query="旅行套装"))
        assert again["recall_strategy"] == "keyword_2gram"
        assert collection.calls == 1
    finally:
        release.set()
        watchdog.cancel()


async def test_chroma_exception_uses_keyword_fallback():
    class Collection:
        def query(self, **kwargs):
            raise RuntimeError("injected retrieval failure")

    class Embedder:
        async def embed(self, text):
            return [0.0]

    index = ChromaProductIndex(SimpleNamespace(chroma_timeout_seconds=0.1))
    index._collection = Collection()
    result = await CatalogSearchUseCase(InMemoryProductRepository([product()]), Embedder(), index).execute(
        ProductSearchSpec(normalized_query="旅行套装", ship_to="CN", price_max_major=150))
    assert result["recall_strategy"] == "keyword_2gram"
    assert result["hits"] == []
    assert result["filtered_out"][0]["reason"] == "over_price_cap"


async def test_real_chroma_in_memory_roundtrip_without_embedding_model(tmp_path, monkeypatch):
    import chromadb

    # Exercise native Chroma collections; disk persistence is a separate integration boundary.
    client = chromadb.EphemeralClient()
    monkeypatch.setattr(chromadb, "PersistentClient", lambda **kwargs: client)
    index = ChromaProductIndex(SimpleNamespace(chroma_timeout_seconds=5, chroma_dir=tmp_path / "chroma"))
    await index.ensure_ready(2)
    await index.upsert_products([product()], [[1.0, 0.0]])
    hits = await index.search([1.0, 0.0], top_n=1)
    assert hits[0].product_id == "P-BOUNDARY"
    assert hits[0].score == pytest.approx(1.0)
    concurrent = await asyncio.gather(*(index.search([1.0, 0.0], top_n=1) for _ in range(4)))
    assert all(rows[0].product_id == "P-BOUNDARY" for rows in concurrent)


async def test_cancelled_chroma_wait_keeps_worker_bounded_and_recovers():
    started, release = threading.Event(), threading.Event()

    class Collection:
        calls = 0

        def query(self, **kwargs):
            self.calls += 1
            started.set()
            release.wait(2)
            return {"ids": [["P-BOUNDARY"]], "metadatas": [[{"product_id": "P-BOUNDARY"}]],
                    "distances": [[0.0]]}

    index = ChromaProductIndex(SimpleNamespace(chroma_timeout_seconds=1,
                                             chroma_max_concurrency=1, chroma_max_queue=0))
    index._collection = collection = Collection()
    task = asyncio.create_task(index.search([0.0], 1))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(RuntimeError, match="in progress"):
            await index.search([0.0], 1)
        assert collection.calls == 1
    finally:
        release.set()
        await asyncio.gather(*tuple(index._pending))
    assert (await index.search([0.0], 1))[0].product_id == "P-BOUNDARY"
    assert collection.calls == 2


def test_chroma_timeout_environment_override(monkeypatch):
    from app.infrastructure.settings import load_settings

    monkeypatch.setenv("CHROMA_TIMEOUT_SECONDS", "1.25")
    assert load_settings().chroma_timeout_seconds == 1.25


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf")])
def test_invalid_chroma_deadline_is_rejected(seconds):
    with pytest.raises(ValueError, match="finite and positive"):
        ChromaProductIndex(SimpleNamespace(chroma_timeout_seconds=seconds))
